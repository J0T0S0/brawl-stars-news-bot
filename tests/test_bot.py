import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


ARCHIVE = """
<a href="/en/games/brawlstars/blog/">Blog</a>
<a href="/en/games/brawlstars/blog/page/2/">Next</a>
<a href="/en/games/brawlstars/blog/new-brawler/?utm=x#top"> New   Brawler </a>
<a href="/en/games/brawlstars/blog/new-brawler/">New Brawler</a>
<a href="/en/games/brawlstars/blog/balance-changes-may/">Balance &amp; Changes</a>
<a href="/en/games/brawlstars/blog/empty/"></a>
<a href="/other/">Other</a>
"""

ARTICLE = """
<meta property="og:title" content="T &amp; U">
<meta property="og:description" content="Desc &amp; more">
<meta property="og:image" content="/img/a.png">
<meta property="article:published_time" content="2026-01-01T00:00:00Z">
"""

FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <yt:videoId>abc</yt:videoId><title>Vid</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
    <published>2026-01-01T00:00:00+00:00</published>
    <media:group><media:description>Hello</media:description>
    <media:thumbnail url="https://i.ytimg.com/vi/abc/hq.jpg"/></media:group>
  </entry>
  <entry>
    <yt:videoId>sh</yt:videoId><title>Short</title>
    <link rel="alternate" href="https://www.youtube.com/shorts/sh"/>
  </entry>
</feed>"""


class ParsingTests(unittest.TestCase):

    def test_news_parsing(self):
        with mock.patch.object(bot, "get", return_value=ARCHIVE.encode()):
            items = bot.get_news()
        self.assertEqual(
            [i["title"] for i in items],
            ["New Brawler", "Balance & Changes"],
        )
        self.assertTrue(items[0]["id"].endswith("/new-brawler/"))

    def test_article_meta(self):
        with mock.patch.object(bot, "get", return_value=ARTICLE.encode()):
            meta = bot.get_article_meta("https://supercell.com/en/x/")
        self.assertEqual(meta["description"], "Desc & more")
        self.assertEqual(meta["image"], "https://supercell.com/img/a.png")

    def test_article_meta_failure(self):
        with mock.patch.object(bot, "get", side_effect=OSError("boom")):
            self.assertEqual(bot.get_article_meta("https://x/"), {})

    def test_youtube_parsing(self):
        with mock.patch.object(bot, "get", return_value=FEED):
            videos = bot.get_youtube()
        self.assertEqual([v["id"] for v in videos], ["abc", "sh"])
        self.assertFalse(videos[0]["is_short"])
        self.assertTrue(videos[1]["is_short"])
        self.assertEqual(videos[0]["description"], "Hello")

    def test_classify(self):
        self.assertEqual(bot.classify_news("Balance Changes"), "balance")
        self.assertEqual(bot.classify_news("New Brawler!"), "news")

    def test_shorten(self):
        self.assertEqual(bot.shorten("a  b"), "a b")
        self.assertLessEqual(len(bot.shorten("word " * 200)), 350)


class StateTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(
            bot, "STATE_FILE", os.path.join(self.dir.name, "state.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remember_keeps_order_and_bounds(self):
        state = {}
        for i in range(bot.MAX_HISTORY + 50):
            bot.remember(state, "k", str(i))
        bot.remember(state, "k", "100")
        self.assertEqual(len(state["k"]), bot.MAX_HISTORY)
        self.assertEqual(state["k"][-1], "100")
        self.assertEqual(state["k"].count("100"), 1)

    def test_corrupt_state_aborts(self):
        with open(bot.STATE_FILE, "w") as f:
            f.write("{nope")
        with self.assertRaises(SystemExit):
            bot.load_state()

    def test_first_run_records_without_sending(self):
        items = [{"id": "a", "title": "A", "url": "u"}]
        state = {}
        with mock.patch.object(bot, "get_news", return_value=items), \
                mock.patch.object(bot, "send_discord") as send:
            self.assertEqual(bot.process_news(state, "w"), 0)
        send.assert_not_called()
        self.assertEqual(state["news"], ["a"])

    def test_sends_only_new_oldest_first_and_saves_progress(self):
        items = [
            {"id": "c", "title": "C", "url": "c"},
            {"id": "b", "title": "B", "url": "b"},
            {"id": "a", "title": "A", "url": "a"},
        ]
        state = {"news": ["a"]}
        calls = []

        def fake_send(webhook, title, **kw):
            if title == "C":
                raise RuntimeError("discord down")
            calls.append(title)

        with mock.patch.object(bot, "get_news", return_value=items), \
                mock.patch.object(bot, "get_article_meta", return_value={}), \
                mock.patch.object(bot, "send_discord", side_effect=fake_send):
            with self.assertRaises(RuntimeError):
                bot.process_news(state, "w")

        self.assertEqual(calls, ["B"])
        self.assertEqual(state["news"], ["a", "b"])  # C will retry next run
        self.assertEqual(bot.load_state()["news"], ["a", "b"])

    def test_youtube_short_category(self):
        videos = [{
            "id": "s", "title": "S", "url": "u", "is_short": True,
            "description": "", "image": None, "published": None,
        }]
        state = {"youtube": ["old"]}
        with mock.patch.object(bot, "get_youtube", return_value=videos), \
                mock.patch.object(bot, "send_discord") as send:
            bot.process_youtube(state, "w")
        self.assertEqual(send.call_args.kwargs["category"], "short")


class DiscordTests(unittest.TestCase):

    def test_payload(self):
        captured = {}

        def fake(request, timeout=30):
            captured["body"] = request.data
            return 204, b""

        with mock.patch.object(bot, "request_with_retry", fake), \
                mock.patch.object(bot.time, "sleep"):
            bot.send_discord("https://w", "T" * 500, "d" * 5000, "https://u", "news")
        import json
        embed = json.loads(captured["body"])["embeds"][0]
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 2048)


if __name__ == "__main__":
    unittest.main()
