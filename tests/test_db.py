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
           ko="2026-06-11T18:00:00Z"):
    return {"match_id": mid, "stage": stage, "home": "A", "away": "B",
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


def test_user_standing_rank(db):
    db.upsert_matches([_match(1)])
    db.place_bet("g1", "winner", 1, "HOME")
    db.place_bet("g1", "loser", 1, "AWAY")
    db.upsert_matches([_match(1, result="HOME", status="FINISHED")])
    s = db.user_standing("g1", "winner")
    assert s["rank"] == 1 and s["score"] == 1
    assert db.user_standing("g1", "nobody") is None
