"""World Cup prediction bot — entry point.

Wires together:
  * Dynamic bet buttons (panels.BetButton) — registered once, survive restarts.
  * Slash commands, in three permission tiers:
      - Everyone:        /bet /wcleaderboard /mybets /champion
      - Server admin:    /postpanel /postleaderboard /setdailychannel /cleardailychannel
                         /exportwinners /giverole /championwinners  (Manage Server
                         permission; only ever touch THIS guild's own data)
      - Operator only:   /syncfixtures /setresult  (OWNER_ID; fixtures & results are shared
                         across ALL servers, so a per-guild admin must not change them)
    Player-facing names are namespaced (/bet, /wcleaderboard) to avoid clashing with other
    bots' generic commands in the same server.
  * Rewards: overall Top 10 (/wcleaderboard, /exportwinners, /giverole), per-stage Top 5
    (same commands with a `stage` choice), and a pre-tournament champion pick (/champion
    → /championwinners). Stage prizes read matches.stage_detail (raw API stage).
  * Two background loops:
      settle_loop  — pull fixtures + finished results from the API, write results
                     (manual overrides preserved), broadcast newly-settled matches.
      reveal_loop  — when a match crosses kickoff, re-render its panel once to grey
                     the buttons and reveal the vote distribution. Idempotent.

Errors in the loops are logged AND DM'd to the owner — never silently swallowed, so
a broken settlement can't leave matches unsettled without anyone noticing.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# Windows consoles default to GBK and crash on emoji/non-GBK log output. Force UTF-8
# so logging of team names etc. works out of the box (lesson from ops-bot-template).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import discord
from discord import app_commands
from discord.ext import tasks

import config
import db
import football_api
import panels

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# Optional: set to a guild id for instant slash-command sync during development.
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()

RESULT_LABELS = {"HOME": "home win", "DRAW": "draw", "AWAY": "away win"}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Stages that get their own Top-5 prize. Value = matches.stage_detail (raw API stage).
# Used as an optional filter on the leaderboard/export/giverole commands; omit = overall.
STAGE_CHOICES = [
    app_commands.Choice(name="Group stage", value="GROUP_STAGE"),
    app_commands.Choice(name="Round of 32", value="LAST_32"),
    app_commands.Choice(name="Round of 16", value="LAST_16"),
    app_commands.Choice(name="Quarter-finals", value="QUARTER_FINALS"),
    app_commands.Choice(name="Semi-finals", value="SEMI_FINALS"),
]


def _result_team(match, result: str) -> str:
    if result == "HOME":
        return match["home"]
    if result == "AWAY":
        return match["away"]
    return "Draw"


def expiry_iso(days: int | None) -> str | None:
    """ISO-UTC timestamp `days` from now, or None for a permanent grant."""
    if not days or days <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class WorldCupBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()  # guilds only; no privileged intents needed
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        db.init_db()
        self.add_dynamic_items(panels.BetButton)  # makes old panels clickable after restart

        if TEST_GUILD_ID:
            guild = discord.Object(id=int(TEST_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Slash commands synced to test guild {TEST_GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to ~1h to appear)")

        settle_loop.start()
        reveal_loop.start()
        daily_panel_loop.start()
        expire_roles_loop.start()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} ({self.user.id})")


bot = WorldCupBot()


# ── Helpers ────────────────────────────────────────────────────────────────

async def dm_owner(text: str) -> None:
    if not config.OWNER_ID:
        return
    try:
        user = await bot.fetch_user(int(config.OWNER_ID))
        await user.send(text[:1900])
    except Exception as e:
        logger.error(f"Could not DM owner: {e}")


def server_slug(interaction: discord.Interaction) -> str:
    """A filesystem-safe fragment of the server name for export filenames, so a CSV is
    identifiable by community at a glance (the bare guild_id is not). Falls back to the
    guild_id when the name has no usable characters."""
    name = interaction.guild.name if interaction.guild else ""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40]
    return slug or str(interaction.guild_id)


def is_operator(interaction: discord.Interaction) -> bool:
    """The single bot operator (OWNER_ID). Owns tournament-global data (fixtures/results)."""
    return bool(config.OWNER_ID) and str(interaction.user.id) == config.OWNER_ID


def is_server_admin(interaction: discord.Interaction) -> bool:
    """A per-server admin: the operator, anyone with Discord's Manage Server permission
    (works in every server with no config), or a member whose role name is in ADMIN_ROLES."""
    if is_operator(interaction):
        return True
    member = interaction.user
    if isinstance(member, discord.Member):
        if member.guild_permissions.manage_guild:
            return True
        if {r.name for r in member.roles} & set(config.ADMIN_ROLES):
            return True
    return False


def server_admin_only():
    """Gate for guild-scoped admin commands (only touch this server's own data)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_server_admin(interaction):
            return True
        await interaction.response.send_message(
            "⛔ You need the **Manage Server** permission to use this.", ephemeral=True)
        return False
    return app_commands.check(predicate)


def operator_only():
    """Gate for tournament-global commands (results/fixtures are shared across ALL servers,
    so only the bot operator may change them — a server admin must not corrupt everyone)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_operator(interaction):
            return True
        await interaction.response.send_message(
            "⛔ Restricted to the bot operator — this affects results in every server.",
            ephemeral=True)
        return False
    return app_commands.check(predicate)


# ── Background: settlement ─────────────────────────────────────────────────

@tasks.loop(seconds=config.SETTLE_INTERVAL_SEC)
async def settle_loop():
    try:
        matches = await football_api.fetch_matches()
    except Exception as e:
        logger.exception("settlement fetch failed")
        await dm_owner(f"❌ World Cup bot: settlement fetch failed: {e}")
        return

    sync = await asyncio.to_thread(db.upsert_matches, matches)

    # The API now contradicts an already-settled result. We kept ours (no silent
    # leaderboard rewrite); alert the operator to decide via /setresult.
    for mid, stored, api in sync.result_conflicts:
        m = await asyncio.to_thread(db.get_match, mid)
        name = f"{m['home']} vs {m['away']}" if m else f"match {mid}"
        await dm_owner(
            f"⚠️ Result conflict for {name} (id {mid}): kept stored **{stored}**, but the "
            f"API now reports **{api}**. If the API is right, run `/setresult {mid} {api}` "
            "to apply the correction; otherwise ignore.")

    newly = sync.newly_settled
    if not newly:
        return

    # Broadcast each newly-settled match to every server's registered channel (the same
    # channel as its daily panels), with that server's own correct-count.
    channels = await asyncio.to_thread(db.all_daily_channels)
    if not channels:
        return

    settled = []
    for mid in newly:
        m = await asyncio.to_thread(db.get_match, mid)
        if m and m["result"]:
            settled.append(m)
    if not settled:
        return

    for row in channels:
        gid = row["guild_id"]
        try:
            channel = bot.get_channel(int(row["channel_id"])) \
                or await bot.fetch_channel(int(row["channel_id"]))
        except Exception as e:
            logger.error(f"result channel unavailable for guild {gid}: {e}")
            continue
        for m in settled:
            counts = await asyncio.to_thread(db.bet_counts, gid, m["match_id"])
            correct = counts.get(m["result"], 0)
            outcome = _result_team(m, m["result"])
            try:
                await channel.send(
                    f"🏁 **{m['home']} vs {m['away']}** — result: **{outcome}** "
                    f"({RESULT_LABELS[m['result']]}). {correct:,} correct in this server."
                )
            except Exception as e:
                logger.error(f"result broadcast failed for guild {gid}: {e}")


@settle_loop.before_loop
async def _before_settle():
    await bot.wait_until_ready()


# ── Background: lock-reveal ────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def reveal_loop():
    now = datetime.now(timezone.utc)
    for p in await asyncio.to_thread(db.all_panels):
        ids = [int(x) for x in p["match_ids"].split(",") if x]
        matches = await asyncio.to_thread(db.matches_by_ids, ids)
        if not matches:
            continue
        match = matches[0]  # one match per panel
        lc = 1 if panels.is_locked(match, now) else 0
        if lc <= p["locked_rendered"]:
            continue  # not newly locked → don't touch the message (no rate-limit churn)

        counts = await asyncio.to_thread(db.bet_counts, p["guild_id"], match["match_id"])
        embed, view = panels.render(match, now, counts)
        try:
            channel = bot.get_channel(int(p["channel_id"])) \
                or await bot.fetch_channel(int(p["channel_id"]))
            msg = await channel.fetch_message(int(p["message_id"]))
            await msg.edit(embed=embed, view=view)
            await asyncio.to_thread(db.set_panel_locked_rendered, p["message_id"], lc)
        except discord.NotFound:
            # message or channel gone — stop trying to reveal it
            await asyncio.to_thread(db.set_panel_locked_rendered, p["message_id"], lc)
        except Exception as e:
            logger.error(f"reveal failed for panel {p['message_id']}: {e}")


@reveal_loop.before_loop
async def _before_reveal():
    await bot.wait_until_ready()


# ── Background: daily auto-post ────────────────────────────────────────────

async def post_upcoming_panels(channel, guild_id: str) -> int:
    """Post a rallying intro + one panel per match kicking off within the lookahead
    window that this guild hasn't posted yet. Driven by kickoff time, not the UTC
    calendar date, so a match kicking off in the early UTC hours is posted ahead of
    time instead of being missed. Returns how many were posted (0 = nothing new)."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=config.PANEL_LOOKAHEAD_HOURS)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    upcoming = await asyncio.to_thread(
        db.matches_in_window, now.strftime(fmt), end.strftime(fmt))
    posted = await asyncio.to_thread(db.posted_match_ids, guild_id)
    fresh = [m for m in upcoming if m["match_id"] not in posted]
    if not fresh:
        return 0
    await channel.send(
        f"🏆 **Upcoming World Cup matches** — **{len(fresh)}** to predict. "
        "Tap the buttons on each to make your call; you can change your pick until "
        "kickoff. Good luck! 👇"
    )
    for m in fresh:
        embed, view = panels.render(m)
        msg = await channel.send(embed=embed, view=view)
        await asyncio.to_thread(
            db.record_panel, guild_id, str(channel.id), str(msg.id),
            m["kickoff_utc"][:10], [m["match_id"]])
    return len(fresh)


