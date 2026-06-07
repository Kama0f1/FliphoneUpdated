"""
cogs/vote.py – Top.gg vote reminders for Fliphone.

Sends periodic DMs to opted-in notify subscribers asking them to vote.
No rewards — just a polite nudge, with one persistent broadcast every 12 hours.
"""

from __future__ import annotations

import discord
from discord.ext import commands, tasks

import config
from database import Database


def _vote_url(bot_id: int) -> str:
    return f"https://top.gg/bot/{bot_id}/vote"


def _vote_embed(bot_id: int) -> discord.Embed:
    embed = discord.Embed(
        title="🗳️ Enjoying Fliphone?",
        description=(
            "Voting helps more servers discover Fliphone and keeps the network growing!\n\n"
            f"**[Click here to vote on top.gg](<{_vote_url(bot_id)}>)**\n\n"
            "It's free and only takes a second. Thank you! 🙏"
        ),
        color=0xFF3366,
    )
    embed.set_footer(text="Fliphone • You're receiving this because you have f.notify enabled.")
    return embed


class Vote(commands.Cog):
    """Handles top.gg vote reminder DMs."""

    JOB_KEY = "vote_reminder_broadcast"
    REMIND_COOLDOWN = 12 * 60 * 60

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        self._vote_reminder_loop.start()

    def cog_unload(self) -> None:
        self._vote_reminder_loop.cancel()

    # Check often, but the persistent database schedule permits one run per 12 h.

    @tasks.loop(minutes=5)
    async def _vote_reminder_loop(self) -> None:
        if not await self.db.claim_scheduled_job(self.JOB_KEY, self.REMIND_COOLDOWN):
            return

        subscribers = await self.db.get_notify_subscribers()
        for uid in subscribers:
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    await user.send(embed=_vote_embed(self.bot.user.id))
            except (discord.Forbidden, discord.HTTPException):
                pass

    @_vote_reminder_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

async def setup(bot) -> None:
    await bot.add_cog(Vote(bot))
