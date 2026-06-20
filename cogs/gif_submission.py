"""User GIF submissions and trusted-moderator review controls."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import time
from collections import defaultdict, deque

import asyncpg
import discord
from discord.ext import commands

import config
from relay_policy import extract_gif_candidate


CAPTURE_SECONDS = 60
MAX_SUBMISSIONS_PER_HOUR = 3
MAX_CAPTURE_REQUESTS_PER_GUILD_MINUTE = 5


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

    async def _punish(self, interaction: discord.Interaction, target: str) -> None:
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("Submission metadata is missing.", ephemeral=True)
            return
        footer = interaction.message.embeds[0].footer.text or ""
        try:
            submission_id = int(footer.removeprefix("Submission #").split()[0])
        except ValueError:
            await interaction.response.send_message("Submission metadata is invalid.", ephemeral=True)
            return
        submission = await interaction.client.db.get_gif_submission(submission_id)
        if not submission:
            await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
            return

        await interaction.response.defer()
        if target == "user":
            target_id = int(submission["submitter_id"])
            await interaction.client.db.ban_user(
                target_id, interaction.user.id, f"GIF submission #{submission_id}"
            )
            decision = f"Submitter `{target_id}` banned by {interaction.user.mention}"
        else:
            target_id = int(submission["guild_id"])
            admin = interaction.client.get_cog("Admin")
            if admin:
                await admin.ban_server_globally(
                    target_id, interaction.user.id, f"GIF submission #{submission_id}"
                )
            else:
                await interaction.client.db.ban_guild(
                    target_id, interaction.user.id, f"GIF submission #{submission_id}"
                )
            decision = f"Source server `{target_id}` banned by {interaction.user.mention}"

        if submission["status"] == "pending":
            await interaction.client.db.review_gif_submission(
                submission_id, interaction.user.id, "blacklisted"
            )
        embed = interaction.message.embeds[0].copy()
        embed.color = config.COLOR_ERR
        embed.add_field(name="Decision", value=decision, inline=False)
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

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.danger, custom_id="gif_submit:ban_user")
    async def ban_user(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._punish(interaction, "user")

    @discord.ui.button(label="Ban Server", style=discord.ButtonStyle.danger, custom_id="gif_submit:ban_server")
    async def ban_server(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._punish(interaction, "server")


class GifSubmission(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self._captures: dict[tuple[int, int], float] = {}
        self._consumed: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._guild_capture_requests: dict[int, deque[float]] = defaultdict(deque)

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
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def addgif(self, ctx: commands.Context) -> None:
        """Capture the next GIF sent by this user in this channel for review."""
        if await self.db.is_user_banned(ctx.author.id) or await self.db.is_guild_banned(ctx.guild.id):
            await ctx.send("🚫 You cannot submit GIFs to Fliphone.")
            return
        now = time.monotonic()
        requests = self._guild_capture_requests[ctx.guild.id]
        while requests and now - requests[0] >= 60:
            requests.popleft()
        if len(requests) >= MAX_CAPTURE_REQUESTS_PER_GUILD_MINUTE:
            await ctx.send("⚠️ This server is submitting GIFs too quickly. Try again in a minute.")
            return
        requests.append(now)
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
