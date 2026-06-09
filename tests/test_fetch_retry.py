"""Offline tests for fetch_matches' retry/backoff. No real network: a fake
aiohttp session replays a scripted sequence of connection errors / HTTP
responses so we can assert when fetch retries, when it fails fast, and when it
gives up. Backoff is zeroed so the tests run instantly."""
import asyncio
import json

import aiohttp
import pytest

import config
import football_api as fa


# ── Fake aiohttp session ───────────────────────────────────────────────────
class _Ctx:
    """Async context manager standing in for `session.get(...)`. An Exception
    outcome is raised on entry (as aiohttp does for connection errors); a
    response outcome is returned."""
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *a):
        return False


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body


class _FakeSession:
    """Replays `outcomes` one per get() call, counting calls."""
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return _Ctx(self._outcomes.pop(0))


_OK_BODY = json.dumps({"matches": [{
    "id": 1, "stage": "GROUP_STAGE", "status": "FINISHED",
    "homeTeam": {"name": "Mexico"}, "awayTeam": {"name": "France"},
    "utcDate": "2026-06-11T18:00:00Z",
    "score": {"winner": None, "fullTime": {"home": 2, "away": 1}},
}]})


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    # Don't actually sleep between retries, and ensure a known attempt budget.
    monkeypatch.setattr(config, "FOOTBALL_RETRY_BACKOFF_SEC", 0)
    monkeypatch.setattr(config, "FOOTBALL_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "FOOTBALL_API_KEY", "test-key")


def test_retries_then_succeeds():
    # Two transient connection blips, then a good response.
    blip = aiohttp.ClientConnectionError("connection reset")
    sess = _FakeSession([blip, blip, _Resp(200, _OK_BODY)])
    matches = asyncio.run(fa.fetch_matches(session=sess))
    assert sess.calls == 3
    assert matches[0]["result"] == "HOME"


def test_gives_up_after_all_attempts():
    blip = aiohttp.ClientConnectionError("down")
    sess = _FakeSession([blip, blip, blip])
    with pytest.raises(fa.FootballAPIError):
        asyncio.run(fa.fetch_matches(session=sess))
    assert sess.calls == 3  # exactly the attempt budget, no more


def test_permanent_error_fails_fast():
    # 403 is an auth problem — retrying is pointless, so stop after one call.
    sess = _FakeSession([_Resp(403, "forbidden"),
                         _Resp(200, _OK_BODY)])  # would succeed if it retried
    with pytest.raises(fa.FootballAPIError):
        asyncio.run(fa.fetch_matches(session=sess))
    assert sess.calls == 1


def test_retryable_status_then_succeeds():
    # 503 is the upstream's problem — retry, then succeed.
    sess = _FakeSession([_Resp(503, "unavailable"), _Resp(200, _OK_BODY)])
    matches = asyncio.run(fa.fetch_matches(session=sess))
    assert sess.calls == 2
    assert matches[0]["result"] == "HOME"
