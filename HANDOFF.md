# Handoff — continuing on another computer

Everything needed to pick this project up on a different machine. **Secrets are NOT
in git** (`.env` is gitignored), so each machine keeps its own.

Private GitHub repo: **https://github.com/derekding666-dotcom/worldcup-bet-bot**
(branch `main`). The two dev machines stay in sync through it: commit + push before you
switch, `git pull` after.

## 1. Get the code on the other machine

**Case A — the machine already has the project folder but it's not a git repo yet**
(e.g. the original `dingyifx` machine this was first built on). Connect that existing
folder to the repo so you keep its already-working `.venv` and `.env`:
```bash
cd <existing worldcup-bet-bot folder>
git init -b main
git remote add origin https://github.com/derekding666-dotcom/worldcup-bet-bot.git
git fetch origin
git reset --hard origin/main          # replace old code with the latest; see warning
git branch --set-upstream-to=origin/main main
```
`.venv/`, `.env`, `data/` are gitignored, so this does **not** touch them — the existing
venv and secrets stay. ⚠️ `git reset --hard` discards local edits to *tracked* code
files; only run it if you haven't changed code in that folder (back up first if unsure).

**Case B — a brand-new machine with nothing here:** `git clone` it, then set up `.env`
and the venv per sections 2 & 4.
```bash
git clone https://github.com/derekding666-dotcom/worldcup-bet-bot.git && cd worldcup-bet-bot
```

**After the first time, daily sync on either machine is just:** `git pull`.

## 2. Carry the secrets (record privately — password manager / private doc)

These live in `.env` here. Copy the values somewhere safe, then re-enter them at home:

| Variable | Where it comes from |
|----------|---------------------|
| `DISCORD_TOKEN` | Discord Developer Portal → your app → Bot |
| `FOOTBALL_API_KEY` | https://www.football-data.org/client/register |
| `TEST_GUILD_ID` | `717906132404011099` (your test server) |
| `OWNER_ID` | your Discord user ID (optional) |

## 3. Discord app reference (not secret)

- Application / Client ID: `1511698802703208508`
- Lives in the Discord Developer Portal under **your Discord account** — sign in with
  the same account to find it.
- Invite link:
  `https://discord.com/oauth2/authorize?client_id=1511698802703208508&permissions=268454912&scope=bot+applications.commands`

