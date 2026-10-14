import json
import os
import signal
import subprocess
import sys
import time
import html
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

import articles
import links


NEWS_URL = "https://supercell.com/en/games/brawlstars/blog/"
YOUTUBE_CHANNEL_ID = "UCooVYzDxdwTtGYAkcPmOgOw"
YOUTUBE_FEED_URL = (
    f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
)

# YouTube's channel feed intermittently returns 404; the uploads playlist
# feed (UC... -> UU...) serves the same videos and is used as a fallback.
YOUTUBE_FEED_URLS = (
    YOUTUBE_FEED_URL,
    "https://www.youtube.com/feeds/videos.xml?playlist_id=UU"
    + YOUTUBE_CHANNEL_ID[2:],
)

RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 0)
LINK_INTERVAL = 60
NEWS_INTERVAL = 300
PURGE_INTERVAL = 600
STOP = False

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_HISTORY = 200
MAX_RETRIES = 4
USER_AGENT = "Mozilla/5.0 (compatible; BrawlStarsNewsBot/2.0)"

COLORS = {
    "youtube": 0xFF0033,
    "short": 0xFF0033,
    "balance": 0xF7C948,
    "news": 0x3B82F6,
}

ICONS = {
    "youtube": "🎬",
    "short": "📱",
    "balance": "⚔️",
    "news": "📰",
}

LABELS = {
    "youtube": "New YouTube video",
    "short": "New YouTube Short",
    "balance": "Balance / update news",
    "news": "Official news",
}


class SourceUnavailable(Exception):
    """A source could not be fetched (transient; retried next run)."""


def log(message):
    print(message, flush=True)


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

def request_with_retry(request, timeout=30):
    """Open a request, retrying on network errors, 429 and 5xx."""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()

        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500

            if not retryable or attempt == MAX_RETRIES:
                raise

            delay = 2 ** attempt

            if error.code == 429:
                try:
                    body = json.loads(error.read().decode("utf-8"))
                    delay = float(body.get("retry_after", delay))
                except (ValueError, TypeError, AttributeError):
                    pass

            log(f"HTTP {error.code}, retrying in {delay:.1f}s...")
            time.sleep(min(delay, 30) + 0.5)

        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == MAX_RETRIES:
                raise

            delay = 2 ** attempt
            log(f"Network error ({error}), retrying in {delay}s...")
            time.sleep(delay)


def get(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en",
        },
    )

    return request_with_retry(request)[1]


# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as error:
        raise SystemExit(
            f"Could not read {STATE_FILE} ({error}). "
            "Refusing to continue so old posts are not re-sent."
        )

    if not isinstance(state, dict):
        raise SystemExit(f"{STATE_FILE} has an unexpected format.")

    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")

    os.replace(tmp, STATE_FILE)


def remember(state, key, item_id):
    """Append an id (keeping order, no duplicates, bounded size)."""

    ids = [i for i in state.get(key, []) if i != item_id]
    ids.append(item_id)
    state[key] = ids[-MAX_HISTORY:]


# ------------------------------------------------------------
# DISCORD
# ------------------------------------------------------------

def send_discord(
    webhook_url,
    title,
    description,
    url,
    category,
    image_url=None,
    timestamp=None,
):
    embed = {
        "title": f"{ICONS[category]} {title}"[:256],
        "url": url,
        "description": description[:2048],
        "color": COLORS[category],
        "footer": {"text": f"Brawl Stars • {LABELS[category]}"},
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }

    if image_url:
        embed["image"] = {"url": image_url}

    post_embeds(webhook_url, [embed])


def post_embeds(webhook_url, embeds):
    payload = {
        "username": "Brawl Stars News",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }

    if DRY_RUN:
        for embed in embeds:
            log("[dry run] " + json.dumps(embed, ensure_ascii=False))
        return

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BrawlStarsNewsBot/2.0",
        },
        method="POST",
    )

    status, _ = request_with_retry(request)

    if status not in (200, 204):
        raise RuntimeError(f"Discord returned HTTP {status}")

    # Stay well below the webhook rate limit (5 requests / 2s).
    time.sleep(1.5)


