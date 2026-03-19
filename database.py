import os
import sqlite3
from datetime import datetime

import config as _cfg

DB_PATH = os.path.join(_cfg.DB_DIR, "youtube_analyzer.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            video_id          TEXT PRIMARY KEY,
            title             TEXT,
            channel_name      TEXT,
            published_at      TEXT,
            views             INTEGER DEFAULT 0,
            likes             INTEGER DEFAULT 0,
            comments          INTEGER DEFAULT 0,
            thumbnail_url     TEXT,
            video_url         TEXT,
            keyword           TEXT,
            engagement_score  REAL DEFAULT 0,
            opportunity_score REAL DEFAULT 0,
            duration_seconds  INTEGER DEFAULT 0,
            fetched_at        TEXT
        )
    ''')
    # Migration: add duration_seconds to existing databases
    try:
        c.execute("ALTER TABLE videos ADD COLUMN duration_seconds INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # column already exists
    c.execute('''
        CREATE TABLE IF NOT EXISTS fetch_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at   TEXT,
            videos_found INTEGER,
            status       TEXT,
            message      TEXT
        )
    ''')
    conn.commit()
    conn.close()


def wipe_videos():
    """Delete all videos (used to clear stale data before a clean re-sync)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM videos")
    conn.commit()
    conn.close()


def upsert_video(video: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO videos
            (video_id, title, channel_name, published_at, views, likes, comments,
             thumbnail_url, video_url, keyword, engagement_score, opportunity_score,
             duration_seconds, fetched_at)
        VALUES
            (:video_id, :title, :channel_name, :published_at, :views, :likes, :comments,
             :thumbnail_url, :video_url, :keyword, :engagement_score, :opportunity_score,
             :duration_seconds, :fetched_at)
    ''', video)
    conn.commit()
    conn.close()


