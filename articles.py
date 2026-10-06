"""Parses Supercell article pages and renders balance changes as Discord embeds.

Supercell blog pages are Next.js apps: the full article is embedded as
Contentful rich text in the __NEXT_DATA__ JSON, which is far more reliable
than the og:description (truncated) or scraping the rendered HTML.
"""

import json
import re

EMBED_DESCRIPTION_LIMIT = 3800   # Discord max is 4096
MESSAGE_CHAR_LIMIT = 5500        # Discord max is 6000 across all embeds
MESSAGE_EMBED_LIMIT = 10
MAX_MESSAGES = 6
MAX_FIX_LINES = 12

KIND_STYLE = {
    "nerf": {"icon": "🔻", "color": 0xE74C3C, "label": "Nerfs"},
    "buff": {"icon": "🔺", "color": 0x2ECC71, "label": "Buffs"},
    "change": {"icon": "🔸", "color": 0xF7C948, "label": "Changes"},
    "fix": {"icon": "🔧", "color": 0x95A5A6, "label": "Bug fixes"},
}

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

# "from 18s → 20s", "from 650 to 750", "100% → 40%"
_CHANGE = re.compile(
    r"(\d+(?:[.,]\d+)*\s?(?:sec|s|%|x)?)\s(?:→|->|to)\s"
    r"(\d+(?:[.,]\d+)*\s?(?:sec|s|%|x)?)"
)


# ------------------------------------------------------------
# Page parsing
# ------------------------------------------------------------

def extract_page_props(page_html):
    match = _NEXT_DATA.search(page_html)

    if not match:
        return None

    try:
        return json.loads(match.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return None


def _text(node):
    if node.get("nodeType") == "text":
        return node.get("value", "")

    return "".join(_text(child) for child in node.get("content", []))


def _tokens(node):
    """Flattens a node into [(text, {marks})]."""

    if node.get("nodeType") == "text":
        marks = {m.get("type") for m in node.get("marks", [])}
        return [(node.get("value", ""), marks)]

    out = []

    for child in node.get("content", []):
        out.extend(_tokens(child))

    return out


def _section_kind(name):
    lowered = name.lower()

    if "nerf" in lowered:
        return "nerf"
    if "buff" in lowered:
        return "buff"
    if "fix" in lowered:
        return "fix"

    return "change"


def _is_section_title(text):
    lowered = text.lower()

    return any(
        word in lowered
        for word in ("nerf", "buff", "fix", "adjust", "change", "rework")
    )


def _clean_line(line):
    return line.strip().lstrip("•·-– ").strip()


def _brawler_name(name):
    name = name.strip()

    return name.title() if name.isupper() else name


class _BlockParser:
    """Turns one rich-text block into balance sections.

    Result: {"sections": [{"kind", "name", "groups": [{"name", "lines"}]}]}
    where a group with name None holds general notes for that section.
    """

    def __init__(self, title):
        self.title = title
        self.sections = []
        self.group = None
        self.context = None
        self.brawler_count = 0

    def section(self, name):
        name = name.strip()
        kind = _section_kind(name)

        if self.context:
            name = f"{self.context} — {name}"

        section = {"kind": kind, "name": name, "groups": []}
        self.sections.append(section)
        self.group = None
        return section

    def current_section(self):
        return self.sections[-1] if self.sections else self.section("Changes")

    def start_group(self, name):
        section = self.current_section()
        self.group = {"name": name, "lines": []}
        section["groups"].append(self.group)

    def add_lines(self, text):
        if not self.sections and self.group is None:
            return  # intro text before the first section

        for raw in text.split("\n"):
            line = _clean_line(raw)

            if not line:
                continue

            if self.group is None:
                self.start_group(None)

            self.group["lines"].append(line)

    def paragraph(self, node):
        first = True

        for text, marks in _tokens(node):
            stripped = text.strip()

            if (
                "underline" in marks
                and stripped
                and not (len(stripped) > 30 and stripped.endswith("."))
            ):
                self.start_group(_brawler_name(stripped))
                self.brawler_count += 1

            elif "bold" in marks and stripped and _is_section_title(stripped):
                if "balance changes" not in stripped.lower():
                    self.section(re.sub(r"[^\w\s&/-]", "", stripped).strip())

            elif "bold" in marks and not stripped:
                pass

            elif stripped:
                if first:
                    self.group = None  # plain text opening a paragraph

                self.add_lines(text)

            first = first and not stripped

    def walk(self, nodes):
        for node in nodes:
            kind = node.get("nodeType", "")

            if kind.startswith("heading"):
                text = _text(node).strip()

                if _is_section_title(text) and text.lower() != self.title.lower():
                    self.section(text)
                elif text.lower() != self.title.lower():
                    self.context = text
                    self.group = None

            elif kind == "paragraph":
                self.paragraph(node)

            elif kind in ("unordered-list", "ordered-list"):
                for item in node.get("content", []):
                    self.add_lines(_text(item).replace("\n", " "))

            elif kind == "list-item":
                self.add_lines(_text(node).replace("\n", " "))


def parse_article(page_html):
    """Returns the article structure, or None if the page isn't parseable.

    {"title", "published", "hero", "intro", "balance": [block...], "other": [titles]}
    """

    props = extract_page_props(page_html)

    if not props:
        return None

    result = {
        "title": props.get("title"),
        "published": props.get("publishDate"),
        "hero": props.get("hero"),
        "intro": "",
        "balance": [],
        "other": [],
    }

    for block in props.get("bodyCollection") or []:
        if block.get("__typename") != "TextBlock":
            continue

        title = block.get("title") or ""
        document = ((block.get("text") or {}).get("json")) or {}
        parser = _BlockParser(title)
        parser.walk(document.get("content", []))

        if parser.brawler_count:
            result["balance"].append(
                {"title": title, "sections": parser.sections}
            )

            if not result["intro"]:
                first = (document.get("content") or [{}])[0]
                result["intro"] = _text(first).strip()

        elif title:
            result["other"].append(title)

    return result


# ------------------------------------------------------------
# Rendering
# ------------------------------------------------------------

def _escape(text):
    return re.sub(r"([*_~|`>])", r"\\\1", text)


def format_line(line, icon):
    text = _escape(line)
    text = _CHANGE.sub(lambda m: f"**{m.group(1)} → {m.group(2)}**", text)

    return f"{icon} {text}"


def _render_groups(section):
    """Yields markdown chunks, one per brawler / note group."""

    icon = KIND_STYLE[section["kind"]]["icon"]
    notes = section["kind"] == "fix"

    for group in section["groups"]:
        lines = group["lines"]

        if notes and len(lines) > MAX_FIX_LINES:
            extra = len(lines) - MAX_FIX_LINES
            lines = lines[:MAX_FIX_LINES] + [f"…and {extra} more fixes"]

        body = "\n".join(
            format_line(line, "•" if notes else icon) for line in lines
        )

        if group["name"]:
            yield f"**{_escape(group['name'])}**\n{body}"
        else:
            yield body


def _embeds_for_section(section, block_title, multi_block):
    style = KIND_STYLE[section["kind"]]
    title = f"{style['icon']} {section['name'] or style['label']}"

    if multi_block and block_title:
        title += f" — {block_title}"

    embeds = []
    current = []
    size = 0

    def flush():
        nonlocal current, size

        if current:
            suffix = "" if not embeds else " (cont.)"
            embeds.append({
                "title": (title + suffix)[:256],
                "description": "\n\n".join(current),
                "color": style["color"],
            })

        current, size = [], 0

    for chunk in _render_groups(section):
        chunk = chunk[:EMBED_DESCRIPTION_LIMIT]

        if size + len(chunk) + 2 > EMBED_DESCRIPTION_LIMIT:
            flush()

        current.append(chunk)
        size += len(chunk) + 2

    flush()

    return embeds


def embed_size(embed):
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
        + sum(
            len(f["name"]) + len(f["value"])
            for f in embed.get("fields", [])
        )
    )


