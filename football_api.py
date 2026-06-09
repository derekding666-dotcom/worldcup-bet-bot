"""DATA SOURCE — football-data.org v4 client for the World Cup.

Split into two layers (mirrors ops-bot-template's "pure logic vs I/O" habit):
  * Pure functions (classify_stage / derive_result / normalize_match): no network,
    unit-tested offline in tests/.
  * Async I/O (fetch_matches): the only part that touches the network.

Run `python football_api.py` as a standalone coverage check: it pulls the 2026
World Cup fixtures with your FOOTBALL_API_KEY and prints what's available. This is
the FIRST verification step — if the data source isn't usable, nothing else matters.

API ref: https://docs.football-data.org/general/v4/resources.html
World Cup: competition code "WC", id 2000.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

import config

logger = logging.getLogger(__name__)

# Outcome constants — our internal vocabulary, stored in the DB and compared to picks.
HOME, DRAW, AWAY = "HOME", "DRAW", "AWAY"

# Match status from the API that means "result is final".
FINISHED_STATUSES = {"FINISHED", "AWARDED"}


# ── Pure logic (offline-testable) ──────────────────────────────────────────

def classify_stage(api_stage: str | None) -> str:
    """Map the API's fine-grained stage to our two panel modes.

    GROUP_STAGE → "GROUP" (3-way: home/draw/away).
    Everything else (LAST_16, QUARTER_FINALS, ... FINAL) → "KNOCKOUT" (2-way:
    which team advances — a draw is impossible after ET/penalties).
    """
    return "GROUP" if (api_stage or "").upper() == "GROUP_STAGE" else "KNOCKOUT"


def derive_result(match: dict) -> str | None:
    """PURE: turn a raw API match into HOME / DRAW / AWAY, or None if not final.

    Group stage: from the full-time score (a draw is a valid outcome).
    Knockout:    from score.winner (who advanced, including ET/penalties) — never
                 a draw.
    """
    if match.get("status") not in FINISHED_STATUSES:
        return None

    stage = classify_stage(match.get("stage"))
    score = match.get("score") or {}

    if stage == "KNOCKOUT":
        winner = score.get("winner")
        if winner == "HOME_TEAM":
            return HOME
        if winner == "AWAY_TEAM":
            return AWAY
        return None  # shouldn't happen in knockout; leave unsettled for manual fix

    full = score.get("fullTime") or {}
    h, a = full.get("home"), full.get("away")
    if h is None or a is None:
        return None
    if h > a:
        return HOME
    if h < a:
        return AWAY
    return DRAW


def normalize_match(match: dict) -> dict:
    """PURE: reduce a raw API match to the fields we persist.

    `stage` is the coarse GROUP/KNOCKOUT used for panel button logic; `stage_detail`
    keeps the raw API stage (GROUP_STAGE/LAST_32/LAST_16/QUARTER_FINALS/SEMI_FINALS/
    THIRD_PLACE/FINAL) so we can score per-stage prizes.
    """
    return {
        "match_id": match["id"],
        "stage": classify_stage(match.get("stage")),
        "stage_detail": (match.get("stage") or "").upper(),
        "home": (match.get("homeTeam") or {}).get("name") or "TBD",
        "away": (match.get("awayTeam") or {}).get("name") or "TBD",
        "kickoff_utc": match.get("utcDate"),
        "status": match.get("status"),
        "result": derive_result(match),
    }


# ── Network I/O ────────────────────────────────────────────────────────────

class FootballAPIError(RuntimeError):
    """Raised on a non-OK response so callers (settlement loop) can log + alert
    instead of silently skipping a settlement."""


# Transient conditions worth retrying. Connection-level aiohttp errors
# (ClientConnectorError, ServerDisconnectedError, …) all subclass
# ClientConnectionError; timeouts surface as asyncio.TimeoutError.
_RETRYABLE_EXC = (aiohttp.ClientConnectionError, asyncio.TimeoutError)
# HTTP statuses that are the upstream's problem, not ours — safe to retry.
# Everything else (403 auth, 404, …) is permanent and fails fast.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _get_matches_json(session: aiohttp.ClientSession, url: str,
                            headers: dict) -> dict:
    """GET the matches endpoint with bounded retry + exponential backoff on
    transient blips. Returns parsed JSON. Raises FootballAPIError on a permanent
    error (immediately) or after all attempts are exhausted — never swallows."""
    attempts = config.FOOTBALL_RETRY_ATTEMPTS
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                body = await resp.text()
                if resp.status == 200:
                    return json.loads(body)
                err = FootballAPIError(f"HTTP {resp.status}: {body[:300]}")
                if resp.status not in _RETRYABLE_STATUS:
                    raise err  # permanent (e.g. 403 auth) — don't waste retries
                last_err = err
        except _RETRYABLE_EXC as e:
            last_err = e

        if attempt < attempts:
            backoff = config.FOOTBALL_RETRY_BACKOFF_SEC * (2 ** (attempt - 1))
            logger.warning(
                f"football-data.org fetch attempt {attempt}/{attempts} failed "
                f"({last_err!r}); retrying in {backoff:.0f}s")
            await asyncio.sleep(backoff)

    raise FootballAPIError(
        f"all {attempts} attempts failed; last error: {last_err!r}")


async def fetch_matches(session: aiohttp.ClientSession | None = None) -> list[dict]:
    """Fetch all World Cup matches and return normalized dicts. Side-effect free
    beyond the GET. Transient upstream blips (connection resets, timeouts, 429/5xx)
    are retried with backoff; raises FootballAPIError only after all attempts fail,
    or immediately on a permanent error like 403 — never swallows."""
    if not config.FOOTBALL_API_KEY:
        raise FootballAPIError("FOOTBALL_API_KEY is not set")

    url = f"{config.FOOTBALL_BASE_URL}/competitions/{config.WC_COMPETITION}/matches"
    headers = {"X-Auth-Token": config.FOOTBALL_API_KEY}

    own_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        data = await _get_matches_json(session, url, headers)
    finally:
        if own_session:
            await session.close()

    return [normalize_match(m) for m in data.get("matches", [])]


# ── Standalone coverage check: `python football_api.py` ────────────────────

async def _coverage_check() -> None:
    print("Pulling 2026 World Cup matches from football-data.org ...\n")
    try:
        matches = await fetch_matches()
    except FootballAPIError as e:
        print(f"❌ FAILED: {e}")
        print("\nIf this is an auth/403 error, the free tier may not cover the World")
        print("Cup — fall back to API-Football (api-sports.io). See the plan.")
        return

    if not matches:
        print("⚠️  API responded but returned 0 matches. The 2026 fixtures may not be")
        print("    populated on this tier yet. Verify before relying on it.")
        return

    finished = [m for m in matches if m["result"] is not None]
    groups = [m for m in matches if m["stage"] == "GROUP"]
    kos = [m for m in matches if m["stage"] == "KNOCKOUT"]
    dates = sorted(m["kickoff_utc"] for m in matches if m["kickoff_utc"])

    print(f"✅ {len(matches)} matches  |  group: {len(groups)}  knockout: {len(kos)}"
          f"  finished: {len(finished)}")
    if dates:
        print(f"   date range: {dates[0]}  →  {dates[-1]}")
    print("\n   sample:")
    for m in matches[:5]:
        print(f"   #{m['match_id']}  [{m['stage']}]  {m['home']} vs {m['away']}"
              f"  @ {m['kickoff_utc']}  status={m['status']}  result={m['result']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_coverage_check())
