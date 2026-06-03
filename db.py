"""SQLite data layer. Replaces the template's JSON storage because at 3000 DAU the
"rewrite the whole file on every save" model corrupts/loses data under concurrent
writes. SQLite gives atomic upserts + a millisecond leaderboard query.

Design choices baked in here:
  * Scores are NOT incremented — they're DERIVED by joining bets to match results.
    Re-running settlement or restarting can never double-count.
  * A result set by the admin (manual_override=1) is authoritative: API sync will
    not overwrite it. The operator has the final say on a result.
  * Bets are scoped by guild_id, so the same bot in many servers keeps independent
    leaderboards and vote distributions.

Functions are synchronous (sqlite3 is fast for these tiny ops). The bot calls them
through asyncio.to_thread so a heavier query never blocks the Discord event loop.
WAL mode lets reads run concurrently with writes.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()  # serialize writes from the executor threads


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    """Create tables and tune the connection. Idempotent."""
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA foreign_keys=ON")

    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS matches (
            match_id        INTEGER PRIMARY KEY,
            stage           TEXT NOT NULL,          -- GROUP | KNOCKOUT
            home            TEXT NOT NULL,
            away            TEXT NOT NULL,
            kickoff_utc     TEXT,                   -- ISO; lock boundary
            status          TEXT,                   -- SCHEDULED | FINISHED | ...
            result          TEXT,                   -- HOME | DRAW | AWAY | NULL
            manual_override INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bets (
            guild_id   TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            match_id   INTEGER NOT NULL,
            pick       TEXT NOT NULL,               -- HOME | DRAW | AWAY
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, match_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bets_match ON bets(guild_id, match_id);

        CREATE TABLE IF NOT EXISTS panels (
            guild_id        TEXT NOT NULL,
            channel_id      TEXT NOT NULL,
            message_id      TEXT NOT NULL PRIMARY KEY,
            panel_date      TEXT NOT NULL,          -- YYYY-MM-DD (UTC) of matches shown
            match_ids       TEXT NOT NULL,          -- CSV of match_ids on this panel
            locked_rendered INTEGER NOT NULL DEFAULT 0  -- # locked matches at last render
        );
        """
    )
    _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init_db() must be called first")
    return _conn


# ── Matches ────────────────────────────────────────────────────────────────

def upsert_matches(matches: list[dict]) -> list[int]:
    """Insert/update fixtures from the API. Returns match_ids whose result just
    transitioned from unset → set (so the caller can broadcast a settlement).

    A manually overridden result is never clobbered by the API.
    """
    newly_settled: list[int] = []
    with _lock:
        c = _c()
        for m in matches:
            row = c.execute(
                "SELECT result, manual_override FROM matches WHERE match_id=?",
                (m["match_id"],),
            ).fetchone()

            api_result = m.get("result")
            if row is None:
                c.execute(
                    """INSERT INTO matches
                       (match_id, stage, home, away, kickoff_utc, status, result)
                       VALUES (?,?,?,?,?,?,?)""",
                    (m["match_id"], m["stage"], m["home"], m["away"],
                     m["kickoff_utc"], m["status"], api_result),
                )
                if api_result is not None:
                    newly_settled.append(m["match_id"])
            else:
                # Preserve a manual override; otherwise let the API result win.
                keep_result = row["result"] if row["manual_override"] else api_result
                c.execute(
                    """UPDATE matches
                       SET stage=?, home=?, away=?, kickoff_utc=?, status=?, result=?
                       WHERE match_id=?""",
                    (m["stage"], m["home"], m["away"], m["kickoff_utc"],
                     m["status"], keep_result, m["match_id"]),
                )
                if row["result"] is None and keep_result is not None:
                    newly_settled.append(m["match_id"])
        c.commit()
    return newly_settled


def set_result_manual(match_id: int, result: str | None) -> bool:
    """Admin override. result in {HOME,DRAW,AWAY} or None to clear. Returns False if
    the match doesn't exist."""
    with _lock:
        c = _c()
        cur = c.execute(
            "UPDATE matches SET result=?, manual_override=? WHERE match_id=?",
            (result, 1 if result is not None else 0, match_id),
        )
        c.commit()
        return cur.rowcount > 0


def get_match(match_id: int) -> sqlite3.Row | None:
    return _c().execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()


def matches_for_date(date_utc: str) -> list[sqlite3.Row]:
    """All matches kicking off on the given UTC date (YYYY-MM-DD), ordered by time."""
    return _c().execute(
        "SELECT * FROM matches WHERE substr(kickoff_utc,1,10)=? ORDER BY kickoff_utc",
        (date_utc,),
    ).fetchall()


