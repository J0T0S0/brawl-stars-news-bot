import os
import sys
import unittest
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import links

BOT = "999"
LINK = "chan"
CMD = "cmd"
PLAYER = {
    "tag": "#2PP0", "name": "Tester", "trophies": 12345, "highestTrophies": 15000,
    "expLevel": 200, "club": {"name": "Club"},
    "brawlers": [{"name": "EL PRIMO", "power": 9, "rank": 25,
                  "trophies": 700, "highestTrophies": 800}],
}


NAMES = {"1": "Alice", "2": "Bob", "3": "alicia"}


class FakeDiscord:
    """In-memory guild with channels, speaking the same (method, path) API."""

    def __init__(self, members):
        self.members = set(members)
        self.messages = []     # oldest first, each tagged with its channel
        self.next_id = 100
        self.reactions = {}    # message id -> [emoji]
        self.deleted = []
        self.guild_code = None

    def post(self, author, content, channel=LINK, **extra):
        self.next_id += 1
        message = {"id": str(self.next_id), "channel": channel,
                   "author": {"id": author, "bot": author == BOT,
                              "username": NAMES.get(author, f"user{author}")},
                   "content": content, "embeds": [], "edited_timestamp": None, **extra}
        self.messages.append(message)
        return message

    def view(self, message):
        reactions = [{"emoji": {"name": e}, "me": True}
                     for e in self.reactions.get(message["id"], [])]
        return {**message, "reactions": reactions}

    def in_channel(self, channel):
        return [m for m in self.messages if m["channel"] == channel]

    def __call__(self, method, path, payload=None):
        if method == "GET" and "/messages" in path:
            channel = path.split("/channels/")[1].split("/")[0]
            newest_first = [self.view(m) for m in reversed(self.in_channel(channel))]
            if "before=" in path:
                before = int(path.split("before=")[1])
                newest_first = [m for m in newest_first if int(m["id"]) < before]
            return 200, newest_first[:100]

        if method == "GET" and "/members/" in path:
            uid = path.rsplit("/", 1)[1]
            if self.guild_code:
                return 404, {"code": self.guild_code}
            return (200, {}) if uid in self.members else (404, {"code": 10007})

        if method == "PUT" and "/reactions/" in path:
            mid = path.split("/messages/")[1].split("/")[0]
            emoji = path.split("/reactions/")[1].split("/")[0]
            self.reactions.setdefault(mid, []).append(unquote(emoji))
            return 204, None

        if method == "POST":
            channel = path.split("/channels/")[1].split("/")[0]
            self.post(BOT, payload.get("content", ""), channel=channel,
                      embeds=payload.get("embeds", []),
                      reply_to=payload["message_reference"]["message_id"])
            return 200, {}

        if method == "DELETE":
            mid = path.rsplit("/", 1)[1]
            self.messages = [m for m in self.messages if m["id"] != mid]
            self.deleted.append(mid)
            return 204, None

        raise AssertionError(f"unexpected call {method} {path}")


def make(discord, command_channel=None, known=("2PP0", "8Q2L", "9YY", "PPP")):
    def fetch(tag):
        if tag not in known:
            raise links.NotFound(tag)
        return {**PLAYER, "tag": "#" + tag}

    return links.LinkScanner(discord, fetch, LINK, "guild",
                             command_channel_id=command_channel, pause=0)


def run(scanner, cursors=None, purge=True):
    """Returns (stats, cursors) so a following run can resume."""
    cursors = dict(cursors or {})

    def save(key, value):
        cursors[key] = value

    stats = scanner.run(cursors, save, purge=purge)
    return stats, cursors


def bot_replies(d, channel=None):
    return [m for m in d.messages if m["author"]["bot"]
            and (channel is None or m["channel"] == channel)]


