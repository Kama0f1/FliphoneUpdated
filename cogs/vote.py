"""
cogs/vote.py – Top.gg vote reminders for Fliphone.

Sends periodic DMs to opted-in notify subscribers asking them to vote.
No rewards — just a polite nudge, max once every 12 hours per user.
"""

from __future__ import annotations

import time

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

    # 12 hours between reminders per user
    REMIND_COOLDOWN = 12 * 60 * 60

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        # user_id → monotonic timestamp of last vote DM sent this session
        self._last_reminded: dict[int, float] = {}
        self._vote_reminder_loop.start()

    def cog_unload(self) -> None:
        self._vote_reminder_loop.cancel()

    # ── Periodic task: every 12 h DM all notify subscribers ──────────────────

    @tasks.loop(hours=12)
    async def _vote_reminder_loop(self) -> None:
        subscribers = await self.db.get_notify_subscribers()
        now = time.monotonic()
        for uid in subscribers:
            if now - self._last_reminded.get(uid, 0) < self.REMIND_COOLDOWN:
                continue
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    await user.send(embed=_vote_embed(self.bot.user.id))
                    self._last_reminded[uid] = now
            except (discord.Forbidden, discord.HTTPException):
                pass

    @_vote_reminder_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Public helper called from phonebooth after a queue notify DM ──────────

    async def maybe_send_vote_dm(self, user_id: int) -> None:
        """
        Send a vote reminder to one user if 12 hours have passed since
        their last reminder. Called alongside queue notify DMs so it
        piggybacks without spamming.
        """
        now = time.monotonic()
        if now - self._last_reminded.get(user_id, 0) < self.REMIND_COOLDOWN:
            return
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if user:
                await user.send(embed=_vote_embed(self.bot.user.id))
                self._last_reminded[user_id] = now
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot) -> None:
    await bot.add_cog(Vote(bot))
