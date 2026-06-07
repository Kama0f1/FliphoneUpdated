"""Help commands for Fliphone."""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

import config


PUBLIC_COMMANDS = [
    ("Calling", "call", ["c", "dial", "connect"], "f.call", "Join the 1:1 queue or connect instantly.", "Everyone"),
    ("Calling", "hangup", ["h", "disconnect", "bye"], "f.hangup", "End a call or leave the queue.", "Everyone"),
    ("Calling", "skip", ["s", "next"], "f.skip", "Leave the current call and search again.", "Everyone"),
    ("Calling", "status", ["pbstatus"], "f.status", "Show this channel's call/queue state.", "Everyone"),
    ("Calling", "block", [], "f.block", "Block the server you are currently talking to.", "Everyone"),
    ("Calling", "friendrequest", ["fr"], "f.fr", "Share your Discord username during a call.", "Everyone"),
    ("Calling", "notify", ["notifications"], "f.notify", "Toggle queue notification DMs.", "Everyone"),
    ("Calling", "profile", ["settings", "me"], "f.profile", "Show your notification setting and current server/channel state.", "Everyone"),
    ("Calling", "report", [], "f.report", "Report your active or most recent call.", "Everyone"),
    ("Rooms", "room", ["r"], "f.room", "Join a group room with up to 6 servers.", "Everyone"),
    ("Rooms", "roomleave", ["rl"], "f.roomleave", "Leave your current group room.", "Everyone"),
    ("Rooms", "roomskip", ["rs"], "f.roomskip", "Leave and immediately search for a new room.", "Everyone"),
    ("Rooms", "roomstatus", ["rst"], "f.roomstatus", "Show current room status.", "Everyone"),
    ("Rooms", "roomkick", ["rk"], "f.roomkick <station>", "Start a room vote-kick.", "Everyone"),
    ("Server Admin", "setup", [], "f.setup [#channel]", "Reset and fully set up Fliphone in one step.", "Manage Channels"),
    ("Server Admin", "check", ["setupcheck", "doctor"], "f.check", "Diagnose broken setup, permissions, webhook, and state.", "Manage Channels"),
    ("Server Admin", "repair", ["fixsetup", "fix"], "f.repair [#channel]", "Reset and rebuild the current setup.", "Manage Channels"),
    ("Server Admin", "teardown", ["remove"], "f.teardown", "Fully remove Fliphone so setup starts clean.", "Manage Channels"),
    ("Server Admin", "anon", ["mask", "anonymous"], "f.anon", "Toggle anonymous relay mode for this server.", "Server members"),
    ("Server Admin", "gifmode", [], "f.gifmode <enabled|limited|disabled>", "Set this server's GIF relay mode.", "Server Admin"),
    ("Server Admin", "blocklist", ["blocked"], "f.blocklist", "List servers blocked by this server.", "Manage Channels"),
    ("Server Admin", "unblock", [], "f.unblock <server_id>", "Remove a server from your blocklist.", "Manage Channels"),
    ("Server Admin", "kick", [], "f.kick", "Force-disconnect this server's active call.", "Manage Channels"),
    ("Info", "stats", [], "f.stats", "Show global call, queue, and server stats.", "Everyone"),
    ("Info", "invite", [], "f.invite", "Get the bot invite link.", "Everyone"),
    ("Info", "ping", [], "f.ping", "Check bot latency.", "Everyone"),
]

SUDO_COMMANDS = [
    ("Owner", "sudohelp", [], "f.sudohelp", "Show this restricted command list.", "Owner / trusted mods"),
    ("Owner", "dbstatus", ["database", "db"], "f.dbstatus", "Show database backend health and safe row counts.", "Owner / trusted mods"),
    ("Owner", "ban", [], "f.ban <user_id> [reason]", "Bot-wide user ban.", "Owner only"),
    ("Owner", "unban", [], "f.unban <user_id>", "Lift a bot-wide user ban.", "Owner only"),
    ("Owner", "notifyignore", [], "f.notifyignore <user_id>", "Exclude a tester from queue notify broadcasts.", "Owner only"),
    ("Owner", "censor", [], "f.censor <word>", "Toggle a custom censored word.", "Owner only"),
    ("Owner", "censorlist", [], "f.censorlist", "List custom censored words.", "Owner only"),
    ("Owner", "guilds", ["servers", "serverlist"], "f.guilds", "List guilds the bot is in.", "Owner only"),
    ("Owner", "leaveguild", ["leaveserver"], "f.leaveguild <guild_id>", "Force the bot to leave a guild.", "Owner only"),
    ("Moderation", "gifreports", [], "f.gifreports", "Open the interactive GIF report panel.", "Owner / trusted mods"),
    ("Moderation", "gifbl", [], "f.gifbl <id/url>", "Blacklist a GIF report or URL.", "Owner / trusted mods"),
    ("Moderation", "gifwl", [], "f.gifwl <id/url>", "Whitelist a GIF report or URL.", "Owner / trusted mods"),
    ("Moderation", "gifcheck", [], "f.gifcheck <url>", "Check GIF blacklist/whitelist status.", "Owner / trusted mods"),
    ("Moderation", "userreports", [], "f.userreports", "Open the interactive call report panel.", "Owner / trusted mods"),
    ("Moderation", "resolvereport", [], "f.resolvereport <id>", "Resolve a call report by ID.", "Owner / trusted mods"),
]