# ------------------------------------------------------------
# SUPERCELL NEWS
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

        href = dict(attrs).get("href")

        if href:
            self.current_href = href
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self.current_href:
            return

        text = " ".join("".join(self.current_text).split())
        self.links.append((self.current_href, text))

        self.current_href = None
        self.current_text = []


class MetaParser(HTMLParser):
    """Collects <meta property/name=... content=...> tags."""

    def __init__(self):
        super().__init__()
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return

        attributes = dict(attrs)
        key = attributes.get("property") or attributes.get("name")
        content = attributes.get("content")

        if key and content and key not in self.meta:
            self.meta[key] = content.strip()


def absolute_url(url):
    return urllib.parse.urljoin(NEWS_URL, url)


def get_news():
    try:
        page = get(NEWS_URL).decode("utf-8", errors="ignore")
    except OSError as error:
        raise SourceUnavailable(f"{NEWS_URL}: {error}")

    parser = LinkParser()
    parser.feed(page)

    articles = []
    seen = set()

    for href, title in parser.links:
        # Drop fragments/query strings so one article has one id.
        url = urllib.parse.urldefrag(absolute_url(href))[0].split("?")[0]

        if "/en/games/brawlstars/blog/" not in url:
            continue

        # Skip the archive itself and pagination.
        if url.rstrip("/") == NEWS_URL.rstrip("/") or "/page/" in url:
            continue

        if not title or url in seen:
            continue

        seen.add(url)

        articles.append({
            "id": url,
            "title": html.unescape(title),
            "url": url,
        })

    return articles[:20]


def get_article_meta(url):
    """Best-effort fetch of image/description for an article."""

    try:
        page = get(url).decode("utf-8", errors="ignore")
    except Exception as error:
        log(f"Could not fetch article metadata for {url}: {error}")
        return {}

    parser = MetaParser()
    parser.feed(page)

    meta = parser.meta
    article = articles.parse_article(page)

    image = meta.get("og:image") or meta.get("twitter:image")

    return {
        "title": html.unescape(meta["og:title"]) if "og:title" in meta else None,
        "description": html.unescape(
            meta.get("og:description") or meta.get("description") or ""
        ),
        "image": urllib.parse.urljoin(url, image) if image else None,
        "published": (
            meta.get("article:published_time")
            or (article or {}).get("published")
        ),
        "article": article,
    }


# ------------------------------------------------------------
# YOUTUBE
# ------------------------------------------------------------

def fetch_youtube_feed():
    errors = []

    for url in YOUTUBE_FEED_URLS:
        for attempt in range(2):
            try:
                return ET.fromstring(get(url))
            except (OSError, ET.ParseError) as error:
                errors.append(f"{url}: {error}")
                time.sleep(2)

    raise SourceUnavailable("; ".join(errors))


def get_youtube():
    root = fetch_youtube_feed()

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    videos = []

    for entry in root.findall("atom:entry", ns):
        video_id = entry.find("yt:videoId", ns)
        title = entry.find("atom:title", ns)
        published = entry.find("atom:published", ns)
        link = entry.find("atom:link", ns)

        if (
            video_id is None
            or not video_id.text
            or title is None
            or link is None
            or not link.attrib.get("href")
        ):
            continue

        url = link.attrib["href"]

        description = entry.find("media:group/media:description", ns)
        thumbnail = entry.find("media:group/media:thumbnail", ns)

        videos.append({
            "id": video_id.text,
            "title": title.text or "New video",
            "url": url,
            "is_short": "/shorts/" in url,
            "description": (
                description.text.strip()
                if description is not None and description.text
                else ""
            ),
            "image": (
                thumbnail.attrib.get("url")
                if thumbnail is not None
                else f"https://i.ytimg.com/vi/{video_id.text}/hqdefault.jpg"
            ),
            "published": published.text if published is not None else None,
        })

    return videos


