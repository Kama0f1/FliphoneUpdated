"""Paged public help and a separate restricted-tool reference."""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

import config


CALL_COMMANDS = [
    ("call", "/call", "Join the 1:1 queue."),
    ("hangup", "/hangup", "End this channel's call or leave its queue."),
    ("skip", "/skip", "End this channel's call and search again."),
    ("status", "/status", "Show this channel's call state."),
    ("block", "/block", "Block the connected server and end the call."),
    ("friendrequest", "/friendrequest", "Share your Discord username in the conversation."),
    ("anon", "/anon", "Toggle your tarot anon identity."),
    ("privacy", "/privacy", "View, enable, or disable message relay for your account."),
    ("notify", "/notify", "Toggle queue notification DMs."),
    ("addgif", "/addgif", "Submit a GIF URL for safe relay approval."),
    ("addemoji", "/addemoji", "Submit up to 5 custom server emojis in one command."),
    ("report", "/report", "Report the active or most recent conversation."),
    ("profile", "/profile", "Show your level, XP, ranks, and banner."),
    ("banner", "/banner", "Reroll your profile banner."),
    ("leaderboard", "/leaderboard", "Show the global user XP leaderboard."),
    ("serverlb", "/serverlb", "Show the global server XP leaderboard."),
    ("vote", "/vote", "Open Fliphone's top.gg page."),
]

ROOM_COMMANDS = [
    ("room", "/room", "Join an available room of up to 5 servers."),
    ("roomcreate", "/roomcreate", "Create a fresh room."),
    ("roomleave", "/roomleave", "Leave this channel's room."),
    ("roomskip", "/roomskip", "Leave and find a different room."),
    ("roomstatus", "/roomstatus", "Show stations and room state."),
    ("roomkick", "/roomkick", "Start a station vote-kick."),
    ("friendrequest", "/friendrequest", "Share with everyone or one station."),
    ("block", "/block", "Block a station and leave the room."),
    ("report", "/report", "Report one station."),
]

ADMIN_COMMANDS = [
    ("setup", "/setup", "Reset and configure Fliphone in one step."),
    ("check", "/check", "Diagnose permissions, webhook, and current state."),
    ("repair", "/repair", "Rebuild a damaged setup."),
    ("teardown", "@Fliphone teardown", "Remove setup after confirmation."),
    ("blocklist", "/blocklist", "List servers blocked by this server."),
    ("unblock", "/unblock", "Remove a server block."),
    ("kick", "/kick", "End the call in the channel where it is run."),
]

SUDO_COMMANDS = [
    ("sudohelp", "@Fliphone sudohelp", "Show restricted tools."),
    ("dbstatus", "@Fliphone dbstatus", "Check safe database health and row counts."),
    ("ban", "@Fliphone ban <user_id> [reason]", "Apply a bot-wide user ban."),
    ("unban", "@Fliphone unban <user_id>", "Remove a bot-wide user ban."),
    ("serverban", "@Fliphone serverban <server_id> [reason]", "Ban a server and remove Fliphone from it."),
    ("serverunban", "@Fliphone serverunban <server_id>", "Remove a bot-wide server ban."),
    ("notifyignore", "@Fliphone notifyignore <user_id>", "Exclude a tester from queue broadcasts."),
    ("servers", "@Fliphone servers", "List servers containing the bot."),
    ("leaveserver", "@Fliphone leaveserver <server_id>", "Force the bot to leave a server."),
    ("censor", "@Fliphone censor <word>", "Toggle a custom censored word."),
    ("censorlist", "@Fliphone censorlist", "List custom censored words."),
    ("gifreports", "@Fliphone gifreports", "Review reported GIFs."),
    ("gifbl", "@Fliphone gifbl <id or url>", "Blacklist a GIF report or URL."),
    ("gifwl", "@Fliphone gifwl <id or url>", "Whitelist a GIF report or URL."),
    ("gifcheck", "@Fliphone gifcheck <url>", "Check GIF whitelist/blacklist status."),
    ("emojicleanup", "@Fliphone emojicleanup [days] [limit]", "Delete unused or stale mirrored app emojis."),
    ("userreports", "@Fliphone userreports", "Review conversation reports."),
    ("resolvereport", "@Fliphone resolvereport <id>", "Resolve a conversation report."),
]

