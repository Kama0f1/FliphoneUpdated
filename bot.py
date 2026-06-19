"""bot.py – PhoneboothBot class."""

import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands, tasks

import config
from database import Database

# Configure module logger
logger = logging.getLogger("fliphone")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class DiscordLogHandler(logging.Handler):
    def __init__(self, bot: commands.AutoShardedBot, channel_id: int) -> None:
        super().__init__(level=logging.INFO)
        self.bot = bot
        self.channel_id = channel_id
        self._pending_messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not self.channel_id:
            return

        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        if not self.bot.is_ready():
            self._pending_messages.append(message)
            return

        try:
            asyncio.run_coroutine_threadsafe(self._send_message(message), self.bot.loop)
        except Exception:
            self._pending_messages.append(message)

    async def flush_pending(self) -> None:
        if not self._pending_messages or not self.channel_id:
            return

        pending = self._pending_messages
        self._pending_messages = []
        for message in pending:
            await self._send_message(message)

    async def _send_message(self, message: str) -> None:
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                self._pending_messages.append(message)
                return

        if not isinstance(channel, discord.abc.Messageable):
            return

        for chunk in self._chunk_message(message):
            safe_chunk = chunk.replace("```", "`\u200b``")
            await channel.send(f"```text\n{safe_chunk}\n```")

    @staticmethod
    def _chunk_message(message: str, limit: int = 1800) -> list[str]:
        return [message[index : index + limit] for index in range(0, len(message), limit)] or [""]

# Required permissions integer (View Channel + Send Messages + Manage Webhooks +
# Embed Links + Attach Files + Read Message History + Add Reactions)
REQUIRED_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    manage_webhooks=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    add_reactions=True,
)


