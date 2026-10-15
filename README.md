# Brawl Stars News Bot

A Discord bot that runs entirely on GitHub Actions (no server):

- **News**: posts new Supercell blog articles, with balance changes laid out per brawler (buffs, nerfs, bug fixes).
- **YouTube**: posts new videos and Shorts from the official channel.
- **Account linking** (optional): members link their Brawl Stars account by posting their tag, and look each other up with `!profile` / `!stats`.

## Setup

1. Add the repository secret `DISCORD_WEBHOOK_URL` (a webhook of the channel that should receive news).
2. Enable Actions. The first run records existing posts without sending them.
3. Optional: set up account linking (below).

## How it runs

GitHub's `schedule` trigger is unreliable (runs end up hours apart), so it can't poll every few minutes on its own.
Instead each workflow run stays alive for about 5h40m and loops: account linking every minute, news and YouTube
every 5 minutes, departed-member cleanup every 10 minutes. Just before it ends it starts its own successor, which
queues behind it and takes over (a gap of about a minute between runs is normal). The 2-hourly cron is only a safety
net that restarts the chain if it breaks.

- Pushing to `main` (bot code, tests or the workflow) cancels the running instance and starts a fresh one, so changes
  apply within a minute or two. You never need to re-run it by hand.
- `state.json` is committed after every cycle, so a cancelled run never loses progress or repeats posts.
- If a source fails 5 cycles in a row, the bot posts a status alert to the news webhook, and another when it recovers.
- To stop the bot, disable the workflow in the Actions tab and cancel the running job.
- Free for public repositories. On a private repository this would use all of the free Actions minutes.

## Account linking

Members post their player tag (e.g. `#2PP0`) in the **link channel**. The bot (polling every minute) checks it with the
Brawl Stars API and reacts ✅ (linked) or ❌ (not a valid tag or unknown player). Each member has **one account**: posting a new valid tag replaces the previous one, and a message with several tags uses the first.

Chat commands, written in a separate **command channel** (`COMMAND_CHANNEL_ID`; if unset they are read in the link channel):

| Command | Shows |
| --- | --- |
| `!profile [name]` | trophies, level, club, wins |
| `!stats [name]` | top 15 brawlers |
| `!help` | usage |

Use the member's Discord name (`!profile alice`): no @ needed, so nobody gets a notification (a real @mention also
works). Leave the name out or write `me` for yourself.
Replies never ping anyone, and each member can run at most 3 commands per minute.

**Tidiness.** With a command channel configured, the link channel is tags-only: ❌ messages, chatter and edited
messages are deleted 10 minutes after the bot has handled them, so people can see why first. Without a command
channel only ❌ messages are deleted. Set the repository variable `LINK_CLEANUP` to `0` to disable deleting.

**Privacy.** The channel is the database: a link is the member's own message carrying the bot's ✅, so no personal data
is written to this repository (`state.json` only stores the ids of the last messages scanned).
- Delete your message to unlink. Editing it also unlinks (edited tags are never trusted).
- Messages of members who left the server, and the bot's replies about them, are deleted every 10 minutes.
- Tags are not ownership-verified: Supercell offers no way to prove an account is yours.

### Setup

1. Developer portal → your bot → enable **Message Content Intent**. Give the bot, in the link channel,
   *View Channel, Read Message History, Add Reactions, Send Messages, Manage Messages*, and in the command channel the same
   except Add Reactions. The Actions log warns if a permission is missing.
2. Create a Brawl Stars API key at developer.brawlstars.com allowing IP `45.79.218.79`
   (the [RoyaleAPI proxy](https://docs.royaleapi.com/proxy.html), since GitHub Actions has no fixed IP;
   the key passes through that third party). A key made for your home IP will **not** work from Actions.
3. Repository secrets: `DISCORD_BOT_TOKEN`, `BRAWLSTARS_API_KEY`.
   Repository variables: `DISCORD_GUILD_ID`, `LINK_CHANNEL_ID`, and optionally `COMMAND_CHANNEL_ID`, `LINK_CLEANUP`.
   Leave `LINK_CHANNEL_ID` unset to switch linking off.

## Development

```
python -m unittest discover -s tests          # all tests, no network needed
DRY_RUN=1 STATE_FILE=sim_state.json python bot.py   # prints embeds, sends nothing
```

| File | Purpose |
| --- | --- |
| `bot.py` | Fetching, Discord output, polling loop, state, health alerts |
| `articles.py` | Parses Supercell articles and renders balance changes |
| `links.py` | Account linking: channel scanning, commands, privacy cleanup |
