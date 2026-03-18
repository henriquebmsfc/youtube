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

# ── Claude model — auto-detectado na inicialização ───────────────────────────
_CLAUDE_CANDIDATES = [
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-0",
    "claude-sonnet-4-0",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-opus-20240229",
    "claude-3-haiku-20240307",
]

def _detect_claude_model() -> str:
    """Interroga a API Anthropic e retorna o melhor modelo disponível."""
    try:
        import anthropic as _a
        client = _a.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        available = {m.id for m in client.models.list()}
        for candidate in _CLAUDE_CANDIDATES:
            if candidate in available:
                print(f"[Claude] Modelo detectado: {candidate}")
                return candidate
        # Nenhum preferido — usa o primeiro disponível que contenha 'sonnet'
        sonnet = sorted([i for i in available if "sonnet" in i.lower()], reverse=True)
        if sonnet:
            print(f"[Claude] Usando fallback sonnet: {sonnet[0]}")
            return sonnet[0]
        if available:
            best = sorted(available, reverse=True)[0]
            print(f"[Claude] Usando fallback: {best}")
            return best
    except Exception as _e:
        print(f"[Claude] Não foi possível listar modelos: {_e}")
    fallback = _CLAUDE_CANDIDATES[0]
    print(f"[Claude] Usando candidato padrão: {fallback}")
    return fallback

CLAUDE_MODEL: str = _detect_claude_model()

# ── GenAIPro constants ────────────────────────────────────────────────────────
GENAIPRO_BASE = "https://genaipro.vn/api/v1"

_LANG_MAP = {
    "pt": "portuguese", "en": "english", "es": "spanish",
    "de": "german", "fr": "french", "it": "italian",
    "ro": "romanian", "pl": "polish",
}

