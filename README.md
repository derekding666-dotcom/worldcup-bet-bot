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

**Players**
- `/leaderboard [top]` — this server's standings (+ your own rank in the footer)
- `/mybets` — your predictions and how they did (ephemeral)

**Admins** (the `OWNER_ID` user, or anyone with a role in `ADMIN_ROLES`)
- `/syncfixtures` — pull fixtures & results from the API (also runs automatically)
- `/postpanel [date]` — post the prediction panel for a UTC date (default: today)
- `/setresult <match_id> <HOME|DRAW|AWAY>` — manually set/correct a result (overrides API)
- `/exportwinners [top]` — CSV of winners (for handing out prize codes)
- `/giverole <role> [top]` — give a role to the current top N (needs Manage Roles)

## Typical run-of-show

1. `/syncfixtures` once before the tournament (and the loop keeps it fresh).
2. Each match day: `/postpanel` in your predictions channel.
3. Results settle automatically; optionally set `RESULT_CHANNEL_ID` for live result posts.
4. At the end: `/exportwinners` for prize codes, `/giverole` for a winner role.

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
