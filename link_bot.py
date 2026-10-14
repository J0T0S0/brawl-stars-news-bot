"""Discord bot that lets members link their Brawl Stars and Minecraft accounts.

Slash commands:
  /link brawlstars <tag>       link your Brawl Stars player tag
  /link minecraft <username>   link your Minecraft (Java) account
  /unlink <game>               remove a link
  /accounts [member]           show the accounts linked to a member

Privacy: links are deleted as soon as a member leaves the server, when the
bot is removed from a server, and on startup for anyone who left while the
bot was offline.

Unlike bot.py this needs a long-running process (it must answer commands),
so it is not run by GitHub Actions. Requires DISCORD_BOT_TOKEN and the
"Server Members Intent" enabled in the Discord developer portal.
BRAWL_STARS_API_KEY (from developer.brawlstars.com) is optional: with it
tags are checked and the player name is shown.
"""

import asyncio
import os

import discord
from discord import app_commands

import links

DB_FILE = os.environ.get("LINKS_DB", "links.db")
BRAWL_STARS_API_KEY = os.environ.get("BRAWL_STARS_API_KEY", "").strip()

GAME_CHOICES = [
    app_commands.Choice(name=label, value=game) for game, label in links.GAMES.items()
]


def log(message):
    print(message, flush=True)


def esc(text):
    return discord.utils.escape_markdown(text)


async def reply(interaction, message, **kwargs):
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(message, ephemeral=True, **kwargs)


class LinkBot(discord.Client):

    def __init__(self, store):
        intents = discord.Intents.default()
        # Needed for on_member_remove and the startup member scan.
        intents.members = True
        super().__init__(intents=intents)
        self.store = store
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(link_group)
        self.tree.add_command(unlink_command)
        self.tree.add_command(accounts_command)
        await self.tree.sync()

    async def on_ready(self):
        log(f"Logged in as {self.user} in {len(self.guilds)} server(s).")
        await self.prune_all()

    async def prune_all(self):
        """Drop data for people who left (or servers we left) while offline."""

        current = {guild.id: guild for guild in self.guilds}

        for guild_id in self.store.guild_ids() - current.keys():
            removed = self.store.forget_guild(guild_id)
            log(f"Removed {removed} link(s) for server {guild_id} (bot no longer in it).")

        for guild in current.values():
            member_ids = [member.id async for member in guild.fetch_members(limit=None)]
            removed = self.store.prune(guild.id, member_ids)
            if removed:
                log(f"Removed links of {removed} former member(s) of {guild.name}.")

    async def on_member_remove(self, member):
        if self.store.unlink(member.guild.id, member.id):
            log(f"Removed links of {member.id} (left {member.guild.name}).")

    async def on_guild_remove(self, guild):
        removed = self.store.forget_guild(guild.id)
        log(f"Removed {removed} link(s) for {guild.name} (bot removed).")


async def save_link(interaction, game, profile, shown_name):
    try:
        interaction.client.store.link(
            interaction.guild_id, interaction.user.id, game, profile["id"], profile["name"]
        )
    except links.AlreadyLinked:
        await reply(
            interaction,
            f"**{shown_name}** is already linked to another member of this server.",
        )
        return

    await reply(
        interaction,
        f"Linked your {links.GAMES[game]} account **{shown_name}**. "
        "Use `/unlink` to remove it. Your links are deleted automatically "
        "if you leave the server.",
    )


link_group = app_commands.Group(
    name="link", description="Link a game account", guild_only=True
)


@link_group.command(name="brawlstars", description="Link your Brawl Stars account")
@app_commands.describe(tag="Your player tag, e.g. #2PP (Profile → under your name)")
async def link_brawl_stars(interaction: discord.Interaction, tag: str):
    if links.normalize_tag(tag) is None:
        await reply(interaction, "That isn't a valid Brawl Stars player tag (e.g. `#2PP`).")
        return

    await interaction.response.defer(ephemeral=True)

    try:
        profile = await asyncio.to_thread(
            links.lookup_brawl_stars, tag, BRAWL_STARS_API_KEY
        )
    except links.LookupUnavailable as error:
        log(f"Brawl Stars lookup failed: {error}")
        await reply(interaction, "Couldn't reach the Brawl Stars API right now, please try again later.")
        return

    if profile is None:
        await reply(interaction, f"No Brawl Stars player with tag **{esc(tag)}** exists.")
        return

    shown = f"{esc(profile['name'])} ({profile['id']})" if profile["name"] else profile["id"]
    await save_link(interaction, links.BRAWL_STARS, profile, shown)


@link_group.command(name="minecraft", description="Link your Minecraft (Java) account")
@app_commands.describe(username="Your Minecraft username")
async def link_minecraft(interaction: discord.Interaction, username: str):
    if not links.valid_username(username):
        await reply(interaction, "That isn't a valid Minecraft username (3–16 letters, numbers or _).")
        return

    await interaction.response.defer(ephemeral=True)

    try:
        profile = await asyncio.to_thread(links.lookup_minecraft, username)
    except links.LookupUnavailable as error:
        log(f"Mojang lookup failed: {error}")
        await reply(interaction, "Couldn't reach Mojang right now, please try again later.")
        return

    if profile is None:
        await reply(interaction, f"No Minecraft account named **{esc(username)}** exists.")
        return

    await save_link(interaction, links.MINECRAFT, profile, esc(profile["name"]))


@app_commands.command(name="unlink", description="Unlink one of your game accounts")
@app_commands.describe(game="Which account to unlink")
@app_commands.choices(game=GAME_CHOICES)
@app_commands.guild_only()
async def unlink_command(interaction: discord.Interaction, game: app_commands.Choice[str]):
    removed = interaction.client.store.unlink(
        interaction.guild_id, interaction.user.id, game.value
    )

    await reply(
        interaction,
        f"Your {game.name} account has been unlinked." if removed
        else f"You don't have a linked {game.name} account.",
    )


@app_commands.command(name="accounts", description="Show a member's linked game accounts")
@app_commands.describe(member="Member to look up (defaults to you)")
@app_commands.guild_only()
async def accounts_command(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    accounts = interaction.client.store.get(interaction.guild_id, member.id)

    if not accounts:
        await reply(interaction, f"{member.mention} hasn't linked any game accounts.")
        return

    embed = discord.Embed(
        title=f"{member.display_name}'s accounts",
        color=0xF7C948,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    brawl = accounts.get(links.BRAWL_STARS)
    if brawl:
        tag = brawl["id"].lstrip("#")
        name = f"{esc(brawl['name'])} " if brawl["name"] else ""
        embed.add_field(
            name="⭐ Brawl Stars",
            value=f"[{name}#{tag}](https://brawlify.com/stats/profile/{tag})",
            inline=False,
        )

    minecraft = accounts.get(links.MINECRAFT)
    if minecraft:
        embed.add_field(
            name="⛏️ Minecraft",
            value=f"[{esc(minecraft['name'])}](https://namemc.com/profile/{minecraft['id']})",
            inline=False,
        )

    await reply(interaction, None, embed=embed)


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")

    if not BRAWL_STARS_API_KEY:
        log("BRAWL_STARS_API_KEY not set: Brawl Stars tags won't be verified.")

    store = links.LinkStore(DB_FILE)

    try:
        LinkBot(store).run(token)
    finally:
        store.close()


if __name__ == "__main__":
    main()
