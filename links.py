"""Discord ↔ Minecraft account links, stored in a local SQLite database.

Links are scoped per Discord server: when a member leaves a server (or the
bot is removed from it) their link for that server is deleted. Nothing is
kept for people who are no longer members.
"""

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
USER_AGENT = "BrawlStarsNewsBot/2.0"


class MojangUnavailable(Exception):
    """Mojang could not be reached (transient)."""


class AlreadyLinked(Exception):
    """The Minecraft account is linked to another member of the server."""

    def __init__(self, discord_id):
        super().__init__(discord_id)
        self.discord_id = discord_id


def valid_username(name):
    return bool(USERNAME_RE.match(name or ""))


def format_uuid(raw):
    raw = raw.replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def lookup_minecraft(name, timeout=10):
    """Return {"uuid", "name"} for a Java Edition username, or None if unknown."""

    if not valid_username(name):
        return None

    request = urllib.request.Request(
        MOJANG_PROFILE_URL + urllib.parse.quote(name),
        headers={"User-Agent": USER_AGENT},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (204, 404):
            return None
        raise MojangUnavailable(f"Mojang returned HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
        raise MojangUnavailable(str(error))

    if not data or "id" not in data:
        return None

    return {"uuid": format_uuid(data["id"]), "name": data.get("name", name)}


class LinkStore:

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                guild_id   INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                mc_uuid    TEXT    NOT NULL,
                mc_name    TEXT    NOT NULL,
                PRIMARY KEY (guild_id, discord_id),
                UNIQUE (guild_id, mc_uuid)
            )
            """
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def link(self, guild_id, discord_id, mc_uuid, mc_name):
        """Link (or re-link) a member. Raises AlreadyLinked if taken."""

        owner = self.db.execute(
            "SELECT discord_id FROM links WHERE guild_id = ? AND mc_uuid = ?",
            (guild_id, mc_uuid),
        ).fetchone()

        if owner and owner[0] != discord_id:
            raise AlreadyLinked(owner[0])

        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?)",
                (guild_id, discord_id, mc_uuid, mc_name),
            )

    def get(self, guild_id, discord_id):
        row = self.db.execute(
            "SELECT mc_uuid, mc_name FROM links WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        ).fetchone()

        return {"uuid": row[0], "name": row[1]} if row else None

    def find_by_minecraft(self, guild_id, mc_uuid):
        row = self.db.execute(
            "SELECT discord_id FROM links WHERE guild_id = ? AND mc_uuid = ?",
            (guild_id, mc_uuid),
        ).fetchone()

        return row[0] if row else None

    def unlink(self, guild_id, discord_id):
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM links WHERE guild_id = ? AND discord_id = ?",
                (guild_id, discord_id),
            )
        return cursor.rowcount > 0

    def forget_guild(self, guild_id):
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM links WHERE guild_id = ?", (guild_id,)
            )
        return cursor.rowcount

    def guild_ids(self):
        return {row[0] for row in self.db.execute("SELECT DISTINCT guild_id FROM links")}

    def prune(self, guild_id, member_ids):
        """Delete links of anyone in the guild who is not in member_ids."""

        member_ids = set(member_ids)
        stale = [
            discord_id
            for (discord_id,) in self.db.execute(
                "SELECT discord_id FROM links WHERE guild_id = ?", (guild_id,)
            )
            if discord_id not in member_ids
        ]

        with self.db:
            self.db.executemany(
                "DELETE FROM links WHERE guild_id = ? AND discord_id = ?",
                [(guild_id, discord_id) for discord_id in stale],
            )

        return len(stale)