@tasks.loop(seconds=60)
async def daily_panel_loop():
    """Once per UTC day, after DAILY_POST_HOUR_UTC, post the upcoming-match panels to
    each registered channel. The set posted is driven by kickoff time (lookahead
    window) + per-guild dedup, not the calendar date, so early-UTC-hour matches are
    never missed. last_posted limits this to one digest per day; dedup prevents any
    match being posted twice."""
    now = datetime.now(timezone.utc)
    if now.hour < config.DAILY_POST_HOUR_UTC:
        return
    today = now.strftime("%Y-%m-%d")
    for row in await asyncio.to_thread(db.all_daily_channels):
        if row["last_posted"] == today:
            continue
        guild_id = row["guild_id"]
        # Claim the day BEFORE posting. If a send fails partway (lost permission,
        # transient API error) this guarantees we never re-post duplicate panels or
        # re-DM the owner every 60s. The trade-off: a transient failure forfeits
        # today's auto-post for this guild — an admin can /postpanel to recover.
        await asyncio.to_thread(db.set_daily_posted, guild_id, today)
        try:
            channel = bot.get_channel(int(row["channel_id"])) \
                or await bot.fetch_channel(int(row["channel_id"]))
        except Exception as e:
            logger.error(f"daily channel unavailable for guild {guild_id}: {e}")
            continue

        try:
            n = await post_upcoming_panels(channel, guild_id)
            if n:
                logger.info(f"daily auto-post: {n} match panel(s) to guild {guild_id} on {today}")
        except Exception as e:
            logger.exception("daily auto-post failed")
            await dm_owner(f"❌ World Cup bot: daily auto-post failed for guild {guild_id}: {e}")


