"""DB-layer tests against a fresh temp SQLite file per test. Verifies the two
correctness-critical behaviors: re-betting overwrites, scoring is derived (idempotent),
and a manual override is never clobbered by an API sync."""
import importlib

import pytest

import config


@pytest.fixture
def db(tmp_path):
    config.DB_PATH = str(tmp_path / "t.db")
    import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    return db_module


def _match(mid, stage="GROUP", result=None, status="SCHEDULED",
           ko="2026-06-11T18:00:00Z", stage_detail=None, home="A", away="B"):
    return {"match_id": mid, "stage": stage, "stage_detail": stage_detail,
            "home": home, "away": away,
            "kickoff_utc": ko, "status": status, "result": result}


def test_rebet_overwrites(db):
    db.upsert_matches([_match(1)])
    db.place_bet("g1", "u1", 1, "HOME")
    db.place_bet("g1", "u1", 1, "DRAW")  # change pick
    counts = db.bet_counts("g1", 1)
    assert counts == {"HOME": 0, "DRAW": 1, "AWAY": 0}


def test_counts_are_per_guild(db):
    db.upsert_matches([_match(1)])
    db.place_bet("g1", "u1", 1, "HOME")
    db.place_bet("g2", "u2", 1, "AWAY")
    assert db.bet_counts("g1", 1) == {"HOME": 1, "DRAW": 0, "AWAY": 0}
    assert db.bet_counts("g2", 1) == {"HOME": 0, "DRAW": 0, "AWAY": 1}


def test_leaderboard_is_derived_and_idempotent(db):
    db.upsert_matches([_match(1), _match(2)])
    db.place_bet("g1", "u1", 1, "HOME")
    db.place_bet("g1", "u1", 2, "DRAW")
    db.place_bet("g1", "u2", 1, "AWAY")
    # settle: match1 HOME (u1 right, u2 wrong), match2 DRAW (u1 right)
    db.upsert_matches([_match(1, result="HOME", status="FINISHED"),
                       _match(2, result="DRAW", status="FINISHED")])

    lb = db.leaderboard("g1", 10)
    by_user = {r["user_id"]: r for r in lb}
    assert by_user["u1"]["score"] == 2 and by_user["u1"]["correct"] == 2
    assert by_user["u2"]["score"] == 0 and by_user["u2"]["correct"] == 0

    # Re-running settlement must NOT double-count (scores are derived, not incremented).
    db.upsert_matches([_match(1, result="HOME", status="FINISHED"),
                       _match(2, result="DRAW", status="FINISHED")])
    lb2 = {r["user_id"]: r for r in db.leaderboard("g1", 10)}
    assert lb2["u1"]["score"] == 2


def test_manual_override_survives_api_sync(db):
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    db.set_result_manual(1, "AWAY")             # admin corrects it
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])  # API re-syncs old value
    assert db.get_match(1)["result"] == "AWAY"  # correction preserved


def test_settled_result_is_sticky_on_conflict(db):
    # A settled result must not be silently flipped by a later, contradicting API read
    # (that would silently rewrite the leaderboard). Keep ours, surface a conflict.
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    res = db.upsert_matches([_match(1, result="DRAW", status="FINISHED")])  # API now disagrees
    assert db.get_match(1)["result"] == "HOME"
    assert res.result_conflicts == [(1, "HOME", "DRAW")]
    assert res.newly_settled == []


def test_settled_result_survives_api_dropout(db):
    # API momentarily reports no result for an already-settled match → keep ours, no conflict.
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    res = db.upsert_matches([_match(1, result=None, status="IN_PLAY")])
    assert db.get_match(1)["result"] == "HOME"
    assert res.result_conflicts == []


def test_first_settlement_reports_newly_settled(db):
    db.upsert_matches([_match(1)])              # scheduled, no result yet
    res = db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    assert res.newly_settled == [1]
    assert res.result_conflicts == []


def test_user_standing_rank(db):
    db.upsert_matches([_match(1)])
    db.place_bet("g1", "winner", 1, "HOME")
    db.place_bet("g1", "loser", 1, "AWAY")
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    s = db.user_standing("g1", "winner")
    assert s["rank"] == 1 and s["score"] == 1
    assert db.user_standing("g1", "nobody") is None


def test_stage_filtered_leaderboard(db):
    db.upsert_matches([
        _match(1, stage="GROUP", stage_detail="GROUP_STAGE", result="HOME", status="FINISHED"),
        _match(2, stage="KNOCKOUT", stage_detail="LAST_16", result="AWAY", status="FINISHED"),
    ])
    db.place_bet("g1", "u1", 1, "HOME")   # correct (group)
    db.place_bet("g1", "u1", 2, "AWAY")   # correct (R16)
    db.place_bet("g1", "u2", 1, "HOME")   # correct (group)
    db.place_bet("g1", "u2", 2, "HOME")   # wrong (R16)

    overall = {r["user_id"]: r["score"] for r in db.leaderboard("g1", 10)}
    assert overall == {"u1": 2, "u2": 1}

    group = {r["user_id"]: r["score"] for r in db.leaderboard("g1", 10, ["GROUP_STAGE"])}
    assert group == {"u1": 1, "u2": 1}

    r16 = {r["user_id"]: r["score"] for r in db.leaderboard("g1", 10, ["LAST_16"])}
    assert r16 == {"u1": 1, "u2": 0}  # u2 settled in R16 but wrong → score 0, still listed


