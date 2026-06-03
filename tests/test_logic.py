"""Offline unit tests for the pure result/normalization logic. No network, no bot."""
import football_api as fa


def _group(status="FINISHED", h=None, a=None, stage="GROUP_STAGE"):
    return {
        "id": 1, "stage": stage, "status": status,
        "homeTeam": {"name": "Mexico"}, "awayTeam": {"name": "France"},
        "utcDate": "2026-06-11T18:00:00Z",
        "score": {"winner": None, "fullTime": {"home": h, "away": a}},
    }


def _ko(status="FINISHED", winner=None, stage="LAST_16"):
    return {
        "id": 2, "stage": stage, "status": status,
        "homeTeam": {"name": "Brazil"}, "awayTeam": {"name": "France"},
        "utcDate": "2026-07-01T18:00:00Z",
        "score": {"winner": winner, "fullTime": {"home": 1, "away": 1}},
    }


def test_classify_stage():
    assert fa.classify_stage("GROUP_STAGE") == "GROUP"
    assert fa.classify_stage("LAST_16") == "KNOCKOUT"
    assert fa.classify_stage("FINAL") == "KNOCKOUT"
    assert fa.classify_stage(None) == "KNOCKOUT"


def test_group_results():
    assert fa.derive_result(_group(h=2, a=1)) == "HOME"
    assert fa.derive_result(_group(h=0, a=3)) == "AWAY"
    assert fa.derive_result(_group(h=1, a=1)) == "DRAW"


def test_not_finished_is_none():
    assert fa.derive_result(_group(status="SCHEDULED", h=None, a=None)) is None
    assert fa.derive_result(_group(status="IN_PLAY", h=1, a=0)) is None


def test_knockout_uses_winner_never_draw():
    assert fa.derive_result(_ko(winner="HOME_TEAM")) == "HOME"
    assert fa.derive_result(_ko(winner="AWAY_TEAM")) == "AWAY"
    # No winner reported on a knockout → unsettled (left for manual fix), never DRAW.
    assert fa.derive_result(_ko(winner=None)) is None


def test_normalize_match():
    n = fa.normalize_match(_group(h=2, a=1))
    assert n == {
        "match_id": 1, "stage": "GROUP", "home": "Mexico", "away": "France",
        "kickoff_utc": "2026-06-11T18:00:00Z", "status": "FINISHED", "result": "HOME",
    }