class ParsingTests(unittest.TestCase):

    def test_extract_tags(self):
        self.assertEqual(links.extract_tags("my tag is #2pp0!"), ["2PP0"])
        self.assertEqual(links.extract_tags("2PPO"), ["2PP0"])
        self.assertEqual(links.extract_tags("#2PP0 and #8Q2L"), ["2PP0", "8Q2L"])
        self.assertEqual(links.extract_tags("#2PP0 #2pp0"), ["2PP0"])
        self.assertEqual(links.extract_tags("hello everyone"), [])
        self.assertEqual(links.extract_tags("#ABC"), [])       # invalid characters
        self.assertEqual(links.extract_tags("lol this is cool"), [])
        self.assertEqual(len(links.extract_tags("#PPP #YYY #LLL #QQQ #GGG")), 3)

    def test_commands(self):
        self.assertEqual(links.parse_command("!profile"), ("profile", None))
        self.assertEqual(links.parse_command("!STATS <@123>"), ("stats", ("id", "123")))
        self.assertEqual(links.parse_command("!profile <@!42>"), ("profile", ("id", "42")))
        self.assertEqual(links.parse_command("!profile Alice"), ("profile", ("name", "Alice")))
        self.assertEqual(links.parse_command("!profile  @alice smith "),
                         ("profile", ("name", "alice smith")))
        self.assertIsNone(links.parse_command("profile"))
        self.assertIsNone(links.parse_command("!profilex"))


