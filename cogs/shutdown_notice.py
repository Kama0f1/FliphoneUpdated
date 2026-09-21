"""One-time service shutdown notices for configured Fliphone servers."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

import config
from database import Database


logger = logging.getLogger("fliphone.shutdown")


def shutdown_date_label() -> str:
    return config.shutdown_date_label()


def shutdown_announcement_text() -> str:
    return (
        "Fliphone is returning for testing.\n\n"
        "Discord has approved the application and the privileged access Fliphone needs to relay messages, "
        "so we can bring the bot back. The bot was previously shut down after Discord rejected our application, "
        "which meant it could not reliably provide its main feature.\n\n"
        "As Fliphone comes back, I am looking for helpers who want to help test, improve, and grow the bot "
        "so it can have a real team behind it. Hosting, database reliability, and future maintenance are still "
        "being worked out, so availability may be limited while testing continues. Thank you for your patience."
    )


def shutdown_announcement_embed() -> discord.Embed:
    return discord.Embed(
        title="Fliphone Testing Update",
        description=shutdown_announcement_text(),
        color=config.COLOR_WARN,
    )


class ShutdownNotice(commands.Cog):
    """Updates Fliphone's profile and sends one notice per configured server."""

    SHUTDOWN_APPLICATION_DESCRIPTION = (
        "Fliphone is returning for testing after Discord approved its required access. "
        "Reliable hosting and maintenance are being restored."
    )
    ACTIVE_APPLICATION_DESCRIPTION = (
        "Fliphone is back for testing. Anonymous cross-server conversations through 1:1 calls and group rooms."
    )

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        self._application_description_updated = False
        self._shutdown_notice_loop.start()

    def cog_unload(self) -> None:
        self._shutdown_notice_loop.cancel()

    async def _update_application_description(self) -> None:
        if self._application_description_updated:
            return
        description = (
            self.SHUTDOWN_APPLICATION_DESCRIPTION
            if config.SHUTDOWN_NOTICE_ENABLED
            else self.ACTIVE_APPLICATION_DESCRIPTION
        )
        try:
            application = await self.bot.application_info()
            if application.description != description:
                await self.bot.http.edit_application_info(
                    reason="Update Fliphone application description",
                    payload={"description": description},
                )
            self._application_description_updated = True
            logger.info("Fliphone application description updated")
        except discord.HTTPException as exc:
            logger.warning("Could not update Fliphone application description: %s", exc)

    async def _broadcast_pending_announcements(self) -> int:
        sent_count = 0
        configs = await self.db.get_all_guild_configs()
        for guild_config in configs:
            guild_id = int(guild_config["guild_id"])
            channel_id = int(guild_config["channel_id"])
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.me is None:
                continue

            # The configured Fliphone channel is the only permitted destination.
            # Never fall back to #general or another arbitrary server channel.
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.send_messages):
                continue

            try:
                claimed = await self.db.claim_shutdown_announcement(guild_id, channel_id)
            except Exception:
                logger.exception("Could not claim shutdown notice for guild %s", guild_id)
                continue
            if not claimed:
                continue

            try:
                if permissions.embed_links:
                    message = await channel.send(
                        embed=shutdown_announcement_embed(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    message = await channel.send(
                        shutdown_announcement_text(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.Forbidden, discord.HTTPException):
                try:
                    await self.db.release_shutdown_announcement(guild_id)
                except Exception:
                    logger.exception(
                        "Could not release failed shutdown notice claim for guild %s",
                        guild_id,
                    )
                logger.warning(
                    "Could not send shutdown notice to guild=%s channel=%s",
                    guild_id,
                    channel_id,
                )
                continue

            try:
                await self.db.complete_shutdown_announcement(
                    guild_id,
                    channel_id,
                    message.id,
                )
            except Exception:
                # Keep the original claim so a successful message is never duplicated.
                logger.exception(
                    "Shutdown notice sent but completion could not be recorded for guild %s",
                    guild_id,
                )
            sent_count += 1
            await asyncio.sleep(0.3)

        return sent_count

    @tasks.loop(minutes=10)
    async def _shutdown_notice_loop(self) -> None:
        await self._update_application_description()
        if not config.SHUTDOWN_NOTICE_ENABLED:
            return
        try:
            sent_count = await self._broadcast_pending_announcements()
        except Exception:
            logger.exception("Shutdown announcement pass failed")
            return
        if sent_count:
            logger.info("Sent shutdown announcement to %d configured server(s)", sent_count)

    @_shutdown_notice_loop.before_loop
    async def _before_shutdown_notice_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(ShutdownNotice(bot))