@daily_panel_loop.before_loop
async def _before_daily():
    await bot.wait_until_ready()


# ── Background: expire temporary roles ─────────────────────────────────────
#
# Discord has no native role expiry, so we track (guild, user, role, expires_at)
# and sweep hourly. Hour-level precision is plenty for day-scale grants. Each
# removal clears its row only after it actually succeeds (or is moot), so the
# sweep is idempotent and safe across restarts — including grants that expired
# while the bot was down.

@tasks.loop(hours=1)
async def expire_roles_loop():
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in await asyncio.to_thread(db.due_temp_roles, now_iso):
        gid, uid, rid = row["guild_id"], row["user_id"], row["role_id"]

        guild = bot.get_guild(int(gid))
        if guild is None:
            await asyncio.to_thread(db.remove_temp_role, gid, uid, rid)  # bot left guild
            continue
        role = guild.get_role(int(rid))
        if role is None:
            await asyncio.to_thread(db.remove_temp_role, gid, uid, rid)  # role deleted
            continue

        try:
            member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
            await member.remove_roles(role, reason="World Cup temporary role expired")
            await asyncio.to_thread(db.remove_temp_role, gid, uid, rid)
        except discord.NotFound:
            await asyncio.to_thread(db.remove_temp_role, gid, uid, rid)  # member left
        except discord.Forbidden:
            # Permission/hierarchy issue — keep the row and retry next sweep, but flag it.
            logger.error(f"expire role forbidden: role {rid} from user {uid} in {gid}")
            await dm_owner(f"⚠️ Couldn't remove expired role <@&{rid}> from <@{uid}> "
                           "— check the bot's Manage Roles permission & role order.")
        except Exception as e:
            logger.error(f"expire role failed for {uid} in {gid}: {e}")


