import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import links

BOT = "999"
PLAYER = {
    "tag": "#2PP0", "name": "Tester", "trophies": 12345, "highestTrophies": 15000,
    "expLevel": 200, "club": {"name": "Club"},
    "brawlers": [{"name": "EL PRIMO", "power": 9, "rank": 25,
                  "trophies": 700, "highestTrophies": 800}],
}


class FakeDiscord:
    """In-memory channel + guild, speaking the same (method, path) API."""

    def __init__(self, members):
        self.members = set(members)
        self.messages = []     # oldest first
        self.next_id = 100
        self.reactions = {}    # message id -> [emoji]
        self.deleted = []
        self.guild_code = None

    def post(self, author, content, **extra):
        self.next_id += 1
        message = {"id": str(self.next_id), "author": {"id": author, "bot": author == BOT},
                   "content": content, "embeds": [], "edited_timestamp": None, **extra}
        self.messages.append(message)
        return message

    def view(self, message):
        reactions = [{"emoji": {"name": e}, "me": True}
                     for e in self.reactions.get(message["id"], [])]
        return {**message, "reactions": reactions}

    def __call__(self, method, path, payload=None):
        if method == "GET" and "/messages" in path:
            newest_first = [self.view(m) for m in reversed(self.messages)]
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
            from urllib.parse import unquote
            self.reactions.setdefault(mid, []).append(unquote(emoji))
            return 204, None

        if method == "POST":
            self.post(BOT, payload.get("content", ""), embeds=payload.get("embeds", []),
                      reply_to=payload["message_reference"]["message_id"])
            return 200, {}

        if method == "DELETE":
            mid = path.rsplit("/", 1)[1]
            self.messages = [m for m in self.messages if m["id"] != mid]
            self.deleted.append(mid)
            return 204, None

        raise AssertionError(f"unexpected call {method} {path}")


def make(discord, known=("2PP0", "8Q2L", "9YY", "PPP")):
    def fetch(tag):
        if tag not in known:
            raise links.NotFound(tag)
        return {**PLAYER, "tag": "#" + tag}

    return links.LinkScanner(discord, fetch, "chan", "guild", pause=0)


def run(scanner, cursor=None):
    saved = []
    stats = scanner.run(cursor, saved.append)
    return stats, saved


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
        self.assertEqual(links.parse_command("!STATS <@123>"), ("stats", "123"))
        self.assertEqual(links.parse_command("!profile <@!42>"), ("profile", "42"))
        self.assertIsNone(links.parse_command("profile"))
        self.assertIsNone(links.parse_command("!profilex"))


class ScannerTests(unittest.TestCase):

    def test_links_valid_tag_and_rejects_unknown(self):
        d = FakeDiscord({"1", "2"})
        good = d.post("1", "#2PP0")
        bad = d.post("2", "#QQQQ")
        d.post("2", "just chatting")

        stats, saved = run(make(d))

        self.assertEqual(d.reactions[good["id"]], [links.OK])
        self.assertEqual(d.reactions[bad["id"]], [links.BAD])
        self.assertEqual((stats["linked"], stats["rejected"]), (1, 1))
        self.assertEqual(saved[-1], d.messages[-1]["id"])  # cursor advanced

    def test_cursor_prevents_reprocessing(self):
        d = FakeDiscord({"1"})
        d.post("1", "#2PP0")
        scanner = make(d)
        _, saved = run(scanner)
        run(scanner, saved[-1])
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
        scanner = make(d)
        run(scanner)
        last = d.messages[-1]["id"]
        self.assertEqual(d.reactions[last], [links.BAD])

    def test_api_outage_keeps_message_for_retry(self):
        d = FakeDiscord({"1"})
        first = d.post("1", "#2PP0")

        def down(tag):
            raise links.Unavailable("maintenance")

        scanner = links.LinkScanner(d, down, "chan", "guild", pause=0)
        saved = []
        with self.assertRaises(links.Unavailable):
            scanner.run(None, saved.append)

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

    def test_edited_messages_do_not_count_as_links(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#8Q2L", edited_timestamp="2026-01-01T00:00:00")
        d.reactions["101"] = [links.OK]
        d.post("2", "!profile <@1>")
        run(make(d))
        reply = d.messages[-1]
        self.assertIn("hasn't linked", reply["content"])

    def test_profile_and_stats_commands(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        d.post("2", "!profile <@1>")
        d.post("2", "!stats")           # 2 has no link
        d.post("1", "!stats")
        run(make(d))

        replies = [m for m in d.messages if m["author"]["bot"]]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["embeds"][0]["title"], "Tester")
        self.assertIn("id:1", replies[0]["embeds"][0]["footer"]["text"])
        self.assertIn("haven't linked", replies[1]["content"])
        self.assertIn("El Primo", replies[2]["embeds"][0]["description"])

    def test_deleting_your_message_unlinks(self):
        d = FakeDiscord({"1", "2"})
        mine = d.post("1", "#2PP0")
        scanner = make(d)
        run(scanner)
        d.messages.remove(mine)
        d.post("2", "!profile <@1>")
        run(scanner, "101")
        self.assertIn("hasn't linked", d.messages[-1]["content"])


class PrivacyTests(unittest.TestCase):

    def test_departed_members_messages_and_replies_are_deleted(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        d.post("2", "#8Q2L")
        d.post("2", "!profile <@1>")        # asks about 1, stays
        d.post("2", "!profile")             # asks about itself
        scanner = make(d)
        _, saved = run(scanner)
        before = len(d.messages)

        d.members.discard("1")              # 1 leaves
        stats, _ = run(scanner, saved[-1])

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

    def test_leaver_cannot_be_looked_up_afterwards(self):
        d = FakeDiscord({"1", "2"})
        d.post("1", "#2PP0")
        scanner = make(d)
        run(scanner)
        d.members.discard("1")
        d.post("2", "!profile <@1>")
        run(scanner, d.messages[-2]["id"])
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
