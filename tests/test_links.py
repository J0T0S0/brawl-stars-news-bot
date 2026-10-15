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
        self.forbid_reactions = False

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
            if self.forbid_reactions:
                return 403, {"code": 50013}
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

    def test_extract_tag(self):
        et = links.extract_tag
        self.assertEqual(et("my tag is #2pp0!"), "2PP0")
        self.assertEqual(et("2PPO"), "2PP0")
        self.assertEqual(et("#2PP0 and #8Q2L"), "2PP0")        # first one counts
        self.assertEqual(et("#hello then #8Q2L"), "8Q2L")      # first *valid* one
        self.assertIsNone(et("hello everyone"))
        self.assertIsNone(et("#ABC"))                          # invalid characters
        self.assertIsNone(et("lol this is cool"))
        self.assertIsNone(et("testidfake"))
        self.assertIsNone(et(""))

    def test_commands(self):
        pc = links.parse_command
        self.assertEqual(pc("!profile"), ("profile", None))
        self.assertEqual(pc("!STATS <@123>"), ("stats", ("id", "123")))
        self.assertEqual(pc("!profile <@!42>"), ("profile", ("id", "42")))
        self.assertEqual(pc("!profile Alice"), ("profile", ("name", "Alice")))
        self.assertEqual(pc("!profile  @alice smith "), ("profile", ("name", "alice smith")))
        self.assertEqual(pc("!profile player4"), ("profile", ("name", "player4")))
        self.assertEqual(pc("!help"), ("help", None))
        self.assertIsNone(pc("profile"))
        self.assertIsNone(pc("!profilex"))


class LinkingTests(unittest.TestCase):

    def test_links_valid_tag_and_rejects_unknown(self):
        d = FakeDiscord({"1", "2"})
        good = d.post("1", "#2PP0")
        bad = d.post("2", "#QQQQ")
        fake = d.post("2", "testidfake")

        stats, cursors = run(make(d))

        self.assertEqual(d.reactions[good["id"]], [links.OK])
        self.assertEqual(d.reactions[bad["id"]], [links.BAD])
        self.assertEqual(d.reactions[fake["id"]], [links.BAD])   # not even a tag
        self.assertEqual((stats["linked"], stats["rejected"]), (1, 2))
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

    def test_one_account_per_member_new_tag_replaces_old(self):
        d = FakeDiscord({"1", "2"})
        first = d.post("1", "#2PP0")
        scanner = make(d)
        _, cursors = run(scanner)
        second = d.post("1", "#8Q2L")
        stats, cursors = run(scanner, cursors)

        self.assertEqual(stats["linked"], 1)
        ids = {m["id"] for m in d.in_channel(LINK)}
        self.assertNotIn(first["id"], ids)                 # old link removed
        self.assertIn(second["id"], ids)
        self.assertEqual(d.reactions[second["id"]], [links.OK])

        d.post("2", "!profile alice")
        run(scanner, cursors)
        self.assertEqual(bot_replies(d)[-1]["embeds"][0]["description"], "`#8Q2L`")

    def test_invalid_new_tag_keeps_the_old_link(self):
        d = FakeDiscord({"1"})
        first = d.post("1", "#2PP0")
        scanner = make(d)
        _, cursors = run(scanner)
        bad = d.post("1", "#QQQQ")
        run(scanner, cursors)
        self.assertIn(first["id"], {m["id"] for m in d.in_channel(LINK)})
        self.assertEqual(d.reactions[bad["id"]], [links.BAD])

    def test_only_the_first_tag_in_a_message_counts(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#QQQQ and #2PP0")                     # first tag unknown
        stats, _ = run(make(d))
        self.assertEqual((stats["linked"], stats["rejected"]), (0, 1))

    def test_relinking_the_same_tag_does_not_hit_the_api(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        calls = []

        def fetch(tag):
            calls.append(tag)
            return {**PLAYER, "tag": "#" + tag}

        scanner = links.LinkScanner(d, fetch, LINK, "guild", pause=0)
        _, cursors = run(scanner)
        d.post("1", "#2PP0")
        run(scanner, cursors)
        self.assertEqual(calls, ["2PP0"])

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


class CommandFeatureTests(unittest.TestCase):

    def setUp(self):
        self.d = FakeDiscord({"1", "2"})
        self.d.post("1", "#2PP0")
        self.scanner = make(self.d, command_channel=CMD)
        _, self.cursors = run(self.scanner)

    def ask(self, text, author="2"):
        self.d.post(author, text, channel=CMD)
        run(self.scanner, self.cursors)
        return bot_replies(self.d, CMD)[-1]

    def test_help(self):
        reply = self.ask("!help")
        self.assertIn("!profile", reply["content"])
        self.assertIn(f"<#{LINK}>", reply["content"])
        self.assertNotIn("1-3", reply["content"])

    def test_command_flood_is_capped(self):
        for _ in range(6):
            self.d.post("2", "!help", channel=CMD)
        stats, _ = run(self.scanner, self.cursors)
        self.assertEqual(stats["commands"], links.MAX_COMMANDS_PER_USER)

    def test_missing_permissions_are_reported(self):
        self.d.forbid_reactions = True
        self.d.post("2", "#QQQQ")
        run(self.scanner, self.cursors)
        self.assertTrue(any("403" in w for w in self.scanner.warnings))


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

    def test_without_command_channel_only_rejected_messages_go(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        fake = d.post("2", "testidfake")
        legacy = d.post("2", "old chatter")
        d.reactions[legacy["id"]] = []          # never processed by the bot
        d.messages.remove(legacy)
        d.messages.insert(0, legacy)            # older than the cursor
        scanner = make(d)
        cursors = {"link": str(d.next_id)}      # everything counts as handled

        d.reactions[fake["id"]] = [links.BAD]
        stats, _ = run(scanner, cursors)

        ids = {m["id"] for m in d.in_channel(LINK)}
        self.assertNotIn(fake["id"], ids)
        self.assertIn(legacy["id"], ids)        # unmarked chatter stays here
        self.assertEqual(stats["cleaned"], 1)

    def test_can_be_disabled(self):
        off = make(self.d, command_channel=CMD)
        off.cleanup = False
        stats, _ = run(off, self.cursors)
        self.assertEqual(stats["cleaned"], 0)


class LegacyLinksTests(unittest.TestCase):

    def test_several_marked_messages_newest_wins_and_old_ones_are_cleaned(self):
        d = FakeDiscord({"1"})
        old = d.post("1", "#2PP0")
        new = d.post("1", "#8Q2L")
        d.reactions[old["id"]] = [links.OK]
        d.reactions[new["id"]] = [links.OK]
        scanner = make(d, command_channel=CMD)
        cursors = {"link": new["id"]}

        d.post("1", "!profile", channel=CMD)
        stats, _ = run(scanner, cursors)

        self.assertEqual(bot_replies(d, CMD)[-1]["embeds"][0]["description"], "`#8Q2L`")
        self.assertEqual({m["id"] for m in d.in_channel(LINK)}, {new["id"]})
        self.assertEqual(stats["cleaned"], 1)


class ContentIntentTests(unittest.TestCase):

    def test_unreadable_messages_are_reported_not_rejected(self):
        d = FakeDiscord({"1"})
        d.post("1", "")                         # intent off: empty content
        d.post("1", "", attachments=[{"id": "1"}])
        stats, _ = run(make(d))
        self.assertEqual(stats["blind"], 1)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(d.reactions, {})


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