@expire_roles_loop.before_loop
async def _before_expire():
    await bot.wait_until_ready()


# ── Player commands ────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="How to play — predictions, leaderboard, and prizes")
async def help_cmd(interaction: discord.Interaction):
    """A self-serve how-to. Ephemeral so anyone can summon it without flooding the
    channel, and name-agnostic so it reads correctly whatever the bot is called."""
    emb = discord.Embed(
        title="⚽ How to Play — World Cup Predictions",
        color=panels.EMBED_COLOR,
        description=("Predict World Cup matches, climb your server's leaderboard, and win "
                     "prizes. No football knowledge required — just nerve."),
    )
    emb.add_field(
        name="① Tap to predict",
        value=("On the daily match panel, tap a button — **winner**, **draw**, or **loser**. "
               "That's the whole game. Change your pick anytime until kickoff; only you see it."),
        inline=False,
    )
    emb.add_field(
        name="② Predict ahead — `/bet`",
        value="Missed the panel? Run `/bet` to pull up upcoming matches privately and predict.",
        inline=False,
    )
    emb.add_field(
        name="③ Call the champion — `/champion`",
        value="Pick who lifts the trophy *before* the first kickoff for bonus glory.",
        inline=False,
    )
    emb.add_field(
        name="④ Track your standing",
        value=("`/wcleaderboard` — see who's on top (add a **stage** for stage prizes).\n"
               "`/mybets` — review your own picks and how they did."),
        inline=False,
    )
    emb.set_footer(text="Tip: type / in chat to see every command. Group stage starts June 11.")
    await interaction.response.send_message(embed=emb, ephemeral=True)


async def _resolve_name(guild, uid: str) -> str:
    """Resolve a stored user_id to a plain-text display name. We store only IDs, so
    names are looked up live: cache first, then a REST fetch (works without the
    members intent). A member who has left can't be resolved → placeholder.

    Plain text, NOT a <@id> mention: a mention inside an embed only renders a name
    for users the *viewer's* client has cached, so in a big server most fall back to
    raw `<@id>`. Resolving server-side shows every current member's name reliably.
    """
    if guild is not None:
        try:
            member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
            return discord.utils.escape_markdown(member.display_name)
        except (discord.DiscordException, ValueError):
            pass
    return "departed player"


async def _leaderboard_embed(guild, gid: str, top: int, stages, stage_label=None,
                             viewer_id: str | None = None) -> discord.Embed:
    """Build the leaderboard embed — shared by the private player view and the public
    admin post. Pass viewer_id to append that player's own standing in the footer;
    omit it for the public post (no personal line)."""
    rows = await asyncio.to_thread(db.leaderboard, gid, top, stages)
    # Include the server name so each guild's board is clearly its own.
    gname = guild.name if guild else "This server"
    base = f"🏆 {gname} · World Cup Leaderboard"
    title = f"{base} · {stage_label}" if stage_label else base
    emb = discord.Embed(title=title[:256], color=panels.EMBED_COLOR)
    if not rows:
        emb.description = "No results settled yet. Check back after the first matches finish."
    else:
        lines = []
        for i, r in enumerate(rows, start=1):
            tag = MEDALS.get(i, f"`#{i}`")
            name = await _resolve_name(guild, r["user_id"])
            lines.append(f"{tag} **{name}** — **{r['score']}** pts "
                         f"({r['correct']}/{r['settled']} correct)")
        emb.description = "\n".join(lines)
    if viewer_id:
        standing = await asyncio.to_thread(db.user_standing, gid, viewer_id, stages)
        if standing:
            emb.set_footer(text=f"You: #{standing['rank']}/{standing['total']} · "
                                f"{standing['score']} pts · "
                                f"{standing['correct']}/{standing['settled']} correct")
    return emb


