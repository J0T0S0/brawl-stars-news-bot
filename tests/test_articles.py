import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import articles


def text(value, *marks):
    return {"nodeType": "text", "value": value,
            "marks": [{"type": m} for m in marks]}


def para(*tokens):
    return {"nodeType": "paragraph", "content": list(tokens)}


def page(blocks, title="Release Notes"):
    props = {"title": title, "publishDate": "2026-01-01T00:00:00Z",
             "bodyCollection": [
                 {"__typename": "TextBlock", "title": t,
                  "text": {"json": {"content": c}}} for t, c in blocks]}
    data = {"props": {"pageProps": props}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


BALANCE = [
    para(text("Intro sentence.")),
    para(text("NERFS", "bold"), text("\n"), text("SHADE", "underline"),
         text("\nGadget 'X' - cooldown increased from 18s → 20s\nHealth reduced from 100 to 90")),
    para(text("BUFFS", "bold"), text("\n"), text("POCO", "underline"),
         text("\nHealing increased from 500 → 700")),
    {"nodeType": "heading-3", "content": [text("Buffs", "bold")]},
    para(text("Bea", "underline", "bold")),
    {"nodeType": "unordered-list", "content": [
        {"nodeType": "list-item", "content": [para(text("Super Damage increased from 100 to 130."))]}]},
    para(text("BUG FIXES", "bold"), text("\n• Fixed a thing\n• Fixed another")),
]


class ParseTests(unittest.TestCase):

    def setUp(self):
        self.article = articles.parse_article(
            page([("Maintenance", BALANCE), ("Skins", [para(text("Hello"))])])
        )

    def test_structure(self):
        self.assertEqual(self.article["intro"], "Intro sentence.")
        self.assertEqual(self.article["other"], ["Skins"])
        sections = self.article["balance"][0]["sections"]
        self.assertEqual([s["kind"] for s in sections],
                         ["nerf", "buff", "buff", "fix"])
        self.assertEqual(sections[0]["groups"][0]["name"], "Shade")
        self.assertEqual(len(sections[0]["groups"][0]["lines"]), 2)
        self.assertEqual(sections[2]["groups"][0]["name"], "Bea")  # list items
        self.assertEqual(sections[3]["groups"][0]["lines"],
                         ["Fixed a thing", "Fixed another"])

    def test_intro_not_rendered_as_change(self):
        kinds = [s["kind"] for s in self.article["balance"][0]["sections"]]
        self.assertNotIn("change", kinds)

    def test_non_balance_article(self):
        article = articles.parse_article(page([("News", [para(text("Hi"))])]))
        self.assertEqual(article["balance"], [])
        self.assertEqual(
            articles.build_balance_messages(article, {"title": "t"}, "f"), [])

    def test_unparseable_page(self):
        self.assertIsNone(articles.parse_article("<html></html>"))
        self.assertEqual(
            articles.build_balance_messages(None, {"title": "t"}, "f"), [])


class RenderTests(unittest.TestCase):

    def test_line_formatting(self):
        out = articles.format_line("cooldown from 18s → 20s, 650 to 750.", "🔻")
        self.assertIn("**18s → 20s**", out)
        self.assertIn("**650 → 750**", out)
        self.assertTrue(out.startswith("🔻 "))

    def test_markdown_escaped(self):
        self.assertIn("\\*",articles.format_line("a * b", "•"))

    def test_messages_respect_discord_limits(self):
        big = [para(text("NERFS", "bold"))]
        for i in range(120):
            big.append(para(text(f"BRAWLER{i}", "underline"),
                            text("\n" + "\n".join(
                                f"Gadget {j} - damage reduced from {j}00 → {j}0"
                                for j in range(8)))))
        article = articles.parse_article(page([("Balance", big)]))
        messages = articles.build_balance_messages(
            article, {"title": "t", "url": "u"}, "footer")

        self.assertLessEqual(len(messages), articles.MAX_MESSAGES)
        for message in messages:
            self.assertLessEqual(len(message), 10)
            self.assertLessEqual(sum(map(articles.embed_size, message)), 6000)
            for embed in message:
                self.assertLessEqual(len(embed.get("description", "")), 4096)
        self.assertEqual(messages[-1][-1]["footer"]["text"], "footer")
        self.assertIn("truncated", messages[-1][-1]["description"])


if __name__ == "__main__":
    unittest.main()
