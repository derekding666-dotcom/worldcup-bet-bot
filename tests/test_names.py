"""Offline tests for leaderboard name resolution. A fake guild stands in for
Discord so we can verify: cached member → name, uncached → REST fetch, gone →
placeholder, and that names are markdown-escaped (so '*' / '_' don't break the embed)."""
import asyncio

import discord

import bot


class _FakeMember:
    def __init__(self, name):
        self.display_name = name


class _FakeGuild:
    def __init__(self, cached=None, fetchable=None):
        self._cached = cached or {}
        self._fetchable = fetchable or {}

    def get_member(self, uid):
        return self._cached.get(uid)

    async def fetch_member(self, uid):
        if uid in self._fetchable:
            return self._fetchable[uid]
        raise discord.DiscordException("unknown member")


def test_resolve_prefers_cache_then_fetch_then_placeholder():
    g = _FakeGuild(cached={111: _FakeMember("Cached Cat")},
                   fetchable={222: _FakeMember("Fetched Fox")})
    assert asyncio.run(bot._resolve_name(g, "111")) == "Cached Cat"   # cache hit
    assert asyncio.run(bot._resolve_name(g, "222")) == "Fetched Fox"  # REST fallback
    assert asyncio.run(bot._resolve_name(g, "333")) == "departed player"  # left guild


def test_resolve_handles_no_guild():
    assert asyncio.run(bot._resolve_name(None, "111")) == "departed player"


def test_resolve_escapes_markdown():
    g = _FakeGuild(cached={111: _FakeMember("*Star*_Lord_")})
    out = asyncio.run(bot._resolve_name(g, "111"))
    assert out == discord.utils.escape_markdown("*Star*_Lord_")
