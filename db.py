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
            stage           TEXT NOT NULL,          -- GROUP | KNOCKOUT (panel button mode)
            stage_detail    TEXT,                   -- raw API stage: GROUP_STAGE|LAST_32|...|FINAL
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

        CREATE TABLE IF NOT EXISTS daily_channels (
            guild_id    TEXT NOT NULL PRIMARY KEY,  -- one daily-post channel per server
            channel_id  TEXT NOT NULL,
            last_posted TEXT                        -- YYYY-MM-DD (UTC) of last auto-post
        );

        CREATE TABLE IF NOT EXISTS champion_picks (
            guild_id   TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            team       TEXT NOT NULL,               -- pre-tournament "who wins it all" pick
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )

    # Migration: add matches.stage_detail to DBs created before per-stage prizes existed.
    cols = {row["name"] for row in _conn.execute("PRAGMA table_info(matches)")}
    if "stage_detail" not in cols:
        _conn.execute("ALTER TABLE matches ADD COLUMN stage_detail TEXT")

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
                       (match_id, stage, stage_detail, home, away, kickoff_utc, status, result)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (m["match_id"], m["stage"], m.get("stage_detail"), m["home"], m["away"],
                     m["kickoff_utc"], m["status"], api_result),
                )
                if api_result is not None:
                    newly_settled.append(m["match_id"])
            else:
                # Preserve a manual override; otherwise let the API result win.
                keep_result = row["result"] if row["manual_override"] else api_result
                c.execute(
                    """UPDATE matches
                       SET stage=?, stage_detail=?, home=?, away=?, kickoff_utc=?, status=?, result=?
                       WHERE match_id=?""",
                    (m["stage"], m.get("stage_detail"), m["home"], m["away"], m["kickoff_utc"],
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


def matches_in_window(start_utc: str, end_utc: str) -> list[sqlite3.Row]:
    """Matches kicking off in [start_utc, end_utc), ordered by time. Bounds are ISO8601
    UTC strings in the SAME 'YYYY-MM-DDTHH:MM:SSZ' form the API stores, so a plain
    lexicographic compare is correct — and it's timezone-relative to 'now', not a
    calendar date, so players in any timezone get the same upcoming set."""
    return _c().execute(
        "SELECT * FROM matches WHERE kickoff_utc >= ? AND kickoff_utc < ? ORDER BY kickoff_utc",
        (start_utc, end_utc),
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


def _stage_clause(stages: list[str] | None) -> tuple[str, list]:
    """Optional 'AND m.stage_detail IN (...)' filter for per-stage prize leaderboards.
    None → no filter (overall board)."""
    if not stages:
        return "", []
    placeholders = ",".join("?" * len(stages))
    return f" AND m.stage_detail IN ({placeholders})", list(stages)


def leaderboard(guild_id: str, limit: int = 10,
                stages: list[str] | None = None) -> list[sqlite3.Row]:
    clause, sparams = _stage_clause(stages)
    return _c().execute(
        f"""SELECT b.user_id,
                   {_score_expr()} AS score,
                   SUM(CASE WHEN b.pick = m.result THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN m.result IS NOT NULL THEN 1 ELSE 0 END) AS settled
            FROM bets b JOIN matches m ON b.match_id = m.match_id
            WHERE b.guild_id=?{clause}
            GROUP BY b.user_id
            HAVING settled > 0
            ORDER BY score DESC, correct DESC
            LIMIT ?""",
        (guild_id, *sparams, limit),
    ).fetchall()


def user_standing(guild_id: str, user_id: str,
                  stages: list[str] | None = None) -> dict | None:
    """One user's score/correct/settled plus rank within the guild (optionally per-stage)."""
    clause, sparams = _stage_clause(stages)
    rows = _c().execute(
        f"""SELECT b.user_id,
                   {_score_expr()} AS score,
                   SUM(CASE WHEN b.pick = m.result THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN m.result IS NOT NULL THEN 1 ELSE 0 END) AS settled
            FROM bets b JOIN matches m ON b.match_id = m.match_id
            WHERE b.guild_id=?{clause}
            GROUP BY b.user_id
            HAVING settled > 0
            ORDER BY score DESC""",
        (guild_id, *sparams),
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


# ── Daily auto-post channels ────────────────────────────────────────────────

def set_daily_channel(guild_id: str, channel_id: str) -> None:
    """Register (or move) the channel where this guild's daily fixtures auto-post.
    Re-registering the same channel keeps last_posted, so it never double-posts today."""
    with _lock:
        c = _c()
        c.execute(
            """INSERT INTO daily_channels (guild_id, channel_id, last_posted)
               VALUES (?,?,NULL)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id""",
            (guild_id, channel_id),
        )
        c.commit()


def clear_daily_channel(guild_id: str) -> bool:
    """Stop daily auto-posts for a guild. Returns False if none was set."""
    with _lock:
        c = _c()
        cur = c.execute("DELETE FROM daily_channels WHERE guild_id=?", (guild_id,))
        c.commit()
        return cur.rowcount > 0


def all_daily_channels() -> list[sqlite3.Row]:
    return _c().execute("SELECT * FROM daily_channels").fetchall()


def set_daily_posted(guild_id: str, date_utc: str) -> None:
    """Mark today's auto-post done so the loop won't repeat it (idempotent across restarts)."""
    with _lock:
        c = _c()
        c.execute("UPDATE daily_channels SET last_posted=? WHERE guild_id=?",
                  (date_utc, guild_id))
        c.commit()


# ── Champion pick (pre-tournament "who wins it all" side-bet) ───────────────

def tournament_start() -> str | None:
    """Earliest kickoff across all matches — the lock deadline for champion picks."""
    row = _c().execute("SELECT MIN(kickoff_utc) AS k FROM matches").fetchone()
    return row["k"] if row else None


def participating_teams() -> list[str]:
    """Sorted distinct team names (excluding 'TBD'), for the champion-pick chooser."""
    rows = _c().execute(
        """SELECT home AS t FROM matches WHERE home <> 'TBD'
           UNION SELECT away FROM matches WHERE away <> 'TBD'
           ORDER BY t"""
    ).fetchall()
    return [r["t"] for r in rows]


def set_champion_pick(guild_id: str, user_id: str, team: str) -> None:
    """Upsert a player's champion pick. Re-picking overwrites until the lock deadline."""
    with _lock:
        c = _c()
        c.execute(
            """INSERT INTO champion_picks (guild_id, user_id, team, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(guild_id, user_id)
               DO UPDATE SET team=excluded.team, created_at=excluded.created_at""",
            (guild_id, user_id, team, _now_iso()),
        )
        c.commit()


def get_champion_pick(guild_id: str, user_id: str) -> str | None:
    row = _c().execute(
        "SELECT team FROM champion_picks WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    return row["team"] if row else None


def champion_team() -> str | None:
    """The tournament winner: the FINAL match's winning team once its result is set."""
    row = _c().execute(
        "SELECT home, away, result FROM matches WHERE stage_detail='FINAL'"
    ).fetchone()
    if not row or not row["result"]:
        return None
    if row["result"] == "HOME":
        return row["home"]
    if row["result"] == "AWAY":
        return row["away"]
    return None


def champion_winners(guild_id: str, team: str) -> list[str]:
    """User ids in this guild who picked `team` as champion."""
    rows = _c().execute(
        "SELECT user_id FROM champion_picks WHERE guild_id=? AND team=? ORDER BY created_at",
        (guild_id, team),
    ).fetchall()
    return [r["user_id"] for r in rows]