def build_command_map(commands_list: list[tuple]) -> dict[str, tuple]:
    command_map: dict[str, tuple] = {}
    for entry in commands_list:
        _, name, aliases, *_ = entry
        command_map[name.lower()] = entry
        for alias in aliases:
            command_map[alias.lower()] = entry
    return command_map


PUBLIC_MAP = build_command_map(PUBLIC_COMMANDS)
SUDO_MAP = build_command_map(SUDO_COMMANDS)


class Help(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _can_use_sudohelp(self, ctx: commands.Context) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True
        if ctx.author.id in config.TRUSTED_MOD_IDS:
            return True
        return False

    def _detail_embed(self, entry: tuple) -> discord.Embed:
        category, name, aliases, usage, description, permission = entry
        embed = discord.Embed(
            title=f"`{config.PREFIX}{name}`",
            description=description,
            color=config.COLOR_WAIT,
        )
        embed.add_field(name="Usage", value=f"`{usage}`", inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Permission", value=permission, inline=True)
        if aliases:
            embed.add_field(
                name="Aliases",
                value="  ".join(f"`{config.PREFIX}{alias}`" for alias in aliases),
                inline=False,
            )
        embed.set_footer(text=f"Tip: f.help <command> | {config.FOOTER}")
        return embed

    def _overview_embed(self, commands_list: list[tuple], *, sudo: bool = False) -> discord.Embed:
        title = "Fliphone Sudo Help" if sudo else "Fliphone Help"
        description = (
            "Restricted owner/trusted-mod tools."
            if sudo
            else "User and server admin commands. Run `f.help <command>` for details."
        )
        embed = discord.Embed(title=title, description=description, color=0x5865F2)
        categories: dict[str, list[tuple]] = {}
        for entry in commands_list:
            categories.setdefault(entry[0], []).append(entry)
        for category, entries in categories.items():
            lines = [
                f"`{usage}` - {description}"
                for _, _, _, usage, description, _ in entries
            ]
            embed.add_field(name=category, value="\n".join(lines), inline=False)
        footer = "Use f.sudohelp for restricted tools." if not sudo else config.FOOTER
        embed.set_footer(text=footer)
        return embed

    @commands.command(name="help", aliases=["commands", "cmds"])
    async def help(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        """Show user/server-admin help."""
        if command:
            key = command.strip().lower().removeprefix(config.PREFIX.lower())
            entry = PUBLIC_MAP.get(key)
            if not entry:
                await ctx.send(
                    embed=discord.Embed(
                        description=f"No public/admin command named `{command}`. Restricted tools are in `f.sudohelp`.",
                        color=config.COLOR_ERR,
                    )
                )
                return
            await ctx.send(embed=self._detail_embed(entry))
            return
        await ctx.send(embed=self._overview_embed(PUBLIC_COMMANDS))

    @commands.command(name="sudohelp", aliases=["ownerhelp", "modhelp"])
    async def sudohelp(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        """Show restricted owner/trusted-mod help."""
        if not await self._can_use_sudohelp(ctx):
            await ctx.send("You do not have permission to view restricted commands.")
            return
        if command:
            key = command.strip().lower().removeprefix(config.PREFIX.lower())
            entry = SUDO_MAP.get(key)
            if not entry:
                await ctx.send(f"No restricted command named `{command}`.")
                return
            await ctx.send(embed=self._detail_embed(entry))
            return
        await ctx.send(embed=self._overview_embed(SUDO_COMMANDS, sudo=True))


async def setup(bot):
    await bot.add_cog(Help(bot))
