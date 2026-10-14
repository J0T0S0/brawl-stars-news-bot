"""Discord bot that lets members link their Minecraft (Java) account.

Slash commands:
  /link <username>    link your Minecraft account
  /unlink             remove your link
  /minecraft [member] show the Minecraft account linked to a member

Privacy: links are deleted as soon as a member leaves the server, when the
bot is removed from a server, and on startup for anyone who left while the
bot was offline.

Unlike bot.py this needs a long-running process (it must answer commands),
so it is not run by GitHub Actions. Requires DISCORD_BOT_TOKEN and the
"Server Members Intent" enabled in the Discord developer portal.
"""

import asyncio
import os

import discord
from discord import app_commands

import links

DB_FILE = os.environ.get("LINKS_DB", "links.db")


def log(message):
    print(message, flush=True)


class LinkBot(discord.Client):

    def __init__(self, store):
        intents = discord.Intents.default()
        # Needed for on_member_remove and the startup member scan.
        intents.members = True
        super().__init__(intents=intents)
        self.store = store
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(link_command)
        self.tree.add_command(unlink_command)
        self.tree.add_command(minecraft_command)
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
                log(f"Removed {removed} link(s) of former members of {guild.name}.")

    async def on_member_remove(self, member):
        if self.store.unlink(member.guild.id, member.id):
            log(f"Removed link of {member.id} (left {member.guild.name}).")

    async def on_guild_remove(self, guild):
        removed = self.store.forget_guild(guild.id)
        log(f"Removed {removed} link(s) for {guild.name} (bot removed).")


@app_commands.command(name="link", description="Link your Minecraft (Java) account")
@app_commands.describe(username="Your Minecraft username")
@app_commands.guild_only()
async def link_command(interaction: discord.Interaction, username: str):
    if not links.valid_username(username):
        await interaction.response.send_message(
            "That isn't a valid Minecraft username (3–16 letters, numbers or _).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        profile = await asyncio.to_thread(links.lookup_minecraft, username)
    except links.MojangUnavailable as error:
        log(f"Mojang lookup failed: {error}")
        await interaction.followup.send(
            "Couldn't reach Mojang right now, please try again later.",
            ephemeral=True,
        )
        return

    if profile is None:
        await interaction.followup.send(
            f"No Minecraft account named **{discord.utils.escape_markdown(username)}** exists.",
            ephemeral=True,
        )
        return

    store = interaction.client.store

    try:
        store.link(interaction.guild_id, interaction.user.id, profile["uuid"], profile["name"])
    except links.AlreadyLinked:
        await interaction.followup.send(
            f"**{discord.utils.escape_markdown(profile['name'])}** is already linked "
            "to another member of this server.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Linked to Minecraft account **{discord.utils.escape_markdown(profile['name'])}**. "
        "Use `/unlink` to remove it. Your link is deleted automatically if you leave the server.",
        ephemeral=True,
    )


@app_commands.command(name="unlink", description="Unlink your Minecraft account")
@app_commands.guild_only()
async def unlink_command(interaction: discord.Interaction):
    removed = interaction.client.store.unlink(interaction.guild_id, interaction.user.id)

    await interaction.response.send_message(
        "Your Minecraft account has been unlinked." if removed
        else "You don't have a linked Minecraft account.",
        ephemeral=True,
    )


@app_commands.command(name="minecraft", description="Show a member's linked Minecraft account")
@app_commands.describe(member="Member to look up (defaults to you)")
@app_commands.guild_only()
async def minecraft_command(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    profile = interaction.client.store.get(interaction.guild_id, member.id)

    if profile is None:
        await interaction.response.send_message(
            f"{member.mention} hasn't linked a Minecraft account.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    embed = discord.Embed(
        title=profile["name"],
        url=f"https://namemc.com/profile/{profile['uuid']}",
        description=f"Minecraft account of {member.mention}",
        color=0x5B8731,
    )
    embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{profile['uuid']}")
    embed.set_footer(text=f"UUID {profile['uuid']}")

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")

    store = links.LinkStore(DB_FILE)

    try:
        LinkBot(store).run(token)
    finally:
        store.close()


if __name__ == "__main__":
    main()
