"""Discord ↔ game account links (Brawl Stars, Minecraft) in a local SQLite database.

Links are scoped per Discord server: when a member leaves a server (or the
bot is removed from it) their links for that server are deleted. Nothing is
kept for people who are no longer members.
"""

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "BrawlStarsNewsBot/2.0"

BRAWL_STARS = "brawlstars"
MINECRAFT = "minecraft"
GAMES = {BRAWL_STARS: "Brawl Stars", MINECRAFT: "Minecraft"}

MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

BRAWL_STARS_PLAYER_URL = "https://api.brawlstars.com/v1/players/"
# Supercell player tags only use these characters.
TAG_RE = re.compile(r"^[0289PYLQGRJCUV]{3,14}$")


class LookupUnavailable(Exception):
    """The game's API could not be reached or refused the request."""


class AlreadyLinked(Exception):
    """The game account is linked to another member of the server."""

    def __init__(self, discord_id):
        super().__init__(discord_id)
        self.discord_id = discord_id


def fetch_json(request, timeout=10):
    """Return parsed JSON, or None if the resource does not exist."""

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (204, 404):
            return None
        raise LookupUnavailable(f"HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
        raise LookupUnavailable(str(error))


# ------------------------------------------------------------
# MINECRAFT
# ------------------------------------------------------------

def valid_username(name):
    return bool(USERNAME_RE.match(name or ""))


def format_uuid(raw):
    raw = raw.replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def lookup_minecraft(name):
    """Return {"id", "name"} for a Java Edition username, or None if unknown."""

    if not valid_username(name):
        return None

    data = fetch_json(urllib.request.Request(
        MOJANG_PROFILE_URL + urllib.parse.quote(name),
        headers={"User-Agent": USER_AGENT},
    ))

    if not data or "id" not in data:
        return None

    return {"id": format_uuid(data["id"]), "name": data.get("name", name)}


# ------------------------------------------------------------
# BRAWL STARS
# ------------------------------------------------------------

def normalize_tag(tag):
    """'#abc o' style input -> 'ABC0', or None if it can't be a player tag."""

    tag = (tag or "").strip().lstrip("#").upper().replace(" ", "").replace("O", "0")
    return tag if TAG_RE.match(tag) else None


def lookup_brawl_stars(tag, api_key):
    """Return {"id": "#TAG", "name", "trophies"}, or None if no such player.

    Without an API key the tag can't be checked, so it is returned as-is
    (name None) after format validation.
    """

    tag = normalize_tag(tag)

    if tag is None:
        return None

    if not api_key:
        return {"id": f"#{tag}", "name": None, "trophies": None}

    data = fetch_json(urllib.request.Request(
        BRAWL_STARS_PLAYER_URL + urllib.parse.quote(f"#{tag}"),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    ))

    if not data or "tag" not in data:
        return None

    return {
        "id": data["tag"],
        "name": data.get("name"),
        "trophies": data.get("trophies"),
    }


# ------------------------------------------------------------
# STORAGE
# ------------------------------------------------------------

class LinkStore:

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                guild_id     INTEGER NOT NULL,
                discord_id   INTEGER NOT NULL,
                game         TEXT    NOT NULL,
                account_id   TEXT    NOT NULL,
                account_name TEXT,
                PRIMARY KEY (guild_id, discord_id, game),
                UNIQUE (guild_id, game, account_id)
            )
            """
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def link(self, guild_id, discord_id, game, account_id, account_name):
        """Link (or re-link) a member's account. Raises AlreadyLinked if taken."""

        owner = self.find_owner(guild_id, game, account_id)

        if owner is not None and owner != discord_id:
            raise AlreadyLinked(owner)

        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?)",
                (guild_id, discord_id, game, account_id, account_name),
            )

    def get(self, guild_id, discord_id):
        """Return {game: {"id", "name"}} for a member."""

        rows = self.db.execute(
            "SELECT game, account_id, account_name FROM links "
            "WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )

        return {game: {"id": account_id, "name": name} for game, account_id, name in rows}

    def find_owner(self, guild_id, game, account_id):
        row = self.db.execute(
            "SELECT discord_id FROM links WHERE guild_id = ? AND game = ? AND account_id = ?",
            (guild_id, game, account_id),
        ).fetchone()

        return row[0] if row else None

    def unlink(self, guild_id, discord_id, game=None):
        """Remove one game's link, or all of a member's links if game is None."""

        query = "DELETE FROM links WHERE guild_id = ? AND discord_id = ?"
        params = [guild_id, discord_id]

        if game is not None:
            query += " AND game = ?"
            params.append(game)

        with self.db:
            return self.db.execute(query, params).rowcount

    def forget_guild(self, guild_id):
        with self.db:
            return self.db.execute(
                "DELETE FROM links WHERE guild_id = ?", (guild_id,)
            ).rowcount

    def guild_ids(self):
        return {row[0] for row in self.db.execute("SELECT DISTINCT guild_id FROM links")}

    def prune(self, guild_id, member_ids):
        """Delete links of anyone in the guild who is not in member_ids.

        Returns the number of people whose data was removed.
        """

        member_ids = set(member_ids)
        stale = [
            discord_id
            for (discord_id,) in self.db.execute(
                "SELECT DISTINCT discord_id FROM links WHERE guild_id = ?", (guild_id,)
            )
            if discord_id not in member_ids
        ]

        with self.db:
            self.db.executemany(
                "DELETE FROM links WHERE guild_id = ? AND discord_id = ?",
                [(guild_id, discord_id) for discord_id in stale],
            )

        return len(stale)