class LinkingTests(unittest.TestCase):

    def test_links_valid_tag_and_rejects_unknown(self):
        d = FakeDiscord({"1", "2"})
        good = d.post("1", "#2PP0")
        bad = d.post("2", "#QQQQ")
        d.post("2", "just chatting")

        stats, cursors = run(make(d))

        self.assertEqual(d.reactions[good["id"]], [links.OK])
        self.assertEqual(d.reactions[bad["id"]], [links.BAD])
        self.assertEqual((stats["linked"], stats["rejected"]), (1, 1))
        self.assertEqual(cursors["link"], d.messages[-1]["id"])

    def test_cursor_prevents_reprocessing(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        scanner = make(d)
        _, cursors = run(scanner)
        run(scanner, cursors)
        self.assertEqual(sum(len(v) for v in d.reactions.values()), 1)

    def test_lost_cursor_does_not_double_react(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        run(make(d))
        run(make(d))  # cursor lost
        self.assertEqual(list(d.reactions.values()), [[links.OK]])

    def test_account_limit(self):
        d = FakeDiscord({"1"})
        for tag in ("2PP0", "8Q2L", "9YY", "PPP"):
            d.post("1", f"#{tag}")
        run(make(d))
        self.assertEqual(d.reactions[d.messages[-1]["id"]], [links.BAD])

    def test_api_outage_keeps_message_for_retry(self):
        d = FakeDiscord({"1"})
        first = d.post("1", "#2PP0")

        def down(tag):
            raise links.Unavailable("maintenance")

        scanner = links.LinkScanner(d, down, LINK, "guild", pause=0)
        saved = []
        with self.assertRaises(links.Unavailable):
            scanner.run({}, lambda k, v: saved.append(v))

        self.assertEqual(saved, [])
        stats, _ = run(make(d))
        self.assertEqual(stats["linked"], 1)
        self.assertEqual(d.reactions[first["id"]], [links.OK])

    def test_bots_are_ignored(self):
        d = FakeDiscord({"1"})
        d.post(BOT, "#2PP0")
        stats, _ = run(make(d))
        self.assertEqual(stats["linked"], 0)
        self.assertEqual(d.reactions, {})

    def test_deleting_your_message_unlinks(self):
        d = FakeDiscord({"1", "2"})
        mine = d.post("1", "#2PP0")
        scanner = make(d)
        _, cursors = run(scanner)
        d.messages.remove(mine)
        d.post("2", "!profile <@1>")
        run(scanner, cursors)
        self.assertIn("hasn't linked", d.messages[-1]["content"])


class CommandTests(unittest.TestCase):

    def test_edited_messages_do_not_count_as_links(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#8Q2L", edited_timestamp="2026-01-01T00:00:00")
        d.reactions["101"] = [links.OK]
        d.post("2", "!profile <@1>")
        run(make(d))
        self.assertIn("hasn't linked", d.messages[-1]["content"])

    def test_profile_and_stats_commands(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        d.post("2", "!profile <@1>")
        d.post("2", "!stats")           # 2 has no link
        d.post("1", "!stats")
        run(make(d))

        replies = bot_replies(d)
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["embeds"][0]["title"], "Tester")
        self.assertIn("id:1", replies[0]["embeds"][0]["footer"]["text"])
        self.assertIn("haven't linked", replies[1]["content"])
        self.assertIn("El Primo", replies[2]["embeds"][0]["description"])

    def test_separate_command_channel(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")                               # link channel
        d.post("2", "!profile <@1>", channel=CMD)          # command channel
        d.post("2", "!profile <@1>", channel=LINK)         # ignored: wrong channel
        d.post("2", "#8Q2L", channel=CMD)                  # tags ignored in commands
        stats, _ = run(make(d, command_channel=CMD))

        self.assertEqual(stats["commands"], 1)
        self.assertEqual(stats["linked"], 1)
        self.assertEqual(len(bot_replies(d, CMD)), 1)
        self.assertEqual(bot_replies(d, CMD)[0]["embeds"][0]["title"], "Tester")
        self.assertEqual(bot_replies(d, LINK), [])         # no reply in link channel
        cmd_tag_message = d.in_channel(CMD)[1]
        self.assertNotIn(cmd_tag_message["id"], d.reactions)

    def test_command_channel_has_its_own_cursor(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        d.post("1", "!stats", channel=CMD)
        scanner = make(d, command_channel=CMD)
        _, cursors = run(scanner)
        self.assertIn("command", cursors)
        run(scanner, cursors)
        self.assertEqual(len(bot_replies(d, CMD)), 1)      # not answered twice


class NameLookupTests(unittest.TestCase):

    def setUp(self):
        self.d = FakeDiscord({"1", "2", "3"})
        self.d.post("1", "#2PP0")
        self.d.post("3", "#8Q2L")
        self.scanner = make(self.d)

    def ask(self, text):
        self.d.post("2", text)
        run(self.scanner)
        return bot_replies(self.d)[-1]

    def test_lookup_by_name_without_mentioning(self):
        reply = self.ask("!profile alice")          # exact beats "alicia"
        self.assertEqual(reply["embeds"][0]["title"], "Tester")
        self.assertIn("id:1", reply["embeds"][0]["footer"]["text"])

    def test_at_sign_is_optional_and_case_insensitive(self):
        reply = self.ask("!stats @ALICE")
        self.assertIn("El Primo", reply["embeds"][0]["description"])

    def test_prefix_match_and_ambiguity(self):
        self.assertIn("Several linked members match",
                      self.ask("!profile ali")["content"])
        self.assertIn("Alice", self.ask("!profile ali")["content"])

    def test_unknown_name(self):
        self.assertIn("No linked member matches", self.ask("!profile nobody")["content"])

    def test_me_means_yourself(self):
        self.d.post("2", "!profile me")
        run(self.scanner)
        self.assertIn("haven't linked", bot_replies(self.d)[-1]["content"])

    def test_replies_never_ping(self):
        sent = []
        original = self.d.__call__

        def spy(method, path, payload=None):
            if method == "POST":
                sent.append(payload)
            return original(method, path, payload)

        scanner = make(self.d)
        scanner.api = spy
        self.d.post("2", "!profile alice")
        scanner.run({}, lambda k, v: None)
        self.assertEqual(sent[0]["allowed_mentions"],
                         {"parse": [], "replied_user": False})


class CleanupTests(unittest.TestCase):

    def setUp(self):
        self.d = FakeDiscord({"1", "2"})
        self.ok = self.d.post("1", "#2PP0")
        self.junk = self.d.post("2", "testidfake")
        self.bad = self.d.post("2", "#QQQQ")
        self.chat = self.d.post("2", "hello there")
        self.scanner = make(self.d, command_channel=CMD)
        _, self.cursors = run(self.scanner)   # first pass: processes, nothing old enough

    def test_stray_messages_removed_after_processing_but_links_kept(self):
        stats, _ = run(self.scanner, self.cursors)
        remaining = {m["id"] for m in self.d.in_channel(LINK)}
        self.assertEqual(remaining, {self.ok["id"]})
        self.assertEqual(stats["cleaned"], 3)

    def test_grace_period_lets_people_see_the_reaction(self):
        self.scanner.now = lambda: links.snowflake_ms(self.chat["id"]) / 1000 + 30
        stats, _ = run(self.scanner, self.cursors)
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(len(self.d.in_channel(LINK)), 4)

    def test_unprocessed_messages_are_never_deleted(self):
        stats, _ = run(self.scanner, {"link": self.ok["id"]})   # cursor stops early
        self.assertEqual(stats["cleaned"], 0)

    def test_edited_links_are_removed(self):
        self.ok["edited_timestamp"] = "2026-01-01T00:00:00"
        stats, _ = run(self.scanner, self.cursors)
        self.assertEqual(stats["cleaned"], 4)

    def test_only_with_a_command_channel_and_when_enabled(self):
        plain = make(self.d)                       # no command channel
        stats, _ = run(plain, self.cursors)
        self.assertEqual(stats["cleaned"], 0)

        off = make(self.d, command_channel=CMD)
        off.cleanup = False
        stats, _ = run(off, self.cursors)
        self.assertEqual(stats["cleaned"], 0)


class PrivacyTests(unittest.TestCase):

    def test_departed_members_messages_and_replies_are_deleted(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        d.post("2", "#8Q2L")
        d.post("2", "!profile <@1>")        # asks about 1, stays
        d.post("2", "!profile")             # asks about itself
        scanner = make(d)
        _, cursors = run(scanner)
        before = len(d.messages)

        d.members.discard("1")              # 1 leaves
        stats, _ = run(scanner, cursors)

        # Nothing the bot posted may still reference the leaver. (Other
        # members' own messages that merely mention them are left alone.)
        bot_text = " ".join(
            m["content"] + str(m["embeds"]) for m in d.messages if m["author"]["bot"]
        )
        self.assertNotIn("id:1", bot_text)
        self.assertNotIn("<@1>", bot_text)
        self.assertFalse(any(m["author"]["id"] == "1" for m in d.messages))
        self.assertTrue(any(m["author"]["id"] == "2" for m in d.messages))
        self.assertEqual(stats["purged"], 2)  # their message + the bot reply about them
        self.assertLess(len(d.messages), before)

    def test_leaver_is_purged_from_command_channel_too(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        d.post("2", "!profile <@1>", channel=CMD)
        d.post("1", "!stats", channel=CMD)
        scanner = make(d, command_channel=CMD)
        _, cursors = run(scanner)
        self.assertEqual(len(bot_replies(d, CMD)), 2)

        d.members.discard("1")
        stats, _ = run(scanner, cursors)

        self.assertEqual([m["author"]["id"] for m in d.in_channel(CMD)], ["2"])
        self.assertEqual(stats["purged"], 4)  # link msg, their command, 2 bot replies

    def test_leaver_cannot_be_looked_up_afterwards(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        scanner = make(d)
        _, cursors = run(scanner)
        d.members.discard("1")
        d.post("2", "!profile <@1>")
        run(scanner, cursors)
        self.assertIn("hasn't linked", d.messages[-1]["content"])

    def test_errors_never_purge(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        scanner = make(d)
        run(scanner)
        d.guild_code = 10004          # unknown guild: misconfiguration
        stats, _ = run(scanner)
        self.assertEqual(stats["purged"], 0)
        self.assertEqual(d.deleted, [])


if __name__ == "__main__":
    unittest.main()
