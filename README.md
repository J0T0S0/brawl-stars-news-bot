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