class PhoneboothBot(commands.AutoShardedBot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        # Determine shard_count (0 in config means auto)
        shard_count = config.SHARD_COUNT if getattr(config, "SHARD_COUNT", 0) else None

        super().__init__(
            command_prefix=config.PREFIXES,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            shard_count=shard_count,
        )
        self.db = Database()
        self.log_channel_id = getattr(config, "LOG_CHANNEL_ID", 0) or getattr(config, "REPORT_LOG_CHANNEL_ID", 0)
        self.discord_log_handler: DiscordLogHandler | None = None
        if self.log_channel_id:
            self.discord_log_handler = DiscordLogHandler(self, self.log_channel_id)
            self.discord_log_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            logger.addHandler(self.discord_log_handler)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        await self.db.init()
        await self.load_extension("cogs.gif_submission")
        await self.load_extension("cogs.phonebooth")
        await self.load_extension("cogs.misc")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.guild_audit")
        await self.load_extension("cogs.help")
        await self.load_extension("cogs.room")
        await self.load_extension("cogs.vote")
        await self.load_extension("cogs.report")
        from cogs.phonebooth import GifReportLogView, GifReportView
        self.add_view(GifReportLogView())
        self.add_view(GifReportView())
        # Load custom censor words from DB into filter
        import filter as flt
        words = await self.db.get_custom_words()
        flt.load_custom_words(words)
        logger.info("Extensions loaded. %d custom censor word(s) loaded.", len(words))
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self) -> None:
        logger.info("Fliphone ready | %s | %d server(s)", self.user, len(self.guilds))
        if self.discord_log_handler is not None:
            await self.discord_log_handler.flush_pending()
        await self._update_presence()
        if not self._presence_sync.is_running():
            self._presence_sync.start()
        if not self._topgg_sync.is_running():
            self._topgg_sync.start()
        await self._post_topgg_stats()

    async def close(self) -> None:
        await super().close()
        if hasattr(self, "http_session") and not self.http_session.closed:
            await self.http_session.close()
        if hasattr(self, "db"):
            await self.db.close()

    # ── Presence ──────────────────────────────────────────────────────────────

    async def _update_presence(self) -> None:
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"f.call  •  {len(self.guilds)} servers  •  Fliphone",
            )
        )

    @tasks.loop(hours=1)
    async def _presence_sync(self) -> None:
        await self._update_presence()

    # ── Top.gg server count ───────────────────────────────────────────────────

    async def _post_topgg_stats(self) -> None:
        """Post current server count to top.gg."""
        if not config.TOPGG_TOKEN:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://top.gg/api/bots/{self.user.id}/stats",
                    headers={"Authorization": config.TOPGG_TOKEN},
                    json={"server_count": len(self.guilds)},
                )
        except Exception as exc:
            print(f"[top.gg] Failed to post stats: {exc}")

    def _format_guild_owner(self, guild: discord.Guild) -> str:
        owner = getattr(guild, "owner", None)
        if owner:
            return f"{owner} ({owner.id})"
        owner_id = getattr(guild, "owner_id", None)
        return f"ID:{owner_id}" if owner_id else "Unknown"

    @tasks.loop(minutes=30)
    async def _topgg_sync(self) -> None:
        await self._post_topgg_stats()

    @_topgg_sync.before_loop
    async def _topgg_sync_before(self) -> None:
        await self.wait_until_ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info(
            "Joined guild: %s (%s) | owner=%s | members=%d",
            guild.name,
            guild.id,
            self._format_guild_owner(guild),
            guild.member_count,
        )
        await self._update_presence()
        await self._post_topgg_stats()

        # ── Check missing permissions ─────────────────────────────────────────
        missing = []
        bot_member = guild.me
        if bot_member:
            perms = bot_member.guild_permissions
            if not perms.send_messages:   missing.append("Send Messages")
            if not perms.manage_webhooks: missing.append("Manage Webhooks")
            if not perms.embed_links:     missing.append("Embed Links")
            if not perms.read_message_history: missing.append("Read Message History")

        # ── Try to send welcome in first available text channel ───────────────
        target = None
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            target = guild.system_channel
        else:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target = ch
                    break

        if target:
            try:
                embed = discord.Embed(
                    title="📞 Thanks for adding Fliphone!",
                    description=(
                        "Fliphone connects your server with random strangers from other Discord servers.\n\n"
                        "**To get started:**\n"
                        "1. Go to the channel you want to use for calls\n"
                        "2. Run `f.setup` in that channel\n"
                        "3. Type `f.call` to connect with someone!\n\n"
                        "**Commands:** `f.call` · `f.hangup` · `f.skip` · `f.anon` · `f.fr`\n\n"
                        "Need help? [Join our support server](<https://discord.gg/t3KHGqPuEP>)"
                    ),
                    color=0x5865F2,
                )
                if missing:
                    invite_url = (
                        "https://discord.com/api/oauth2/authorize"
                        f"?client_id={self.user.id}&permissions={config.BOT_PERMISSIONS}"
                        f"&scope=bot%20applications.commands&guild_id={guild.id}&disable_guild_select=true"
                    )
                    embed.add_field(
                        name="⚠️ Missing Permissions",
                        value=(
                            f"I'm missing: **{', '.join(missing)}**\n"
                            f"**[Re-invite me with the correct permissions]({invite_url})**, then run `f.setup`.\n"
                            "If setup still reports a missing permission, the chosen channel or category is denying it."
                        ),
                        inline=False,
                    )
                embed.set_footer(text="Fliphone • Cross-server chat roulette")
                await target.send(embed=embed)
            except discord.HTTPException:
                pass

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info(
            "Left guild: %s (%s) | owner=%s | members=%d",
            guild.name,
            guild.id,
            self._format_guild_owner(guild),
            guild.member_count,
        )
        await self.db.delete_guild(guild.id)
        await self._update_presence()
        await self._post_topgg_stats()

    # ── Global error handler ──────────────────────────────────────────────────

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Only a server admin can run this command.",
                    color=config.COLOR_ERR,
                )
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=discord.Embed(
                    description=f"⏳ Slow down! Try again in **{error.retry_after:.1f}s**.",
                    color=config.COLOR_WARN,
                )
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ This command can't be used in DMs.",
                    color=config.COLOR_ERR,
                )
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Bad argument: {error}",
                    color=config.COLOR_ERR,
                )
            )
            return
        raise error

    # ── Command / Interaction logging ────────────────────────────────────

    async def on_command(self, ctx: commands.Context) -> None:
        """Log prefix command invocations."""
        try:
            cmd = ctx.command.qualified_name if ctx.command else "(unknown)"
            guild = f"{ctx.guild.name}({ctx.guild.id})" if ctx.guild else "DM"
            logger.info("Command: %s invoked by %s in %s: %s", cmd, ctx.author, guild, ctx.message.content)
        except Exception:
            logger.exception("Failed to log command invocation")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Log application command (slash) invocations."""
        try:
            if interaction.type == discord.InteractionType.application_command:
                name = interaction.data.get("name") if isinstance(interaction.data, dict) else str(interaction.data)
                guild = f"{interaction.guild_id}" if interaction.guild_id else "DM"
                logger.info("App command: %s invoked by %s in %s", name, interaction.user, guild)
        except Exception:
            logger.exception("Failed to log interaction")
