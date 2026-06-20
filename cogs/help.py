"""Paged public help and a separate restricted-tool reference."""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

import config


CALL_COMMANDS = [
    ("call", "f.call", "Join the 1:1 queue."),
    ("hangup", "f.hangup", "End this channel's call or leave its queue."),
    ("skip", "f.skip", "End this channel's call and search again."),
    ("status", "f.status", "Show this channel's call state."),
    ("block", "f.block", "Block the connected server and end the call."),
    ("friendrequest", "f.fr", "Share your Discord username in the conversation."),
    ("mask", "f.mask", "Toggle your personal Stranger identity."),
    ("notify", "f.notify", "Toggle queue notification DMs."),
    ("addgif", "f.addgif", "Submit a GIF URL for safe relay approval."),
    ("report", "f.report", "Report the active or most recent conversation."),
    ("profile", "f.profile", "Show your level, XP, ranks, and banner."),
    ("banner", "f.banner", "Reroll your profile banner."),
    ("leaderboard", "f.lb", "Show the global user XP leaderboard."),
    ("serverlb", "f.serverlb", "Show the global server XP leaderboard."),
    ("vote", "f.vote or /vote", "Open Fliphone's top.gg page."),
]

ROOM_COMMANDS = [
    ("room", "f.room", "Join an available room of up to 5 servers."),
    ("roomcreate", "f.roomcreate", "Create a fresh room."),
    ("roomleave", "f.roomleave", "Leave this channel's room."),
    ("roomskip", "f.roomskip", "Leave and find a different room."),
    ("roomstatus", "f.roomstatus", "Show stations and room state."),
    ("roomkick", "f.roomkick <station>", "Start a station vote-kick."),
    ("friendrequest", "f.fr [station]", "Share with everyone or one station."),
    ("block", "f.block <station>", "Block a station and leave the room."),
    ("report", "f.report <station>", "Report one station."),
]

ADMIN_COMMANDS = [
    ("setup", "f.setup [#channel]", "Reset and configure Fliphone in one step."),
    ("check", "f.check", "Diagnose permissions, webhook, and current state."),
    ("repair", "f.repair [#channel]", "Rebuild a damaged setup."),
    ("teardown", "f.teardown", "Remove setup after confirmation."),
    ("blocklist", "f.blocklist", "List servers blocked by this server."),
    ("unblock", "f.unblock <server_id>", "Remove a server block."),
    ("kick", "f.kick", "End the call in the channel where it is run."),
]

SUDO_COMMANDS = [
    ("sudohelp", "f.sudohelp", "Show restricted tools."),
    ("dbstatus", "f.dbstatus", "Check safe database health and row counts."),
    ("ban", "f.ban <user_id> [reason]", "Apply a bot-wide user ban."),
    ("unban", "f.unban <user_id>", "Remove a bot-wide user ban."),
    ("serverban", "f.serverban <server_id> [reason]", "Ban a server and remove Fliphone from it."),
    ("serverunban", "f.serverunban <server_id>", "Remove a bot-wide server ban."),
    ("notifyignore", "f.notifyignore <user_id>", "Exclude a tester from queue broadcasts."),
    ("servers", "f.servers", "List servers containing the bot."),
    ("leaveserver", "f.leaveserver <server_id>", "Force the bot to leave a server."),
    ("censor", "f.censor <word>", "Toggle a custom censored word."),
    ("censorlist", "f.censorlist", "List custom censored words."),
    ("gifreports", "f.gifreports", "Review reported GIFs."),
    ("userreports", "f.userreports", "Review conversation reports."),
    ("resolvereport", "f.resolvereport <id>", "Resolve a conversation report."),
]

ALL_PUBLIC = {name: item for item in CALL_COMMANDS + ROOM_COMMANDS + ADMIN_COMMANDS for name in [item[0]]}
ALL_SUDO = {name: item for item in SUDO_COMMANDS for name in [item[0]]}