## 4. Set up the environment (only on a machine that doesn't already have a venv)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                            # then paste the secrets back in
.venv/bin/python football_api.py               # sanity-check the data source
.venv/bin/python bot.py                         # run it
```

**The `.venv` is NOT portable between machines.** Its `python.exe` is hard-wired to the
base interpreter path of the machine that created it, so a venv copied from another
computer fails with "did not find executable at '…\\python.exe'". Always build the venv
on the machine you'll run on (or just reuse the one already there via Case A above).

If you ever hit a broken/copied venv but the packages are present, you can run with the
system Python pointed at the existing packages instead of rebuilding:
```powershell
$env:PYTHONPATH = "<repo>\.venv\Lib\site-packages"
& "C:\path\to\real\python.exe" bot.py        # e.g. ...\Programs\Python\Python314\python.exe
```

## 5. Important: one instance per token

Discord allows only one healthy gateway connection per bot token at a time. **Before
running at home, make sure no other copy is running** (this dev machine, or the VPS).
Two instances will both respond to interactions and behave erratically.

## 6. Running in production (the VPS)

Live on a Debian VPS at `/opt/worldcup-bet-bot`, run by systemd as the unit
`worldcup-bot` (User=root, `Restart=always`, starts on boot). SSH in with
`ssh root@<vps-ip>`, then everything below runs from `/opt/worldcup-bet-bot`.

Handy commands:
```bash
systemctl status worldcup-bot          # is it running?
systemctl restart worldcup-bot         # restart it
journalctl -u worldcup-bot -n 50       # last 50 log lines (q to quit)
journalctl -u worldcup-bot -f          # follow logs live (Ctrl-C to stop)
```

### Hot-fixing code while it's live — data is NOT affected

Code and data are separate: the DB (`data/worldcup.db`) is gitignored, so `git pull`
only touches code, never bets/leaderboards/champion picks. Scores are *derived* (a join,
not a running total), so a restart can never lose or double-count points. New columns are
added by non-destructive `ALTER TABLE` in `init_db()`, preserving existing rows.

Standard fix flow (after the change is committed + pushed from a dev machine):
```bash
cd /opt/worldcup-bet-bot
git pull
systemctl restart worldcup-bot
systemctl status worldcup-bot          # confirm: active (running)
```
The only cost is a ~2–3 s gap during restart: a button tap in that window shows
"application did not respond", but nothing is lost (SQLite writes are durable) — the
player just taps again. Prefer a low-traffic moment (no live match) for the restart.

### Rollback if a new commit won't start

`systemctl status` shows `failed` / `activating (auto-restart)` and `journalctl` shows the
traceback. Roll back to the previous commit and restart:
```bash
cd /opt/worldcup-bet-bot
git reset --hard HEAD~1
systemctl restart worldcup-bot
```

### Backup before ANY manual DB edit

`git pull` is safe. The only thing that changes data is running SQL directly (e.g. the
one-liners used to clear test bets). **Always copy the DB first:**
```bash
cp /opt/worldcup-bet-bot/data/worldcup.db /opt/worldcup-bet-bot/data/worldcup.db.bak
```
There is no `sqlite3` CLI installed; inspect/edit the DB via the project's Python instead:
```bash
.venv/bin/python3 -c "import db; db.init_db(); print([r[1] for r in db._conn.execute('PRAGMA table_info(matches)')])"
```
Note `matches` columns are `match_id`, `home`, `away` (not `id`/`home_team`/`away_team`).

### Deploy gotchas already hit (so you don't re-hit them)

- The systemd unit ships with `User=botuser`; that user doesn't exist → `status=217/USER`.
  It was changed to `User=root` on the VPS.
- `init_db()` needs `data/` to exist: `mkdir -p /opt/worldcup-bet-bot/data` before first run.
- Pasting multi-line blocks over SSH can inject `\r` (`$'\r': command not found`) — paste
  one line at a time, or use the `cat > file <<'EOF' … EOF` heredoc form.

## 7. Reference

- Data source verified: football-data.org free tier covers all 104 World Cup 2026
  matches (competition id 2000). Stages: GROUP_STAGE / LAST_32 / LAST_16 /
  QUARTER_FINALS / SEMI_FINALS / THIRD_PLACE / FINAL.

## 8. Current state (what's built)

See `README.md` for full command docs and `git log` for the change history. As of the
latest commit the bot has:

- **Panels:** one match per message; flags filled in for 2026 teams.
- **Daily channel** (`/setdailychannel`, per server): posts that day's panels at 09:00
  UTC *and* broadcasts match results there as they settle. `/cleardailychannel` stops both.
- **Self-serve `/bet [date]`:** private, timezone-neutral (defaults to the next 24h of
  still-open matches).
- **Rewards:** overall Top 10 + per-stage Top 5 (a `stage` choice on `/wcleaderboard`,
  `/exportwinners`, `/giverole`) + a pre-tournament champion pick (`/champion` →
  `/championwinners`). `matches.stage_detail` holds the raw API stage.
- **Permissions (multi-server):** *operator-only* (`OWNER_ID`) for tournament-global
  commands `/syncfixtures` and `/setresult` (results are shared across every server);
  *server admin* (Discord **Manage Server** permission) for the guild-scoped commands.
  Player command names are namespaced (`/bet`, `/wcleaderboard`) to avoid clashes.
- **Tests:** `python -m pytest` → 18 passing (offline: result logic, scoring, overrides,
  stage leaderboards, champion pick).

**Before public launch:** start from a clean `data/worldcup.db` (the dev DB holds test
bets / a champion pick) — back up and delete it, the bot recreates an empty one on start.

**Not done yet (optional):** `on_guild_join` self-serve setup message; clearer
role-assign error ("can't give a role to the server owner / bot role must be higher").