# ------------------------------------------------------------
# CLASSIFICATION
# ------------------------------------------------------------

BALANCE_WORDS = (
    "balance",
    "release notes",
    "patch notes",
    "buff",
    "nerf",
    "maintenance",
    "update",
)


def classify_news(title):
    title_lower = title.lower()

    if any(word in title_lower for word in BALANCE_WORDS):
        return "balance"

    return "news"


def shorten(text, limit=350):
    text = " ".join(text.split())

    if len(text) <= limit:
        return text

    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


# ------------------------------------------------------------
# ACCOUNT LINKING (see links.py)
# ------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
# Supercell only accepts requests from whitelisted IPs, which GitHub Actions
# doesn't have, so calls go through RoyaleAPI's fixed-IP proxy by default
# (whitelist 45.79.218.79 on the key). Override to call the API directly.
BRAWLSTARS_API_BASE = os.environ.get(
    "BRAWLSTARS_API_BASE", "https://bsproxy.royaleapi.dev/v1"
)


def discord_api(method, path, payload=None):
    """Bot-authenticated Discord REST call -> (status, parsed JSON or None)."""

    request = urllib.request.Request(
        DISCORD_API + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (brawl-stars-news-bot, 2.0)",
        },
        method=method,
    )

    try:
        status, body = request_with_retry(request)
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    except OSError as error:
        raise links.Unavailable(f"Discord unreachable: {error}")

    try:
        return status, (json.loads(body) if body else None)
    except ValueError:
        return status, None


