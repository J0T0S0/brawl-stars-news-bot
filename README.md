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

## Account linking (optional)

Members link their Brawl Stars account by **posting their tag in one dedicated channel** (e.g. `#2PP0`).
The bot (polling every minute), validates it with the Brawl Stars API and reacts ✅ (linked) or ❌ (unknown tag / limit of 3 reached).

Chat commands `!profile [@member]` and `!stats [@member]` go in a **separate command channel** (`COMMAND_CHANNEL_ID`) and are answered with a reply there; the link channel then only accepts tags. If `COMMAND_CHANNEL_ID` is unset, commands are read in the link channel instead.

**Privacy:** the channel is the database. A link is the member's own message carrying the bot's ✅, so no
personal data is written to this repository (`state.json` only stores the id of the last message scanned).
- Delete your message to unlink. Editing it also unlinks (edited tags are never trusted).
- Every run, messages of members who left the server, and bot replies about them, are deleted.

**Setup**
1. Developer portal → your bot → enable **Message Content Intent**. Invite the bot to the channel with
   *View Channel, Read Message History, Add Reactions, Send Messages, Manage Messages* (the command channel needs all but Add Reactions).
2. Create a Brawl Stars API key at developer.brawlstars.com allowing IP `45.79.218.79`
   (the [RoyaleAPI proxy](https://docs.royaleapi.com/proxy.html), since GitHub Actions has no fixed IP;
   the key passes through that third party). A key made for your home IP will **not** work from Actions.
3. Repository secrets: `DISCORD_BOT_TOKEN`, `BRAWLSTARS_API_KEY`.
   Repository variables: `DISCORD_GUILD_ID`, `LINK_CHANNEL_ID`, `COMMAND_CHANNEL_ID` (optional).

Reactions and replies arrive within about a minute (see below). Tags are not ownership-verified
(Supercell offers no way to prove an account is yours).

## How it runs

GitHub's `schedule` trigger is unreliable (runs end up hours apart), so it can't be used to poll every few minutes.
Instead each workflow run stays alive for ~5h40m and loops: account linking every minute, news and YouTube every 5 minutes,
departed-member cleanup every 10 minutes. Just before it ends it starts its own successor, which queues behind it
and takes over (a short gap of about a minute between runs is normal). The 2-hourly cron is only a safety net that
restarts the chain if it breaks, and pushing code to `main` replaces the running instance immediately.
State is committed after every cycle, so a cancelled run never loses progress.

To stop the bot, disable the workflow in the Actions tab (and cancel the running job).
Free for public repositories; on a private repository this would use all of the free Actions minutes.
