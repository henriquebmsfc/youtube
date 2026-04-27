import os
import re
import tempfile
import threading
import uuid

import requests as http_requests
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from googleapiclient.discovery import build

import config
import database
import fetcher

# Stop words across all target languages (used for similarity scoring)
_STOP = {
    # English
    'the','a','an','of','in','at','on','to','for','and','or','with','that','this',
    'is','are','was','were','be','been','by','from','as','how','what','why','when',
    # Spanish
    'de','la','el','los','las','en','un','una','y','o','con','del','al','su','se',
    'que','es','por','para','como','más','sobre','pero',
    # German
    'die','der','des','den','dem','ein','eine','und','oder','mit','von','im','am',
    'ist','war','wie','das','was','für',
    # French
    'le','les','un','une','du','et','ou','avec','dans','sur','par','pour','est',
    'qui','que','se','au','aux','leur','leurs',
    # Italian
    'il','lo','gli','i','una','degli','dei','delle','e','o','con','da','in','per',
    'che','si','ha','è','sono',
    # Romanian
    'din','de','la','un','o','si','cu','pe','în','cel','cea','cei',
    # Polish
    'z','w','na','i','do','nie','sie','to','jest','jak','ale','przez','przy',
}


def _similarity(query: str, title: str) -> int:
    """
    Similarity between a translated query and a YouTube result title.
    Uses token overlap PLUS substring matching to handle inflected words
    (e.g. 'hambruna' matches 'hambre', 'medieval' matches 'medievales').
    Returns 0–100.
    """
    def tok(s):
        return [w for w in re.sub(r'[^\w\s]', '', s.lower()).split() if w not in _STOP and len(w) >= 3]

    q_tok = tok(query)
    r_str = re.sub(r'[^\w\s]', '', title.lower())

    if not q_tok:
        return 0

    matched = 0
    for w in q_tok:
        # Exact token match OR substring (handles inflected forms like plural/conjugation)
        if w in r_str.split() or (len(w) >= 5 and w[:5] in r_str):
            matched += 1

    score = (matched / len(q_tok)) * 100

    # Numbers (years, specific dates) carry strong semantic weight
    q_nums = set(re.findall(r'\d+', query))
    r_nums = set(re.findall(r'\d+', title))
    if q_nums:
        if q_nums <= r_nums:
            score = min(score * 1.3, 100)
        elif not (q_nums & r_nums):
            score *= 0.7

    return round(min(score, 100))

# ── Claude model ──────────────────────────────────────────────────────────────
# Max output tokens por modelo
_MODEL_MAX_TOKENS: dict = {
    "claude-opus-4-5":            64000,
    "claude-sonnet-4-5":          64000,
    "claude-opus-4-0":            32000,
    "claude-sonnet-4-0":          64000,
    "claude-3-7-sonnet-20250219": 64000,
    "claude-3-5-sonnet-20241022":  8192,
    "claude-3-5-sonnet-20240620":  8192,
    "claude-3-5-haiku-20241022":   8192,
    "claude-3-opus-20240229":      4096,
    "claude-3-haiku-20240307":     4096,
}

# Usa env ANTHROPIC_MODEL se definido e não-vazio; senão fixa no sonnet-4-5
CLAUDE_MODEL: str = (config.ANTHROPIC_MODEL.strip() or "claude-sonnet-4-5")
print(f"[Claude] Usando modelo: {CLAUDE_MODEL}")


def _model_max_tokens(model: str) -> int:
    """Retorna o limite de output tokens do modelo."""
    for k, v in _MODEL_MAX_TOKENS.items():
        if k in model:
            return v
    return 8192  # padrão seguro para modelos desconhecidos

# ── GenAIPro constants ────────────────────────────────────────────────────────
GENAIPRO_BASE = "https://genaipro.vn/api/v1"

_LANG_MAP = {
    "pt": "portuguese", "en": "english", "es": "spanish",
    "de": "german", "fr": "french", "it": "italian",
    "ro": "romanian", "pl": "polish", "hr": "croatian", "hu": "hungarian",
}

_thumbnail_jobs: dict = {}   # job_id → {status, urls, error, prompt}
_claude_jobs:    dict = {}   # job_id → {prod_id, task_type, status, error, style}

# ── Persistent media cache (thumbnails + audio) ───────────────────────────────
MEDIA_DIR = os.path.join(config.DB_DIR, "media")
try:
    os.makedirs(os.path.join(MEDIA_DIR, "thumbs"), exist_ok=True)
    os.makedirs(os.path.join(MEDIA_DIR, "audio"),  exist_ok=True)
except Exception as _media_init_err:
    print(f"[Media] Warning: could not create media dirs: {_media_init_err}")


def _media_thumb_path(prod_id: int, index: int) -> str:
    return os.path.join(MEDIA_DIR, "thumbs", f"{prod_id}_{index}.png")


def _media_audio_path(prod_id: int) -> str:
    return os.path.join(MEDIA_DIR, "audio", f"{prod_id}.mp3")


def _cleanup_media(prod_id: int):
    """Remove cached thumbnails and audio for a deleted/reset production."""
    import glob as _glob
    for f in _glob.glob(os.path.join(MEDIA_DIR, "thumbs", f"{prod_id}_*.png")):
        try: os.remove(f)
        except Exception: pass
    ap = _media_audio_path(prod_id)
    if os.path.exists(ap):
        try: os.remove(ap)
        except Exception: pass

def _gp_headers():
    return {"Authorization": f"Bearer {config.GENAIPRO_API_KEY}"}

# Load DOTTI agent system prompt from file
_DOTTI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "dotti_agent.txt")
DOTTI_SYSTEM = open(_DOTTI_PATH, encoding="utf-8").read() if os.path.exists(_DOTTI_PATH) else ""

