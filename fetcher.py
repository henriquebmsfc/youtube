import math
import re
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

import config
import database

MIN_DURATION_SECONDS = 8 * 60  # 8 minutes


def _parse_duration(duration: str) -> int:
    """Convert ISO 8601 duration (PT#H#M#S) to total seconds."""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration or "")
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mn = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mn * 60 + s


def _calculate_scores(views: int, likes: int, comments: int, published_at: str):
    """Return (engagement_rate_pct, opportunity_score_0_100)."""
    # Engagement rate
    engagement = ((likes + comments * 2) / views * 100) if views > 0 else 0

    # Views score: log scale, 1 M views → 100 (more sensitive to the real range)
    views_score = min(math.log10(views + 1) / math.log10(1_000_000) * 100, 100)

    # Engagement score: 3 % = 100 (low weight — just a tie-breaker)
    eng_score = min(engagement / 3 * 100, 100)

    # Recency score: exponential decay, half-life = 2 days
    # Differentiates well in the first few days then flattens (day 7 ≈ 11)
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        days_old = (datetime.now(timezone.utc) - pub).days
        recency = 100 * math.exp(-days_old * math.log(2) / 2)
    except Exception:
        recency = 50.0

    # Weights: views 65%, engagement 5%, recency 30%
    opportunity = views_score * 0.65 + eng_score * 0.05 + recency * 0.30
    return round(engagement, 4), round(opportunity, 1)


def fetch_videos() -> int:
    if config.YOUTUBE_API_KEY == "SUA_API_KEY_AQUI":
        msg = "API Key não configurada. Edite config.py e insira sua chave."
        database.log_fetch(0, "error", msg)
        raise ValueError(msg)

    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)

    published_after = (
        datetime.utcnow() - timedelta(days=config.DAYS_BACK)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 1. Search for video IDs per keyword ──────────────────────────────────
    # videoDuration=long → only videos 20+ minutes (YouTube API definition).
    # One search per keyword = 100 quota units each.
    video_keyword: dict[str, str] = {}

    for keyword in database.get_keywords():
        try:
            resp = (
                youtube.search()
                .list(
                    part="snippet",
                    q=keyword,
                    type="video",
                    publishedAfter=published_after,
                    maxResults=config.MAX_RESULTS_PER_KEYWORD,
                    order="viewCount",
                    relevanceLanguage="en",
                    regionCode="US",
                )
                .execute()
            )
            for item in resp.get("items", []):
                vid = item["id"]["videoId"]
                if vid not in video_keyword:
                    video_keyword[vid] = keyword
        except Exception as e:
            print(f"  [WARN] Busca '{keyword}': {e}")

    # ── 2. Fetch detailed stats in batches of 50 ─────────────────────────────
    ids = list(video_keyword.keys())
    saved = 0

    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        try:
            resp = (
                youtube.videos()
                .list(part="statistics,snippet,contentDetails", id=",".join(batch))
                .execute()
            )
            for item in resp.get("items", []):
                vid = item["id"]
                snip = item["snippet"]
                stats = item.get("statistics", {})

                # Skip videos shorter than 8 minutes
                duration_secs = _parse_duration(
                    item.get("contentDetails", {}).get("duration", "")
                )
                if duration_secs < MIN_DURATION_SECONDS:
                    continue

                # Skip videos explicitly declared as non-English
                audio_lang = (snip.get("defaultAudioLanguage") or "").lower()
                text_lang  = (snip.get("defaultLanguage") or "").lower()
                for lang in (audio_lang, text_lang):
                    if lang and not lang.startswith("en"):
                        break
                else:
                    lang = ""  # both empty or both English — allow through
                if lang and not lang.startswith("en"):
                    continue

                views = int(stats.get("viewCount", 0))
                likes = int(stats.get("likeCount", 0))
                comments = int(stats.get("commentCount", 0))
                published_at = snip.get("publishedAt", "")

                engagement, opportunity = _calculate_scores(
                    views, likes, comments, published_at
                )

                thumbs = snip.get("thumbnails", {})
                thumbnail = (
                    thumbs.get("medium", {}).get("url")
                    or thumbs.get("default", {}).get("url", "")
                )

                database.upsert_video(
                    {
                        "video_id":          vid,
                        "title":             snip.get("title", ""),
                        "channel_name":      snip.get("channelTitle", ""),
                        "published_at":      published_at,
                        "views":             views,
                        "likes":             likes,
                        "comments":          comments,
                        "thumbnail_url":     thumbnail,
                        "video_url":         f"https://youtube.com/watch?v={vid}",
                        "keyword":           video_keyword[vid],
                        "engagement_score":  engagement,
                        "opportunity_score": opportunity,
                        "duration_seconds":  duration_secs,
                        "fetched_at":        datetime.now().isoformat(),
                    }
                )
                saved += 1
        except Exception as e:
            print(f"  [WARN] Stats batch: {e}")

    database.log_fetch(saved, "success", f"{saved} vídeos atualizados")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetch concluído — {saved} vídeos salvos")
    return saved