def matches_by_ids(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return _c().execute(
        f"SELECT * FROM matches WHERE match_id IN ({q}) ORDER BY kickoff_utc", ids
    ).fetchall()


# ── Bets ───────────────────────────────────────────────────────────────────

def place_bet(guild_id: str, user_id: str, match_id: int, pick: str) -> None:
    """Upsert a pick. Re-betting overwrites the previous pick for this match."""
    with _lock:
        c = _c()
        c.execute(
            """INSERT INTO bets (guild_id, user_id, match_id, pick, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, user_id, match_id)
               DO UPDATE SET pick=excluded.pick, created_at=excluded.created_at""",
            (guild_id, user_id, match_id, pick, _now_iso()),
        )
        c.commit()


def bet_counts(guild_id: str, match_id: int) -> dict[str, int]:
    """Per-guild vote distribution for one match: {'HOME':n,'DRAW':n,'AWAY':n}."""
    rows = _c().execute(
        "SELECT pick, COUNT(*) n FROM bets WHERE guild_id=? AND match_id=? GROUP BY pick",
        (guild_id, match_id),
    ).fetchall()
    out = {"HOME": 0, "DRAW": 0, "AWAY": 0}
    for r in rows:
        out[r["pick"]] = r["n"]
    return out


def user_bets(guild_id: str, user_id: str) -> list[sqlite3.Row]:
    """A user's picks joined with match info + correctness, newest matches last."""
    return _c().execute(
        """SELECT m.match_id, m.home, m.away, m.kickoff_utc, m.stage,
                  m.result, b.pick
           FROM bets b JOIN matches m ON b.match_id = m.match_id
           WHERE b.guild_id=? AND b.user_id=?
           ORDER BY m.kickoff_utc""",
        (guild_id, user_id),
    ).fetchall()


# ── Leaderboard (derived, idempotent) ──────────────────────────────────────

def _score_expr() -> str:
    """Points per correct pick, weighted by stage (config-driven)."""
    return ("SUM(CASE WHEN b.pick = m.result THEN "
            f"(CASE WHEN m.stage='KNOCKOUT' THEN {config.POINTS_KNOCKOUT} "
            f"ELSE {config.POINTS_GROUP} END) ELSE 0 END)")


def leaderboard(guild_id: str, limit: int = 10) -> list[sqlite3.Row]:
    return _c().execute(
        f"""SELECT b.user_id,
                   {_score_expr()} AS score,
                   SUM(CASE WHEN b.pick = m.result THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN m.result IS NOT NULL THEN 1 ELSE 0 END) AS settled
            FROM bets b JOIN matches m ON b.match_id = m.match_id
            WHERE b.guild_id=?
            GROUP BY b.user_id
            HAVING settled > 0
            ORDER BY score DESC, correct DESC
            LIMIT ?""",
        (guild_id, limit),
    ).fetchall()


def user_standing(guild_id: str, user_id: str) -> dict | None:
    """One user's score/correct/settled plus dense rank within the guild."""
    rows = _c().execute(
        f"""SELECT b.user_id,
                   {_score_expr()} AS score,
                   SUM(CASE WHEN b.pick = m.result THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN m.result IS NOT NULL THEN 1 ELSE 0 END) AS settled
            FROM bets b JOIN matches m ON b.match_id = m.match_id
            WHERE b.guild_id=?
            GROUP BY b.user_id
            HAVING settled > 0
            ORDER BY score DESC""",
        (guild_id,),
    ).fetchall()
    rank = 0
    for i, r in enumerate(rows, start=1):
        if r["user_id"] == user_id:
            rank = i
            return {"rank": rank, "total": len(rows), "score": r["score"],
                    "correct": r["correct"], "settled": r["settled"]}
    return None


# ── Panels (for the lock-reveal loop) ──────────────────────────────────────

def record_panel(guild_id: str, channel_id: str, message_id: str,
                 panel_date: str, match_ids: list[int]) -> None:
    csv_ids = ",".join(str(i) for i in match_ids)
    with _lock:
        c = _c()
        c.execute(
            """INSERT OR REPLACE INTO panels
               (guild_id, channel_id, message_id, panel_date, match_ids, locked_rendered)
               VALUES (?,?,?,?,?,0)""",
            (guild_id, channel_id, message_id, panel_date, csv_ids),
        )
        c.commit()


def all_panels() -> list[sqlite3.Row]:
    return _c().execute("SELECT * FROM panels").fetchall()


def set_panel_locked_rendered(message_id: str, count: int) -> None:
    with _lock:
        c = _c()
        c.execute("UPDATE panels SET locked_rendered=? WHERE message_id=?",
                  (count, message_id))
        c.commit()