_thumbnail_jobs: dict = {}   # job_id → {status, urls, error, prompt}

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


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        count = fetcher.fetch_videos()
        return jsonify({"success": True, "count": count, "message": f"{count} vídeos atualizados"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400


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
    return jsonify(database.get_productions(channel_id))


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
    database.delete_production(prod_id)
    return jsonify({"success": True})


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


@app.route("/api/productions/<int:prod_id>/tasks/script/generate", methods=["POST"])
def api_script_generate(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    channel = database.get_channel(prod["channel_id"])
    lang_name = channel["name"] if channel else "Português"
    lang_code = channel["language_code"] if channel else "pt"

    # Language name map
    _lang_names = {
        "pt": "Português", "en": "English", "es": "Español",
        "de": "Deutsch", "fr": "Français", "it": "Italiano",
        "ro": "Română", "pl": "Polski",
    }
    target_lang = _lang_names.get(lang_code, lang_code)

    # Optional: include existing transcription
    existing_ts = (prod.get("tasks") or {}).get("transcription", {})
    transcription_text = existing_ts.get("result_text", "") if existing_ts else ""

    prompt_parts = [
        f"Crie um roteiro completo em {target_lang} para um vídeo de YouTube sobre história medieval.",
        f"\nTítulo original (em inglês): \"{prod['source_title']}\"",
        f"Título adaptado ({target_lang}): \"{prod['adapted_title']}\"",
        f"Canal de origem: {prod['source_channel']}",
    ]
    if transcription_text:
        prompt_parts.append(f"\nTranscrição do vídeo original:\n{transcription_text[:6000]}")
    prompt_parts.append(
        "\n\nO roteiro deve ter:\n"
        "- Introdução chamativa (gancho nos primeiros 30 segundos)\n"
        "- Desenvolvimento com informações históricas precisas e bem explicadas\n"
        "- Conclusão com call-to-action (curtir, inscrever, comentar)\n"
        "- Indicações de tempo estimado de narração por seção\n"
        "- Tom educativo mas envolvente, acessível ao público geral\n"
        "Escreva o roteiro completo pronto para ser gravado."
    )
    full_prompt = "\n".join(prompt_parts)

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system="Você é um roteirista especialista em vídeos educativos de YouTube sobre história medieval. Cria roteiros envolventes, precisos e adaptados culturalmente para o público-alvo.",
            messages=[{"role": "user", "content": full_prompt}],
        )
        script_text = message.content[0].text
        database.upsert_task(prod_id, "script", "done", result_text=script_text)
        return jsonify({"success": True, "script": script_text})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/productions/<int:prod_id>/channel", methods=["GET"])
def api_production_channel(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404
    return jsonify(database.get_channel(prod["channel_id"]))


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


@app.route("/api/productions/<int:prod_id>/tasks/audio/status/<task_id>")
def api_audio_status(prod_id, task_id):
    import json as _json
    try:
        r    = http_requests.get(f"{GENAIPRO_BASE}/labs/task/{task_id}",
                                  headers=_gp_headers(), timeout=10)
        data = r.json()
        if data.get("status") == "completed":
            mp3_url = data.get("result", "")
            database.upsert_task(prod_id, "audio", "done",
                                 result_text=_json.dumps({"task_id": task_id, "audio_url": mp3_url}))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Auto-transcription from audio URL ─────────────────────────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/transcription/auto", methods=["POST"])
def api_transcription_auto(prod_id):
    import json as _json, time as _time
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    try:
        audio_data = _json.loads((prod.get("tasks") or {}).get("audio", {}).get("result_text", "{}"))
        audio_url  = audio_data.get("audio_url", "")
    except Exception:
        audio_url = ""
    if not audio_url:
        return jsonify({"error": "Gere o áudio primeiro"}), 400

    channel   = database.get_channel(prod["channel_id"])
    lang_code = (channel or {}).get("language_code", "pt")

    job_id = str(uuid.uuid4())
    _transcription_jobs[job_id] = {
        "status": "pending", "progress": "Baixando áudio…",
        "result": None, "error": None, "detected_language": None,
    }

    def _run():
        try:
            resp = http_requests.get(audio_url, timeout=120)
            tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            tmp.write(resp.content)
            tmp.close()
            _run_transcription(job_id, tmp.name, "base", lang_code, True, 8)
            # Wait for Whisper to finish (max 10 min)
            for _ in range(600):
                _time.sleep(1)
                if _transcription_jobs[job_id]["status"] in ("done", "error"):
                    break
            if _transcription_jobs[job_id]["status"] == "done":
                database.upsert_task(prod_id, "transcription", "done",
                                     result_text=_transcription_jobs[job_id]["result"])
        except Exception as exc:
            _transcription_jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


# ── Prompts Veo3 generation (DOTTI agent) ─────────────────────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/prompts/generate", methods=["POST"])
def api_prompts_generate(prod_id):
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    tasks          = prod.get("tasks") or {}
    script_text    = tasks.get("script",        {}).get("result_text", "")
    transcription  = tasks.get("transcription", {}).get("result_text", "")
    if not script_text:
        return jsonify({"error": "Gere o roteiro primeiro"}), 400

    user_msg = f"ROTEIRO:\n{script_text}"
    if transcription:
        user_msg += f"\n\nSINCRONIZAÇÃO (transcrição em blocos de 8 segundos):\n{transcription}"
    user_msg += "\n\nPor favor, analise os personagens e gere todos os prompts de cena para o Veo 3 Flow."

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=DOTTI_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        prompts_text = msg.content[0].text
        database.upsert_task(prod_id, "prompts", "done", result_text=prompts_text)
        return jsonify({"success": True, "prompts": prompts_text})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── Thumbnail generation (GenAIPro VEO) ───────────────────────────────────────

@app.route("/api/productions/<int:prod_id>/tasks/thumbnails/generate", methods=["POST"])
def api_thumbnails_generate(prod_id):
    import json as _json
    prod = database.get_production(prod_id)
    if not prod:
        return jsonify({"error": "Produção não encontrada"}), 404

    tasks       = prod.get("tasks") or {}
    script_text = tasks.get("script", {}).get("result_text", "")
    title       = prod.get("adapted_title") or prod.get("source_title", "")

    # Generate 4 diverse thumbnail prompts with Claude
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt_req = (
            f"Create 4 different YouTube thumbnail image prompts for a medieval history video.\n"
            f"Title: {title}\n"
            + (f"Script excerpt: {script_text[:600]}\n" if script_text else "") +
            "\nRequirements for each prompt:\n"
            "- Epic medieval scene, dramatically different visual concept for each\n"
            "- Photorealistic, cinematic, 16:9 format, high detail\n"
            "- Each one should emphasize a different aspect: e.g. battle, castle, portrait, map/artifact\n"
            "Return a JSON array of 4 strings, each a complete image generation prompt. "
            "No extra text, just the JSON array."
        )
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt_req}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        thumb_prompts = _json.loads(raw)
        if not isinstance(thumb_prompts, list):
            raise ValueError("Expected list")
    except Exception:
        thumb_prompts = [
            f"Epic medieval battle scene, dramatic cinematic lighting, {title}, photorealistic 8K",
            f"Majestic medieval castle at sunset, dramatic sky, {title}, photorealistic cinematic",
            f"Medieval knight portrait, dramatic close-up, armor detail, {title}, photorealistic 8K",
            f"Ancient medieval map and artifacts, dramatic lighting, {title}, photorealistic cinematic",
        ]

    job_id = str(uuid.uuid4())
    _thumbnail_jobs[job_id] = {"status": "processing", "urls": [], "error": None,
                                "prompts": thumb_prompts}

    def _generate():
        urls = []
        for p in thumb_prompts:
            try:
                resp = http_requests.post(
                    f"{GENAIPRO_BASE}/veo/create-image",
                    headers={"Authorization": f"Bearer {config.GENAIPRO_API_KEY}"},
                    data={"prompt": p, "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                          "number_of_images": 1},
                    stream=True, timeout=300,
                )
                found = []
                for line in resp.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line.startswith("data:"):
                        try:
                            d = _json.loads(line[5:].strip())
                            if d.get("status") == "completed":
                                found = d.get("file_urls", [])
                                break
                            if d.get("file_urls"):
                                found = d["file_urls"]
                        except Exception:
                            pass
                urls.extend(found if found else [])
            except Exception:
                pass
        if urls:
            _thumbnail_jobs[job_id].update(status="done", urls=urls)
            database.upsert_task(prod_id, "thumbnails", "done",
                                 result_text=_json.dumps({"urls": urls, "prompts": thumb_prompts}))
        else:
            _thumbnail_jobs[job_id].update(status="error",
                                            error="Nenhuma imagem gerada — verifique créditos VEO")

    threading.Thread(target=_generate, daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/thumbnails/status/<job_id>")
def api_thumbnails_status(job_id):
    job = _thumbnail_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


# ── Startup (runs under both `python app.py` and gunicorn) ───────────────────

def _initialize():
    """Init DB, scheduler e auto-fetch. Idempotente."""
    database.init_db()
    database.init_production_tables()

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