LANGUAGES = {
    "es": {"name": "Espanhol",  "flag": "🇪🇸"},
    "de": {"name": "Alemão",    "flag": "🇩🇪"},
    "it": {"name": "Italiano",  "flag": "🇮🇹"},
    "fr": {"name": "Francês",   "flag": "🇫🇷"},
    "ro": {"name": "Romeno",    "flag": "🇷🇴"},
    "pl": {"name": "Polonês",   "flag": "🇵🇱"},
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload

# ── Transcriber job queue ─────────────────────────────────────────────────────
_transcription_jobs: dict = {}  # job_id → {status, progress, result, error, detected_language}


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _format_with_timestamps(result: dict, interval: int) -> str:
    segments = result.get("segments", [])
    if not segments:
        return result.get("text", "")

    blocks: dict = {}
    for seg in segments:
        start, end = seg["start"], seg["end"]
        text = seg["text"].strip()
        words = seg.get("words", [])
        if words:
            for wi in words:
                idx = int(wi.get("start", start) // interval)
                blocks.setdefault(idx, []).append(wi.get("word", "").strip())
        else:
            dur = end - start
            ws = text.split()
            wps = len(ws) / dur if dur > 0 else 0
            cur = start
            for w in ws:
                blocks.setdefault(int(cur // interval), []).append(w)
                if wps > 0:
                    cur += 1.0 / wps

    out = ""
    sorted_b = sorted(blocks)
    for n, bi in enumerate(sorted_b, 1):
        t0 = _fmt_time(bi * interval)
        t1 = _fmt_time((bi + 1) * interval)
        line = " ".join(blocks[bi]).strip()
        if line:
            out += f"{n}: {t0} até {t1} {line}\n\n"
    if out:
        total = len(sorted_b)
        out += f"\n{'=' * 60}\nTotal: {total} blocos de {interval} segundos cada\n{'=' * 60}"
    return out.strip()


def _run_transcription(job_id: str, tmp_path: str, model_name: str,
                       language: str, use_ts: bool, interval: int) -> None:
    try:
        import whisper  # imported here so missing whisper doesn't break the main app
        _transcription_jobs[job_id].update(status="loading",
                                           progress=f"Carregando modelo '{model_name}'…")
        model = whisper.load_model(model_name)

        _transcription_jobs[job_id].update(status="transcribing",
                                           progress="Transcrevendo áudio…")
        lang = None if language == "auto" else language
        result = model.transcribe(tmp_path, language=lang, verbose=False,
                                  fp16=False, word_timestamps=use_ts)

        text = _format_with_timestamps(result, interval) if use_ts else result.get("text", "")
        _transcription_jobs[job_id].update(
            status="done", progress="Concluído!",
            result=text,
            detected_language=result.get("language", "?"),
        )
    except Exception as exc:
        _transcription_jobs[job_id].update(status="error", error=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/videos")
def api_videos():
    return jsonify(database.get_videos(limit=200))


@app.route("/api/stats")
def api_stats():
    return jsonify(database.get_stats())


@app.route("/api/settings/keywords", methods=["GET"])
def api_keywords_get():
    return jsonify({"keywords": database.get_keywords()})


@app.route("/api/settings/keywords", methods=["PUT"])
def api_keywords_put():
    body = request.get_json(force=True)
    keywords = body.get("keywords", [])
    if not isinstance(keywords, list):
        return jsonify({"error": "keywords deve ser uma lista"}), 400
    keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    if not keywords:
        return jsonify({"error": "Pelo menos uma keyword é obrigatória"}), 400
    database.set_keywords(keywords)
    return jsonify({"keywords": database.get_keywords()})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        count = fetcher.fetch_videos()
        return jsonify({"success": True, "count": count, "message": f"{count} vídeos atualizados"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400


@app.route("/api/recalculate-scores", methods=["POST"])
def api_recalculate_scores():
    """Recalculate opportunity/engagement scores for all videos already in the DB."""
    videos = database.get_videos(limit=10_000)
    updated = 0
    conn = __import__("sqlite3").connect(database.DB_PATH)
    for v in videos:
        try:
            eng, opp = fetcher._calculate_scores(
                v["views"], v["likes"], v["comments"], v["published_at"]
            )
            conn.execute(
                "UPDATE videos SET engagement_score=?, opportunity_score=? WHERE video_id=?",
                (eng, opp, v["video_id"]),
            )
            updated += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return jsonify({"success": True, "updated": updated, "message": f"{updated} scores recalculados"})


@app.route("/video/<video_id>")
def video_detail(video_id):
    video = database.get_video(video_id)
    if not video:
        return "Vídeo não encontrado", 404
    return render_template("video.html", video=video, languages=LANGUAGES)


@app.route("/api/translate", methods=["POST"])
def api_translate():
    from concurrent.futures import ThreadPoolExecutor
    text = request.get_json(force=True).get("text", "")
    if not text:
        return jsonify({})

    def _fetch(code, info):
        try:
            r = http_requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": f"en|{code}"},
                timeout=6,
            )
            t = r.json().get("responseData", {}).get("translatedText", "")
            if t and "PLEASE SELECT" not in t.upper():
                return code, {**info, "translated": t}
        except Exception as _e:
            app.logger.warning("MyMemory [%s]: %s", code, _e)
        return code, {**info, "translated": text}

    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for code, entry in ex.map(lambda item: _fetch(*item), LANGUAGES.items()):
            results[code] = entry
    return jsonify(results)


@app.route("/api/competition", methods=["POST"])
def api_competition():
    translations = request.get_json(force=True).get("translations", {})
    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)
    results = {}

    for code, translated_text in translations.items():
        try:
            # Use only the main part of the title (before : — |) for better search
            search_query = re.split(r'[:\—\|]', translated_text)[0].strip()
            if len(search_query) < 8:          # if subtitle was the main part, keep full
                search_query = translated_text

            resp = youtube.search().list(
                part="snippet",
                q=search_query,
                type="video",
                maxResults=10,
                relevanceLanguage=code,
                order="relevance",
            ).execute()

            videos = []
            for item in resp.get("items", []):
                title   = item["snippet"]["title"]
                sim     = _similarity(translated_text, title)
                videos.append({
                    "video_id":   item["id"]["videoId"],
                    "title":      title,
                    "channel":    item["snippet"]["channelTitle"],
                    "url":        f"https://youtube.com/watch?v={item['id']['videoId']}",
                    "thumb":      item["snippet"]["thumbnails"].get("default", {}).get("url", ""),
                    "similarity": sim,
                })

            # Sort by similarity, show top 6 (always show results if YouTube returned any)
            videos.sort(key=lambda v: v["similarity"], reverse=True)
            videos = videos[:6]

            max_sim  = max((v["similarity"] for v in videos), default=0)
            direct   = [v for v in videos if v["similarity"] >= 55]
            related  = [v for v in videos if 20 <= v["similarity"] < 55]

            results[code] = {
                "videos":    videos,
                "max_sim":   max_sim,
                "n_direct":  len(direct),
                "n_related": len(related),
            }
        except Exception as e:
            results[code] = {"videos": [], "max_sim": 0, "n_direct": 0, "n_related": 0, "error": str(e)}

    return jsonify(results)


@app.route("/api/wipe-and-refresh", methods=["POST"])
def api_wipe_and_refresh():
    try:
        database.wipe_videos()
        count = fetcher.fetch_videos()
        return jsonify({"success": True, "count": count, "message": f"Banco limpo. {count} vídeos re-importados."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400


# ── Transcriber routes ────────────────────────────────────────────────────────

@app.route("/transcriber")
def transcriber():
    return render_template("transcriber.html")


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe_start():
    if "audio" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    file = request.files["audio"]
    if not file.filename:
        return jsonify({"error": "Arquivo inválido"}), 400

    model_name = request.form.get("model", "base")
    language   = request.form.get("language", "pt")
    use_ts     = request.form.get("timestamps", "sim") == "sim"
    try:
        interval = max(1, int(request.form.get("interval", 8)))
    except ValueError:
        interval = 8

    ext = os.path.splitext(file.filename)[1] or ".mp3"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    file.save(tmp.name)
    tmp.close()

    job_id = str(uuid.uuid4())
    _transcription_jobs[job_id] = {
        "status": "pending", "progress": "Na fila…",
        "result": None, "error": None, "detected_language": None,
    }

    threading.Thread(
        target=_run_transcription,
        args=(job_id, tmp.name, model_name, language, use_ts, interval),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/transcribe/status/<job_id>")
def api_transcribe_status(job_id):
    job = _transcription_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


# ── Channel / Production routes ───────────────────────────────────────────────

@app.route("/channels")
def channels_page():
    return render_template("channels.html")


@app.route("/channel/<int:channel_id>")
def channel_detail_page(channel_id):
    channel = database.get_channel(channel_id)
    if not channel:
        return "Canal não encontrado", 404
    return render_template("channel_detail.html", channel=channel, config=config)


@app.route("/api/media/<path:filename>")
def serve_media(filename):
    """Serve locally cached thumbnails and audio files."""
    from flask import send_from_directory
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/api/channels", methods=["GET"])
def api_channels_list():
    return jsonify(database.get_channels())


@app.route("/api/channels", methods=["POST"])
def api_channels_create():
    body = request.get_json(force=True)
    name  = (body.get("name") or "").strip()
    lang  = (body.get("language_code") or "").strip()
    flag  = (body.get("flag") or "").strip()
    desc  = (body.get("description") or "").strip()
    if not name or not lang:
        return jsonify({"error": "Nome e idioma são obrigatórios"}), 400
    new_id = database.create_channel(name, lang, flag, desc)
    return jsonify({"success": True, "id": new_id})


@app.route("/api/channels/<int:channel_id>", methods=["DELETE"])
def api_channels_delete(channel_id):
    database.delete_channel(channel_id)
    return jsonify({"success": True})


@app.route("/api/productions", methods=["GET"])
def api_productions_list():
    channel_id = request.args.get("channel_id", type=int)
    if not channel_id:
        return jsonify({"error": "channel_id obrigatório"}), 400
    status = request.args.get("status", "active")
    return jsonify(database.get_productions(channel_id, status))


@app.route("/api/productions", methods=["POST"])
def api_productions_create():
    body = request.get_json(force=True)
    channel_id = body.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id obrigatório"}), 400
    prod_id = database.create_production(
        channel_id       = int(channel_id),
        source_url       = body.get("source_url", ""),
        source_title     = body.get("source_title", ""),
        source_channel   = body.get("source_channel", ""),
        source_language  = body.get("source_language", ""),
        source_thumbnail = body.get("source_thumbnail", ""),
        adapted_title    = body.get("adapted_title", ""),
        source_video_id  = body.get("source_video_id"),
    )
    return jsonify({"success": True, "id": prod_id})


@app.route("/api/productions/<int:prod_id>", methods=["GET"])
def api_production_get(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404
    return jsonify(prod)


@app.route("/api/productions/<int:prod_id>", methods=["DELETE"])
def api_productions_delete(prod_id):
    _cleanup_media(prod_id)
    database.delete_production(prod_id)
    return jsonify({"success": True})


@app.route("/api/productions/<int:prod_id>/mark-posted", methods=["POST"])
def api_production_mark_posted(prod_id):
    if not database.get_production(prod_id):
        return jsonify({"error": "Produção não encontrada"}), 404
    database.mark_production_posted(prod_id)
    return jsonify({"success": True})


@app.route("/api/productions/<int:prod_id>/mark-active", methods=["POST"])
def api_production_mark_active(prod_id):
    if not database.get_production(prod_id):
        return jsonify({"error": "Produção não encontrada"}), 404
    database.mark_production_active(prod_id)
    return jsonify({"success": True})


@app.route("/api/productions/<int:prod_id>/title", methods=["PATCH"])
def api_production_update_title(prod_id):
    """Update the adapted_title of an existing production."""
    body = request.get_json(force=True)
    title = (body.get("adapted_title") or "").strip()
    if not title:
        return jsonify({"error": "Título não pode ser vazio"}), 400
    if not database.get_production(prod_id):
        return jsonify({"error": "Produção não encontrada"}), 404
    database.update_production_title(prod_id, title)
    return jsonify({"success": True})


@app.route("/api/translate-title-options", methods=["POST"])
def api_translate_title_options():
    """Generate 4 adapted title options (1 literal + 3 cultural) using Claude."""
    import json as _json
    import anthropic as _anthropic

    body       = request.get_json(force=True)
    title      = (body.get("title") or "").strip()
    target_lang = (body.get("target_lang") or "pt").strip()
    if not title:
        return jsonify({"error": "Título obrigatório"}), 400

    _LANG_INFO = {
        "pt": ("Portuguese", "Portugal/Brazil", "Portugal"),
        "es": ("Spanish",    "Spain",           "Spain"),
        "de": ("German",     "Germany",         "Germany"),
        "fr": ("French",     "France",          "France"),
        "it": ("Italian",    "Italy",           "Italy"),
        "ro": ("Romanian",   "Romania",         "Romania"),
        "pl": ("Polish",     "Poland",          "Poland"),
        "hr": ("Croatian",   "Croatia",         "Croatia"),
        "hu": ("Hungarian",  "Hungary",         "Hungary"),
    }
    lang_name, country_name, country_ex = _LANG_INFO.get(target_lang, ("Portuguese", "Brazil", "Brazil"))

    user_msg = (
        f'Original English YouTube title: "{title}"\n'
        f'Target language: {lang_name}\n'
        f'Target market: {country_name}\n\n'
        f'Generate EXACTLY 4 YouTube title options in {lang_name}:\n\n'
        f'1. LITERAL: Direct translation keeping all original place/culture references intact\n'
        f'2. LOCALIZED: Same concept, but replace any foreign country/region/culture references '
        f'with equivalent ones from {country_name} or its history. '
        f'Example: "in England" → "in {country_ex}", "English kings" → local equivalent\n'
        f'3. CREATIVE: Fresh angle on the same theme that resonates with {country_name} viewers\n'
        f'4. DRAMATIC: Maximum engagement, strong emotional hook, perfect for viral YouTube in {country_name}\n\n'
        f'For each option also provide a very brief explanation in Portuguese (Brazil) of what '
        f'the title means (1 short sentence — so Brazilian producers can understand non-PT titles).\n\n'
        f'Return ONLY a valid JSON array of exactly 4 objects:\n'
        f'  {{"text": "<title in {lang_name}>", "pt": "<brief explanation in Portuguese>"}}\n'
        f'No markdown, no extra text, just the JSON array.'
    )
    import time as _time
    client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    last_err = None
    for attempt in range(3):
        try:
            if attempt > 0:
                _time.sleep(2 * attempt)  # 2s, 4s between retries
            with client.messages.stream(
                model=CLAUDE_MODEL, max_tokens=600,
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                raw = stream.get_final_text().strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw).strip()
            options = _json.loads(raw)
            if not isinstance(options, list):
                raise ValueError("Expected list")
            return jsonify({"options": options[:4]})
        except Exception as e:
            last_err = e
            print(f"[TranslateOptions] attempt {attempt+1} error: {e}")
            # Only retry on Anthropic 5xx errors; fail fast on others
            if "500" not in str(e) and "529" not in str(e) and "overloaded" not in str(e).lower():
                break
    return jsonify({"options": [], "error": str(last_err)}), 500


@app.route("/api/productions/<int:prod_id>/tasks/<task_type>", methods=["PATCH"])
def api_task_update(prod_id, task_type):
    if task_type not in database.TASK_TYPES:
        return jsonify({"error": "Tipo de tarefa inválido"}), 400
    body = request.get_json(force=True)
    database.upsert_task(
        production_id = prod_id,
        task_type     = task_type,
        status        = body.get("status", "pending"),
        result_text   = body.get("result_text", ""),
        notes         = body.get("notes", ""),
    )
    return jsonify({"success": True})


@app.route("/api/productions/<int:prod_id>/tasks/<task_type>/reset", methods=["POST"])
def api_task_reset(prod_id, task_type):
    """Reset a stuck in_progress task back to done (if real result exists) or pending."""
    import json as _json_mod
    if task_type not in database.TASK_TYPES:
        return jsonify({"error": "Tipo de tarefa inválido"}), 400
    task = database.get_task(prod_id, task_type)
    if not task:
        return jsonify({"error": "Tarefa não encontrada"}), 404

    # Determine new status: "done" if a real result exists, else "pending"
    new_status = "pending"
    rt = task.get("result_text") or ""
    if rt:
        if task_type == "audio":
            # Audio result_text during in_progress is just {"task_id":"..."}, not a real result
            try:
                new_status = "done" if _json_mod.loads(rt).get("audio_url") else "pending"
            except Exception:
                pass
        else:
            new_status = "done"

    database.set_task_status(prod_id, task_type, new_status)

    # Clear any in-memory job entries so new generation can start fresh
    if task_type in ("script", "prompts"):
        to_remove = [jid for jid, j in _claude_jobs.items()
                     if j.get("prod_id") == prod_id and j.get("task_type") == task_type]
        for jid in to_remove:
            del _claude_jobs[jid]
    elif task_type == "thumbnails":
        _thumbnail_jobs.pop(prod_id, None)

    print(f"[Reset] prod={prod_id} task={task_type} → {new_status}")
    return jsonify({"success": True, "new_status": new_status})


def _bg_script(job_id, prod_id, system_prompt, user_msg, style_name, target_lang):
    """Background thread: calls Claude and saves the script to DB."""
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=min(8000, _model_max_tokens(CLAUDE_MODEL)),
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            script_text = stream.get_final_text()
        database.upsert_task(prod_id, "script", "done",
                             result_text=script_text,
                             notes=f"Style: {style_name} | Lang: {target_lang}")
        _claude_jobs[job_id].update(status="done", style=style_name)
        print(f"[Script BG] prod={prod_id} style='{style_name}' lang={target_lang} tokens≈{len(script_text)//4}")
    except Exception as exc:
        # Use set_task_status so that any previously completed result_text is preserved
        database.set_task_status(prod_id, "script", "pending", notes=f"Erro: {exc}")
        _claude_jobs[job_id].update(status="error", error=str(exc))
        print(f"[Script BG] prod={prod_id} error: {exc}")


@app.route("/api/productions/<int:prod_id>/tasks/script/generate", methods=["POST"])
def api_script_generate(prod_id):
    import random
    import importlib as _importlib

    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    channel = database.get_channel(prod["channel_id"])
    lang_code = channel["language_code"] if channel else "pt"

    _lang_names = {
        "pt": "Portuguese", "en": "English", "es": "Spanish",
        "de": "German",     "fr": "French",  "it": "Italian",
        "ro": "Romanian",   "pl": "Polish",  "hr": "Croatian",  "hu": "Hungarian",
    }
    target_lang = _lang_names.get(lang_code, lang_code)

    # Channel profile fields for context injection
    _ch = channel or {}
    _tema_principal     = _ch.get("tema_principal", "").strip()
    _subtema            = _ch.get("subtema", "").strip()
    _tipo_canal         = _ch.get("tipo_canal", "").strip()
    _instrucoes_roteiro = _ch.get("instrucoes_roteiro", "").strip()

    # Load styles (hot-reload: edits take effect without restart)
    _styles_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "script_styles.py")
    _spec = _importlib.util.spec_from_file_location("script_styles", _styles_path)
    _mod  = _importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    style = random.choice(_mod.SCRIPT_STYLES)

    system_prompt = (
        style["system"]
        + f"\n\nLANGUAGE REQUIREMENT: You MUST write the entire script in {target_lang}."
        " Do not use any other language. Even technical terms must be adapted to {target_lang}."
        "\n\nFORMAT REQUIREMENT: Output PLAIN TEXT only — the script must be ready to be read aloud."
        " Absolutely NO markdown: no **, no __, no ##, no *, no bullet dashes, no backticks, no > blockquotes."
        " Separate paragraphs with a blank line. That is the only allowed formatting."
        "\n\nYOUTUBE CONTENT POLICY: This is an educational history documentary."
        " Do NOT describe graphic violence, gore, or torture in explicit or gratuitous detail."
        " Battles and conflicts must be framed from a historical and analytical perspective."
        " Executions, deaths, and suffering must be referenced factually and briefly, never sensationalized."
        " Content must comply with YouTube's advertiser-friendly guidelines at all times."
    )

    # Channel profile injection — omit individual lines if field is empty;
    # omit entire block if all fields are empty
    _profile_lines = []
    if _tema_principal:
        _profile_lines.append(f"Tema principal: {_tema_principal}")
    if _subtema:
        _profile_lines.append(f"Subtema: {_subtema}")
    if _tipo_canal:
        _profile_lines.append(f"Tipo: {_tipo_canal}")
    if _instrucoes_roteiro:
        _profile_lines.append(f"Direcionamento narrativo: {_instrucoes_roteiro}")
    if _profile_lines:
        system_prompt += "\n\nPERFIL DO CANAL:\n" + "\n".join(_profile_lines)

    # Structural variation — always injected so scripts don't repeat the same opening pattern
    system_prompt += (
        "\n\nVARIAÇÃO ESTRUTURAL OBRIGATÓRIA:\n"
        "Cada roteiro deve começar de forma estruturalmente diferente dos roteiros padrão de IA.\n"
        "Escolha UMA das seguintes abordagens de abertura (varie entre vídeos, nunca repita a mesma):\n"
        "- Começar com uma pergunta retórica provocativa\n"
        "- Começar in media res (no meio de uma cena ou evento)\n"
        "- Começar com um dado ou estatística surpreendente\n"
        "- Começar com uma citação histórica ou frase atribuída\n"
        "- Começar com uma descrição sensorial de lugar/tempo\n"
        "- Começar com uma contradição ou paradoxo histórico\n"
        "- Começar com a conclusão e trabalhar o caminho até ela\n\n"
        "A estrutura interna também deve variar: nem sempre cronológica, nem sempre 3 atos.\n"
        "O objetivo é que cada vídeo pareça escrito por um narrador diferente com perspectiva própria."
    )

    existing_ts = (prod.get("tasks") or {}).get("transcription", {})
    transcription_text = existing_ts.get("result_text", "") if existing_ts else ""

    user_parts = [
        'You are writing the complete voiceover script for a YouTube video.',
        '',
        f'VIDEO TITLE (exact): "{prod["adapted_title"]}"',
        '',
        'This title is a promise to the viewer. The script must deliver EXACTLY what this title says'
        ' — not a general overview of the theme, but the specific story, event, or analysis'
        ' that the title describes. Every paragraph must serve this exact title.',
        '',
        f'Reference (original English video with the same subject): "{prod["source_title"]}"'
        f' by {prod["source_channel"]}',
    ]
    if transcription_text:
        user_parts.append(
            "\nOriginal video transcription — use as the primary factual source for names, dates,"
            " events and narrative structure:\n" + transcription_text[:6000]
        )
    user_parts.append(
        f"\nWrite the full script now, in {target_lang}."
        " Plain text only — no markdown, no asterisks, no headers, no symbols. Narration-ready prose."
    )
    user_msg = "\n".join(user_parts)

    # Mark in_progress immediately and launch background thread
    # Use set_task_status (not upsert_task) so that any previous result_text is preserved
    # in case generation fails and we need to restore to the prior completed run.
    job_id = str(uuid.uuid4())[:12]
    _claude_jobs[job_id] = {"prod_id": prod_id, "task_type": "script", "status": "running", "error": None, "style": None}
    database.set_task_status(prod_id, "script", "in_progress", notes=f"Style: {style['name']} | Lang: {target_lang}")
    threading.Thread(
        target=_bg_script,
        args=(job_id, prod_id, system_prompt, user_msg, style["name"], target_lang),
        daemon=True,
    ).start()
    print(f"[Script] prod={prod_id} queued job={job_id} style='{style['name']}'")
    return jsonify({"queued": True, "job_id": job_id})


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    """Poll background Claude job status (script / prompts generation)."""
    job = _claude_jobs.get(job_id)
    if not job:
        # Job not in memory (server restarted?) — check DB for current task status
        return jsonify({"status": "unknown"}), 404
    return jsonify(job)


@app.route("/api/productions/<int:prod_id>/channel", methods=["GET"])
def api_production_channel(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404
    return jsonify(database.get_channel(prod["channel_id"]))


@app.route("/api/channels/<int:channel_id>", methods=["GET"])
def api_channel_get(channel_id):
    """Return a single channel by id."""
    ch = database.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "Canal não encontrado"}), 404
    return jsonify(ch)


@app.route("/api/channels/<int:channel_id>", methods=["PUT"])
def api_channel_update(channel_id):
    """Update channel fields (profile + basic info). Accepts any subset of allowed fields."""
    ch = database.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "Canal não encontrado"}), 404
    body = request.get_json(force=True) or {}
    allowed = {
        "name", "language_code", "flag", "description",
        "tema_principal", "subtema", "tipo_canal",
        "instrucoes_roteiro", "instrucoes_visuais",
    }
    fields = {k: str(v) for k, v in body.items() if k in allowed}
    if not fields:
        return jsonify({"error": "Nenhum campo válido para atualizar"}), 400
    database.update_channel(channel_id, **fields)
    return jsonify(database.get_channel(channel_id))


@app.route("/api/channels/<int:channel_id>/generate-profile", methods=["POST"])
def api_channel_generate_profile(channel_id):
    """Use Claude to analyse channel productions and suggest a structured profile.
    Returns {profile: {tema_principal, subtema, tipo_canal, instrucoes_roteiro, instrucoes_visuais}}.
    Never saves automatically — caller must confirm via PUT /api/channels/<id>."""
    import json as _json, re as _re
    ch = database.get_channel(channel_id)
    if not ch:
        return jsonify({"error": "Canal não encontrado"}), 404

    prods = database.get_productions(channel_id)[:10]
    samples = []
    for p in prods:
        title  = (p.get("adapted_title") or p.get("source_title", "")).strip()
        script = (p.get("tasks") or {}).get("script", {}).get("result_text", "")[:500]
        if title or script:
            samples.append(f"Título: {title}\nRoteiro (trecho): {script}")

    if not samples:
        return jsonify({"error": "Canal sem produções para analisar"}), 400

    samples_text = "\n\n---\n\n".join(samples)
    user_msg = (
        "Analise as produções abaixo de um canal do YouTube e gere um perfil estruturado.\n\n"
        f"PRODUÇÕES DO CANAL:\n{samples_text}\n\n"
        "Retorne APENAS um JSON válido com exatamente estes 5 campos:\n"
        '{"tema_principal": "...", "subtema": "...", "tipo_canal": "...", '
        '"instrucoes_roteiro": "...", "instrucoes_visuais": "..."}\n\n'
        "instrucoes_roteiro: direcionamento de TOM e REGISTRO apenas "
        "(ex: 'Tom sério e imersivo, linguagem acessível, topo de funil'). "
        "NUNCA prescreva estrutura narrativa, tipo de abertura ou sequência de seções.\n"
        "instrucoes_visuais: restrições de época, estilo e anacronismos para geração de imagens "
        "(ex: 'Apenas Europa medieval séc. XIII–XV. Proibido elementos modernos ou fantasia.')."
    )

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            result = stream.get_final_text()
        m = _re.search(r'\{[\s\S]*?\}', result)
        if not m:
            return jsonify({"error": "Resposta inválida da IA — JSON não encontrado"}), 500
        profile = _json.loads(m.group(0))
        return jsonify({"profile": profile})
    except Exception as exc:
        print(f"[GenerateProfile] channel={channel_id} error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/youtube/video-info", methods=["POST"])
def api_youtube_video_info():
    """Fetch YouTube video metadata from a URL or video ID."""
    body = request.get_json(force=True)
    url_or_id = (body.get("url") or "").strip()

    # Extract video_id from URL
    vid_match = re.search(r'(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})', url_or_id)
    video_id = vid_match.group(1) if vid_match else (url_or_id if len(url_or_id) == 11 else None)

    if not video_id:
        return jsonify({"error": "URL ou ID de vídeo inválido"}), 400

    # Check local DB first
    local = database.get_video(video_id)
    if local:
        return jsonify({
            "video_id":   video_id,
            "title":      local.get("title", ""),
            "channel":    local.get("channel_name", ""),
            "language":   "en",
            "thumbnail":  local.get("thumbnail_url", ""),
            "url":        local.get("video_url", f"https://youtube.com/watch?v={video_id}"),
            "source":     "local_db",
        })

    # Fetch from YouTube API
    try:
        youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)
        resp = youtube.videos().list(
            part="snippet,contentDetails",
            id=video_id,
        ).execute()
        items = resp.get("items", [])
        if not items:
            return jsonify({"error": "Vídeo não encontrado"}), 404
        snip = items[0]["snippet"]
        audio_lang = snip.get("defaultAudioLanguage", "") or snip.get("defaultLanguage", "")
        return jsonify({
            "video_id":  video_id,
            "title":     snip.get("title", ""),
            "channel":   snip.get("channelTitle", ""),
            "language":  audio_lang,
            "thumbnail": snip.get("thumbnails", {}).get("medium", {}).get("url", ""),
            "url":       f"https://youtube.com/watch?v={video_id}",
            "source":    "youtube_api",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GenAIPro: voices ──────────────────────────────────────────────────────────

@app.route("/api/voices")
def api_voices():
    lang_code = request.args.get("language", "")
    gp_lang   = _LANG_MAP.get(lang_code, "")
    params    = {"page_size": 50, "sort": "trending", "include_live_moderated": "true"}
    if gp_lang:
        params["language"] = gp_lang
    try:
        r = http_requests.get(
            f"{GENAIPRO_BASE}/labs/voices", headers=_gp_headers(), params=params, timeout=12
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GenAIPro: audio TTS generation ────────────────────────────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/audio/generate", methods=["POST"])
def api_audio_generate(prod_id):
    import json as _json
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    script_text = (prod.get("tasks") or {}).get("script", {}).get("result_text", "")
    if not script_text:
        return jsonify({"error": "Gere o roteiro primeiro antes de gerar o áudio"}), 400

    body     = request.get_json(force=True)
    voice_id = "TumdjBNWanlT3ysvclWh"  # Peter — fixo
    model_id = body.get("model_id", "eleven_turbo_v2_5")
    speed    = float(body.get("speed",      0.9))
    stab     = float(body.get("stability",  0.5))
    sim      = float(body.get("similarity", 0.75))

    payload = {
        "input":             script_text,
        "voice_id":          voice_id,
        "model_id":          model_id,
        "speed":             speed,
        "stability":         stab,
        "similarity":        sim,
        "style":             0.0,
        "use_speaker_boost": True,
    }
    try:
        r    = http_requests.post(f"{GENAIPRO_BASE}/labs/task", headers=_gp_headers(),
                                   json=payload, timeout=30)
        data = r.json()
        # API pode retornar 'task_id' ou 'id'
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            app.logger.error("GenAIPro /labs/task HTTP %s → %s", r.status_code, data)
            return jsonify({
                "error":  f"API retornou HTTP {r.status_code} sem task_id",
                "detail": data,
            }), 500
        database.upsert_task(prod_id, "audio", "in_progress",
                             result_text=_json.dumps({"task_id": task_id}))
        return jsonify({"success": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _auto_trigger_srt(prod_id: int):
    """Background: export SRT via GenAIPro API then save as transcription."""
    import json as _json
    import time as _time
    _time.sleep(2)  # brief delay to ensure the DB write is committed
    result_text = None
    try:
        prod = database.get_production(prod_id)
        if not prod:
            return
        audio_task = (prod.get("tasks") or {}).get("audio", {})
        if audio_task.get("status") != "done":
            return
        # Skip if transcription already done
        trans_task = (prod.get("tasks") or {}).get("transcription", {})
        if trans_task.get("status") == "done" and trans_task.get("result_text"):
            print(f"[Auto-SRT] prod={prod_id}: transcription already exists, skipping")
            return
        try:
            audio_data = _json.loads(audio_task.get("result_text") or "{}")
        except Exception:
            return
        task_id_a = audio_data.get("task_id", "")
        if not task_id_a:
            print(f"[Auto-SRT] prod={prod_id}: no task_id in audio result, skipping")
            return

        # ── Step 1: export SRT via GenAIPro subtitle endpoint ────────────────
        srt_text = None
        try:
            export_resp = http_requests.post(
                f"{GENAIPRO_BASE}/labs/task/subtitle/{task_id_a}",
                headers={**_gp_headers(), "Content-Type": "application/json"},
                json={"max_characters_per_line": 42, "max_lines_per_cue": 2, "max_seconds_per_cue": 7},
                timeout=30,
            )
            if export_resp.status_code == 200:
                srt_text = export_resp.text
                print(f"[Auto-SRT] prod={prod_id}: SRT exported via API ({len(srt_text)} chars)")
            else:
                print(f"[Auto-SRT] prod={prod_id}: subtitle export HTTP {export_resp.status_code}")
        except Exception as e:
            print(f"[Auto-SRT] prod={prod_id}: subtitle export error: {e}")

        # ── Step 2: fallback — download from stored subtitle_url ─────────────
        if not srt_text:
            srt_url = audio_data.get("subtitle_url", "")
            if not srt_url:
                try:
                    r = http_requests.get(f"{GENAIPRO_BASE}/labs/task/{task_id_a}",
                                          headers=_gp_headers(), timeout=10)
                    srt_url = r.json().get("subtitle", "")
                except Exception:
                    pass
            if srt_url:
                try:
                    dl = http_requests.get(srt_url, timeout=30)
                    dl.raise_for_status()
                    srt_text = dl.text
                    print(f"[Auto-SRT] prod={prod_id}: SRT downloaded from URL ({len(srt_text)} chars)")
                except Exception as e:
                    print(f"[Auto-SRT] prod={prod_id}: SRT download error: {e}")

        if not srt_text:
            print(f"[Auto-SRT] prod={prod_id}: could not obtain SRT content, skipping")
            return

        # ── Step 3: convert & save ────────────────────────────────────────────
        result_text = _srt_to_blocks(srt_text, interval=8)
        if result_text:
            database.upsert_task(prod_id, "transcription", "done", result_text=result_text)
            print(f"[Auto-SRT] prod={prod_id}: transcription saved ({len(result_text)} chars)")
        else:
            print(f"[Auto-SRT] prod={prod_id}: SRT was empty or unrecognised format")
    except Exception as exc:
        print(f"[Auto-SRT] prod={prod_id} error: {exc}")
    else:
        # After successful transcription, trigger description generation
        if result_text:
            threading.Thread(target=_auto_trigger_description, args=(prod_id,), daemon=True).start()


def _auto_trigger_description(prod_id: int):
    """Background: auto-generate YouTube description after transcription is ready."""
    import json as _json
    try:
        prod = database.get_production(prod_id)
        if not prod:
            return
        tasks = prod.get("tasks") or {}
        # Skip if description already done
        desc_task = tasks.get("description", {})
        if desc_task.get("status") == "done" and desc_task.get("result_text"):
            print(f"[Auto-Desc] prod={prod_id}: description already exists, skipping")
            return
        script_text = tasks.get("script", {}).get("result_text", "")
        if not script_text:
            print(f"[Auto-Desc] prod={prod_id}: no script, skipping")
            return
        title = prod.get("adapted_title") or prod.get("source_title", "")
        channel = database.get_channel(prod["channel_id"])
        lang_code = (channel or {}).get("language_code", "pt")
        lang_name = _LANG_MAP.get(lang_code, "portuguese").capitalize()

        database.set_task_status(prod_id, "description", "in_progress")
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        user_msg = (
            f'YouTube video title: "{title}"\n\n'
            f'Script (first 2500 words):\n{script_text[:6000]}\n\n'
            f'Generate a complete YouTube description in {lang_name} for this historical documentary video.\n\n'
            f'Structure:\n'
            f'1. Hook (2 sentences visible before "Show more" — must compel clicking)\n'
            f'2. Summary of what the viewer will learn (3-5 sentences)\n'
            f'3. Key topics covered (brief bullet list using • symbol)\n'
            f'4. Call to action (subscribe, like, comment)\n'
            f'5. 15-20 relevant hashtags on the last line\n\n'
            f'Write entirely in {lang_name}. No markdown bold/italic. Use plain text.'
        )
        with client.messages.stream(
            model=CLAUDE_MODEL, max_tokens=800,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            desc_text = stream.get_final_text()
        database.upsert_task(prod_id, "description", "done", result_text=desc_text)
        print(f"[Auto-Desc] prod={prod_id}: description saved ({len(desc_text)} chars)")
    except Exception as exc:
        database.set_task_status(prod_id, "description", "pending")
        print(f"[Auto-Desc] prod={prod_id} error: {exc}")


@app.route("/api/productions/<int:prod_id>/tasks/audio/download")
def api_audio_download(prod_id):
    """Serve the cached MP3 (or download+cache from GenAIPro on first request)."""
    import json as _json, re as _re
    from flask import send_file, Response, stream_with_context
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    try:
        audio_data = _json.loads((prod.get("tasks") or {}).get("audio", {}).get("result_text", "{}"))
    except Exception:
        audio_data = {}

    # Build a safe filename from the video title
    raw_title = (prod.get("adapted_title") or prod.get("source_title") or f"audio_{prod_id}").strip()
    safe_title = _re.sub(r'[\\/:*?"<>|]', '', raw_title)
    safe_title = _re.sub(r'\s+', ' ', safe_title).strip() or f"audio_{prod_id}"
    filename = safe_title + ".mp3"

    local_path = _media_audio_path(prod_id)

    # Serve from local cache if available
    if os.path.exists(local_path):
        return send_file(local_path, as_attachment=True, download_name=filename, mimetype="audio/mpeg")

    # Otherwise download from GenAIPro, cache, then serve
    audio_url = audio_data.get("audio_url", "")
    if not audio_url:
        return jsonify({"error": "URL de áudio não disponível"}), 404

    r = http_requests.get(audio_url, timeout=120)
    r.raise_for_status()

    # Save to cache
    try:
        with open(local_path, "wb") as _f:
            _f.write(r.content)
        return send_file(local_path, as_attachment=True, download_name=filename, mimetype="audio/mpeg")
    except Exception as _cache_err:
        print(f"[Audio cache] write error: {_cache_err} — streaming directly")
        # Fallback: stream directly without caching
        def _gen():
            yield r.content
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": r.headers.get("Content-Type", "audio/mpeg"),
            "Content-Length": str(len(r.content)),
        }
        return Response(_gen(), headers=headers)


@app.route("/api/productions/<int:prod_id>/tasks/audio/status/<task_id>")
def api_audio_status(prod_id, task_id):
    import json as _json
    try:
        r    = http_requests.get(f"{GENAIPRO_BASE}/labs/task/{task_id}",
                                  headers=_gp_headers(), timeout=10)
        data = r.json()
        if data.get("status") == "completed":
            mp3_url  = data.get("result", "")
            srt_url  = data.get("subtitle", "")
            database.upsert_task(prod_id, "audio", "done",
                                 result_text=_json.dumps({
                                     "task_id":      task_id,
                                     "audio_url":    mp3_url,
                                     "subtitle_url": srt_url,
                                 }))
            # Auto-trigger SRT / transcription in background
            threading.Thread(target=_auto_trigger_srt, args=(prod_id,), daemon=True).start()
            # Auto-cache audio file locally so it persists without user interaction
            def _cache_audio(pid, url):
                local = _media_audio_path(pid)
                if os.path.exists(local):
                    return  # already cached
                try:
                    resp = http_requests.get(url, timeout=180)
                    resp.raise_for_status()
                    with open(local, "wb") as _f:
                        _f.write(resp.content)
                    print(f"[Audio cache] prod={pid} saved ({len(resp.content)//1024}KB)")
                except Exception as _ce:
                    print(f"[Audio cache] prod={pid} error: {_ce}")
            if mp3_url:
                threading.Thread(target=_cache_audio, args=(prod_id, mp3_url), daemon=True).start()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SRT → blocos de 8s (Veo3 Flow) ────────────────────────────────────────────

def _srt_to_blocks(srt_text: str, interval: int = 8) -> str:
    """Converte SRT em blocos de N segundos, mesmo formato do Whisper."""
    import re
    blocks: dict = {}
    pattern = re.compile(
        r"\d+\s*\n(\d+):(\d+):(\d+)[,.:](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.:](\d+)\s*\n([\s\S]*?)(?=\n\n|\Z)",
        re.MULTILINE,
    )
    for m in pattern.finditer(srt_text.strip() + "\n\n"):
        h0, m0, s0, ms0 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start = h0 * 3600 + m0 * 60 + s0 + ms0 / 1000
        text  = m.group(9).strip().replace("\n", " ")
        if not text:
            continue
        idx = int(start // interval)
        blocks.setdefault(idx, []).append(text)

    if not blocks:
        return ""

    out = ""
    for n, bi in enumerate(sorted(blocks), 1):
        t0   = _fmt_time(bi * interval)
        t1   = _fmt_time((bi + 1) * interval)
        line = " ".join(blocks[bi]).strip()
        if line:
            out += f"{n}: {t0} até {t1} {line}\n\n"
    total = len(blocks)
    out  += f"\n{'='*60}\nTotal: {total} blocos de {interval} segundos cada\n{'='*60}"
    return out.strip()


# ── Auto-transcrição: usa SRT da GenAIPro (sem Whisper) ───────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/transcription/auto", methods=["POST"])
def api_transcription_auto(prod_id):
    import json as _json
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    try:
        audio_data = _json.loads((prod.get("tasks") or {}).get("audio", {}).get("result_text", "{}"))
    except Exception:
        audio_data = {}

    if not audio_data.get("audio_url"):
        return jsonify({"error": "Gere o áudio primeiro"}), 400

    srt_url     = audio_data.get("subtitle_url", "")
    task_id_a   = audio_data.get("task_id", "")

    # Se subtitle_url não foi salvo (áudio gerado antes da atualização), busca na API
    if not srt_url and task_id_a:
        try:
            r = http_requests.get(f"{GENAIPRO_BASE}/labs/task/{task_id_a}",
                                  headers=_gp_headers(), timeout=10)
            d = r.json()
            srt_url = d.get("subtitle", "")
            if srt_url:
                audio_data["subtitle_url"] = srt_url
                database.upsert_task(prod_id, "audio", "done",
                                     result_text=_json.dumps(audio_data))
        except Exception:
            pass

    if not srt_url:
        return jsonify({"error": "Legenda SRT não disponível. Tente regerar o áudio."}), 400

    job_id = str(uuid.uuid4())
    _transcription_jobs[job_id] = {
        "status": "pending", "progress": "Baixando legenda…",
        "result": None, "error": None, "detected_language": None,
    }

    def _run():
        try:
            _transcription_jobs[job_id]["progress"] = "Baixando SRT da GenAIPro…"
            resp = http_requests.get(srt_url, timeout=30)
            resp.raise_for_status()

            _transcription_jobs[job_id]["progress"] = "Convertendo em blocos de 8s…"
            result_text = _srt_to_blocks(resp.text, interval=8)

            if result_text:
                database.upsert_task(prod_id, "transcription", "done", result_text=result_text)
                _transcription_jobs[job_id].update(
                    status="done", progress="Concluído!", result=result_text
                )
                # Auto-generate YouTube description
                threading.Thread(target=_auto_trigger_description, args=(prod_id,), daemon=True).start()
            else:
                _transcription_jobs[job_id].update(
                    status="error", error="SRT vazio ou formato não reconhecido"
                )
        except Exception as exc:
            _transcription_jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


# ── Prompts Veo3 generation (DOTTI agent) ─────────────────────────────────────

def _extract_dotti_anchor(script_text: str) -> dict | None:
    """Phase 0: call Claude to extract historical/geographic anchor from script.
    Returns dict {periodo, localizacao, restricoes} or None on any failure (non-blocking)."""
    import json as _json, re as _re
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        user_msg = (
            "Leia o roteiro abaixo e extraia o contexto histórico/geográfico para ancorar os prompts visuais.\n\n"
            "Retorne APENAS um JSON válido com exatamente estes 3 campos:\n"
            '{"periodo": "...", "localizacao": "...", "restricoes": ["...", "..."]}\n\n'
            "periodo: período histórico e época (ex: 'Bizâncio, séc. X, reinado de Constantino VII')\n"
            "localizacao: localizações principais citadas no roteiro\n"
            "restricoes: lista de elementos que NÃO devem aparecer visualmente "
            "(ex: 'anacronismos', 'elementos modernos', 'personagens identificáveis')\n\n"
            f"ROTEIRO:\n{script_text[:8000]}"
        )
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            result = stream.get_final_text()
        m = _re.search(r'\{[\s\S]*?\}', result)
        if not m:
            print("[DOTTI Phase0] JSON não encontrado na resposta — âncora ignorada")
            return None
        anchor = _json.loads(m.group(0))
        if not anchor.get("periodo"):
            print("[DOTTI Phase0] âncora incompleta — ignorada")
            return None
        print(f"[DOTTI Phase0] âncora extraída: {anchor.get('periodo')}")
        return anchor
    except Exception as exc:
        print(f"[DOTTI Phase0] erro (ignorado, geração continua): {exc}")
        return None


def _validate_and_fix_prompts(text: str) -> str:
    """Post-generation: fix prompt sequential numbering and timestamp overlaps.
    Anchors regex to PROMPT NNN [...] | MM:SS - MM:SS to avoid false positives.
    Gaps between prompts are kept (valid in Veo3 Flow); only overlaps are fixed."""
    import re as _re

    pattern = _re.compile(
        r'(PROMPT\s+)(\d+)(\s*\[.*?\]\s*\|\s*)(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})',
        _re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return text

    def _to_sec(ts: str) -> int:
        parts = ts.strip().split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(parts[0]) * 60 + int(parts[1])

    def _from_sec(s: int) -> str:
        return f"{s // 60:02d}:{s % 60:02d}"

    numbers = [int(m.group(2)) for m in matches]
    starts  = [_to_sec(m.group(4)) for m in matches]
    ends    = [_to_sec(m.group(5)) for m in matches]

    corrections = []

    # A) Fix sequential numbering (gaps / duplicates)
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        corrections.append(f"Numeração corrigida de {numbers[:5]}… → sequencial 001-{len(numbers):03d}")
        numbers = expected

    # B) Fix timestamp overlaps (start must be ≥ previous end)
    for i in range(1, len(starts)):
        if starts[i] < ends[i - 1]:
            corrections.append(
                f"Sobreposição PROMPT {numbers[i]:03d}: início {_from_sec(starts[i])} "
                f"< fim anterior {_from_sec(ends[i - 1])} → ajustado"
            )
            starts[i] = ends[i - 1]

    if not corrections:
        return text

    # Apply corrections back to front to preserve string positions
    result = text
    for i in range(len(matches) - 1, -1, -1):
        m = matches[i]
        new_fragment = (
            f"{m.group(1)}{numbers[i]:03d}{m.group(3)}"
            f"{_from_sec(starts[i])} - {_from_sec(ends[i])}"
        )
        result = result[:m.start()] + new_fragment + result[m.end():]

    for c in corrections:
        print(f"[Prompts Validate] {c}")

    return result


def _bg_prompts(job_id, prod_id, user_msg, script_text: str = "", instrucoes_visuais: str = ""):
    """Background thread: Phase 0 anchor → Claude DOTTI → validate/fix → save to DB."""
    try:
        # Phase 0: extract contextual anchor (non-blocking — failure is silently ignored)
        anchor = _extract_dotti_anchor(script_text) if script_text else None

        final_user_msg = user_msg
        if anchor:
            restricoes = anchor.get("restricoes", [])
            restricoes_str = (
                "; ".join(restricoes) if isinstance(restricoes, list) else str(restricoes)
            )
            anchor_block = (
                "ÂNCORA CONTEXTUAL DO EPISÓDIO (extraída do roteiro — respeitar rigorosamente):\n"
                f"Período: {anchor.get('periodo', '')}\n"
                f"Localização: {anchor.get('localizacao', '')}\n"
                f"Restrições absolutas de cena: {restricoes_str}\n"
            )
            if instrucoes_visuais:
                anchor_block += f"\nPERFIL VISUAL DO CANAL:\n{instrucoes_visuais}\n"
            anchor_block += "\n"
            final_user_msg = anchor_block + final_user_msg

        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=_model_max_tokens(CLAUDE_MODEL),
            system=DOTTI_SYSTEM,
            messages=[{"role": "user", "content": final_user_msg}],
        ) as stream:
            prompts_text = stream.get_final_text()

        # Validate and fix numbering + timestamp overlaps
        prompts_text = _validate_and_fix_prompts(prompts_text)

        database.upsert_task(prod_id, "prompts", "done", result_text=prompts_text)
        _claude_jobs[job_id].update(status="done")
        print(f"[Prompts BG] prod={prod_id} done tokens≈{len(prompts_text)//4}")
    except Exception as exc:
        # Use set_task_status so that any previously completed result_text is preserved
        database.set_task_status(prod_id, "prompts", "pending", notes=f"Erro: {exc}")
        _claude_jobs[job_id].update(status="error", error=str(exc))
        print(f"[Prompts BG] prod={prod_id} error: {exc}")


@app.route("/api/productions/<int:prod_id>/tasks/description/generate", methods=["POST"])
def api_description_generate(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404
    if not (prod.get("tasks") or {}).get("script", {}).get("result_text"):
        return jsonify({"error": "Gere o roteiro primeiro"}), 400
    database.set_task_status(prod_id, "description", "in_progress")
    threading.Thread(target=_auto_trigger_description, args=(prod_id,), daemon=True).start()
    return jsonify({"queued": True})


@app.route("/api/productions/<int:prod_id>/tasks/prompts/reprompt", methods=["POST"])
def api_prompts_reprompt(prod_id):
    """Regenerate specific failed prompt numbers with new policy-compliant versions."""
    import anthropic as _anthropic
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    tasks = prod.get("tasks") or {}
    existing_prompts = tasks.get("prompts", {}).get("result_text", "")
    if not existing_prompts:
        return jsonify({"error": "Gere os prompts primeiro"}), 400

    body = request.get_json(force=True)
    numbers = body.get("numbers", [])
    if not numbers or not isinstance(numbers, list):
        return jsonify({"error": "Informe os números dos prompts a regerar"}), 400
    numbers = [int(n) for n in numbers if str(n).strip().isdigit()]
    if not numbers:
        return jsonify({"error": "Números inválidos"}), 400

    nums_str = ", ".join(str(n) for n in sorted(numbers))
    user_msg = (
        f"Abaixo estão todos os prompts gerados anteriormente para este projeto.\n\n"
        f"{existing_prompts}\n\n"
        f"═══════════════════════════════════════\n"
        f"Os seguintes prompts foram REPROVADOS pelo Veo3 por violação de políticas: {nums_str}\n\n"
        f"Regenere APENAS esses prompts ({nums_str}) com versões novas e completamente conformes.\n"
        f"Regras obrigatórias para os novos prompts:\n"
        f"- Sem personagens identificáveis, sem rostos, sem atores\n"
        f"- Sem violência explícita — reescrever como cena épica distante, artefato simbólico ou ambiente atmosférico\n"
        f"- Sem conteúdo sensível — reformular de forma implícita e artística\n"
        f"- Manter o timestamp e a essência narrativa do prompt original\n"
        f"- Seguir exatamente o formato PROMPT [Nº] [] | [TIMESTAMP]: ...\n\n"
        f"Retorne APENAS os prompts regerados ({nums_str}), mantendo a numeração original. "
        f"Não inclua os outros prompts nem texto extra."
    )

    try:
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=_model_max_tokens(CLAUDE_MODEL),
            system=DOTTI_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            result = stream.get_final_text()
        return jsonify({"result": result, "numbers": numbers})
    except Exception as e:
        print(f"[Reprompt] prod={prod_id} error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/productions/<int:prod_id>/tasks/prompts/generate", methods=["POST"])
def api_prompts_generate(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    tasks         = prod.get("tasks") or {}
    script_text   = tasks.get("script",        {}).get("result_text", "")
    transcription = tasks.get("transcription", {}).get("result_text", "")
    if not script_text:
        return jsonify({"error": "Gere o roteiro primeiro"}), 400

    # Channel visual instructions for DOTTI Phase 0 anchor injection
    channel = database.get_channel(prod["channel_id"])
    instrucoes_visuais = (channel or {}).get("instrucoes_visuais", "").strip()

    user_msg = f"ROTEIRO:\n{script_text}"
    if transcription:
        user_msg += f"\n\nSINCRONIZAÇÃO (transcrição em blocos de 8 segundos):\n{transcription}"
        user_msg += "\n\nGere TODOS os prompts de cena para o Veo 3 Flow, seguindo a sincronização fornecida (um prompt por bloco de 8 segundos)."
    else:
        user_msg += (
            "\n\nNão há sincronização disponível ainda. Estime a duração total do roteiro "
            "(média: 130 palavras = 60 segundos de narração) e gere prompts de 8 segundos "
            "cobrindo toda a duração estimada. Distribua as cenas de forma uniforme ao longo do roteiro."
        )

    # Mark in_progress immediately and launch background thread
    # Use set_task_status (not upsert_task) so any previous result_text is preserved
    job_id = str(uuid.uuid4())[:12]
    _claude_jobs[job_id] = {"prod_id": prod_id, "task_type": "prompts", "status": "running", "error": None}
    database.set_task_status(prod_id, "prompts", "in_progress")
    threading.Thread(
        target=_bg_prompts,
        args=(job_id, prod_id, user_msg, script_text, instrucoes_visuais),
        daemon=True,
    ).start()
    print(f"[Prompts] prod={prod_id} queued job={job_id}")
    return jsonify({"queued": True, "job_id": job_id})


# ── Thumbnail generation (GenAIPro VEO) ───────────────────────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/thumbnails/generate", methods=["POST"])
def api_thumbnails_generate(prod_id):
    import json as _json
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    # Reject if already running for this production
    existing = _thumbnail_jobs.get(prod_id, {})
    if existing.get("status") == "processing":
        return jsonify({"queued": True, "already_running": True})

    tasks            = prod.get("tasks") or {}
    script_text      = tasks.get("script", {}).get("result_text", "")
    title            = prod.get("adapted_title") or prod.get("source_title", "")
    source_thumbnail = (prod.get("source_thumbnail") or "").strip()

    # Mark in_progress in DB immediately so panel shows spinner on return
    # Use set_task_status (not upsert_task) so previous thumbnail URLs are preserved
    # in case generation fails and we need to restore via the reset endpoint.
    database.set_task_status(prod_id, "thumbnails", "in_progress")

    # Job keyed by prod_id (not a random UUID) so it can be found without job_id
    _thumbnail_jobs[prod_id] = {
        "status":  "processing",
        "phase":   "prompts",   # "prompts" → "images"
        "total":   4,
        "done":    0,
        "urls":    [],
        "prompts": [],
        "error":   None,
    }

    def _bg_thumbnails():
        import json as _j
        import base64 as _b64
        job = _thumbnail_jobs[prod_id]

        # ── Outer safety net: any unhandled exception → job marked error ──────
        try:
            # BUG-FIX: initialize before inner try so except block can always read them
            has_ref_image = False
            thumb_prompts = []

            # ── Phase 1: Claude generates 4 DALL-E prompts ───────────────────
            try:
                import anthropic as _anthropic
                _ac = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

                user_content = []

                if source_thumbnail:
                    try:
                        img_resp = http_requests.get(source_thumbnail, timeout=10)
                        if img_resp.status_code == 200:
                            ct = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                            if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                                ct = "image/jpeg"
                            img_b64 = _b64.standard_b64encode(img_resp.content).decode("utf-8")
                            user_content.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": ct, "data": img_b64},
                            })
                            has_ref_image = True
                            print(f"[Thumbnails] Source thumbnail loaded ({len(img_resp.content)//1024}KB)")
                    except Exception as _te:
                        print(f"[Thumbnails] Could not load source thumbnail: {_te}")

                if has_ref_image:
                    prompt_text = (
                        f"You are a YouTube thumbnail design expert.\n"
                        f"The image above is the thumbnail of the original English video.\n"
                        f"I am producing an adapted version of this video titled: \"{title}\"\n"
                        + (f"Script excerpt: {script_text[:400]}\n" if script_text else "")
                        + """
Generate 4 DALL-E 3 image prompts for YouTube thumbnails engineered for MAXIMUM CLICK-THROUGH RATE (CTR).

PROMPTS 1 & 2 — INSPIRED BY SOURCE THUMBNAIL:
Study the source thumbnail's composition, color palette and mood. Adapt those visual qualities to the new title while applying all CTR rules below.

PROMPTS 3 & 4 — COMPLETELY ORIGINAL CONCEPTS:
Ignore the source thumbnail. Invent bold, distinct visual concepts for the same title.

━━━ HIGH-CTR RULES (mandatory for ALL 4 prompts) ━━━
FACES & PEOPLE: If the concept includes a human figure, their face must be LARGE (filling 30–50 % of the frame), showing a powerful emotion — awe, shock, fierce determination, or raw menace. Eyes sharp, bright and expressive. Skin must show real human texture: visible pores, subtle imperfections, natural weathering — NOT plastic, NOT wax, NOT CGI doll-like. Think a real actor photographed by a professional cinematographer.
FOCAL POINT: One single dominant subject. No clutter, no busy backgrounds competing for attention.
CONTRAST: Maximum tonal contrast — dramatically dark background against a brilliant, luminous subject (or inverse). Deep blacks, crisp highlights, punchy midtones.
COLOR: Hyper-saturated bold palette. Dominant warm tones (molten amber, deep crimson, rich gold) plus one cool accent (cobalt blue, royal purple). Colors that POP against YouTube's light-grey UI.
LIGHTING: Hard cinematic lighting — blazing rim light, golden-hour warmth, torch/fire glow, or dramatic god-rays. Absolutely no flat or even lighting.
EMOTION & SCALE: Evoke instant emotion — epic grandeur, imminent danger, forbidden discovery, or breathtaking spectacle. Viewer must feel something in under one second.
PHOTOREALISM: RAW photograph aesthetic. Shot on Canon EOS R5 with 85mm f/1.4 lens. Natural film grain, true-to-life material textures (rough stone, worn leather, aged metal, real fabric weave). Everything must look physically real and tangible — NOT rendered, NOT illustrated, NOT 3D CGI.
TECHNICAL: 8K hyperdetailed, shallow depth of field (subject tack-sharp, background artistically blurred), 16:9 widescreen 1792×1024. STRICTLY NO text, letters, numbers, words, watermarks, logos or any writing anywhere in the image.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY a JSON array of 4 strings (prompts 1–4). No markdown, no extra text."""
                    )
                else:
                    prompt_text = (
                        f"Create 4 DALL-E 3 image prompts for a YouTube thumbnail engineered for MAXIMUM CTR.\n"
                        f"Video title: \"{title}\"\n"
                        + (f"Script excerpt: {script_text[:600]}\n" if script_text else "")
                        + """
Each of the 4 prompts must describe a COMPLETELY DIFFERENT visual concept (e.g. dramatic human portrait, epic landscape/architecture, powerful artifact close-up, tense action moment).

━━━ HIGH-CTR RULES (mandatory for ALL 4 prompts) ━━━
FACES & PEOPLE: If the concept includes a human figure, their face must be LARGE (filling 30–50 % of the frame), showing a powerful emotion — awe, shock, fierce determination, or raw menace. Eyes sharp, bright and expressive.
FOCAL POINT: One single dominant subject. No clutter, no busy backgrounds competing for attention.
CONTRAST: Maximum tonal contrast — dramatically dark background against a brilliant, luminous subject (or inverse). Deep blacks, crisp highlights, punchy midtones.
COLOR: Hyper-saturated bold palette. Dominant warm tones (molten amber, deep crimson, rich gold) plus one cool accent (cobalt blue, royal purple). Colors that POP against YouTube's light-grey UI.
LIGHTING: Hard cinematic lighting — blazing rim light, golden-hour warmth, torch/fire glow, or dramatic god-rays. Absolutely no flat or even lighting.
EMOTION & SCALE: Evoke instant emotion — epic grandeur, imminent danger, forbidden discovery, or breathtaking spectacle. Viewer must feel something in under one second.
TECHNICAL: Photorealistic, 8K hyperdetailed, shallow depth of field (subject tack-sharp, background artistically blurred), 16:9 widescreen 1792×1024. STRICTLY NO text, letters, numbers, words, watermarks, logos or any writing anywhere in the image.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY a JSON array of 4 strings. No markdown, no extra text."""
                    )

                user_content.append({"type": "text", "text": prompt_text})

                with _ac.messages.stream(
                    model=CLAUDE_MODEL, max_tokens=1500,
                    messages=[{"role": "user", "content": user_content}],
                ) as stream:
                    raw = stream.get_final_text().strip()
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw).strip()
                thumb_prompts = _j.loads(raw)
                if not isinstance(thumb_prompts, list) or len(thumb_prompts) == 0:
                    raise ValueError("Expected non-empty list")
                thumb_prompts = [str(p) for p in thumb_prompts[:4]]
                print(f"[Thumbnails] {len(thumb_prompts)} prompts generated (ref_image={has_ref_image})")
            except Exception as _pe:
                print(f"[Thumbnails] Claude prompt error: {_pe} — using fallback prompts")
                thumb_prompts = [
                    "Extreme close-up RAW photograph of a real medieval knight's weathered face, fierce determined eyes, dramatic hard rim light casting deep shadows, visible skin pores and battle scars, natural skin texture NOT plastic NOT wax, molten amber and crimson tones, hyper-saturated, shot on Canon EOS R5 85mm f/1.4, shallow depth of field, 16:9 widescreen, no text no letters no watermarks",
                    "Epic RAW photograph of a medieval army charging at golden hour, silhouetted real warriors backlit by a blazing orange sun, god-rays piercing dust clouds, maximum contrast dark foreground vs incandescent sky, natural film grain, photorealistic 8K, 16:9, no text no letters no watermarks",
                    "Towering gothic castle on a cliff at night, ring of torches below and full moon above, deep cobalt sky vs warm amber firelight, lone real human figure at the gate, rough stone texture visible, hyperdetailed RAW photograph, 16:9, no text no letters no watermarks",
                    "Close-up RAW photograph of an ancient crown on a stone altar, single shaft of golden light from above, deep black background, real metal texture with age patina, jewel tones hyper-saturated, film grain, shallow depth of field, 8K, 16:9, no text no letters no watermarks",
                ]

            job["prompts"]  = thumb_prompts
            job["phase"]    = "images"
            job["total"]    = len(thumb_prompts)
            job["used_ref"] = has_ref_image

            # ── Phase 2: DALL-E 3 generates each image ────────────────────────
            if not config.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY não configurada nas variáveis de ambiente do Railway")

            from openai import OpenAI as _OAI
            oai = _OAI(api_key=config.OPENAI_API_KEY)
            urls = []
            last_error = ""
            for i, p in enumerate(thumb_prompts):
                try:
                    resp = oai.images.generate(
                        model="dall-e-3",
                        prompt=p,
                        size="1792x1024",
                        quality="hd",
                        n=1,
                    )
                    ext_url = resp.data[0].url
                    # Try local file first, fall back to base64 data URI (never store expiring URL)
                    serve_url = None
                    try:
                        img_bytes = http_requests.get(ext_url, timeout=60).content
                        local_path = _media_thumb_path(prod_id, i)
                        try:
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, "wb") as _f:
                                _f.write(img_bytes)
                            serve_url = f"/api/media/thumbs/{prod_id}_{i}.png"
                            print(f"[DALL-E] {i+1}/{len(thumb_prompts)} cached locally prod={prod_id}")
                        except Exception as _fs_err:
                            serve_url = "data:image/png;base64," + _b64.b64encode(img_bytes).decode()
                            print(f"[DALL-E] {i+1} stored as base64 (fs error: {_fs_err})")
                    except Exception as _dl_err:
                        print(f"[DALL-E] download error image {i+1}: {_dl_err}")
                    urls.append(serve_url)
                    job["done"] = i + 1
                except Exception as _ie:
                    last_error = str(_ie)
                    print(f"[DALL-E] generation error image {i+1}: {_ie}")
                    urls.append(None)
                    job["done"] = i + 1

            real_urls = [u for u in urls if u]
            if real_urls:
                job.update(status="done", urls=real_urls)
                database.upsert_task(prod_id, "thumbnails", "done",
                                     result_text=_j.dumps({
                                         "urls":     real_urls,
                                         "prompts":  thumb_prompts,
                                         "used_ref": has_ref_image,
                                     }))
            else:
                # Surface the actual OpenAI error so the user knows what's wrong
                err_msg = f"Nenhuma imagem gerada. Erro DALL-E: {last_error}" if last_error \
                          else "Nenhuma imagem gerada — verifique créditos OpenAI / DALL-E 3"
                job.update(status="error", error=err_msg)
                database.set_task_status(prod_id, "thumbnails", "pending", notes=err_msg)

        except Exception as _fatal:
            import traceback as _tb
            full_tb = _tb.format_exc()
            err_msg = f"{type(_fatal).__name__}: {_fatal}"
            print(f"[Thumbnails] Fatal error prod={prod_id}:\n{full_tb}")
            # Use job["key"] = value directly — safer than .get(id,{}).update() which
            # silently modifies a throwaway dict if prod_id was removed from the dict
            job["status"] = "error"
            job["error"]  = err_msg
            database.set_task_status(prod_id, "thumbnails", "pending",
                                     notes=err_msg[:500])

    threading.Thread(target=_bg_thumbnails, daemon=True).start()
    return jsonify({"queued": True})


@app.route("/api/productions/<int:prod_id>/tasks/thumbnails/status")
def api_thumbnails_status(prod_id):
    """Poll thumbnail generation progress keyed by prod_id."""
    job = _thumbnail_jobs.get(prod_id)
    if not job:
        # No active job in memory — check DB for current task status
        prod = database.get_production(prod_id)
        if prod:
            task = (prod.get("tasks") or {}).get("thumbnails", {})
            notes = task.get("notes", "")
            return jsonify({
                "status": task.get("status", "pending"),
                "done": 0, "total": 4,
                # surface DB notes as error so UI can display them
                "error": notes or None,
            })
        return jsonify({"status": "unknown"}), 404
    total = job.get("total") or 4
    done  = job.get("done", 0)
    return jsonify({**job, "progress": f"{done}/{total}"})


# ── Startup (runs under both `python app.py` and gunicorn) ───────────────────

def _initialize():
    """Init DB, scheduler e auto-fetch. Idempotente."""
    database.init_db()
    database.init_production_tables()

    # On every startup, reset tasks stuck in in_progress (threads don't survive restarts)
    stale = database.reset_stale_tasks(stale_minutes=0)  # 0 = ALL in_progress
    if stale:
        print(f"[Startup] Reset {stale} stale in_progress task(s) to pending/done")

    scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")
    scheduler.add_job(
        fetcher.fetch_videos,
        CronTrigger(hour=config.SCHEDULE_HOUR, minute=config.SCHEDULE_MINUTE),
        id="daily_fetch",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()

    def _startup_fetch():
        import time as _t
        _t.sleep(4)
        if not database.fetched_today():
            app.logger.info("Startup: nenhum fetch hoje — executando agora…")
            try:
                fetcher.fetch_videos()
            except Exception as _e:
                app.logger.warning("Startup fetch error: %s", _e)
    threading.Thread(target=_startup_fetch, daemon=True).start()

# Corre sempre — gunicorn importa o módulo sem chamar __main__
_initialize()

if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║   YouTube Analyzer — Idade Medieval          ║")
    print(f"║   Acesse: http://localhost:{config.PORT}             ║")
    print(f"║   Modelo Claude: {CLAUDE_MODEL[:30]:<30}║")
    print(f"║   Sync automático: todo dia às {config.SCHEDULE_HOUR:02d}h{config.SCHEDULE_MINUTE:02d}       ║")
    print("╚══════════════════════════════════════════════╝")
    print()
    app.run(debug=False, host="0.0.0.0", port=config.PORT)