@bot.tree.command(name="wcleaderboard",
                  description="Show this server's World Cup prediction leaderboard (only you see it)")
@app_commands.describe(top="How many top players to show (default 10)",
                       stage="(Optional) only this stage's predictions; default = overall")
@app_commands.choices(stage=STAGE_CHOICES)
async def leaderboard(interaction: discord.Interaction, top: int = 10,
                      stage: app_commands.Choice[str] | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    gid = str(interaction.guild_id)
    top = max(1, min(top, 25))
    stages = [stage.value] if stage else None
    emb = await _leaderboard_embed(interaction.guild, gid, top, stages,
                                   stage_label=stage.name if stage else None,
                                   viewer_id=str(interaction.user.id))
    # Ephemeral so any number of players can check standings without flooding the channel.
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="postleaderboard",
                  description="(Admin) Post the leaderboard publicly to this channel")
@app_commands.describe(top="How many top players to show (default 10)",
                       stage="(Optional) only this stage's predictions; default = overall")
@app_commands.choices(stage=STAGE_CHOICES)
@app_commands.default_permissions(manage_guild=True)
async def postleaderboard(interaction: discord.Interaction, top: int = 10,
                          stage: app_commands.Choice[str] | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    gid = str(interaction.guild_id)
    top = max(1, min(top, 25))
    stages = [stage.value] if stage else None
    emb = await _leaderboard_embed(interaction.guild, gid, top, stages,
                                   stage_label=stage.name if stage else None)
    await interaction.channel.send(embed=emb)
    await interaction.response.send_message("✅ Leaderboard posted to this channel.", ephemeral=True)


@bot.tree.command(description="Show your own predictions and how they did")
async def mybets(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    gid, uid = str(interaction.guild_id), str(interaction.user.id)
    rows = await asyncio.to_thread(db.user_bets, gid, uid)
    champ = await asyncio.to_thread(db.get_champion_pick, gid, uid)
    if not rows and not champ:
        await interaction.response.send_message(
            "You haven't made any predictions yet. Find the prediction panel and tap a button!",
            ephemeral=True)
        return

    # Newest first (user_bets is oldest→newest): in the back half of the tournament a
    # player has dozens of picks, and the recent / knockout ones are what they care about.
    lines = []
    for r in reversed(rows):
        pick_label = panels.outcome_label(r, r["pick"])
        suffix = ""
        if r["result"] is None:
            mark = "⏳"
        elif r["result"] == r["pick"]:
            mark = "✅"
        else:
            mark = "❌"
            suffix = f" (result: {panels.outcome_label(r, r['result'])})"
        lines.append(f"{mark} {r['home']} vs {r['away']} — you picked **{pick_label}**{suffix}")

    # Summary header: total picks + (once anything has settled) correct/score/rank.
    standing = await asyncio.to_thread(db.user_standing, gid, uid)
    if standing:
        summary = (f"**{len(rows)}** prediction(s) · ✅ {standing['correct']}/{standing['settled']} "
                   f"settled correct · **{standing['score']}** pts · "
                   f"rank #{standing['rank']}/{standing['total']}")
    else:
        summary = f"**{len(rows)}** prediction(s) · nothing settled yet — check back after kickoff."

    # Fit as many (newest) lines as the embed description allows (Discord cap is 4096).
    DESC_BUDGET = 3900
    body, used, shown = [], len(summary) + 2, 0
    for ln in lines:
        if used + len(ln) + 1 > DESC_BUDGET:
            break
        body.append(ln)
        used += len(ln) + 1
        shown += 1
    desc = summary + ("\n\n" + "\n".join(body) if body else "")
    hidden = len(rows) - shown
    if hidden > 0:
        desc += f"\n\n…and {hidden} more (showing your {shown} most recent)."

    emb = discord.Embed(title="📋 Your World Cup Predictions",
                        description=desc, color=panels.EMBED_COLOR)

    if champ:
        winner = await asyncio.to_thread(db.champion_team)
        if winner is None:
            cmark, note = "⏳", ""
        elif winner == champ:
            cmark, note = "✅", " — you called it!"
        else:
            cmark, note = "❌", f" (champion: {winner})"
        emb.add_field(name="🏆 Champion pick",
                      value=f"{cmark} **{champ}**{note}", inline=False)
    await interaction.response.send_message(embed=emb, ephemeral=True)


# Default look-ahead for /bet when no date is given.
VOTE_WINDOW_HOURS = 24


@bot.tree.command(
    name="bet",
    description="Privately pull up upcoming matches and place your predictions (in case you missed the panel)")
@app_commands.describe(date="(Optional) a specific UTC date YYYY-MM-DD; default shows the next 24h of matches you can still bet on")
async def bet(interaction: discord.Interaction, date: str | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    gid = str(interaction.guild_id)

    if date:
        # Explicit UTC date (includes matches that already kicked off, shown locked).
        matches = await asyncio.to_thread(db.matches_for_date, date)
        empty = (f"No matches on {date} (UTC). Try another date, "
                 "or run /bet with no date to see what's coming up.")
        header = (f"⚽ **{date} (UTC)** — **{len(matches)}** match(es). "
                  "Tap to predict; you can change your pick until kickoff 👇")
    else:
        # Rolling window from NOW — timezone-neutral, so players anywhere get the same
        # set of still-votable matches. Kickoff times render in each viewer's local zone.
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(hours=VOTE_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        matches = await asyncio.to_thread(db.matches_in_window, start, end)
        empty = (f"No matches open for betting in the next {VOTE_WINDOW_HOURS}h. "
                 "Try a specific date, e.g. /bet date:2026-06-12.")
        header = (f"⚽ **{len(matches)}** match(es) you can still bet on in the next "
                  f"**{VOTE_WINDOW_HOURS}h** — tap to predict; change your pick until kickoff 👇")

    if not matches:
        await interaction.response.send_message(empty, ephemeral=True)
        return

    # Private panels: only the caller sees them, so summoning never floods the channel.
    await interaction.response.send_message(
        header + "\n*(Only you can see this. Kickoff times show in your local timezone.)*",
        ephemeral=True)
    for m in matches[:10]:
        counts = await asyncio.to_thread(db.bet_counts, gid, m["match_id"])
        embed, view = panels.render(m, counts=counts)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _champion_autocomplete(interaction: discord.Interaction, current: str):
    teams = await asyncio.to_thread(db.participating_teams)
    cur = current.casefold()
    matches = [t for t in teams if cur in t.casefold()] if cur else teams
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


@bot.tree.command(description="Pick the team you think will WIN the World Cup (locks at the first kickoff)")
@app_commands.describe(team="The team you think lifts the trophy")
@app_commands.autocomplete(team=_champion_autocomplete)
async def champion(interaction: discord.Interaction, team: str):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return

    start = await asyncio.to_thread(db.tournament_start)
    if start is None:
        await interaction.response.send_message(
            "Fixtures aren't loaded yet — ask an admin to run /syncfixtures first.",
            ephemeral=True)
        return
    if datetime.now(timezone.utc) >= panels.parse_kickoff(start):
        await interaction.response.send_message(
            "⛔ Champion picks are closed — the tournament has already started.", ephemeral=True)
        return

    # Validate against real team names (case-insensitive) so the stored pick matches the
    # Final winner exactly later on.
    teams = await asyncio.to_thread(db.participating_teams)
    canonical = next((t for t in teams if t.casefold() == team.casefold()), None)
    if canonical is None:
        await interaction.response.send_message(
            f"“{team}” isn't a participating team. Start typing and pick one from the list.",
            ephemeral=True)
        return

    await asyncio.to_thread(
        db.set_champion_pick, str(interaction.guild_id), str(interaction.user.id), canonical)
    await interaction.response.send_message(
        f"🏆 Champion pick locked in: **{canonical}**. "
        "You can change it until the first match kicks off.", ephemeral=True)


# ── Admin commands ─────────────────────────────────────────────────────────

@bot.tree.command(description="(Operator) Pull World Cup fixtures & results from the API")
@app_commands.default_permissions(manage_guild=True)
@operator_only()
async def syncfixtures(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        matches = await football_api.fetch_matches()
    except Exception as e:
        await interaction.followup.send(f"❌ Fetch failed: {e}", ephemeral=True)
        return
    await asyncio.to_thread(db.upsert_matches, matches)
    await interaction.followup.send(f"✅ Synced {len(matches)} matches.", ephemeral=True)


@bot.tree.command(description="(Admin) Post the prediction panel for a date (UTC)")
@app_commands.describe(date="YYYY-MM-DD in UTC; defaults to today")
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def postpanel(interaction: discord.Interaction, date: str | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches = await asyncio.to_thread(db.matches_for_date, date)
    if not matches:
        await interaction.response.send_message(
            f"No matches found for {date}. Run /syncfixtures first, or check the date.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    posted = 0
    for m in matches:
        embed, view = panels.render(m)
        try:
            msg = await interaction.channel.send(embed=embed, view=view)
        except discord.Forbidden:
            # Without a clear reply this just leaves the "thinking…" spinner hanging.
            extra = f" (Posted {posted} before this.)" if posted else ""
            await interaction.followup.send(
                "⛔ I can't post in this channel. Give me **View Channel**, "
                "**Send Messages** and **Embed Links** permission here, then try again."
                + extra,
                ephemeral=True)
            return
        await asyncio.to_thread(
            db.record_panel, str(interaction.guild_id), str(interaction.channel_id),
            str(msg.id), date, [m["match_id"]])
        posted += 1
    await interaction.followup.send(
        f"✅ Posted {posted} match panel(s) for {date}.", ephemeral=True)


@bot.tree.command(
    name="setdailychannel",
    description="(Admin) Use THIS channel for daily fixture panels AND result broadcasts")
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def setdailychannel(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await asyncio.to_thread(
        db.set_daily_channel, str(interaction.guild_id), str(interaction.channel_id))
    await interaction.response.send_message(
        f"✅ This server's World Cup channel is now <#{interaction.channel_id}>:\n"
        f"• each day at **{config.DAILY_POST_HOUR_UTC:02d}:00 UTC** I post that day's match "
        "panels here (rest days skipped);\n"
        "• match results are also broadcast here as they settle.",
        ephemeral=True)


@bot.tree.command(
    name="cleardailychannel",
    description="(Admin) Stop daily panels AND result broadcasts in this server")
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def cleardailychannel(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    ok = await asyncio.to_thread(db.clear_daily_channel, str(interaction.guild_id))
    await interaction.response.send_message(
        "✅ Stopped daily panels and result broadcasts for this server."
        if ok else "This server has no World Cup channel set.",
        ephemeral=True)


@bot.tree.command(description="(Operator) Manually set/correct a match result (affects ALL servers)")
@app_commands.describe(match_id="The match id (see panels / API)", result="Final outcome")
@app_commands.choices(result=[
    app_commands.Choice(name="Home win", value="HOME"),
    app_commands.Choice(name="Draw", value="DRAW"),
    app_commands.Choice(name="Away win", value="AWAY"),
])
@app_commands.default_permissions(manage_guild=True)
@operator_only()
async def setresult(interaction: discord.Interaction, match_id: int,
                    result: app_commands.Choice[str]):
    ok = await asyncio.to_thread(db.set_result_manual, match_id, result.value)
    if not ok:
        await interaction.response.send_message(
            f"❌ No match with id {match_id}.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ Match {match_id} result set to **{result.name}** (overrides the API).",
        ephemeral=True)


@bot.tree.command(description="(Admin) Export the winner list as CSV (for prize codes)")
@app_commands.describe(top="How many top players to export (default 20)",
                       stage="(Optional) score only this stage; default = overall")
@app_commands.choices(stage=STAGE_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def exportwinners(interaction: discord.Interaction, top: int = 20,
                        stage: app_commands.Choice[str] | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    stages = [stage.value] if stage else None
    rows = await asyncio.to_thread(db.leaderboard, gid, max(1, min(top, 100)), stages)
    if not rows:
        await interaction.followup.send("No settled results yet.", ephemeral=True)
        return

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "user_id", "display_name", "score", "correct", "settled"])
    for i, r in enumerate(rows, start=1):
        name = str(r["user_id"])
        try:
            member = interaction.guild.get_member(int(r["user_id"])) \
                or await interaction.guild.fetch_member(int(r["user_id"]))
            name = member.display_name
        except Exception:
            pass
        w.writerow([i, r["user_id"], name, r["score"], r["correct"], r["settled"]])

    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    suffix = f"_{stage.value}" if stage else ""
    file = discord.File(data, filename=f"wc_winners_{server_slug(interaction)}{suffix}.csv")
    label = f" ({stage.name})" if stage else ""
    await interaction.followup.send(
        f"✅ Top {len(rows)} winners{label} exported.", file=file, ephemeral=True)


@bot.tree.command(description="(Admin) Give a role to the current top N players")
@app_commands.describe(role="Role to assign", top="How many top players (default 10)",
                       stage="(Optional) rank by this stage only; default = overall",
                       days="(Optional) auto-remove the role this many days after granting")
@app_commands.choices(stage=STAGE_CHOICES)
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def giverole(interaction: discord.Interaction, role: discord.Role, top: int = 10,
                   stage: app_commands.Choice[str] | None = None,
                   days: int | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    stages = [stage.value] if stage else None
    rows = await asyncio.to_thread(db.leaderboard, gid, max(1, min(top, 100)), stages)
    if not rows:
        await interaction.followup.send("No settled results yet.", ephemeral=True)
        return

    expires_at = expiry_iso(days)
    given, failed = 0, 0
    for r in rows:
        try:
            member = interaction.guild.get_member(int(r["user_id"])) \
                or await interaction.guild.fetch_member(int(r["user_id"]))
            await member.add_roles(role, reason="World Cup prediction winner")
            given += 1
            if expires_at:
                await asyncio.to_thread(
                    db.add_temp_role, gid, str(r["user_id"]), str(role.id), expires_at)
        except Exception as e:
            logger.error(f"giverole failed for {r['user_id']}: {e}")
            failed += 1

    label = f" ({stage.name})" if stage else ""
    msg = f"✅ Gave **{role.name}** to {given} player(s){label}."
    if expires_at:
        msg += f" ⏳ Auto-removed in {days} day(s)."
    if failed:
        msg += f" ⚠️ {failed} failed (check bot's Manage Roles permission & role order)."
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(description="(Admin) List who correctly picked the champion; optionally give them a role")
@app_commands.describe(role="(Optional) role to give every correct guesser",
                       days="(Optional) auto-remove the role this many days after granting")
@app_commands.default_permissions(manage_guild=True)
@server_admin_only()
async def championwinners(interaction: discord.Interaction, role: discord.Role | None = None,
                         days: int | None = None):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)

    team = await asyncio.to_thread(db.champion_team)
    if team is None:
        await interaction.followup.send(
            "The Final isn't settled yet — no champion to score against. "
            "Use /setresult on the Final once it's played.", ephemeral=True)
        return

    user_ids = await asyncio.to_thread(db.champion_winners, gid, team)
    if not user_ids:
        await interaction.followup.send(
            f"🏆 Champion: **{team}** — but nobody in this server picked it.", ephemeral=True)
        return

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "display_name", "champion_pick"])
    expires_at = expiry_iso(days)
    given, failed = 0, 0
    for uid in user_ids:
        name = str(uid)
        member = None
        try:
            member = interaction.guild.get_member(int(uid)) \
                or await interaction.guild.fetch_member(int(uid))
            name = member.display_name
        except Exception:
            pass
        w.writerow([uid, name, team])
        if role and member:
            try:
                await member.add_roles(role, reason="World Cup champion pick winner")
                given += 1
                if expires_at:
                    await asyncio.to_thread(
                        db.add_temp_role, gid, str(uid), str(role.id), expires_at)
            except Exception as e:
                logger.error(f"championwinners role failed for {uid}: {e}")
                failed += 1

    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    file = discord.File(data, filename=f"wc_champion_winners_{server_slug(interaction)}.csv")
    msg = f"🏆 Champion: **{team}** — {len(user_ids)} player(s) called it."
    if role:
        msg += f" Gave **{role.name}** to {given}."
        if expires_at:
            msg += f" ⏳ Auto-removed in {days} day(s)."
        if failed:
            msg += f" ⚠️ {failed} failed (check Manage Roles permission & role order)."
    await interaction.followup.send(msg, file=file, ephemeral=True)


# ── Entry ──────────────────────────────────────────────────────────────────

def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
