"""World Cup prediction bot — entry point.

Wires together:
  * Dynamic bet buttons (panels.BetButton) — registered once, survive restarts.
  * Slash commands: /leaderboard /mybets (everyone); admin: /syncfixtures /postpanel
    /setresult /exportwinners /giverole.
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
import sys
from datetime import datetime, timezone

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


def _result_team(match, result: str) -> str:
    if result == "HOME":
        return match["home"]
    if result == "AWAY":
        return match["away"]
    return "Draw"


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


def is_admin(interaction: discord.Interaction) -> bool:
    if str(interaction.user.id) == config.OWNER_ID:
        return True
    if isinstance(interaction.user, discord.Member):
        names = {r.name for r in interaction.user.roles}
        if names & set(config.ADMIN_ROLES):
            return True
    return False


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
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

    newly = await asyncio.to_thread(db.upsert_matches, matches)
    if not newly or not config.RESULT_CHANNEL_ID:
        return

    try:
        channel = bot.get_channel(int(config.RESULT_CHANNEL_ID)) \
            or await bot.fetch_channel(int(config.RESULT_CHANNEL_ID))
    except Exception as e:
        logger.error(f"result channel unavailable: {e}")
        return

    guild_id = str(channel.guild.id)
    for mid in newly:
        m = await asyncio.to_thread(db.get_match, mid)
        if not m or not m["result"]:
            continue
        counts = await asyncio.to_thread(db.bet_counts, guild_id, mid)
        correct = counts.get(m["result"], 0)
        outcome = _result_team(m, m["result"])
        await channel.send(
            f"🏁 **{m['home']} vs {m['away']}** — result: **{outcome}** "
            f"({RESULT_LABELS[m['result']]}). {correct:,} correct in this server."
        )


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


# ── Player commands ────────────────────────────────────────────────────────

@bot.tree.command(description="Show this server's World Cup prediction leaderboard")
@app_commands.describe(top="How many top players to show (default 10)")
async def leaderboard(interaction: discord.Interaction, top: int = 10):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    gid = str(interaction.guild_id)
    top = max(1, min(top, 25))
    rows = await asyncio.to_thread(db.leaderboard, gid, top)

    emb = discord.Embed(title="🏆 World Cup Prediction Leaderboard", color=panels.EMBED_COLOR)
    if not rows:
        emb.description = "No results settled yet. Check back after the first matches finish."
    else:
        lines = []
        for i, r in enumerate(rows, start=1):
            tag = MEDALS.get(i, f"`#{i}`")
            lines.append(f"{tag} <@{r['user_id']}> — **{r['score']}** pts "
                         f"({r['correct']}/{r['settled']} correct)")
        emb.description = "\n".join(lines)

    standing = await asyncio.to_thread(db.user_standing, gid, str(interaction.user.id))
    if standing:
        emb.set_footer(text=f"You: #{standing['rank']}/{standing['total']} · "
                            f"{standing['score']} pts · "
                            f"{standing['correct']}/{standing['settled']} correct")
    await interaction.response.send_message(embed=emb)


@bot.tree.command(description="Show your own predictions and how they did")
async def mybets(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    rows = await asyncio.to_thread(db.user_bets, str(interaction.guild_id), str(interaction.user.id))
    if not rows:
        await interaction.response.send_message(
            "You haven't made any predictions yet. Find the prediction panel and tap a button!",
            ephemeral=True)
        return

    lines = []
    for r in rows:
        pick_label = panels.outcome_label(r, r["pick"])
        if r["result"] is None:
            mark = "⏳"
        elif r["result"] == r["pick"]:
            mark = "✅"
        else:
            mark = "❌"
        lines.append(f"{mark} {r['home']} vs {r['away']} — you picked **{pick_label}**")

    emb = discord.Embed(title="📋 Your World Cup Predictions",
                        description="\n".join(lines[:40]), color=panels.EMBED_COLOR)
    await interaction.response.send_message(embed=emb, ephemeral=True)


# ── Admin commands ─────────────────────────────────────────────────────────

@bot.tree.command(description="(Admin) Pull World Cup fixtures & results from the API")
@admin_only()
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
@admin_only()
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
        msg = await interaction.channel.send(embed=embed, view=view)
        await asyncio.to_thread(
            db.record_panel, str(interaction.guild_id), str(interaction.channel_id),
            str(msg.id), date, [m["match_id"]])
        posted += 1
    await interaction.followup.send(
        f"✅ Posted {posted} match panel(s) for {date}.", ephemeral=True)


@bot.tree.command(description="(Admin) Manually set/correct a match result")
@app_commands.describe(match_id="The match id (see panels / API)", result="Final outcome")
@app_commands.choices(result=[
    app_commands.Choice(name="Home win", value="HOME"),
    app_commands.Choice(name="Draw", value="DRAW"),
    app_commands.Choice(name="Away win", value="AWAY"),
])
@admin_only()
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
@app_commands.describe(top="How many top players to export (default 20)")
@admin_only()
async def exportwinners(interaction: discord.Interaction, top: int = 20):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    rows = await asyncio.to_thread(db.leaderboard, gid, max(1, min(top, 100)))
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
    file = discord.File(data, filename=f"wc_winners_{gid}.csv")
    await interaction.followup.send(
        f"✅ Top {len(rows)} winners exported.", file=file, ephemeral=True)


@bot.tree.command(description="(Admin) Give a role to the current top N players")
@app_commands.describe(role="Role to assign", top="How many top players (default 10)")
@admin_only()
async def giverole(interaction: discord.Interaction, role: discord.Role, top: int = 10):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = str(interaction.guild_id)
    rows = await asyncio.to_thread(db.leaderboard, gid, max(1, min(top, 100)))
    if not rows:
        await interaction.followup.send("No settled results yet.", ephemeral=True)
        return

    given, failed = 0, 0
    for r in rows:
        try:
            member = interaction.guild.get_member(int(r["user_id"])) \
                or await interaction.guild.fetch_member(int(r["user_id"]))
            await member.add_roles(role, reason="World Cup prediction winner")
            given += 1
        except Exception as e:
            logger.error(f"giverole failed for {r['user_id']}: {e}")
            failed += 1

    msg = f"✅ Gave **{role.name}** to {given} player(s)."
    if failed:
        msg += f" ⚠️ {failed} failed (check bot's Manage Roles permission & role order)."
    await interaction.followup.send(msg, ephemeral=True)


# ── Entry ──────────────────────────────────────────────────────────────────

def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
