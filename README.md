# World Cup Prediction Bot

A Discord bot for running a **2026 World Cup prediction game** in a large community
(built for 3000+ DAU). Players predict win/draw/loss on a daily fixture panel by
tapping buttons; results auto-settle from football-data.org; per-server leaderboards
decide winners. One bot instance works across **multiple Discord servers** — data is
isolated per `guild_id`, so any server can just invite it.

## How it works

- **Daily panel + buttons** (`/postpanel`): one message lists the day's matches, each
  with buttons. Tapping records your pick; you can change it until kickoff. Confirmation
  is ephemeral (only you see it), so the channel never floods at scale.
- **Vote shares hidden until lock**: during voting the panel is static (no percentages),
  which both prevents herding and keeps the bot from editing a high-traffic message
  (avoids Discord's edit rate limit). When a match kicks off, that row flips once to
  grey the buttons and reveal the final distribution.
- **Auto-settlement**: a background loop pulls finished results from the API and scores
  predictions. Scores are *derived* from a join (correct pick = points), never
  incremented — so restarts and re-runs can't double-count.
- **Admin override is authoritative**: `/setresult` wins over the API and is never
  overwritten by a later sync.
- **Knockouts**: a draw can't survive ET/penalties, so knockout panels show a 2-way
  "who advances" choice, settled by the API's winner field.

## Setup

You need two secrets:

1. **Discord bot token** — https://discord.com/developers/applications → New Application →
   Bot → Reset Token. Under **Bot**, enable nothing special (no privileged intents
   needed). Invite it with the `bot` + `applications.commands` scopes and permissions:
   **Send Messages, Embed Links, Manage Roles** (Manage Roles only for `/giverole`).
2. **football-data.org API key** — free: https://www.football-data.org/client/register

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
cp .env.example .env                              # then fill in DISCORD_TOKEN + FOOTBALL_API_KEY + OWNER_ID
```

### Step 1 — verify the data source FIRST

Before anything else, confirm the free tier actually covers the 2026 World Cup:

```bash
.venv/bin/python football_api.py
```

It prints the match count, date range, and a sample. If it errors with 403/auth, the
free tier doesn't cover the World Cup → switch to API-Football (see the plan). **Don't
build the activity on an unverified data source.**

### Step 2 — run

```bash
.venv/bin/python bot.py
```

For fast slash-command testing in one server, set `TEST_GUILD_ID=<your server id>` in
`.env` (global sync can take ~1h to propagate; guild sync is instant).

## Commands

Command names are deliberately namespaced (`/bet`, `/wcleaderboard`) to avoid clashing
with other bots' generic commands in the same server.

**Players**
- `/bet [date]` — privately pull up upcoming matches and predict (defaults to the next
  24h of still-open matches; pass a UTC date to see a specific day). Use it if you missed
  the daily panel — the reply is ephemeral, so it never floods the channel.
- `/wcleaderboard [top] [stage]` — this server's standings (+ your own rank in the footer).
  Pass `stage` to see a single round's board (Group / R32 / R16 / QF / SF).
- `/mybets` — your predictions and how they did, plus your champion pick (ephemeral)
- `/champion <team>` — pick the team you think wins the whole tournament. Locks at the
  first kickoff; changeable until then (autocomplete lists the teams).

**Server admins** — anyone with Discord's **Manage Server** permission (works in every
server with no setup), plus the operator and anyone holding a role named in `ADMIN_ROLES`.
These commands only ever touch **their own server's** data:
- `/postpanel [date]` — post the prediction panel for a UTC date (default: today)
- `/setdailychannel` — set the current channel as this server's World Cup channel. Two
  things post here: (1) the daily fixture panels every day at `DAILY_POST_HOUR_UTC`
  (default 09:00 UTC), with a short call-to-vote intro (rest days skipped); (2) match
  result broadcasts as games settle (`🏁 … — N correct in this server`).
- `/cleardailychannel` — stop both the daily panels and the result broadcasts here
- `/exportwinners [top] [stage]` — CSV of winners (for prize codes); `stage` scopes it to
  one round
- `/giverole <role> [top] [stage]` — give a role to the current top N (needs Manage Roles);
  `stage` ranks by one round
- `/championwinners [role]` — once the Final is settled, list everyone who picked the
  champion correctly (CSV), and optionally give them a role

**Operator only** — just the `OWNER_ID` user. Fixtures and results are **shared across all
servers**, so only the operator may change them (a per-server admin must never be able to
corrupt everyone's results):
- `/syncfixtures` — pull fixtures & results from the API (also runs automatically)
- `/setresult <match_id> <HOME|DRAW|AWAY>` — manually set/correct a result (overrides API,
  affects every server's leaderboard)

Each fixture posts as its **own message** so the bet buttons always sit directly under
the match they belong to (no ambiguity about which "Draw" is which).

## Typical run-of-show

1. `/syncfixtures` once before the tournament (and the loop keeps it fresh).
2. `/setdailychannel` once in your predictions channel — from then on the day's panels
   post automatically each morning, and match results are broadcast there as they settle.
   (Or `/postpanel [date]` manually any time.)
3. Results settle automatically in the background (and post to the channel above).
4. Before kickoff, nudge players to `/champion` their tournament winner.
5. At the end: `/exportwinners` for prize codes, `/giverole` for a winner role.

## Rewards

Three parallel tracks keep different kinds of players engaged across the whole tournament
(so falling behind on the overall board isn't game-over):

| Track | Who wins | How to award |
|-------|----------|--------------|
| 🏆 **Overall Top 10** | best total score at the end | `/exportwinners top:10` → prize codes; `/giverole <role> top:10` → champion role |
| 📅 **Per-stage Top 5** | best in each round: Group / R32 / R16 / QF / SF | same commands with `stage`, e.g. `/exportwinners stage:"Round of 16" top:5`, `/giverole <role> stage:"Round of 16" top:5` |
| 🔮 **Champion pick** | everyone who picked the eventual winner (chosen pre-kickoff via `/champion`) | after the Final settles: `/championwinners role:<role>` lists them and assigns the role |

Notes: stage scoring uses the raw API stage (`matches.stage_detail`); the Final and 3rd-place
playoff don't get a separate stage prize but still count toward the overall board. Scoring
weights live in `.env` (`POINTS_GROUP` / `POINTS_KNOCKOUT`). Ties at a Top-N cut (e.g. several
players level on 5th) are the operator's call.

## Deploy on the VPS (Linux)

See [deploy/worldcup-bot.service](deploy/worldcup-bot.service) for a systemd unit that
auto-restarts and starts on boot. Clone to `/opt/worldcup-bet-bot`, create the venv,
fill `.env`, then enable the service.

## Tests

```bash
.venv/bin/python -m pytest        # offline; covers result logic, scoring, overrides
```

## Project layout

| File | Role |
|------|------|
| `bot.py` | Entry point: slash commands, button wiring, settlement & reveal loops |
| `panels.py` | Panel embed/view rendering + dynamic bet buttons |
| `db.py` | SQLite layer: bets, derived leaderboard, manual-override-safe results |
| `football_api.py` | football-data.org client + offline-testable result logic |
| `flags.py` | Country → flag emoji (decorative) |
| `config.py` | `.env` configuration |