def get_video(video_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM videos WHERE video_id = ?', (video_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_videos(limit: int = 200) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM videos ORDER BY opportunity_score DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('SELECT COUNT(*) AS total FROM videos')
    total = c.fetchone()['total']

    c.execute('SELECT MAX(fetched_at) AS last_update FROM videos')
    row = c.fetchone()
    last_update = row['last_update'] if row else None

    c.execute('SELECT AVG(engagement_score) AS avg_eng FROM videos')
    row = c.fetchone()
    avg_engagement = round(row['avg_eng'] or 0, 2)

    c.execute('''
        SELECT keyword,
               COUNT(*)                 AS count,
               AVG(views)               AS avg_views,
               AVG(opportunity_score)   AS avg_opp
        FROM   videos
        GROUP  BY keyword
        ORDER  BY avg_opp DESC
    ''')
    keywords = [dict(r) for r in c.fetchall()]

    c.execute('SELECT * FROM fetch_log ORDER BY id DESC LIMIT 10')
    logs = [dict(r) for r in c.fetchall()]

    conn.close()
    return {
        "total_videos":   total,
        "last_update":    last_update,
        "avg_engagement": avg_engagement,
        "keywords":       keywords,
        "fetch_log":      logs,
    }


def log_fetch(videos_found: int, status: str, message: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT INTO fetch_log (fetched_at, videos_found, status, message) VALUES (?, ?, ?, ?)',
        (datetime.now().isoformat(), videos_found, status, message),
    )
    conn.commit()
    conn.close()


# ── Production pipeline tables ────────────────────────────────────────────────

TASK_TYPES = ["script", "audio", "transcription", "prompts", "thumbnails", "video", "posting"]


def init_production_tables():
    """Create channels, productions and production_tasks tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS channels (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            language_code TEXT NOT NULL,
            flag          TEXT DEFAULT "",
            description   TEXT DEFAULT "",
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS productions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id       INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            source_video_id  TEXT,
            source_url       TEXT NOT NULL,
            source_title     TEXT DEFAULT "",
            source_channel   TEXT DEFAULT "",
            source_language  TEXT DEFAULT "",
            source_thumbnail TEXT DEFAULT "",
            adapted_title    TEXT DEFAULT "",
            status           TEXT DEFAULT "active",
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS production_tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
            task_type     TEXT NOT NULL,
            status        TEXT DEFAULT "pending",
            result_text   TEXT DEFAULT "",
            notes         TEXT DEFAULT "",
            updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    # Migration: add thumbnails task to existing productions that don't have it
    for row in conn.execute("SELECT id FROM productions").fetchall():
        exists = conn.execute(
            "SELECT id FROM production_tasks WHERE production_id=? AND task_type='thumbnails'",
            (row[0],)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO production_tasks (production_id, task_type) VALUES (?, 'thumbnails')",
                (row[0],)
            )
    conn.commit()
    conn.close()


def fetched_today() -> bool:
    """Return True if a successful fetch was already logged today."""
    conn = sqlite3.connect(DB_PATH)
    today = datetime.now().strftime('%Y-%m-%d')
    row = conn.execute(
        "SELECT id FROM fetch_log WHERE fetched_at LIKE ? AND status='ok' LIMIT 1",
        (f"{today}%",)
    ).fetchone()
    conn.close()
    return row is not None


# ── Channel CRUD ──────────────────────────────────────────────────────────────

def create_channel(name: str, language_code: str, flag: str = "", description: str = "") -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT INTO channels (name, language_code, flag, description) VALUES (?, ?, ?, ?)',
        (name, language_code, flag, description),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def get_channels() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT ch.*,
               COUNT(DISTINCT p.id)                                          AS production_count,
               COUNT(CASE WHEN pt.status != "done" AND pt.status IS NOT NULL
                          THEN 1 END)                                        AS pending_tasks
        FROM   channels ch
        LEFT JOIN productions p  ON p.channel_id = ch.id AND p.status = "active"
        LEFT JOIN production_tasks pt ON pt.production_id = p.id
        GROUP  BY ch.id
        ORDER  BY ch.created_at DESC
    ''')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_channel(channel_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM channels WHERE id = ?', (channel_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_channel(channel_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM channels WHERE id = ?', (channel_id,))
    conn.commit()
    conn.close()


# ── Production CRUD ───────────────────────────────────────────────────────────

def create_production(channel_id: int, source_url: str, source_title: str = "",
                      source_channel: str = "", source_language: str = "",
                      source_thumbnail: str = "", adapted_title: str = "",
                      source_video_id: str | None = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO productions
            (channel_id, source_video_id, source_url, source_title,
             source_channel, source_language, source_thumbnail, adapted_title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (channel_id, source_video_id, source_url, source_title,
          source_channel, source_language, source_thumbnail, adapted_title))
    prod_id = c.lastrowid
    # Create all 6 tasks as pending
    for task_type in TASK_TYPES:
        c.execute(
            'INSERT INTO production_tasks (production_id, task_type) VALUES (?, ?)',
            (prod_id, task_type),
        )
    conn.commit()
    conn.close()
    return prod_id


def get_productions(channel_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT * FROM productions
        WHERE channel_id = ? AND status = "active"
        ORDER BY created_at DESC
    ''', (channel_id,))
    prods = [dict(r) for r in c.fetchall()]
    for p in prods:
        c.execute(
            'SELECT task_type, status, result_text, notes, updated_at FROM production_tasks WHERE production_id = ?',
            (p['id'],),
        )
        p['tasks'] = {r['task_type']: dict(r) for r in c.fetchall()}
    conn.close()
    return prods


def get_production(production_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM productions WHERE id = ?', (production_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    prod = dict(row)
    c.execute(
        'SELECT task_type, status, result_text, notes, updated_at FROM production_tasks WHERE production_id = ?',
        (production_id,),
    )
    prod['tasks'] = {r['task_type']: dict(r) for r in c.fetchall()}
    conn.close()
    return prod


def delete_production(production_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM productions WHERE id = ?', (production_id,))
    conn.commit()
    conn.close()


def update_production_title(production_id: int, adapted_title: str):
    """Update only the adapted_title of a production."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE productions SET adapted_title=? WHERE id=?', (adapted_title, production_id))
    conn.commit()
    conn.close()


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def upsert_task(production_id: int, task_type: str, status: str,
                result_text: str = "", notes: str = ""):
    """Update task — overwrites result_text and notes. Use for done/pending/error."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE production_tasks
        SET status = ?, result_text = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
        WHERE production_id = ? AND task_type = ?
    ''', (status, result_text, notes, production_id, task_type))
    conn.commit()
    conn.close()


def set_task_status(production_id: int, task_type: str, status: str, notes: str = ""):
    """Update only status (and optionally notes). Preserves result_text."""
    conn = sqlite3.connect(DB_PATH)
    if notes:
        conn.execute('''
            UPDATE production_tasks
            SET status = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE production_id = ? AND task_type = ?
        ''', (status, notes, production_id, task_type))
    else:
        conn.execute('''
            UPDATE production_tasks
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE production_id = ? AND task_type = ?
        ''', (status, production_id, task_type))
    conn.commit()
    conn.close()


def reset_stale_tasks(stale_minutes: int = 15):
    """Reset in_progress tasks older than N minutes back to pending/done on startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Find stale in_progress tasks
    rows = conn.execute('''
        SELECT id, production_id, task_type, result_text
        FROM production_tasks
        WHERE status = 'in_progress'
          AND updated_at < datetime('now', ? || ' minutes')
    ''', (f'-{stale_minutes}',)).fetchall()

    for row in rows:
        # If there's a previous result, restore to done; otherwise reset to pending
        new_status = 'done' if row['result_text'] else 'pending'
        conn.execute('''
            UPDATE production_tasks
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_status, row['id']))
        print(f"[Startup] Reset stale in_progress task {row['task_type']} "
              f"prod={row['production_id']} → {new_status}")

    conn.commit()
    conn.close()
    return len(rows)


def get_task(production_id: int, task_type: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        'SELECT * FROM production_tasks WHERE production_id = ? AND task_type = ?',
        (production_id, task_type),
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None
