"""User GIF submissions and trusted-moderator review controls."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import time

import asyncpg
import discord
from discord.ext import commands

import config
from relay_policy import extract_gif_candidate


CAPTURE_SECONDS = 60
MAX_SUBMISSIONS_PER_HOUR = 3


class GifSubmissionReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if interaction.user.id in config.TRUSTED_MOD_IDS or await bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message("This review panel is restricted.", ephemeral=True)
        return False

    async def _review(self, interaction: discord.Interaction, action: str) -> None:
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("Submission metadata is missing.", ephemeral=True)
            return
        footer = interaction.message.embeds[0].footer.text or ""
        try:
            submission_id = int(footer.removeprefix("Submission #").split()[0])
        except ValueError:
            await interaction.response.send_message("Submission metadata is invalid.", ephemeral=True)
            return
        await interaction.response.defer()
        submission = await interaction.client.db.review_gif_submission(
            submission_id, interaction.user.id, action
        )
        if not submission:
            await interaction.followup.send("This submission was already reviewed.", ephemeral=True)
            return
        embed = interaction.message.embeds[0].copy()
        embed.color = {
            "approved": config.COLOR_OK,
            "rejected": config.COLOR_WARN,
            "blacklisted": config.COLOR_ERR,
        }[action]
        embed.add_field(
            name="Decision",
            value=f"{action.title()} by {interaction.user.mention}",
            inline=False,
        )
        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="gif_submit:approve")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.secondary, custom_id="gif_submit:reject")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "rejected")

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.danger, custom_id="gif_submit:blacklist")
    async def blacklist(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "blacklisted")


class GifSubmission(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self._captures: dict[tuple[int, int], float] = {}
        self._consumed: dict[int, float] = {}
        self._lock = asyncio.Lock()

    def is_consumed(self, message_id: int) -> bool:
        expiry = self._consumed.get(message_id, 0)
        if expiry <= time.monotonic():
            self._consumed.pop(message_id, None)
            return False
        return True

    async def consume_capture(self, message: discord.Message) -> bool:
        key = (message.author.id, message.channel.id)
        async with self._lock:
            expiry = self._captures.get(key, 0)
            if expiry <= time.monotonic():
                self._captures.pop(key, None)
                return False
            url = extract_gif_candidate(message)
            if not url:
                return False
            self._captures.pop(key, None)
            self._consumed[message.id] = time.monotonic() + 120

        since = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        if await self.db.count_recent_gif_submissions(message.author.id, since) >= MAX_SUBMISSIONS_PER_HOUR:
            await message.channel.send(
                "You have reached the limit of 3 GIF submissions per hour.", delete_after=8
            )
            return True
        if await self.db.get_pending_gif_submission_by_url(url):
            await message.channel.send("That GIF is already waiting for review.", delete_after=8)
            return True
        try:
            submission_id = await self.db.add_gif_submission(
                url, message.author.id, message.guild.id, message.channel.id
            )
        except (asyncpg.UniqueViolationError, Exception) as exc:
            if isinstance(exc, asyncpg.UniqueViolationError) or "UNIQUE constraint" in str(exc):
                await message.channel.send("That GIF is already waiting for review.", delete_after=8)
                return True
            raise

        review_channel = self.bot.get_channel(config.GIF_REVIEW_CHANNEL_ID)
        if review_channel:
            embed = discord.Embed(title="GIF Whitelist Submission", color=config.COLOR_WAIT)
            embed.description = url
            embed.add_field(name="Submitted by", value=f"{message.author} (`{message.author.id}`)", inline=False)
            embed.add_field(name="Server / channel", value=f"{message.guild.name} / {message.channel.mention}", inline=False)
            embed.set_image(url=url)
            embed.set_footer(text=f"Submission #{submission_id}")
            review_message = await review_channel.send(embed=embed, view=GifSubmissionReviewView())
            await self.db.set_gif_submission_review_message(submission_id, review_message.id)
        await message.channel.send(
            f"GIF submission #{submission_id} was sent for review. It was not relayed.",
            delete_after=10,
        )
        return True

    @commands.hybrid_command(name="addgif")
    @commands.guild_only()
    async def addgif(self, ctx: commands.Context) -> None:
        """Capture the next GIF sent by this user in this channel for review."""
        self._captures[(ctx.author.id, ctx.channel.id)] = time.monotonic() + CAPTURE_SECONDS
        await ctx.send(
            "Send the GIF you want to submit in this channel within 60 seconds. "
            "It will stay local and will not be relayed."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        await self.consume_capture(message)


async def setup(bot) -> None:
    bot.add_view(GifSubmissionReviewView())
    await bot.add_cog(GifSubmission(bot))
