"""
cogs/misc.py — small utility commands (ping, shards)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config


class Misc(commands.Cog, name="Misc"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Measure gateway, command, REST, and DB latency."""
        started = time.perf_counter()
        received_delay_ms = max(
            0,
            int((datetime.now(timezone.utc) - ctx.message.created_at).total_seconds() * 1000),
        )
        probe = await ctx.send("Pinging...")
        sent_ms = int((time.perf_counter() - started) * 1000)

        db_ms: int | None = None
        db_ok = None
        db_started = time.perf_counter()
        try:
            db_ok = await self.bot.db.ping()
            db_ms = int((time.perf_counter() - db_started) * 1000)
        except Exception:
            db_ms = int((time.perf_counter() - db_started) * 1000)
            db_ok = False

        ws_latency = self.bot.latency or 0.0
        backend = getattr(self.bot.db, "backend", "unknown")
        embed = discord.Embed(
            title="Pong",
            color=config.COLOR_OK if sent_ms < 1000 and received_delay_ms < 1500 else config.COLOR_WARN,
        )
        embed.add_field(name="Gateway", value=f"{ws_latency*1000:.0f} ms", inline=True)
        embed.add_field(name="Command Delay", value=f"{received_delay_ms} ms", inline=True)
        embed.add_field(name="Send Roundtrip", value=f"{sent_ms} ms", inline=True)
        embed.add_field(
            name="Database",
            value=f"{'OK' if db_ok else 'Failed'} ({db_ms} ms, {backend})",
            inline=False,
        )
        embed.set_footer(text="Command Delay is how long it took Discord to reach the bot.")
        await probe.edit(content=None, embed=embed)

    @commands.command(name="shards")
    async def shards(self, ctx: commands.Context) -> None:
        """Show sharding info (if sharded)."""
        shard_count = getattr(self.bot, "shard_count", None)
        shard_id = getattr(self.bot, "shard_id", None)
        if shard_count is None:
            await ctx.send("This bot is not using explicit sharding (auto mode).")
            return
        await ctx.send(f"Shard: {shard_id} / {shard_count}")


async def setup(bot: commands.Bot) -> None:  # for discord.py loader
    await bot.add_cog(Misc(bot))