def test_participating_teams_excludes_tbd(db):
    db.upsert_matches([
        _match(1, home="Brazil", away="France"),
        _match(2, stage="KNOCKOUT", stage_detail="FINAL", home="TBD", away="TBD"),
    ])
    assert db.participating_teams() == ["Brazil", "France"]


def test_tournament_start_is_earliest(db):
    db.upsert_matches([
        _match(1, ko="2026-06-12T02:00:00Z"),
        _match(2, ko="2026-06-11T19:00:00Z"),
    ])
    assert db.tournament_start() == "2026-06-11T19:00:00Z"


def test_champion_pick_and_winners(db):
    db.set_champion_pick("g1", "u1", "Brazil")
    db.set_champion_pick("g1", "u1", "France")  # change pick (overwrite)
    assert db.get_champion_pick("g1", "u1") == "France"
    db.set_champion_pick("g1", "u2", "France")
    db.set_champion_pick("g2", "u3", "France")  # other guild

    assert db.champion_winners("g1", "France") == ["u1", "u2"]  # per-guild, by created_at
    assert db.champion_winners("g2", "France") == ["u3"]
    assert db.champion_winners("g1", "Brazil") == []


def test_champion_team_from_final(db):
    db.upsert_matches([_match(99, stage="KNOCKOUT", stage_detail="FINAL",
                              home="France", away="Brazil")])
    assert db.champion_team() is None  # not settled yet
    db.upsert_matches([_match(99, stage="KNOCKOUT", stage_detail="FINAL",
                              home="France", away="Brazil", result="HOME", status="FINISHED")])
    assert db.champion_team() == "France"


def test_matches_in_window(db):
    db.upsert_matches([
        _match(1, ko="2026-06-12T02:00:00Z"),
        _match(2, ko="2026-06-12T20:00:00Z"),
        _match(3, ko="2026-06-15T02:00:00Z"),  # outside the window
    ])
    rows = db.matches_in_window("2026-06-12T00:00:00Z", "2026-06-13T00:00:00Z")
    assert [r["match_id"] for r in rows] == [1, 2]  # ordered by kickoff, #3 excluded


def test_daily_channel_register_move_clear(db):
    # register, then move to another channel (one row per guild, last_posted preserved)
    db.set_daily_channel("g1", "chan_a")
    db.set_daily_posted("g1", "2026-06-12")
    db.set_daily_channel("g1", "chan_b")  # move channel
    rows = {r["guild_id"]: r for r in db.all_daily_channels()}
    assert rows["g1"]["channel_id"] == "chan_b"
    assert rows["g1"]["last_posted"] == "2026-06-12"  # not reset → no double-post today

    # per-guild isolation
    db.set_daily_channel("g2", "chan_c")
    assert len(db.all_daily_channels()) == 2

    # clear
    assert db.clear_daily_channel("g1") is True
    assert db.clear_daily_channel("g1") is False  # already gone
    assert {r["guild_id"] for r in db.all_daily_channels()} == {"g2"}


def test_posted_match_ids_dedup_per_channel(db):
    db.upsert_matches([_match(1), _match(2), _match(3)])
    db.record_panel("g1", "daily", "msg1", "2026-06-11", [1])
    db.record_panel("g1", "other", "msg2", "2026-06-12", [2])  # same guild, OTHER channel
    db.record_panel("g2", "daily", "msg3", "2026-06-11", [3])  # different guild
    # Scoped to (guild, channel): a post to another channel must not count for the
    # daily channel — otherwise a test/mod-channel post suppresses the real one.
    assert db.posted_match_ids("g1", "daily") == {1}
    assert db.posted_match_ids("g1", "other") == {2}
    assert db.posted_match_ids("g2", "daily") == {3}
    assert db.posted_match_ids("g1", "nope") == set()


def test_temp_roles_add_due_remove(db):
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    db.add_temp_role("g1", "u1", "r1", past)     # already expired
    db.add_temp_role("g1", "u2", "r1", future)   # not yet
    assert [r["user_id"] for r in db.due_temp_roles(now)] == ["u1"]

    # Re-granting the same (guild,user,role) resets the clock — latest expiry wins.
    db.add_temp_role("g1", "u1", "r1", future)
    assert db.due_temp_roles(now) == []

    # Removal forgets just that grant.
    db.remove_temp_role("g1", "u2", "r1")
    rows = db._conn.execute("SELECT user_id FROM temp_roles ORDER BY user_id").fetchall()
    assert [r["user_id"] for r in rows] == ["u1"]
