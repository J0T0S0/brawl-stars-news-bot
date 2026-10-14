# Brawl Stars News Bot

Posts new Supercell blog articles and official YouTube videos/Shorts to a Discord channel.
Runs on GitHub Actions every 5 minutes; seen items are tracked in `state.json`.

## Setup
1. Add a repository secret `DISCORD_WEBHOOK_URL`.
2. Enable Actions. The first run records existing posts without sending them.

## Local testing
```
python -m unittest discover -s tests
DRY_RUN=1 STATE_FILE=sim_state.json python bot.py   # prints embeds, sends nothing
```

## Updating
Pushing changes to `bot.py`, `tests/` or the workflow to `main` runs the tests and then the bot.
The scheduled run always uses the latest code on `main`, so no manual re-run is needed.

## Account linking (`link_bot.py`)
A separate Discord bot that lets members link their Brawl Stars and Minecraft (Java) accounts with slash commands:

- `/link brawlstars <tag>`: links your Brawl Stars player tag (e.g. `#2PP`).
- `/link minecraft <username>`: links your Minecraft account. The name is checked against Mojang and stored by UUID.
- `/unlink <game>`: removes one of your links.
- `/accounts [member]`: shows a member's linked accounts.

Each game account can be linked to only one member per server.

**Privacy:** links are kept in a local SQLite file (`links.db`, git-ignored, never committed). A member's links are deleted when they leave the server. All links for a server are deleted when the bot is removed from it. On every startup the bot also deletes links of anyone who left while it was offline.

This bot has to answer commands live, so it can't run on GitHub Actions. Run it on any always-on machine:
1. Create an application at https://discord.com/developers/applications, add a bot, and enable **Server Members Intent** (it's needed to notice when members leave).
2. Invite it with the `bot` and `applications.commands` scopes.
3. Optional: create a key at https://developer.brawlstars.com for the machine's public IP. With it, tags are checked and player names are shown. Without it, tags are only format-checked.
4. `pip install -r requirements.txt`
5. `DISCORD_BOT_TOKEN=... BRAWL_STARS_API_KEY=... python link_bot.py`. Use `LINKS_DB` to change where the database is stored.
