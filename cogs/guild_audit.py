"""Owner-only commands for auditing guild membership."""

from __future__ import annotations

import io

import discord
from discord.ext import commands


class GuildAudit(commands.Cog, name="GuildAudit"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="guilds", aliases=["servers", "serverlist"])
    @commands.is_owner()
    async def guilds(self, ctx: commands.Context) -> None:
        """List all guilds the bot is currently in (owner only)."""
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

        summary = f"Total guilds: {len(guilds)}"
        body = "\n".join(lines) if lines else "No guilds found."
        content = f"{summary}\n\n{body}"

        if len(content) <= 1900:
            await ctx.send(f"```\n{content}\n```")
            return

        # Fallback to file when the list is too long for one message.
        data = io.BytesIO(content.encode("utf-8"))
        await ctx.send(
            summary,
            file=discord.File(data, filename="guild_audit.txt"),
        )

    @commands.command(name="leaveguild", aliases=["leaveserver"])
    @commands.is_owner()
    async def leaveguild(self, ctx: commands.Context, guild_id: int) -> None:
        """Force the bot to leave a guild by ID (owner only)."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.send(f"I am not in guild ID {guild_id}.")
            return

        guild_name = guild.name
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to leave {guild_name} ({guild_id}): {exc}")
            return

        await ctx.send(f"Left guild: {guild_name} ({guild_id})")


async def setup(bot) -> None:
    await bot.add_cog(GuildAudit(bot))