def _command_lines(items: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"`{usage}` - {description}" for _name, usage, description in items)


def _page_embed(page: int) -> discord.Embed:
    if page == 0:
        embed = discord.Embed(
            title="Fliphone Help - Calls & Profile",
            description="Commands for 1:1 conversations and your Fliphone profile.",
            color=config.COLOR_WAIT,
        )
        embed.add_field(name="Commands", value=_command_lines(CALL_COMMANDS), inline=False)
    elif page == 1:
        embed = discord.Embed(
            title="Fliphone Help - Rooms",
            description=(
                "Rooms connect up to five servers as named stations. They begin when a second "
                "server joins and close after ten minutes without a relayed text message or GIF."
            ),
            color=config.COLOR_WAIT,
        )
        embed.add_field(name="Commands", value=_command_lines(ROOM_COMMANDS), inline=False)
    else:
        embed = discord.Embed(
            title="Fliphone Help - Server Setup",
            description="These commands require Manage Channels in the server.",
            color=config.COLOR_WAIT,
        )
        embed.add_field(name="Commands", value=_command_lines(ADMIN_COMMANDS), inline=False)
    embed.set_footer(text="Use f.help <command> for details. Restricted tools: f.sudohelp")
    return embed


class HelpView(discord.ui.View):
    def __init__(self, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.page = page
        self.add_item(discord.ui.Button(label="Support", style=discord.ButtonStyle.link, url=config.SUPPORT_URL))
        self.add_item(discord.ui.Button(label="Top.gg", style=discord.ButtonStyle.link, url=config.TOPGG_URL))
        self.add_item(discord.ui.Button(label="Privacy", style=discord.ButtonStyle.link, url=config.PRIVACY_URL))
        self.add_item(discord.ui.Button(label="Terms", style=discord.ButtonStyle.link, url=config.TOS_URL))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                item.disabled = item.custom_id == f"help:{self.page}"

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        self.page = page
        self._sync_buttons()
        await interaction.response.edit_message(embed=_page_embed(page), view=self)

    @discord.ui.button(label="Calls", style=discord.ButtonStyle.primary, custom_id="help:0")
    async def calls(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._show(interaction, 0)

    @discord.ui.button(label="Rooms", style=discord.ButtonStyle.primary, custom_id="help:1")
    async def rooms(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._show(interaction, 1)

    @discord.ui.button(label="Server Setup", style=discord.ButtonStyle.primary, custom_id="help:2")
    async def setup(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._show(interaction, 2)

class Help(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _can_use_sudohelp(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author) or ctx.author.id in config.TRUSTED_MOD_IDS

    @commands.command(name="help", aliases=["commands", "cmds"])
    async def help(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        if command:
            key = command.strip().lower().removeprefix(config.PREFIX.lower())
            entry = ALL_PUBLIC.get(key)
            if not entry:
                await ctx.send(f"No public command named `{command}`.")
                return
            _name, usage, description = entry
            embed = discord.Embed(title=usage, description=description, color=config.COLOR_WAIT)
            await ctx.send(embed=embed)
            return
        await ctx.send(embed=_page_embed(0), view=HelpView())

    @commands.command(name="sudohelp", aliases=["ownerhelp", "modhelp"])
    async def sudohelp(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        if not await self._can_use_sudohelp(ctx):
            await ctx.send("You do not have permission to view restricted commands.")
            return
        if command:
            key = command.strip().lower().removeprefix(config.PREFIX.lower())
            entry = ALL_SUDO.get(key)
            if not entry:
                await ctx.send(f"No restricted command named `{command}`.")
                return
            _name, usage, description = entry
            await ctx.send(embed=discord.Embed(title=usage, description=description, color=config.COLOR_WAIT))
            return
        embed = discord.Embed(
            title="Fliphone Restricted Tools",
            description=_command_lines(SUDO_COMMANDS),
            color=config.COLOR_WAIT,
        )
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Help(bot))
