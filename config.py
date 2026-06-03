"""Environment configuration. All tunables live here, loaded from .env.

Mirrors ops-bot-template/config.py: os.getenv + dotenv, fail-soft defaults so the
module imports even when nothing is configured (tests/offline tooling still run).
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ── Discord ────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Admin user that always has rights and receives error DMs.
OWNER_ID = os.getenv("OWNER_ID", "").strip()

# Role names (per guild) that grant admin command access.
ADMIN_ROLES = [r.strip() for r in os.getenv("ADMIN_ROLES", "Admin").split(",") if r.strip()]

# ── Football data source ───────────────────────────────────────────────────
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()
# World Cup competition on football-data.org: code "WC", id 2000.
WC_COMPETITION = os.getenv("WC_COMPETITION", "2000").strip()
FOOTBALL_BASE_URL = "https://api.football-data.org/v4"

# ── Scoring ────────────────────────────────────────────────────────────────
POINTS_GROUP = int(os.getenv("POINTS_GROUP", "1"))
POINTS_KNOCKOUT = int(os.getenv("POINTS_KNOCKOUT", "1"))

# ── Settlement loop ────────────────────────────────────────────────────────
SETTLE_INTERVAL_SEC = int(os.getenv("SETTLE_INTERVAL_SEC", "900"))
RESULT_CHANNEL_ID = os.getenv("RESULT_CHANNEL_ID", "").strip()

# ── Daily auto-post ────────────────────────────────────────────────────────
# Hour (UTC, 0–23) at which the bot auto-posts each day's fixtures to channels
# registered via /setdailychannel. "Today" is the UTC calendar date.
DAILY_POST_HOUR_UTC = int(os.getenv("DAILY_POST_HOUR_UTC", "9"))

# ── Feishu export (optional) ───────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()

# ── Paths ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", os.path.join("data", "worldcup.db"))