def fetch_player(tag):
    request = urllib.request.Request(
        f"{BRAWLSTARS_API_BASE}/players/%23{tag}",
        headers={
            "Authorization": f"Bearer {os.environ['BRAWLSTARS_API_KEY']}",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        return json.loads(request_with_retry(request)[1])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise links.NotFound(tag)
        if error.code == 403:
            raise links.Unavailable(
                "Brawl Stars API rejected the key (is the proxy IP whitelisted?)"
            )
        raise links.Unavailable(f"Brawl Stars API returned HTTP {error.code}")
    except (OSError, ValueError) as error:
        raise links.Unavailable(f"Brawl Stars API unreachable: {error}")


def process_links(state, _webhook_url, purge=True):
    channel = os.environ.get("LINK_CHANNEL_ID", "").strip()

    if not channel:
        log("Links: LINK_CHANNEL_ID not set, skipping.")
        return 0

    if DRY_RUN:
        log("Links: dry run, skipping (this source reads and writes Discord).")
        return 0

    missing = [
        name
        for name in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "BRAWLSTARS_API_KEY")
        if not os.environ.get(name)
    ]

    if missing:
        raise SystemExit(f"Account linking needs: {', '.join(missing)}")

    def save_cursor(key, message_id):
        state[f"{key}_cursor"] = message_id
        save_state(state)

    scanner = links.LinkScanner(
        discord_api,
        fetch_player,
        channel,
        os.environ["DISCORD_GUILD_ID"],
        command_channel_id=os.environ.get("COMMAND_CHANNEL_ID", "").strip(),
        cleanup=os.environ.get("LINK_CLEANUP", "1") != "0",
    )

    cursors = {
        "link": state.get("link_cursor"),
        "command": state.get("command_cursor"),
    }

    try:
        stats = scanner.run(cursors, save_cursor, purge=purge)
    except links.Unavailable as error:
        raise SourceUnavailable(str(error))

    for warning in sorted(scanner.warnings):
        log(
            f"::warning title=Links::Discord: {warning}. Check the bot's "
            "permissions in the link/command channels."
        )

    if stats["blind"]:
        log(
            f"::warning title=Links::{stats['blind']} message(s) had no readable "
            "text. Enable the Message Content Intent for the bot in the Discord "
            "developer portal, or links and commands cannot work."
        )

    log(
        f"Links: {stats['linked']} linked, {stats['rejected']} rejected, "
        f"{stats['commands']} command(s), {stats['cleaned']} stray message(s) "
        f"cleaned, {stats['purged']} message(s) of departed members removed."
    )

    return sum(stats.values())


# ------------------------------------------------------------
# PROCESSORS
# ------------------------------------------------------------

def process_news(state, webhook_url):
    """Returns the number of messages sent. Raises on failure."""

    news = get_news()
    known = set(state.get("news", []))

    # First run for this source: record only, don't spam the channel.
    if "news" not in state:
        state["news"] = [item["id"] for item in reversed(news)]
        save_state(state)
        log(f"News: first run, recorded {len(news)} existing articles.")
        return 0

    new_items = [item for item in news if item["id"] not in known]
    sent = 0

    # Oldest first, saving after each message so a later failure
    # never causes duplicates on the next run.
    for item in reversed(new_items):
        meta = get_article_meta(item["url"])
        category = classify_news(item["title"])

        messages = articles.build_balance_messages(
            meta.get("article"),
            intro_embed={
                "title": f"{ICONS['balance']} {item['title']}"[:256],
                "url": item["url"],
                "color": COLORS["balance"],
                "timestamp": (
                    meta.get("published")
                    or datetime.now(timezone.utc).isoformat()
                ),
                **({"image": {"url": meta["image"]}} if meta.get("image") else {}),
            },
            footer=f"Brawl Stars • {LABELS['balance']}",
        )

        if messages:
            for message in messages:
                post_embeds(webhook_url, message)
        else:
            description = meta.get("description") or (
                "Open the article for the full announcement."
            )

            send_discord(
                webhook_url,
                title=item["title"],
                description=shorten(description),
                url=item["url"],
                category=category,
                image_url=meta.get("image"),
                timestamp=meta.get("published"),
            )

        remember(state, "news", item["id"])
        save_state(state)
        sent += 1

    return sent


def process_youtube(state, webhook_url):
    videos = get_youtube()
    known = set(state.get("youtube", []))

    if "youtube" not in state:
        state["youtube"] = [item["id"] for item in reversed(videos)]
        save_state(state)
        log(f"YouTube: first run, recorded {len(videos)} existing videos.")
        return 0

    new_items = [item for item in videos if item["id"] not in known]
    sent = 0

    for item in reversed(new_items):
        category = "short" if item["is_short"] else "youtube"

        send_discord(
            webhook_url,
            title=item["title"],
            description=shorten(item["description"]) or (
                "New video from the official Brawl Stars YouTube channel."
            ),
            url=item["url"],
            category=category,
            image_url=item["image"],
            timestamp=item["published"],
        )

        remember(state, "youtube", item["id"])
        save_state(state)
        sent += 1

    return sent


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if DRY_RUN and not webhook_url:
        webhook_url = "https://example.invalid/dry-run"

    if not webhook_url:
        raise SystemExit(
            "DISCORD_WEBHOOK_URL is not set. Add it as a repository secret "
            "(Settings → Secrets and variables → Actions)."
        )

    state = load_state()

    if RUN_MINUTES <= 0:
        # Single pass (local use / tests).
        if run_sources(state, webhook_url, SOURCES):
            sys.exit(1)
        return

    def request_stop(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        failed = run_loop(state, webhook_url, RUN_MINUTES * 60)
    finally:
        persist_state()

    if STOP:
        # Told to stop (a cancelled run or a newer push is taking over). The
        # workflow reads this marker so it doesn't start a competing successor.
        open(".stopped", "w").close()

    if failed:
        sys.exit(1)


SOURCES = ("news", "youtube", "links")

# Resolved by name at call time so each processor can be swapped out in tests.
PROCESSORS = {
    "news": ("News", "process_news"),
    "youtube": ("YouTube", "process_youtube"),
    "links": ("Links", "process_links"),
}


def run_sources(state, webhook_url, names, results=None, **options):
    """Runs the named sources, each isolated. Returns the number of failures.

    If `results` is given it is filled with {key: error message or None}.
    """

    failures = 0

    for key in names:
        name, function = PROCESSORS[key]
        processor = globals()[function]

        try:
            handled = processor(state, webhook_url, **options.get(key, {}))
            log(f"{name}: {handled} item(s) handled.")

            if results is not None:
                results[key] = None
        except SourceUnavailable as error:
            # Transient upstream problem: nothing was lost, retried next cycle.
            log(f"::warning title={name} unavailable::{error}")

            if results is not None:
                results[key] = f"unavailable: {error}"
        except Exception as error:
            failures += 1
            log(f"::error title={name} check failed::{type(error).__name__}: {error}")

            if results is not None:
                results[key] = f"{type(error).__name__}: {error}"

    return failures


ALERT_AFTER = 5  # consecutive failed cycles before posting an alert


def send_alert(webhook_url, title, description, color):
    try:
        post_embeds(webhook_url, [{
            "title": title[:256],
            "description": description[:2000],
            "color": color,
            "footer": {"text": "Brawl Stars bot • status"},
        }])
    except Exception as error:  # an alert must never take the bot down
        log(f"Could not send status alert: {error}")


def track_health(webhook_url, streaks, results, alerted):
    """Posts one alert when a source keeps failing, and one when it recovers."""

    for key, error in results.items():
        name = PROCESSORS[key][0]

        if error:
            streaks[key] = streaks.get(key, 0) + 1

            if streaks[key] == ALERT_AFTER and key not in alerted:
                alerted.add(key)
                send_alert(
                    webhook_url,
                    f"⚠️ {name} has been failing",
                    f"{ALERT_AFTER} checks in a row failed. Latest error:\n"
                    f"```{error[:500]}```",
                    0xE74C3C,
                )
        else:
            streaks[key] = 0

            if key in alerted:
                alerted.discard(key)
                send_alert(webhook_url, f"✅ {name} is working again", "", 0x2ECC71)


def persist_state():
    """Commits and pushes state.json (CI only), so progress survives a
    cancelled or timed-out job."""

    if os.environ.get("PERSIST_STATE") != "1":
        return

    def git(*args):
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=120
        )

    git("config", "user.name", "Brawl Stars News Bot")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", STATE_FILE)

    if git("diff", "--cached", "--quiet").returncode == 0:
        return

    git("commit", "-m", "Update news state [skip ci]")

    for _ in range(3):
        if git("push").returncode == 0:
            return

        git("pull", "--rebase", "-X", "theirs")

    log("::warning::Could not push state.json; will retry next cycle.")


def run_loop(
    state,
    webhook_url,
    seconds,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Polls for `seconds`: links every minute, news/YouTube every 5 minutes,
    member purge every 10 minutes. Returns True if the last cycle failed."""

    deadline = clock() + seconds
    next_news = next_purge = clock()
    failed = False
    streaks, alerted = {}, set()

    while not STOP and clock() < deadline:
        started = clock()
        names = []
        options = {"links": {"purge": started >= next_purge}}

        if started >= next_news:
            names += ["news", "youtube"]
            next_news = started + NEWS_INTERVAL

        if started >= next_purge:
            next_purge = started + PURGE_INTERVAL

        names.append("links")
        results = {}
        failed = bool(
            run_sources(state, webhook_url, names, results=results, **options)
        )
        track_health(webhook_url, streaks, results, alerted)
        persist_state()

        # Interruptible wait until the next tick.
        wake = started + LINK_INTERVAL

        while not STOP and clock() < min(wake, deadline):
            sleep(min(1, max(0, wake - clock())))

    return failed


if __name__ == "__main__":
    main()
