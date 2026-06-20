"""Owner-only commands for auditing server membership."""

from __future__ import annotations

import io

import discord
from discord.ext import commands

import config


class GuildAudit(commands.Cog, name="GuildAudit"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _is_global_mod(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author) or ctx.author.id in config.TRUSTED_MOD_IDS

    @commands.command(name="servers", aliases=["serverlist"], hidden=True)
    @commands.is_owner()
    async def servers(self, ctx: commands.Context) -> None:
        """List all servers the bot is currently in (owner only)."""
        guilds = sorted(
            self.bot.guilds,
            key=lambda guild: guild.member_count or 0,
            reverse=True,
        )

        lines = []
        for index, guild in enumerate(guilds, start=1):
            members = guild.member_count if guild.member_count is not None else "?"
            owner_id = guild.owner_id if guild.owner_id is not None else "?"
            lines.append(
                f"{index}. {guild.name} | id={guild.id} | owner={owner_id} | members={members}"
            )

        summary = f"Total servers: {len(guilds)}"
        body = "\n".join(lines) if lines else "No servers found."
        content = f"{summary}\n\n{body}"

        if len(content) <= 1900:
            await ctx.send(f"```\n{content}\n```")
            return

        # Fallback to file when the list is too long for one message.
        data = io.BytesIO(content.encode("utf-8"))
        await ctx.send(
            summary,
            file=discord.File(data, filename="server_audit.txt"),
        )

    @commands.command(name="leaveserver", hidden=True)
    @commands.is_owner()
    async def leaveserver(self, ctx: commands.Context, server_id: int) -> None:
        """Force the bot to leave a server by ID (owner only)."""
        guild = self.bot.get_guild(server_id)
        if guild is None:
            await ctx.send(f"I am not in server ID {server_id}.")
            return

        guild_name = guild.name
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to leave {guild_name} ({server_id}): {exc}")
            return

        await ctx.send(f"Left server: {guild_name} ({server_id})")

    @commands.command(name="serverban", aliases=["banguild"], hidden=True)
    async def serverban(
        self, ctx: commands.Context, server_id: int, *, reason: str = "No reason given"
    ) -> None:
        """Ban a server from Fliphone and leave it."""
        if not await self._is_global_mod(ctx):
            await ctx.send("You do not have permission to ban servers.")
            return
        guild = self.bot.get_guild(server_id)
        name = guild.name if guild else f"Server {server_id}"
        admin = self.bot.get_cog("Admin")
        if admin:
            await admin.ban_server_globally(server_id, ctx.author.id, reason)
        else:
            await self.bot.db.ban_guild(server_id, ctx.author.id, reason)
            if guild:
                await guild.leave()
        await ctx.send(f"🔨 **{name}** (`{server_id}`) is banned from Fliphone.\nReason: {reason}")

    @commands.command(name="serverunban", aliases=["unbanguild"], hidden=True)
    async def serverunban(self, ctx: commands.Context, server_id: int) -> None:
        """Remove a global Fliphone server ban."""
        if not await self._is_global_mod(ctx):
            await ctx.send("You do not have permission to unban servers.")
            return
        removed = await self.bot.db.unban_guild(server_id)
        await ctx.send(
            f"✅ Server `{server_id}` was unbanned."
            if removed else f"Server `{server_id}` is not banned."
        )


async def setup(bot) -> None:
    await bot.add_cog(GuildAudit(bot))
