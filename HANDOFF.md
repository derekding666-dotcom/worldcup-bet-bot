# Handoff — continuing on another computer

Everything needed to pick this project up on a different machine. **Secrets are NOT
in git** (`.env` is gitignored), so you must carry them over separately.

## 1. Get the code

Recommended — private git repo:
```bash
# on this machine, once:
git init && git add . && git commit -m "World Cup prediction bot"
git remote add origin <your-private-repo-url>
git push -u origin main
# at home:
git clone <your-private-repo-url> && cd worldcup-bet-bot
```
`.venv/` and `.env` are gitignored on purpose — recreate them at home (below).

Or just copy the `worldcup-bet-bot` folder, but **exclude `.venv/`** (rebuild it).

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

## 4. Set up the environment at home

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                            # then paste the secrets back in
.venv/bin/python football_api.py               # sanity-check the data source
.venv/bin/python bot.py                         # run it
```

## 5. Important: one instance per token

Discord allows only one healthy gateway connection per bot token at a time. **Before
running at home, make sure no other copy is running** (this dev machine, or the VPS).
Two instances will both respond to interactions and behave erratically.

## 6. Reference

- Plan / design doc: `mighty-cuddling-boot.md` (the approved implementation plan)
- Data source verified: football-data.org free tier covers all 104 World Cup 2026
  matches (competition id 2000).
- Next milestone: deploy to the Warpath VPS via `deploy/worldcup-bot.service`.
