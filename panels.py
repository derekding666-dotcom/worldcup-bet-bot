"""Panel rendering + the dynamic bet buttons.

Two UI states per match row, driven purely by `now` vs kickoff:
  * VOTING  — active buttons, NO percentages shown (kills herding, and keeps the
              message static during the high-traffic window so we never hit
              Discord's edit rate limit).
  * LOCKED  — buttons greyed, final vote distribution revealed.

Buttons are discord.py DynamicItems keyed by custom_id `wcbet:<match_id>:<pick>`.
A single dynamic handler (registered once via bot.add_dynamic_items) keeps every
panel working across restarts — no per-message re-registration needed.

Kickoff times render with Discord's `<t:unix:f>` token so each viewer sees them in
their own local timezone — important for an international community.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord

import db
from flags import flag

EMBED_COLOR = 0x1ABC9C


# ── Time / lock helpers (pure) ─────────────────────────────────────────────

def parse_kickoff(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def is_locked(match, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    ko = parse_kickoff(match["kickoff_utc"])
    return ko is not None and now >= ko


def locked_count(matches, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return sum(1 for m in matches if is_locked(m, now))


def outcomes_for(stage: str) -> list[str]:
    """Group → 3-way; knockout → 2-way (a draw can't survive ET/penalties)."""
    return ["HOME", "AWAY"] if stage == "KNOCKOUT" else ["HOME", "DRAW", "AWAY"]


def outcome_label(match, pick: str) -> str:
    if pick == "HOME":
        return match["home"]
    if pick == "AWAY":
        return match["away"]
    return "Draw"


def _distribution(match, counts: dict[str, int]) -> str:
    """Reveal text shown after lock, e.g. 'Mexico 17% · Draw 21% · ✅ France 62% (1,284)'."""
    if match["stage"] == "KNOCKOUT":
        picks = [("HOME", match["home"]), ("AWAY", match["away"])]
        total = counts["HOME"] + counts["AWAY"]
    else:
        picks = [("HOME", match["home"]), ("DRAW", "Draw"), ("AWAY", match["away"])]
        total = counts["HOME"] + counts["DRAW"] + counts["AWAY"]
    if total == 0:
        return "no predictions"
    parts = []
    for key, label in picks:
        pct = round(100 * counts[key] / total)
        mark = "✅ " if match["result"] == key else ""
        parts.append(f"{mark}{label} {pct}%")
    return " · ".join(parts) + f"  ({total:,})"


# ── Dynamic bet button ─────────────────────────────────────────────────────

class BetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"wcbet:(?P<mid>\d+):(?P<pick>HOME|DRAW|AWAY)",
):
    def __init__(self, match_id: int, pick: str, label: str,
                 disabled: bool = False, row: int | None = None):
        self.match_id = match_id
        self.pick = pick
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                row=row,
                custom_id=f"wcbet:{match_id}:{pick}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["mid"]), match["pick"], label=match["pick"].title())

    async def callback(self, interaction: discord.Interaction):
        await handle_bet_click(interaction, self.match_id, self.pick)


async def handle_bet_click(interaction: discord.Interaction, match_id: int, pick: str):
    """Click path stays light: re-check lock, then a single upsert. Ephemeral reply
    confirms WITHOUT revealing the distribution (no herding)."""
    if interaction.guild_id is None:
        await interaction.response.send_message("Predictions only work inside a server.", ephemeral=True)
        return

    m = await asyncio.to_thread(db.get_match, match_id)
    if m is None:
        await interaction.response.send_message("This match is no longer available.", ephemeral=True)
        return
    if is_locked(m):
        await interaction.response.send_message(
            "⛔ This match has kicked off — predictions are closed.", ephemeral=True)
        return

    await asyncio.to_thread(
        db.place_bet, str(interaction.guild_id), str(interaction.user.id), match_id, pick)

    matchup = f"{m['home']} vs {m['away']}"
    await interaction.response.send_message(
        f"✅ Locked in: {matchup} → you picked **{outcome_label(m, pick)}**\n"
        "You can change your pick anytime until kickoff.",
        ephemeral=True,
    )


# ── Render (embed + view) ──────────────────────────────────────────────────
#
# One match per message: each panel is a single fixture, so its buttons sit
# directly under their own embed — there's never any ambiguity about which
# "Draw" button belongs to which game.

def build_view(match, now: datetime | None = None) -> discord.ui.View:
    """The bet buttons for a single match, on one row."""
    now = now or datetime.now(timezone.utc)
    view = discord.ui.View(timeout=None)
    locked = is_locked(match, now)
    for pick in outcomes_for(match["stage"]):
        view.add_item(BetButton(match["match_id"], pick,
                                label=outcome_label(match, pick),
                                disabled=locked, row=0))
    return view


def build_embed(match, now: datetime | None = None,
                counts: dict[str, int] | None = None) -> discord.Embed:
    """Embed for a single match. Title is the matchup itself; the body switches
    between the open (voting) and locked (revealed) states."""
    now = now or datetime.now(timezone.utc)

    ko = parse_kickoff(match["kickoff_utc"])
    ts = f"<t:{int(ko.timestamp())}:f>" if ko else "TBD"
    rel = f" (<t:{int(ko.timestamp())}:R>)" if ko else ""
    stage_label = "Knockout" if match["stage"] == "KNOCKOUT" else "Group stage"

    title = f"⚽ {flag(match['home'])}  vs  {flag(match['away'])}"
    emb = discord.Embed(title=title, color=EMBED_COLOR)

    if is_locked(match, now):
        counts = counts or {"HOME": 0, "DRAW": 0, "AWAY": 0}
        emb.description = (
            f"🔒 **Voting closed** · kicked off {ts}\n"
            f"📊 {_distribution(match, counts)}"
        )
    else:
        mode = ("Tap who advances 👇" if match["stage"] == "KNOCKOUT"
                else "Tap the winner — or a draw 👇")
        emb.description = (
            f"🕒 **Kicks off {ts}**{rel}\n"
            f"🏟️ {stage_label}\n\n"
            f"{mode}\n"
            "*You can change your pick until kickoff. "
            "Vote shares stay hidden until the match locks.*"
        )
    return emb


def render(match, now: datetime | None = None,
           counts: dict[str, int] | None = None):
    """Convenience: returns (embed, view) for posting or editing a single-match panel."""
    return build_embed(match, now, counts), build_view(match, now)
