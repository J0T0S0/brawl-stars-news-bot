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
_COMMAND = re.compile(r"^\s*!(profile|stats|help)\b[ \t]*(.*)$", re.I | re.S)
MAX_COMMANDS_PER_USER = 3          # per run, so nobody can flood the API
_MENTION = re.compile(r"^<@!?(\d+)>$")
CLEANUP_GRACE_SECONDS = 600
DISCORD_EPOCH_MS = 1420070400000


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
    """-> (kind, target, index) or None.

    target is None (yourself), ("id", discord_id) for an @mention, or
    ("name", text). index (1-3) picks one of a member's linked accounts.
    """

    match = _COMMAND.match(content or "")

    if not match:
        return None

    kind = match.group(1).lower()
    tokens = match.group(2).split()
    index = None

    if tokens and tokens[-1] in ("1", "2", "3"):
        index = int(tokens.pop())

    if not tokens:
        return kind, None, index

    mention = _MENTION.match(tokens[0])

    if mention:
        return kind, ("id", mention.group(1)), index

    # A plain name needs no @-mention, so nobody gets a notification.
    return kind, ("name", " ".join(tokens).lstrip("@").strip()[:64]), index


def snowflake_ms(message_id):
    return (int(message_id) >> 22) + DISCORD_EPOCH_MS


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
                 command_channel_id=None, pause=0.3, cleanup=True):
        self.api = api
        self.fetch_player = fetch_player
        self.channel = channel_id
        self.command_channel = command_channel_id or None
        self.guild = guild_id
        self.pause = pause
        self.cleanup = cleanup
        self.now = time.time
        self._left = {}
        self._names = {}
        self._command_counts = {}
        self.warnings = set()

    # -- Discord helpers --------------------------------------

    def _call(self, method, path, payload=None):
        status, data = self.api(method, path, payload)

        # 404 just means the message is already gone. Anything else (almost
        # always 403) is a setup problem worth surfacing.
        if status >= 400 and status != 404:
            kind = re.sub(r"\d+", "#", path.split("?")[0])
            self.warnings.add(f"{method} {kind} returned HTTP {status}")

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
            "allowed_mentions": {"parse": [], "replied_user": False},
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

    def _remember_name(self, author):
        self._names[author["id"]] = [
            n for n in (author.get("global_name"), author.get("username")) if n
        ]

    def linked_tags(self, messages):
        """{discord_id: [tags]} from messages carrying the bot's check mark.

        Edited messages are ignored: an edit could swap in a tag that was never
        validated, so editing effectively unlinks.
        """

        linked = {}
        self._names = {}

        for message in reversed(messages):  # oldest first
            if message["author"].get("bot") or message.get("edited_timestamp"):
                continue

            if not self._bot_reacted(message, OK):
                continue

            author = message["author"]
            tags = linked.setdefault(author["id"], [])
            self._remember_name(author)

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

    def clean_link_channel(self, messages, cursor):
        """Deletes members' messages in the link channel that are not a live
        link. Messages the bot marked with a cross always qualify; with a
        separate command channel the channel is tags-only, so anything else
        that isn't a live link (old chatter, edited links) goes too. Only
        messages the bot has already processed and that are older than a grace
        period are touched, so people still see the reaction. Returns the
        number deleted.
        """

        last = int(cursor or 0)
        now_ms = self.now() * 1000
        removed = 0

        for message in messages:
            message_id = int(message["id"])

            live_link = self._bot_reacted(message, OK) and not message.get("edited_timestamp")
            rejected = self._bot_reacted(message, BAD)

            if (
                message["author"].get("bot")
                or live_link
                or not (rejected or self.command_channel)
                or message_id > last
                or now_ms - snowflake_ms(message_id) < CLEANUP_GRACE_SECONDS * 1000
            ):
                continue

            self.delete(self.channel, message["id"])
            removed += 1

        return removed

    # -- main -------------------------------------------------

    def run(self, cursors, save_cursor, purge=True):
        """Processes new messages in the link (and command) channel.

        `cursors` maps "link"/"command" to the last handled message id and
        `save_cursor(key, id)` persists progress. Returns counters.
        """

        stats = {"linked": 0, "rejected": 0, "commands": 0, "purged": 0,
                 "cleaned": 0, "blind": 0}
        self._command_counts = {}
        self.warnings = set()

        channels = {self.channel: self.history(self.channel)}

        if self.command_channel:
            channels[self.command_channel] = self.history(self.command_channel)

        if purge:
            channels, stats["purged"] = self.purge_departed(channels)

        linked = self.linked_tags(channels[self.channel])

        if purge and self.cleanup:
            stats["cleaned"] = self.clean_link_channel(
                channels[self.channel], cursors.get("link")
            )

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
                used = self._command_counts.get(author, 0)
                self._command_counts[author] = used + 1

                if used < MAX_COMMANDS_PER_USER:
                    self._command(channel, message, command, linked)
                    stats["commands"] += 1
            return

        if not allow_links:
            return

        if self._bot_reacted(message, OK) or self._bot_reacted(message, BAD):
            return  # already handled (cursor was lost)

        tags = extract_tags(content)

        if not tags:
            if content.strip():
                # Not a valid tag (e.g. "testidfake"): mark it so people see why.
                self.react(channel, message, BAD)
                stats["rejected"] += 1
            elif not (message.get("attachments") or message.get("embeds")
                      or message.get("sticker_items")):
                # Empty text with nothing attached: the bot cannot read message
                # content (Message Content Intent is off).
                stats["blind"] += 1
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

        if accepted:
            self._remember_name(message["author"])

        self.react(channel, message, OK if accepted else BAD)
        stats["linked" if accepted else "rejected"] += 1

    def display_name(self, user_id):
        return (self._names.get(user_id) or [user_id])[0]

    def find_members(self, query, linked):
        """Linked members whose name matches, best match tier first
        (exact, then starts-with, then contains)."""

        query = query.casefold()

        def names(uid):
            return [n.casefold() for n in self._names.get(uid, [])]

        for matches in (
            lambda n: n == query,
            lambda n: n.startswith(query),
            lambda n: query in n,
        ):
            found = [uid for uid in linked if any(matches(n) for n in names(uid))]

            if found:
                return found

        return []

    def help_text(self):
        where = f"<#{self.channel}>"
        return (
            "**Brawl Stars account linking**\n"
            f"Post your player tag (e.g. `#2PP0`) in {where} to link it "
            f"(up to {MAX_ACCOUNTS} accounts). Delete the message to unlink.\n\n"
            "`!profile [name] [1-3]` shows a linked player's profile\n"
            "`!stats [name] [1-3]` shows their top brawlers\n"
            "Use the member's Discord name, no @ needed (nobody gets pinged). "
            "Leave the name out for yourself; the number picks one of "
            "their linked accounts."
        )

    def _command(self, channel, message, command, linked):
        kind, argument, index = command

        if kind == "help":
            self.reply(channel, message, self.help_text())
            return

        target = message["author"]["id"]

        if argument and argument[0] == "id":
            target = argument[1]
        elif argument and argument[1].casefold() not in ("me", "myself"):
            matches = self.find_members(argument[1], linked)

            if not matches:
                self.reply(channel, message,
                           f"No linked member matches **{argument[1]}**. "
                           "Use their Discord name, no @ needed.")
                return

            if len(matches) > 1:
                names = ", ".join(self.display_name(uid) for uid in matches[:8])
                self.reply(channel, message,
                           f"Several linked members match: {names}. Be more specific.")
                return

            target = matches[0]

        tags = linked.get(target)

        if not tags:
            # Never name the target: a reply naming someone could outlive them
            # if they leave, and they would have no messages left to trigger a purge.
            who = "You haven't" if target == message["author"]["id"] else "That member hasn't"
            self.reply(channel, message,
                       f"{who} linked a Brawl Stars account yet. "
                       f"Post your tag (e.g. `#2PP0`) in <#{self.channel}>.")
            return

        position = (index or 1) - 1

        if position >= len(tags):
            self.reply(channel, message,
                       f"That member has only {len(tags)} linked account"
                       f"{'s' if len(tags) != 1 else ''}.")
            return

        try:
            player = self.fetch_player(tags[position])
        except NotFound:
            self.reply(channel, message, "That linked tag no longer exists.")
            return

        build = profile_embed if kind == "profile" else stats_embed
        self.reply(channel, message, embed=build(player, target))
