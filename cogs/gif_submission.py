"""User GIF and custom emoji submissions with trusted-moderator review controls."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
import re
import time

import aiohttp
import asyncpg
import discord
from discord.ext import commands

import config
from relay_policy import (
    CustomEmojiCandidate,
    custom_emoji_asset_url,
    extract_custom_emojis,
    extract_gif_candidate,
)


CAPTURE_SECONDS = 60
MAX_CAPTURE_REQUESTS_PER_GUILD_MINUTE = 5
MAX_EMOJIS_PER_SUBMISSION = 5


def _safe_app_emoji_name(name: str, original_emoji_id: int) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "emoji"
    suffix = str(original_emoji_id)[-8:]
    max_base_len = max(2, 32 - len(suffix) - 1)
    base = base[:max_base_len].strip("_") or "emoji"
    if len(base) < 2:
        base = (base + "xx")[:2]
    return f"{base}_{suffix}"[:32]


async def _is_submission_mod(bot: commands.Bot, user: discord.abc.User) -> bool:
    return user.id in config.TRUSTED_MOD_IDS or await bot.is_owner(user)


def _truthy_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "t", "yes"}
    return bool(value)


class GifSubmissionReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_submission_mod(interaction.client, interaction.user):
            return True
        await interaction.response.send_message("This review panel is restricted.", ephemeral=True)
        return False

    async def _finish_review(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        confirmation: str,
    ) -> None:
        removed = True
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            removed = False
            try:
                await interaction.message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        suffix = (
            " Removed from the review channel."
            if removed
            else " The review was saved, but Discord would not remove the message."
        )
        await interaction.followup.send(confirmation + suffix, ephemeral=True)

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
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
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
        await self._finish_review(
            interaction,
            embed,
            f"Submission #{submission_id} {action}.",
        )

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
        await self._finish_review(interaction, embed, f"{decision}.")

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


class EmojiSubmissionReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_submission_mod(interaction.client, interaction.user):
            return True
        await interaction.response.send_message("This review panel is restricted.", ephemeral=True)
        return False

    @staticmethod
    def _submission_id(interaction: discord.Interaction) -> int | None:
        if not interaction.message or not interaction.message.embeds:
            return None
        footer = interaction.message.embeds[0].footer.text or ""
        try:
            return int(footer.removeprefix("Emoji Submission #").split()[0])
        except ValueError:
            return None

    async def _fetch_emoji_image(self, bot: commands.Bot, submission: dict) -> tuple[bytes, bool]:
        emoji_id = int(submission["original_emoji_id"])
        animated = _truthy_flag(submission["animated"])
        candidates: list[tuple[str, bool]] = []
        if animated:
            candidates.append((
                f"https://cdn.discordapp.com/emojis/{emoji_id}.webp?animated=true&size=128&quality=lossless",
                True,
            ))
            candidates.append((custom_emoji_asset_url(emoji_id, True), True))
        candidates.append((
            f"https://cdn.discordapp.com/emojis/{emoji_id}.webp?size=128&quality=lossless",
            False,
        ))
        candidates.append((custom_emoji_asset_url(emoji_id, False), False))

        session = getattr(bot, "http_session", None)
        own_session = session is None or session.closed
        if own_session:
            session = aiohttp.ClientSession()
        errors: list[str] = []
        try:
            for url, image_is_animated in candidates:
                async with session.get(url) as response:
                    if response.status >= 400:
                        errors.append(f"{url} -> HTTP {response.status}")
                        continue
                    return await response.read(), image_is_animated
            raise RuntimeError("; ".join(errors) or "Discord CDN did not return an emoji image")
        finally:
            if own_session:
                await session.close()

    async def _finish_review(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        confirmation: str,
    ) -> None:
        removed = True
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            removed = False
            try:
                await interaction.message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        suffix = (
            " Removed from the review channel."
            if removed
            else " The review was saved, but Discord would not remove the message."
        )
        await interaction.followup.send(confirmation + suffix, ephemeral=True)

    async def _review(self, interaction: discord.Interaction, action: str) -> None:
        submission_id = self._submission_id(interaction)
        if submission_id is None:
            await interaction.response.send_message("Submission metadata is invalid.", ephemeral=True)
            return

        await interaction.response.defer()
        submission = await interaction.client.db.get_emoji_submission(submission_id)
        if not submission or submission["status"] != "pending":
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            await interaction.followup.send("This emoji submission was already reviewed.", ephemeral=True)
            return

        app_emoji_id = None
        app_emoji_name = None
        app_emoji_animated = False
        app_emoji = None
        if action == "approved":
            try:
                image, image_is_animated = await self._fetch_emoji_image(interaction.client, submission)
                app_name = _safe_app_emoji_name(
                    str(submission["original_name"]),
                    int(submission["original_emoji_id"]),
                )
                app_emoji = await interaction.client.create_application_emoji(
                    name=app_name,
                    image=image,
                )
                app_emoji_id = int(app_emoji.id)
                app_emoji_name = app_emoji.name
                app_emoji_animated = bool(getattr(app_emoji, "animated", image_is_animated))
            except (discord.HTTPException, aiohttp.ClientError, RuntimeError) as exc:
                await interaction.followup.send(
                    "Could not mirror this emoji into Fliphone's application emojis. "
                    f"It is still pending. Discord said: `{str(exc)[:180]}`",
                    ephemeral=True,
                )
                return

        reviewed = await interaction.client.db.review_emoji_submission(
            submission_id,
            interaction.user.id,
            action,
            app_emoji_id=app_emoji_id,
            app_emoji_name=app_emoji_name,
            app_emoji_animated=app_emoji_animated,
        )
        if not reviewed:
            if app_emoji is not None:
                try:
                    await app_emoji.delete(reason="Emoji submission was already reviewed")
                except discord.HTTPException:
                    pass
            await interaction.followup.send("This emoji submission was already reviewed.", ephemeral=True)
            return

        embed = interaction.message.embeds[0].copy()
        embed.color = {
            "approved": config.COLOR_OK,
            "rejected": config.COLOR_WARN,
            "blacklisted": config.COLOR_ERR,
        }[action]
        decision = f"{action.title()} by {interaction.user.mention}"
        if app_emoji_id:
            prefix = "a" if app_emoji_animated else ""
            decision += f"\nMirrored as `<{prefix}:{app_emoji_name}:{app_emoji_id}>`"
            if _truthy_flag(submission["animated"]) and not app_emoji_animated:
                decision += "\nDiscord only provided a static copy, so this will relay as a static emoji."
        embed.add_field(name="Decision", value=decision, inline=False)
        await self._finish_review(
            interaction,
            embed,
            f"Emoji submission #{submission_id} {action}.",
        )

    async def _punish(self, interaction: discord.Interaction, target: str) -> None:
        submission_id = self._submission_id(interaction)
        if submission_id is None:
            await interaction.response.send_message("Submission metadata is invalid.", ephemeral=True)
            return
        submission = await interaction.client.db.get_emoji_submission(submission_id)
        if not submission:
            await interaction.response.send_message("That submission no longer exists.", ephemeral=True)
            return

        await interaction.response.defer()
        if target == "user":
            target_id = int(submission["submitter_id"])
            await interaction.client.db.ban_user(
                target_id, interaction.user.id, f"Emoji submission #{submission_id}"
            )
            decision = f"Submitter `{target_id}` banned by {interaction.user.mention}"
        else:
            target_id = int(submission["guild_id"])
            admin = interaction.client.get_cog("Admin")
            if admin:
                await admin.ban_server_globally(
                    target_id, interaction.user.id, f"Emoji submission #{submission_id}"
                )
            else:
                await interaction.client.db.ban_guild(
                    target_id, interaction.user.id, f"Emoji submission #{submission_id}"
                )
            decision = f"Source server `{target_id}` banned by {interaction.user.mention}"

        if submission["status"] == "pending":
            await interaction.client.db.review_emoji_submission(
                submission_id, interaction.user.id, "blacklisted"
            )
        embed = interaction.message.embeds[0].copy()
        embed.color = config.COLOR_ERR
        embed.add_field(name="Decision", value=decision, inline=False)
        await self._finish_review(interaction, embed, f"{decision}.")

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="emoji_submit:approve")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.secondary, custom_id="emoji_submit:reject")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "rejected")

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.danger, custom_id="emoji_submit:blacklist")
    async def blacklist(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._review(interaction, "blacklisted")

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.danger, custom_id="emoji_submit:ban_user")
    async def ban_user(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._punish(interaction, "user")

    @discord.ui.button(label="Ban Server", style=discord.ButtonStyle.danger, custom_id="emoji_submit:ban_server")
    async def ban_server(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._punish(interaction, "server")


class GifSubmission(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self._captures: dict[tuple[int, int], tuple[float, str]] = {}
        self._consumed: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._guild_capture_requests: dict[int, deque[float]] = defaultdict(deque)

    def is_consumed(self, message_id: int) -> bool:
        expiry = self._consumed.get(message_id, 0)
        if expiry <= time.monotonic():
            self._consumed.pop(message_id, None)
            return False
        return True

    def _claim_guild_capture_slot(self, guild_id: int) -> bool:
        now = time.monotonic()
        requests = self._guild_capture_requests[guild_id]
        while requests and now - requests[0] >= 60:
            requests.popleft()
        if len(requests) >= MAX_CAPTURE_REQUESTS_PER_GUILD_MINUTE:
            return False
        requests.append(now)
        return True

    async def consume_capture(self, message: discord.Message) -> bool:
        key = (message.author.id, message.channel.id)
        async with self._lock:
            capture = self._captures.get(key)
            if not capture:
                return False
            expiry, kind = capture
            if expiry <= time.monotonic():
                self._captures.pop(key, None)
                return False

            if kind == "gif":
                url = extract_gif_candidate(message)
                if not url:
                    return False
                self._captures.pop(key, None)
                self._consumed[message.id] = time.monotonic() + 120
            else:
                all_emojis = extract_custom_emojis(message.content)
                if not all_emojis:
                    return False
                selected_emojis = all_emojis[:MAX_EMOJIS_PER_SUBMISSION]
                truncated = len(all_emojis) > MAX_EMOJIS_PER_SUBMISSION
                self._captures.pop(key, None)
                self._consumed[message.id] = time.monotonic() + 120

        if kind == "gif":
            return await self._submit_gif(message, url)
        return await self._submit_emojis(message, selected_emojis, truncated)

    async def _submit_gif(self, message: discord.Message, url: str) -> bool:
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

    async def _send_emoji_review(
        self,
        review_channel: discord.abc.Messageable,
        message: discord.Message,
        emoji: CustomEmojiCandidate,
        submission_id: int,
    ) -> None:
        embed = discord.Embed(title="Custom Emoji Submission", color=config.COLOR_WAIT)
        embed.add_field(name="Emoji", value=f"`{emoji.markup}`", inline=False)
        embed.add_field(name="Name", value=emoji.name, inline=True)
        embed.add_field(name="ID", value=str(emoji.emoji_id), inline=True)
        embed.add_field(name="Animated", value="Yes" if emoji.animated else "No", inline=True)
        embed.add_field(name="Submitted by", value=f"{message.author} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Server / channel", value=f"{message.guild.name} / {message.channel.mention}", inline=False)
        embed.set_image(url=custom_emoji_asset_url(emoji.emoji_id, False))
        embed.set_footer(text=f"Emoji Submission #{submission_id}")
        review_message = await review_channel.send(embed=embed, view=EmojiSubmissionReviewView())
        await self.db.set_emoji_submission_review_message(submission_id, review_message.id)

    async def _submit_emojis(
        self,
        message: discord.Message,
        emojis: list[CustomEmojiCandidate],
        truncated: bool,
    ) -> bool:
        review_channel = self.bot.get_channel(config.EMOJI_REVIEW_CHANNEL_ID)
        if review_channel and not hasattr(review_channel, "send"):
            review_channel = None
        queued: list[str] = []
        skipped: list[str] = []
        review_errors = 0

        for emoji in emojis:
            submission_id, status = await self.db.add_emoji_submission(
                emoji.emoji_id,
                emoji.name,
                emoji.animated,
                message.author.id,
                message.guild.id,
                message.channel.id,
            )
            if status == "pending":
                queued.append(f"#{submission_id} `{emoji.name}`")
                if review_channel:
                    try:
                        await self._send_emoji_review(review_channel, message, emoji, submission_id)
                    except (discord.Forbidden, discord.HTTPException):
                        review_errors += 1
            elif status == "already_pending":
                skipped.append(f"`{emoji.name}` is already waiting for review")
            elif status == "approved":
                skipped.append(f"`{emoji.name}` is already approved")
            elif status == "blacklisted":
                skipped.append(f"`{emoji.name}` is blacklisted")
            else:
                skipped.append(f"`{emoji.name}` is already marked {status}")

        lines: list[str] = []
        if queued:
            lines.append(f"Emoji submissions sent for review: {', '.join(queued)}.")
        if skipped:
            lines.append("Skipped: " + "; ".join(skipped[:5]) + ".")
        if truncated:
            lines.append(f"Only the first {MAX_EMOJIS_PER_SUBMISSION} unique custom emojis were captured.")
        if queued and not review_channel:
            lines.append("Emoji review channel is not configured, so moderators will not see them yet.")
        elif review_errors:
            lines.append(f"{review_errors} review message(s) could not be posted for moderators.")
        await message.channel.send("\n".join(lines) or "No new emoji submissions were created.", delete_after=12)
        return True

    @commands.hybrid_command(name="addgif")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def addgif(self, ctx: commands.Context) -> None:
        """Capture the next GIF sent by this user in this channel for review."""
        if await self.db.is_user_banned(ctx.author.id) or await self.db.is_guild_banned(ctx.guild.id):
            await ctx.send("You cannot submit GIFs to Fliphone.")
            return
        if not self._claim_guild_capture_slot(ctx.guild.id):
            await ctx.send("This server is submitting review items too quickly. Try again in a minute.")
            return
        self._captures[(ctx.author.id, ctx.channel.id)] = (time.monotonic() + CAPTURE_SECONDS, "gif")
        await ctx.send(
            "Send the GIF you want to submit in this channel within 60 seconds. "
            "It will stay local and will not be relayed."
        )

    @commands.hybrid_command(name="addemoji", aliases=["addemojis", "addemote", "submitemoji"])
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def addemoji(self, ctx: commands.Context) -> None:
        """Capture up to 5 custom emojis sent by this user for review."""
        if await self.db.is_user_banned(ctx.author.id) or await self.db.is_guild_banned(ctx.guild.id):
            await ctx.send("You cannot submit emojis to Fliphone.")
            return
        if not self._claim_guild_capture_slot(ctx.guild.id):
            await ctx.send("This server is submitting review items too quickly. Try again in a minute.")
            return
        self._captures[(ctx.author.id, ctx.channel.id)] = (time.monotonic() + CAPTURE_SECONDS, "emoji")
        await ctx.send(
            f"Send **one message** with up to {MAX_EMOJIS_PER_SUBMISSION} custom server emojis "
            "in this channel within 60 seconds. They will stay local and will not be relayed."
        )

    @commands.command(name="emojicleanup")
    @commands.guild_only()
    async def emojicleanup(self, ctx: commands.Context, days: int = 90, limit: int = 25) -> None:
        """[Trusted mods] Delete unused or stale mirrored application emojis."""
        if not await _is_submission_mod(self.bot, ctx.author):
            await ctx.send("You don't have permission to use this command.")
            return
        cutoff = (datetime.utcnow() - timedelta(days=max(1, int(days)))).isoformat()
        candidates = await self.db.get_emoji_cleanup_candidates(cutoff, limit)
        if not candidates:
            await ctx.send("No mirrored emojis matched the cleanup criteria.")
            return

        deleted = 0
        failed = 0
        for row in candidates:
            app_emoji_id = row.get("app_emoji_id")
            if not app_emoji_id:
                continue
            try:
                app_emoji = await self.bot.fetch_application_emoji(int(app_emoji_id))
                await app_emoji.delete(reason=f"Fliphone emoji cleanup by {ctx.author}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                failed += 1
                continue
            marked = await self.db.mark_emoji_submission_deleted(row["id"], ctx.author.id)
            if marked:
                deleted += 1

        await ctx.send(
            f"Emoji cleanup complete. Deleted **{deleted}** mirrored emoji(s). "
            f"Failed: **{failed}**."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        await self.consume_capture(message)


async def setup(bot) -> None:
    bot.add_view(GifSubmissionReviewView())
    bot.add_view(EmojiSubmissionReviewView())
    await bot.add_cog(GifSubmission(bot))
