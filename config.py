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
# Transient upstream blips (connection resets, timeouts, 429/5xx) are retried
# in-cycle so a single hiccup never aborts a settlement or alerts the owner.
FOOTBALL_RETRY_ATTEMPTS = int(os.getenv("FOOTBALL_RETRY_ATTEMPTS", "3"))
FOOTBALL_RETRY_BACKOFF_SEC = float(os.getenv("FOOTBALL_RETRY_BACKOFF_SEC", "1"))

# ── Scoring ────────────────────────────────────────────────────────────────
POINTS_GROUP = int(os.getenv("POINTS_GROUP", "1"))
POINTS_KNOCKOUT = int(os.getenv("POINTS_KNOCKOUT", "1"))

# ── Settlement loop ────────────────────────────────────────────────────────
SETTLE_INTERVAL_SEC = int(os.getenv("SETTLE_INTERVAL_SEC", "900"))
# Result broadcasts go to each server's channel registered via /setdailychannel
# (same channel as the daily panels) — there is no single global result channel.

# ── Daily auto-post ────────────────────────────────────────────────────────
# Hour (UTC, 0–23) at which the bot auto-posts each day's fixtures to channels
# registered via /setdailychannel. "Today" is the UTC calendar date.
DAILY_POST_HOUR_UTC = int(os.getenv("DAILY_POST_HOUR_UTC", "9"))
# Each daily post covers matches kicking off within this many hours, not just the
# current UTC calendar date. MUST exceed 24 so a match kicking off in the early UTC
# hours (e.g. a North-American evening game at 02:00 UTC) is already posted by the
# previous day's run instead of slipping through. Min lead before kickoff = this − 24h.
PANEL_LOOKAHEAD_HOURS = int(os.getenv("PANEL_LOOKAHEAD_HOURS", "36"))

# ── Feishu export (optional) ───────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()

# ── Paths ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", os.path.join("data", "worldcup.db"))
