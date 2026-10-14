"""Account linking by scanning one Discord channel.

Members post their Brawl Stars tag (e.g. ``#2PP0``) in the link channel. The
bot validates it with the Brawl Stars API and reacts with ✅ (or ❌).

Privacy design: the channel itself is the database. A link is simply a
member's own message carrying the bot's ✅ reaction, so nothing personal is
written to the repository (state.json only holds the id of the last message
scanned). Deleting your message unlinks you, and messages (plus bot replies)
of members who left the server are deleted on every run.

Chat commands, answered with a reply in the same channel:
    !profile [@member]    trophies, level, club, wins
    !stats [@member]      top brawlers
They are read from a separate command channel when one is configured (the
link channel is then for tags only); otherwise from the link channel.
"""

import re
import time
import urllib.parse

OK = "✅"
BAD = "❌"

MAX_ACCOUNTS = 3
MAX_HISTORY_PAGES = 10            # 1000 messages
UNKNOWN_MEMBER_CODES = (10007, 10013)

TAG_CHARS = "0289PYLQGRJCUV"
_TAG = re.compile(rf"^[{TAG_CHARS}]{{3,15}}$")
_HASH_TAG = re.compile(r"#\s?([0-9A-Za-z]{3,15})\b")
_COMMAND = re.compile(r"^\s*!(profile|stats)\b(?:\s+<@!?(\d+)>)?", re.I)


class NotFound(Exception):
    """The player tag does not exist."""


class Unavailable(Exception):
    """Brawl Stars API/Discord temporarily unusable; retry next run."""


# ------------------------------------------------------------
# Parsing
# ------------------------------------------------------------

def normalize_tag(text):
    tag = text.strip().lstrip("#").upper().replace("O", "0")
    return tag if _TAG.match(tag) else None


def extract_tags(content):
    """Tags in a message: '#TAG' anywhere, or the whole message being a tag."""

    candidates = _HASH_TAG.findall(content or "")

    if not candidates and content and len(content.split()) == 1:
        candidates = [content]

    tags = []

    for candidate in candidates:
        tag = normalize_tag(candidate)

        if tag and tag not in tags:
            tags.append(tag)

    return tags[:MAX_ACCOUNTS]


def parse_command(content):
    match = _COMMAND.match(content or "")

    if not match:
        return None

    return match.group(1).lower(), match.group(2)


# ------------------------------------------------------------
# Embeds
# ------------------------------------------------------------

def _n(value):
    return f"{int(value or 0):,}"


def _title(name):
    return re.sub(r"(^|[\s-])(\S)", lambda m: m.group(1) + m.group(2).upper(),
                  str(name).lower())


def profile_embed(player, discord_id):
    color = 0xF7C948

    try:
        color = int(str(player.get("nameColor", "")).replace("0x", ""), 16) & 0xFFFFFF
    except ValueError:
        pass

    return {
        "title": player["name"],
        "url": "https://brawlify.com/stats/profile/" + player["tag"].lstrip("#"),
        "description": f"`{player['tag']}`",
        "color": color,
        "fields": [
            {"name": "🏆 Trophies",
             "value": f"{_n(player.get('trophies'))}\nBest: {_n(player.get('highestTrophies'))}",
             "inline": True},
            {"name": "⭐ Level", "value": _n(player.get("expLevel")), "inline": True},
            {"name": "🛡️ Club",
             "value": (player.get("club") or {}).get("name", "No club"), "inline": True},
            {"name": "⚔️ 3v3 wins", "value": _n(player.get("3vs3Victories")), "inline": True},
            {"name": "🧍 Solo wins", "value": _n(player.get("soloVictories")), "inline": True},
            {"name": "👥 Duo wins", "value": _n(player.get("duoVictories")), "inline": True},
            {"name": "🥊 Brawlers", "value": _n(len(player.get("brawlers", []))), "inline": True},
        ],
        "footer": {"text": f"Brawl Stars • id:{discord_id}"},
    }