def pack_messages(embeds):
    messages, current, size = [], [], 0

    for embed in embeds:
        length = embed_size(embed)

        if current and (
            size + length > MESSAGE_CHAR_LIMIT
            or len(current) >= MESSAGE_EMBED_LIMIT
        ):
            messages.append(current)
            current, size = [], 0

        current.append(embed)
        size += length

    if current:
        messages.append(current)

    return messages


def build_balance_messages(article, intro_embed, footer):
    """Returns a list of messages (each a list of embeds), or [] if the
    article has no per-brawler balance changes.

    `intro_embed` is the already-styled headline embed (title, url, image).
    """

    if not article or not article["balance"]:
        return []

    blocks = article["balance"]
    multi = len(blocks) > 1

    total_brawlers = len({
        group["name"].lower()
        for block in blocks
        for section in block["sections"]
        for group in section["groups"]
        if group["name"] and section["kind"] != "fix"
    })

    summary = [f"**{total_brawlers} brawler{'s' if total_brawlers != 1 else ''} "
               "with balance changes.**"]

    if article["intro"]:
        summary.insert(0, _escape(article["intro"]))

    if article["other"]:
        shown = ", ".join(article["other"][:12])
        summary.append(f"Also in these notes: {shown}")

    intro_embed = dict(intro_embed)
    intro_embed["description"] = "\n\n".join(summary)[:EMBED_DESCRIPTION_LIMIT]

    embeds = [intro_embed]

    for block in blocks:
        for section in block["sections"]:
            embeds.extend(_embeds_for_section(section, block["title"], multi))

    messages = pack_messages(embeds)

    if len(messages) > MAX_MESSAGES:
        messages = messages[:MAX_MESSAGES]
        messages[-1][-1] = dict(messages[-1][-1])
        messages[-1][-1]["description"] = (
            messages[-1][-1]["description"][:3000]
            + "\n\n…**truncated — open the article for the full list.**"
        )

    messages[-1][-1] = dict(messages[-1][-1], footer={"text": footer})

    return messages
