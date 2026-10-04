import json
import os
import re
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser


DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

NEWS_URL = "https://supercell.com/en/games/brawlstars/blog/"
YOUTUBE_CHANNEL_ID = "UCooVYzDxdwTtGYAkcPmOgOw"
YOUTUBE_FEED_URL = (
    f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
)

STATE_FILE = "state.json"


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

def get(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; BrawlStarsNewsBot/1.0)"
            )
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "news": [],
            "youtube": []
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ------------------------------------------------------------
# DISCORD
# ------------------------------------------------------------

def send_discord(
    title,
    description,
    url,
    category,
    image_url=None,
):
    if category == "youtube":
        icon = "🎬"
    elif category == "balance":
        icon = "⚔️"
    else:
        icon = "📰"

    embed = {
        "title": f"{icon} {title}",
        "url": url,
        "description": description[:2048],
        "color": 0xF7C948,
        "footer": {
            "text": "Brawl Stars • Official"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    if image_url:
        embed["image"] = {
            "url": image_url
        }

    payload = {
        "username": "Brawl Stars News",
        "embeds": [embed],
        "allowed_mentions": {
            "parse": []
        }
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BrawlStarsNewsBot/1.0"
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord returned HTTP {response.status}"
            )


# ------------------------------------------------------------
# SUPERcell NEWS
# ------------------------------------------------------------

class LinkParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.links = []
        self.current_href = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return

        attributes = dict(attrs)

        href = attributes.get("href")

        if href:
            self.current_href = href
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag != "a":
            return

        if self.current_href:
            text = " ".join(
                "".join(self.current_text).split()
            )

            self.links.append(
                (
                    self.current_href,
                    text
                )
            )

        self.current_href = None
        self.current_text = []


def absolute_url(url):
    return urllib.parse.urljoin(
        NEWS_URL,
        url
    )


def get_news():
    html_data = get(NEWS_URL).decode(
        "utf-8",
        errors="ignore"
    )

    parser = LinkParser()
    parser.feed(html_data)

    articles = []
    seen = set()

    for href, title in parser.links:

        url = absolute_url(href)

        if "/en/games/brawlstars/blog/" not in url:
            continue

        # Skip the archive itself.
        if url.rstrip("/") == NEWS_URL.rstrip("/"):
            continue

        # Ignore pagination.
        if "/page/" in url:
            continue

        # Ignore empty titles.
        if not title:
            continue

        if url in seen:
            continue

        seen.add(url)

        title = html.unescape(title)

        articles.append({
            "id": url,
            "title": title,
            "url": url,
        })

    return articles[:20]


# ------------------------------------------------------------
# YOUTUBE
# ------------------------------------------------------------

def get_youtube():
    data = get(YOUTUBE_FEED_URL)

    root = ET.fromstring(data)

    namespace = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"
    }

    videos = []

    for entry in root.findall("atom:entry", namespace):

        video_id = entry.find(
            "yt:videoId",
            namespace
        )

        title = entry.find(
            "atom:title",
            namespace
        )

        published = entry.find(
            "atom:published",
            namespace
        )

        link = entry.find(
            "atom:link",
            namespace
        )

        if (
            video_id is None
            or title is None
            or link is None
        ):
            continue

        video_id = video_id.text
        title = title.text

        url = link.attrib.get("href")

        videos.append({
            "id": video_id,
            "title": title,
            "url": url,
            "published": (
                published.text
                if published is not None
                else None
            )
        })

    return videos


# ------------------------------------------------------------
# CLASSIFICATION
# ------------------------------------------------------------

def classify_news(title):

    title_lower = title.lower()

    balance_words = [
        "balance",
        "release notes",
        "balance changes",
        "buff",
        "nerf",
        "maintenance",
    ]

    for word in balance_words:
        if word in title_lower:
            return "balance"

    return "news"


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    state = load_state()

    known_news = set(state.get("news", []))
    known_youtube = set(state.get("youtube", []))

    news = get_news()
    videos = get_youtube()

    new_news = [
        item
        for item in news
        if item["id"] not in known_news
    ]

    new_videos = [
        item
        for item in videos
        if item["id"] not in known_youtube
    ]

    # --------------------------------------------------------
    # First run:
    #
    # Don't spam the Discord channel with old posts.
    # We simply remember everything currently available.
    # --------------------------------------------------------

    first_run = (
        len(known_news) == 0
        and len(known_youtube) == 0
    )

    if first_run:

        state["news"] = [
            item["id"]
            for item in news
        ]

        state["youtube"] = [
            item["id"]
            for item in videos
        ]

        save_state(state)

        print("First run completed.")
        print("Existing posts were recorded without sending them.")

        return

    # --------------------------------------------------------
    # Send new news
    # --------------------------------------------------------

    for item in reversed(new_news):

        category = classify_news(
            item["title"]
        )

        if category == "balance":

            description = (
                "**Official Brawl Stars balance/update news.**\n\n"
                "Open the official article for the complete "
                "list of changes."
            )

        else:

            description = (
                "**New official Brawl Stars news.**\n\n"
                "Open the article for the full announcement."
            )

        send_discord(
            title=item["title"],
            description=description,
            url=item["url"],
            category=category
        )

        known_news.add(item["id"])

    # --------------------------------------------------------
    # Send new YouTube videos
    # --------------------------------------------------------

    for item in reversed(new_videos):

        description = (
            "**New video from the official Brawl Stars "
            "YouTube channel.**"
        )

        send_discord(
            title=item["title"],
            description=description,
            url=item["url"],
            category="youtube"
        )

        known_youtube.add(item["id"])

    # --------------------------------------------------------
    # Keep only recent history
    # --------------------------------------------------------

    state["news"] = list(known_news)[-200:]
    state["youtube"] = list(known_youtube)[-200:]

    save_state(state)

    print(
        f"Processed {len(new_news)} new articles "
        f"and {len(new_videos)} new videos."
    )


if __name__ == "__main__":
    main()