def stats_embed(player, discord_id, limit=15):
    brawlers = sorted(player.get("brawlers", []),
                      key=lambda b: b.get("trophies", 0), reverse=True)[:limit]

    lines = [
        f"**{i}. {_title(b['name'])}** — 🏆 {_n(b.get('trophies'))} "
        f"(best {_n(b.get('highestTrophies'))}) · P{b.get('power')} · Rank {b.get('rank')}"
        for i, b in enumerate(brawlers, 1)
    ]

    return {
        "title": f"{player['name']} — top brawlers",
        "description": "\n".join(lines) or "No brawlers unlocked yet.",
        "color": 0xF7C948,
        "footer": {"text": f"{player['tag']} • id:{discord_id}"},
    }


# ------------------------------------------------------------
# Channel scanning
# ------------------------------------------------------------

class LinkScanner:
    """
    api(method, path, payload=None) -> (status, json_or_None)
    fetch_player(tag) -> dict; raises NotFound / Unavailable

    `command_channel` is optional; when set, commands are only honoured there
    and the link channel only accepts tags.
    """

    def __init__(self, api, fetch_player, channel_id, guild_id,
                 command_channel_id=None, pause=0.3):
        self.api = api
        self.fetch_player = fetch_player
        self.channel = channel_id
        self.command_channel = command_channel_id or None
        self.guild = guild_id
        self.pause = pause
        self._left = {}

    # -- Discord helpers --------------------------------------

    def _call(self, method, path, payload=None):
        status, data = self.api(method, path, payload)

        if method != "GET":
            time.sleep(self.pause)

        return status, data

    def history(self, channel):
        """Newest-first messages of a channel (bounded)."""

        messages, before = [], None

        for _ in range(MAX_HISTORY_PAGES):
            path = f"/channels/{channel}/messages?limit=100"

            if before:
                path += f"&before={before}"

            status, page = self.api("GET", path)

            if status != 200 or not isinstance(page, list):
                raise Unavailable(f"Could not read channel {channel} (HTTP {status}).")

            messages.extend(page)

            if len(page) < 100:
                break

            before = page[-1]["id"]

        return messages

    def react(self, channel, message, emoji):
        encoded = urllib.parse.quote(emoji)
        self._call(
            "PUT",
            f"/channels/{channel}/messages/{message['id']}/reactions/{encoded}/@me",
        )

    def reply(self, channel, message, content=None, embed=None):
        payload = {
            "message_reference": {"message_id": message["id"], "fail_if_not_exists": False},
            "allowed_mentions": {"parse": []},
        }

        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]

        self._call("POST", f"/channels/{channel}/messages", payload)

    def delete(self, channel, message_id):
        self._call("DELETE", f"/channels/{channel}/messages/{message_id}")

    def has_left(self, user_id):
        if user_id not in self._left:
            status, data = self.api("GET", f"/guilds/{self.guild}/members/{user_id}")

            # Only an explicit "Unknown Member/User" counts. Any error
            # (including a wrong guild id, code 10004) must never be mistaken
            # for "left".
            self._left[user_id] = (
                status == 404 and (data or {}).get("code") in UNKNOWN_MEMBER_CODES
            )

        return self._left[user_id]

    # -- derived state ----------------------------------------

    @staticmethod
    def _bot_reacted(message, emoji):
        return any(
            r.get("me") and r.get("emoji", {}).get("name") == emoji
            for r in message.get("reactions", [])
        )

    def linked_tags(self, messages):
        """{discord_id: [tags]} from messages carrying the bot's check mark.

        Edited messages are ignored: an edit could swap in a tag that was never
        validated, so editing effectively unlinks.
        """

        linked = {}

        for message in reversed(messages):  # oldest first
            if message["author"].get("bot") or message.get("edited_timestamp"):
                continue

            if not self._bot_reacted(message, OK):
                continue

            tags = linked.setdefault(message["author"]["id"], [])

            for tag in extract_tags(message.get("content")):
                if tag not in tags and len(tags) < MAX_ACCOUNTS:
                    tags.append(tag)

        return linked

    # -- privacy purge ----------------------------------------

    def purge_departed(self, channels):
        """Deletes messages (and bot replies about them) of members who left,
        in every given {channel_id: messages} mapping.

        Returns (remaining {channel_id: messages}, removed_count).
        """

        self._left = {}
        authors = {
            m["author"]["id"]
            for messages in channels.values()
            for m in messages
            if not m["author"].get("bot")
        }
        left = {uid for uid in authors if self.has_left(uid)}

        if not left:
            return channels, 0

        def about_leaver(message):
            author = message["author"]
            if author["id"] in left:
                return True
            if not author.get("bot"):
                return False

            text = message.get("content", "") + " ".join(
                e.get("footer", {}).get("text", "") for e in message.get("embeds", [])
            )
            return any(f"id:{uid}" in text or f"<@{uid}>" in text for uid in left)

        remaining, removed = {}, 0

        for channel, messages in channels.items():
            doomed = [m for m in messages if about_leaver(m)]

            for message in doomed:
                self.delete(channel, message["id"])

            removed += len(doomed)
            doomed_ids = {m["id"] for m in doomed}
            remaining[channel] = [m for m in messages if m["id"] not in doomed_ids]

        return remaining, removed

    # -- main -------------------------------------------------

    def run(self, cursors, save_cursor, purge=True):
        """Processes new messages in the link (and command) channel.

        `cursors` maps "link"/"command" to the last handled message id and
        `save_cursor(key, id)` persists progress. Returns counters.
        """

        stats = {"linked": 0, "rejected": 0, "commands": 0, "purged": 0}

        channels = {self.channel: self.history(self.channel)}

        if self.command_channel:
            channels[self.command_channel] = self.history(self.command_channel)

        if purge:
            channels, stats["purged"] = self.purge_departed(channels)

        linked = self.linked_tags(channels[self.channel])

        # (cursor key, channel, accepts tags, accepts commands)
        scans = [("link", self.channel, True, not self.command_channel)]

        if self.command_channel:
            scans.append(("command", self.command_channel, False, True))

        for key, channel, allow_links, allow_commands in scans:
            last = int(cursors.get(key) or 0)

            for message in reversed(channels[channel]):  # oldest first
                if int(message["id"]) <= last:
                    continue

                if not message["author"].get("bot"):
                    self._handle(channel, message, linked, stats,
                                 allow_links, allow_commands)

                last = int(message["id"])
                save_cursor(key, str(last))

        return stats

    def _handle(self, channel, message, linked, stats, allow_links, allow_commands):
        author = message["author"]["id"]
        content = message.get("content", "")

        command = parse_command(content)

        if command:
            if allow_commands:
                self._command(channel, message, command, linked)
                stats["commands"] += 1
            return

        if not allow_links:
            return

        if self._bot_reacted(message, OK) or self._bot_reacted(message, BAD):
            return  # already handled (cursor was lost)

        tags = extract_tags(content)

        if not tags:
            return

        mine = linked.setdefault(author, [])
        accepted = 0

        for tag in tags:
            if tag in mine:
                accepted += 1
                continue

            if len(mine) >= MAX_ACCOUNTS:
                break

            try:
                self.fetch_player(tag)
            except NotFound:
                continue

            mine.append(tag)
            accepted += 1

        self.react(channel, message, OK if accepted else BAD)
        stats["linked" if accepted else "rejected"] += 1

    def _command(self, channel, message, command, linked):
        kind, mentioned = command
        target = mentioned or message["author"]["id"]
        tags = linked.get(target)

        if not tags:
            # No mention of the target: a reply naming someone could outlive them
            # if they leave, and they would have no messages left to trigger a purge.
            who = "You haven't" if target == message["author"]["id"] else "That member hasn't"
            self.reply(channel, message,
                       f"{who} linked a Brawl Stars account yet. "
                       "Post your tag (e.g. `#2PP0`) in the link channel.")
            return

        try:
            player = self.fetch_player(tags[0])
        except NotFound:
            self.reply(channel, message, "That linked tag no longer exists.")
            return

        build = profile_embed if kind == "profile" else stats_embed
        self.reply(channel, message, embed=build(player, target))