PUBLIC_ALIASES = {
    "c": "call",
    "h": "hangup",
    "s": "skip",
    "fr": "friendrequest",
    "notifications": "notify",
    "addemojis": "addemoji",
    "addemote": "addemoji",
    "submitemoji": "addemoji",
    "r": "room",
    "rc": "roomcreate",
    "rl": "roomleave",
    "rs": "roomskip",
    "rst": "roomstatus",
    "rk": "roomkick",
    "setupcheck": "check",
    "doctor": "check",
    "fixsetup": "repair",
    "fix": "repair",
    "blocked": "blocklist",
    "remove": "teardown",
}

SUDO_ALIASES = {
    "ownerhelp": "sudohelp",
    "modhelp": "sudohelp",
    "database": "dbstatus",
    "db": "dbstatus",
}

ALL_PUBLIC = {item[0]: item for item in CALL_COMMANDS + ROOM_COMMANDS + ADMIN_COMMANDS}
ALL_SUDO = {item[0]: item for item in SUDO_COMMANDS}


def _command_lines(items: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"`{usage}` - {description}" for _name, usage, description in items)


def _add_command_fields(embed: discord.Embed, title: str, items: list[tuple[str, str, str]]) -> None:
    lines = [f"`{usage}` - {description}" for _name, usage, description in items]
    chunk: list[str] = []
    index = 1
    for line in lines:
        candidate = "\n".join(chunk + [line])
        if chunk and len(candidate) > 1000:
            name = title if index == 1 else f"{title} {index}"
            embed.add_field(name=name, value="\n".join(chunk), inline=False)
            chunk = [line]
            index += 1
        else:
            chunk.append(line)
    if chunk:
        name = title if index == 1 else f"{title} {index}"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)


def _page_embed(page: int) -> discord.Embed:
    if page == 0:
        embed = discord.Embed(
            title="Fliphone Help - Calls & Profile",
            description="Commands for 1:1 conversations and your Fliphone profile.",
            color=config.COLOR_WAIT,
        )
        _add_command_fields(embed, "Commands", CALL_COMMANDS)
    elif page == 1:
        embed = discord.Embed(
            title="Fliphone Help - Rooms",
            description=(
                "Rooms connect up to five servers as named stations. They begin when a second "
                "server joins and close after ten minutes without a relayed text message or GIF."
            ),
            color=config.COLOR_WAIT,
        )
        _add_command_fields(embed, "Commands", ROOM_COMMANDS)
    else:
        embed = discord.Embed(
            title="Fliphone Help - Server Setup",
            description="These commands require Manage Channels in the server.",
            color=config.COLOR_WAIT,
        )
        _add_command_fields(embed, "Commands", ADMIN_COMMANDS)
    embed.set_footer(text="Use /help for details. Restricted tools use @Fliphone commands.")
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

    @commands.hybrid_command(name="help", aliases=["commands", "cmds"])
    async def help(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        if command:
            key = command.strip().lower().removeprefix("/")
            key = PUBLIC_ALIASES.get(key, key)
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
            key = command.strip().lower().removeprefix("@fliphone ")
            key = SUDO_ALIASES.get(key, key)
            entry = ALL_SUDO.get(key)
            if not entry:
                await ctx.send(f"No restricted command named `{command}`.")
                return
            _name, usage, description = entry
            await ctx.send(embed=discord.Embed(title=usage, description=description, color=config.COLOR_WAIT))
            return
        embed = discord.Embed(
            title="Fliphone Restricted Tools",
            color=config.COLOR_WAIT,
        )
        _add_command_fields(embed, "Commands", SUDO_COMMANDS)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Help(bot))
