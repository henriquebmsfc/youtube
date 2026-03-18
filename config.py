# ================================================================
# YouTube Analyzer — Configurações
# ================================================================
# Em produção (Railway/Render/etc.) configure as variáveis de
# ambiente no painel do servidor.
# Localmente crie um arquivo .env com as chaves (ver .env.example).
# ================================================================

import os

# ── APIs ──────────────────────────────────────────────────────────
YOUTUBE_API_KEY   = os.getenv("YOUTUBE_API_KEY",   "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GENAIPRO_API_KEY  = os.getenv("GENAIPRO_API_KEY",  "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",    "")

# ── Modelo Claude ─────────────────────────────────────────────────
# Padrão: claude-sonnet-4-5 (64k tokens de saída)
# Pode ser sobrescrito via variável de ambiente no Railway
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# ── Voz padrão TTS ────────────────────────────────────────────────
DEFAULT_VOICE_ID  = os.getenv("DEFAULT_VOICE_ID", "douDhHvfoViWmZth0cUX")   # Peter

# ── Banco de dados ────────────────────────────────────────────────
# Em produção aponte para um volume persistente, ex: /data
# Railway: configure DB_DIR=/data  (com volume montado em /data)
DB_DIR = os.getenv("DB_DIR", os.path.dirname(os.path.abspath(__file__)))

# ── Monitoramento YouTube ─────────────────────────────────────────
KEYWORDS = [
    "medieval history documentary",
    "middle ages documentary",
    "medieval civilization history",
    "medieval europe history",
    "medieval documentary",
]
DAYS_BACK              = int(os.getenv("DAYS_BACK", "7"))
MAX_RESULTS_PER_KEYWORD = int(os.getenv("MAX_RESULTS", "50"))

# ── Scheduler ────────────────────────────────────────────────────
SCHEDULE_HOUR   = int(os.getenv("SCHEDULE_HOUR",   "8"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))

# ── Servidor ─────────────────────────────────────────────────────
# Railway e Render injetam $PORT automaticamente
PORT = int(os.getenv("PORT", "5000"))
