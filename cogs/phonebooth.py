"""
cogs/phonebooth.py – Core Phonebooth logic.

Commands
--------
f.call / f.c      – Join queue or connect instantly
f.hangup / f.h    – End call or leave queue
f.skip / f.s      – Hang up and immediately redial
f.status          – Show current status
f.block           – Block the server you're talking to
f.anon            – Toggle your personal tarot identity
f.fr              – Share your username as a friend request card
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import config
from database import Database
from filter import filter_message
from relay_policy import (
    downgrade_animated_custom_emoji_markup,
    extract_urls,
    is_direct_gif_url,
    is_local_only,
    is_provider_gif,
    plain_custom_emoji_fallback,
    replace_approved_custom_emojis,
)

# This catches Tenor, Giphy, Klipy, and ANY link that ends in .gif
# Everything else is treated as a potentially unsafe link and stripped before relay.
GIF_LINK_PATTERN = re.compile(
    r"https?://(?:\S*\.)?(?:tenor\.com|giphy\.com|klipy\.com|static\.klipy\.com)\S*"
    r"|https?://\S+\.gif(?:\?\S*)?",
    re.IGNORECASE,
)

# Matches any URL that is NOT a Tenor/Giphy/gif link — these are blocked from relay.
LINK_PATTERN = re.compile(
    r"https?://\S+",
    re.IGNORECASE,
)

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")

# Matches custom Discord emojis: <:name:id> and animated <a:name:id>.
# Regular Unicode/keyboard emojis are plain text and are allowed through.
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[a-zA-Z0-9_]+:[0-9]+>")
UNICODE_EMOJI_PATTERN = re.compile(
    r"(?:"
    r"[\U0001F1E6-\U0001F1FF]{2}|"
    r"(?:[\U0001F300-\U0001FAFF]|[\u2600-\u27BF])"
    r"[\ufe0f\U0001F3FB-\U0001F3FF]*"
    r"(?:\u200d(?:[\U0001F300-\U0001FAFF]|[\u2600-\u27BF])"
    r"[\ufe0f\U0001F3FB-\U0001F3FF]*)*"
    r")"
)
MAX_EMOJIS_PER_MESSAGE = 10
PROFILE_BANNER_DIR = Path(__file__).resolve().parents[1] / "assets" / "profile_banners"
CHAT_XP_COOLDOWN_SECONDS = 60
CHAT_XP_MIN = 12
CHAT_XP_MAX = 22

# ── Helpers ───────────────────────────────────────────────────────────────────

def _duration_str(started_at: str) -> str:
    delta = datetime.utcnow() - datetime.fromisoformat(started_at)
    total = int(delta.total_seconds())
    return f"{total // 60}m {total % 60}s"


def _elapsed_seconds(timestamp: Optional[str]) -> float:
    if not timestamp:
        return 0.0
    try:
        return max(0.0, (datetime.utcnow() - datetime.fromisoformat(timestamp)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _anon_identity(seed: int) -> tuple[str, str]:
    rng = random.Random(seed)
    name = rng.choice(config.ANON_NAMES)
    avatar = f"https://robohash.org/{seed}.png?set=set4&size=128x128"
    return name, avatar


def _get_avatar_url(member: discord.Member | discord.User) -> str:
    # Guild-specific avatars are not reliable when Discord fetches them for a
    # webhook in another server. Global profile avatars are cross-server assets.
    asset = member.avatar or member.default_avatar
    try:
        return str(asset.with_size(256).url)
    except Exception:
        try:
            return str(asset.url)
        except Exception:
            return str(member.default_avatar.url)


def _relay_display_name(member: object) -> str:
    name = getattr(member, "display_name", None) or getattr(member, "name", "User")
    filtered_name, _ = filter_message(str(name))
    return filtered_name.strip()[:80] or "User"


def _render_user_mentions(text: str, guild: discord.Guild | None) -> str:
    if not text or guild is None:
        return text

    def _replace(match: re.Match[str]) -> str:
        member = guild.get_member(int(match.group(1)))
        if member:
            return f"@{_relay_display_name(member)}"
        return "@user"

    return MENTION_PATTERN.sub(_replace, text)


def _profile_banner_files() -> list[Path]:
    if not PROFILE_BANNER_DIR.exists():
        return []

    def _banner_sort_key(path: Path) -> tuple[int, str]:
        try:
            return int(path.stem.rsplit("_", 1)[-1]), path.name
        except ValueError:
            return 0, path.name

    return sorted(
        (
            path
            for path in PROFILE_BANNER_DIR.iterdir()
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ),
        key=_banner_sort_key,
    )


def _fallback_banner_index(user_id: int, banner_count: int) -> int:
    return int(user_id) % banner_count if banner_count else 0


def _level_from_xp(total_xp: int) -> tuple[int, int, int]:
    level = 1
    remaining = max(0, int(total_xp))
    needed = 100
    while remaining >= needed:
        remaining -= needed
        level += 1
        needed = 100 + (level - 1) * 50
    return level, remaining, needed


def _rank_label(rank: Optional[int]) -> str:
    return f"#{rank:,}" if rank else "Unranked"


def _limit_unicode_emojis(text: str, limit: int = MAX_EMOJIS_PER_MESSAGE) -> tuple[str, bool]:
    count = 0
    trimmed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal count, trimmed
        count += 1
        if count <= limit:
            return match.group(0)
        trimmed = True
        return ""

    return UNICODE_EMOJI_PATTERN.sub(_replace, text), trimmed
# ── GIF Report View ───────────────────────────────────────────────────────────

class GifReportView(discord.ui.View):
    """
    Persistent compact button under every reportable relayed GIF.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🚩 Report GIF",
        style=discord.ButtonStyle.danger,
        custom_id="pb_gif_report",
    )
    async def report_gif(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db: Database = interaction.client.db
        await interaction.response.defer()

        report = None
        if interaction.message:
            report = await db.get_gif_report_by_prompt(interaction.message.id)
        if not report:
            await interaction.followup.send(
                "This report button is no longer active.", ephemeral=True
            )
            return
        report_id = report["id"]

        # ── Already reported / reviewed? ──────────────────────────────────────
        if report["status"] == "reported":
            await interaction.followup.send(
                "⚠️ This GIF has already been reported and is pending review.", ephemeral=True
            )
            return
        if report["status"] in ("blacklisted", "whitelisted"):
            await interaction.followup.send(
                "✅ This GIF has already been reviewed by the bot owner.", ephemeral=True
            )
            return

        # ── Whitelisted? ──────────────────────────────────────────────────────
        url_status = await db.check_gif_url(report["url"])
        if url_status == "whitelist":
            await interaction.followup.send(
                "✅ This GIF has been verified as safe and cannot be reported.", ephemeral=True
            )
            return

        # ── Mark reported in DB ───────────────────────────────────────────────
        await db.mark_gif_reported(report_id, interaction.user.id)

        # ── Add to per-call block list so it can't be sent again this call ────
        pb_cog = interaction.client.get_cog("Phonebooth")
        if pb_cog and report.get("channel_id"):
            conn = await db.get_connection(report["channel_id"])
            if conn:
                conn_id = conn["id"]
                norm = report["url"].split("?")[0].rstrip("/").lower()
                pb_cog._call_reported_gifs.setdefault(conn_id, set()).add(norm)

        # ── Auto-delete the GIF message from the channel ──────────────────────
        deleted = False
        if report["msg_id"] and report["channel_id"]:
            ch = interaction.client.get_channel(report["channel_id"])
            try:
                if ch:
                    msg = await ch.fetch_message(report["msg_id"])
                    await msg.delete()
                    deleted = True
            except discord.NotFound:
                deleted = True
            except (discord.Forbidden, discord.HTTPException):
                pass

            # Bot-authored deletion needs Manage Messages. Use the owning
            # webhook token instead when the channel only grants Manage Webhooks.
            if ch and not deleted:
                try:
                    webhooks = await ch.webhooks()
                    for webhook in webhooks:
                        if webhook.user != interaction.client.user or webhook.name != "Fliphone":
                            continue
                        try:
                            await webhook.delete_message(report["msg_id"])
                            deleted = True
                            break
                        except discord.NotFound:
                            continue
                except (discord.Forbidden, discord.HTTPException):
                    pass

        if interaction.message:
            result = "GIF reported and removed." if deleted else "GIF reported; removal failed."
            await interaction.message.edit(content=result, view=None)

        # ── Log to report channel ─────────────────────────────────────────────
        report_ch_id = int(config.REPORT_LOG_CHANNEL_ID) if config.REPORT_LOG_CHANNEL_ID else 0
        if report_ch_id:
            log_ch = interaction.client.get_channel(report_ch_id)
            if log_ch:
                log_embed = discord.Embed(
                    title="🚩 New GIF Report",
                    color=0xFF6B6B,
                    timestamp=datetime.utcnow(),
                )
                log_embed.add_field(
                    name="URL",
                    value=f"```\n{report['url'][:900]}\n```",
                    inline=False,
                )
                log_embed.add_field(
                    name="Reported by",
                    value=f"{interaction.user.mention} in <#{report['channel_id']}>",
                    inline=True,
                )
                log_embed.add_field(
                    name="GIF deleted",
                    value="✅ Yes" if deleted else "⚠️ Could not delete",
                    inline=True,
                )
                log_embed.add_field(name="Report ID", value=f"#{report_id}", inline=True)
                log_embed.set_footer(
                    text=f"f.gifbl {report_id} → blacklist  |  f.gifwl {report_id} → whitelist  |  {config.FOOTER}"
                )
                try:
                    review_message = await log_ch.send(embed=log_embed, view=GifReportLogView())
                    await db.set_gif_report_review_message(
                        report_id, review_message.id, review_message.channel.id
                    )
                except discord.HTTPException:
                    pass

        confirmation = (
            "✅ GIF removed and flagged for review. Thanks!"
            if deleted
            else "⚠️ GIF flagged for review, but Discord would not let Fliphone remove the message."
        )
        await interaction.followup.send(confirmation, ephemeral=True)

class GifReportLogView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Review Reports",
        style=discord.ButtonStyle.primary,
        custom_id="pb_gif_report_panel",
    )
    async def review_reports(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        admin_cog = interaction.client.get_cog("Admin")
        if not admin_cog or not hasattr(admin_cog, "send_gif_report_panel_interaction"):
            await interaction.response.send_message("GIF report panel is not loaded yet.", ephemeral=True)
            return
        await admin_cog.send_gif_report_panel_interaction(interaction)


_CONNECTED_MSG = (
    "📞 **Call answered! say hi!** 👋\n"
    "You are now in a call!\n"
    "Please remember to respect the user on the other end.\n"
    "To skip a user, use `f.skip`  "
    "To report a user, use `/report`. To block a user, use `f.block`.\n\n"
    "*By continuing, you agree to be respectful. "
    "To opt out, ask an admin to run `f.setup` in the channel to unconfigure it.*"
)
_ANON_NOTICE = (
    "🎭 **Anon mode is ON**"
)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Phonebooth(commands.Cog):
    """Core Phonebooth commands and message relay."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        self._timeouts: dict[int, asyncio.Task] = {}
        self._queue_nudges: dict[int, asyncio.Task] = {}
        self._wh_avatar_cache: dict[str, tuple[int, str]] = {}
        # conn_id -> set of normalised GIF URLs reported during that call
        self._call_reported_gifs: dict[int, set[str]] = {}
        # conn_id -> asyncio.Task for inactivity timeout
        self._inactivity_tasks: dict[int, asyncio.Task] = {}
        # Rate limiting: channel_id -> deque of (monotonic timestamp, user_id)
        self._rl_times: dict[int, deque] = {}
        self._rl_warns: dict[int, int]   = {}
        self._call_rate_cooldowns: dict[int, float] = {}
        # guild_id -> last call info, used by report.py to identify partner after hangup/skip
        self._last_calls: dict[int, dict] = {}
        self._conn_by_channel: dict[int, dict] = {}
        self._conn_miss_until: dict[int, float] = {}
        self._cfg_by_channel: dict[int, dict] = {}
        self._cfg_by_guild: dict[int, dict] = {}
        self._session: aiohttp.ClientSession | None = None
        self._wh_obj_cache: dict[str, discord.Webhook] = {}
        self._channel_webhook_urls: dict[int, str] = {}
        self._user_policy_cache: dict[int, tuple[float, bool, bool]] = {}
        self._ending_broken_connections: set[int] = set()
        self._reaction_routes: dict[tuple[int, int], tuple[int, int]] = {}
        self._reaction_times: dict[int, deque] = {}
        self._relay_locks: dict[int, asyncio.Lock] = {}
        self._recovery_task: asyncio.Task | None = None
        self._cleanup_loop.start()

    async def cog_load(self) -> None:
        if not self.db._conn:
            await self.db.init()
        self._session = aiohttp.ClientSession()
        self._recovery_task = asyncio.create_task(self._recover_runtime_state())

    def cog_unload(self) -> None:
        self._cleanup_loop.cancel()
        if self._recovery_task:
            self._recovery_task.cancel()
        for task in self._timeouts.values():
            task.cancel()
        for task in self._queue_nudges.values():
            task.cancel()
        for task in self._inactivity_tasks.values():
            task.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    def _cache_config(self, cfg: Optional[dict]) -> None:
        if not cfg:
            return
        self._cfg_by_channel[int(cfg["channel_id"])] = cfg
        self._cfg_by_guild[int(cfg["guild_id"])] = cfg

    def _invalidate_config(self, *, guild_id: Optional[int] = None, channel_id: Optional[int] = None) -> None:
        cfg = None
        if channel_id is not None:
            cfg = self._cfg_by_channel.pop(int(channel_id), None)
        if guild_id is not None:
            cfg = self._cfg_by_guild.pop(int(guild_id), cfg)
        if cfg:
            self._cfg_by_channel.pop(int(cfg["channel_id"]), None)
            self._cfg_by_guild.pop(int(cfg["guild_id"]), None)

    async def _get_config_by_channel_cached(self, channel_id: int) -> Optional[dict]:
        cfg = self._cfg_by_channel.get(int(channel_id))
        if cfg:
            return cfg
        cfg = await self.db.get_config_by_channel(channel_id)
        self._cache_config(cfg)
        return cfg

    async def _get_guild_config_cached(self, guild_id: int) -> Optional[dict]:
        cfg = self._cfg_by_guild.get(int(guild_id))
        if cfg:
            return cfg
        cfg = await self.db.get_guild_config(guild_id)
        self._cache_config(cfg)
        return cfg

    def _cache_connection(self, conn: Optional[dict]) -> None:
        if not conn:
            return
        for channel_id in (int(conn["channel_a"]), int(conn["channel_b"])):
            self._conn_miss_until.pop(channel_id, None)
            self._conn_by_channel[channel_id] = conn

    def _invalidate_connection(self, conn: Optional[dict]) -> None:
        if not conn:
            return
        self._conn_by_channel.pop(int(conn["channel_a"]), None)
        self._conn_by_channel.pop(int(conn["channel_b"]), None)
        expiry = time.monotonic() + 30.0
        self._conn_miss_until[int(conn["channel_a"])] = expiry
        self._conn_miss_until[int(conn["channel_b"])] = expiry

    async def _get_connection_cached(self, channel_id: int) -> Optional[dict]:
        conn = self._conn_by_channel.get(int(channel_id))
        if conn:
            return conn
        if self._conn_miss_until.get(int(channel_id), 0.0) > time.monotonic():
            return None
        conn = await self.db.get_connection(channel_id)
        self._cache_connection(conn)
        if not conn:
            self._conn_miss_until[int(channel_id)] = time.monotonic() + 30.0
        return conn

    # ── Queue timeout ─────────────────────────────────────────────────────────

    QUEUE_NOTIFY_NUDGE_SECONDS = 90

    async def _run_timeout(self, channel_id: int, delay_seconds: Optional[float] = None) -> None:
        delay = config.QUEUE_TIMEOUT * 60 if delay_seconds is None else max(0.0, delay_seconds)
        await asyncio.sleep(delay)
        entry = await self.db.get_queue_entry(channel_id)
        if entry:
            await self.db.remove_from_queue(channel_id)
            self._cancel_queue_nudge(channel_id)
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(
                        f"📵 No one picked up after **{config.QUEUE_TIMEOUT} minutes**. "
                        f"Use `f.call` to try again."
                    )
                except discord.HTTPException:
                    pass
        self._timeouts.pop(channel_id, None)

    async def _run_queue_nudge(
        self,
        channel_id: int,
        user_id: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        delay = self.QUEUE_NOTIFY_NUDGE_SECONDS if delay_seconds is None else max(0.0, delay_seconds)
        await asyncio.sleep(delay)
        entry = await self.db.get_queue_entry(channel_id)
        if not entry or entry["user_id"] != user_id:
            self._queue_nudges.pop(channel_id, None)
            return
        if await self._get_connection_cached(channel_id):
            self._queue_nudges.pop(channel_id, None)
            return
        if await self.db.get_notify_status(user_id):
            self._queue_nudges.pop(channel_id, None)
            return

        channel = self.bot.get_channel(channel_id)
        if channel:
            try:
                await channel.send(
                    f"<@{user_id}> still waiting? Fliphone is quiet right now.\n"
                    "Run `f.notify` to opt into a DM whenever someone joins the queue, "
                    "then you can use `f.hangup` and come back when there is activity."
                )
            except discord.HTTPException:
                pass
        self._queue_nudges.pop(channel_id, None)

    def _start_timeout(self, channel_id: int, delay_seconds: Optional[float] = None) -> None:
        self._cancel_timeout(channel_id)
        self._timeouts[channel_id] = asyncio.create_task(self._run_timeout(channel_id, delay_seconds))

    def _cancel_timeout(self, channel_id: int) -> None:
        task = self._timeouts.pop(channel_id, None)
        if task:
            task.cancel()

    def _start_queue_nudge(
        self,
        channel_id: int,
        user_id: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        self._cancel_queue_nudge(channel_id)
        self._queue_nudges[channel_id] = asyncio.create_task(
            self._run_queue_nudge(channel_id, user_id, delay_seconds)
        )

    def _cancel_queue_nudge(self, channel_id: int) -> None:
        task = self._queue_nudges.pop(channel_id, None)
        if task:
            task.cancel()

    # ── Inactivity timeout ───────────────────────────────────────────────────

    INACTIVITY_MINUTES = 10

    async def _inactivity_timer(
        self,
        conn_id: int,
        channel_a: int,
        channel_b: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        """Auto-hangup a call after INACTIVITY_MINUTES of no messages."""
        delay = self.INACTIVITY_MINUTES * 60 if delay_seconds is None else max(0.0, delay_seconds)
        await asyncio.sleep(delay)
        # Check call still active
        conn = await self._get_connection_cached(channel_a)
        if not conn or conn["id"] != conn_id:
            return
        # Cache last call for both guilds so report.py can find the partner
        for gid, other_gid in (
            (conn["guild_a"], conn["guild_b"]),
            (conn["guild_b"], conn["guild_a"]),
        ):
            self._last_calls[gid] = {
                "other_guild_id": other_gid,
                "started_at":     conn["started_at"],
                "ended_at":       datetime.utcnow().isoformat(),
                "active":         False,
            }
        report_cog = self.bot.get_cog("Report")
        if report_cog:
            report_cog.clear_log(conn["id"])
        self._last_calls[conn["guild_a"]] = {
            "other_guild_id": conn["guild_b"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        self._last_calls[conn["guild_b"]] = {
            "other_guild_id": conn["guild_a"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        await self.db.remove_connection(conn_id)
        self._invalidate_connection(conn)
        self._call_reported_gifs.pop(conn_id, None)
        self._inactivity_tasks.pop(conn_id, None)
        self._clear_rl_state(channel_a)
        self._clear_rl_state(channel_b)
        msg = (
            f"📵 Call ended after {self.INACTIVITY_MINUTES} minutes of inactivity.\n"
            "Use `f.call` to start a new call."
        )
        for ch_id in (channel_a, channel_b):
            ch = self.bot.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(msg)
                except discord.HTTPException:
                    pass

    def _reset_inactivity(
        self,
        conn_id: int,
        channel_a: int,
        channel_b: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        """Reset the inactivity timer when a message is sent."""
        old = self._inactivity_tasks.pop(conn_id, None)
        if old:
            old.cancel()
        self._inactivity_tasks[conn_id] = asyncio.create_task(
            self._inactivity_timer(conn_id, channel_a, channel_b, delay_seconds)
        )

    def _cancel_inactivity(self, conn_id: int) -> None:
        task = self._inactivity_tasks.pop(conn_id, None)
        if task:
            task.cancel()

    async def _recover_runtime_state(self) -> None:
        """Resume or expire persisted queues/calls after a bot restart."""
        await self.bot.wait_until_ready()
        try:
            await self._recover_active_connections()
            await self._recover_queue_entries()
        except Exception as exc:
            print(f"[recovery] phonebooth recovery failed: {exc}")

    async def _recover_queue_entries(self) -> None:
        timeout_seconds = config.QUEUE_TIMEOUT * 60
        for entry in await self.db.get_all_queue_entries():
            channel_id = int(entry["channel_id"])
            elapsed = _elapsed_seconds(entry.get("joined_at"))
            if elapsed >= timeout_seconds:
                await self._run_timeout(channel_id, 0)
                continue

            self._start_timeout(channel_id, timeout_seconds - elapsed)
            nudge_delay = self.QUEUE_NOTIFY_NUDGE_SECONDS - elapsed
            self._start_queue_nudge(
                channel_id,
                int(entry["user_id"]),
                max(1.0, nudge_delay),
            )

    async def _recover_active_connections(self) -> None:
        timeout_seconds = self.INACTIVITY_MINUTES * 60
        seen_channels: set[int] = set()
        connections = sorted(
            await self.db.get_active_connections(),
            key=lambda item: (item.get("started_at") or "", int(item["id"])),
        )
        for conn in connections:
            conn_id = int(conn["id"])
            channel_a = int(conn["channel_a"])
            channel_b = int(conn["channel_b"])
            if channel_a in seen_channels or channel_b in seen_channels:
                await self.db.remove_connection(conn_id)
                for channel_id in (channel_a, channel_b):
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(
                                "📵 A conflicting duplicate call was cleared after restart. "
                                "Use `f.call` to connect again."
                            )
                        except discord.HTTPException:
                            pass
                continue
            seen_channels.update((channel_a, channel_b))
            await asyncio.gather(
                self.db.remove_from_queue(channel_a),
                self.db.remove_from_queue(channel_b),
            )
            last_activity = conn.get("last_activity_at") or conn.get("started_at")
            elapsed = _elapsed_seconds(last_activity)
            if elapsed >= timeout_seconds:
                await self._inactivity_timer(
                    conn_id,
                    channel_a,
                    channel_b,
                    0,
                )
                continue

            self._cache_connection(conn)
            self._reset_inactivity(
                conn_id,
                channel_a,
                channel_b,
                timeout_seconds - elapsed,
            )

    # ── Rate limiting (1:1 calls) ─────────────────────────────────────────────
    _RL_BASE_MSGS = 30
    _RL_PER_EXTRA_SPEAKER = 15
    _RL_MAX_MSGS = 120
    _RL_WINDOW = 15.0
    _RL_WARN_MAX = 3
    _CALL_RL_COOLDOWN_SECONDS = 5 * 60
    _WEBHOOK_REPAIR_ATTEMPTS = 2

    @staticmethod
    def _dynamic_rl_limit(active_speakers: int) -> int:
        speakers = max(1, active_speakers)
        return min(
            Phonebooth._RL_MAX_MSGS,
            Phonebooth._RL_BASE_MSGS + (speakers - 1) * Phonebooth._RL_PER_EXTRA_SPEAKER,
        )

    def _check_call_rate_limit(self, channel_id: int, user_id: int) -> tuple[bool, int, int, int]:
        """Return (limited, count, limit, active_speakers) for this source channel."""
        now = time.monotonic()
        dq  = self._rl_times.setdefault(channel_id, deque())
        dq.append((now, user_id))
        cutoff = now - self._RL_WINDOW
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        active_speakers = len({uid for _, uid in dq})
        limit = self._dynamic_rl_limit(active_speakers)
        return len(dq) > limit, len(dq), limit, active_speakers

    def _clear_rl_state(self, channel_id: int) -> None:
        self._rl_times.pop(channel_id, None)
        self._rl_warns.pop(channel_id, None)

    def _call_cooldown_remaining(self, channel_id: int) -> int:
        expiry = self._call_rate_cooldowns.get(channel_id, 0.0)
        remaining = int(expiry - time.monotonic())
        if remaining <= 0:
            self._call_rate_cooldowns.pop(channel_id, None)
            return 0
        return remaining

    @tasks.loop(minutes=5)
    async def _cleanup_loop(self) -> None:
        now = datetime.utcnow()
        for report in await self.db.get_open_gif_report_prompts():
            session_active = False
            if report.get("session_type") == "call" and report.get("session_id"):
                session_active = bool(await self.db.get_connection_by_id(report["session_id"]))
            elif report.get("session_type") == "room" and report.get("session_id"):
                room = await self.db.get_room_by_id(report["session_id"])
                session_active = bool(room and room["status"] in {"waiting", "active"})
            if session_active:
                continue
            if not report.get("expires_at"):
                await self.db.set_gif_report_expiry(
                    report["id"], (now + timedelta(minutes=30)).isoformat()
                )
                continue
            if datetime.fromisoformat(report["expires_at"]) > now:
                continue
            channel = self.bot.get_channel(report.get("prompt_channel_id"))
            if channel:
                try:
                    prompt = await channel.fetch_message(report["prompt_msg_id"])
                    await prompt.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await self.db.resolve_gif_report(report["id"], "expired")

    # ── Webhook helpers ───────────────────────────────────────────────────────

    def _invalidate_webhook_url(self, url: Optional[str]) -> None:
        if not url:
            return
        self._wh_obj_cache.pop(url, None)
        stale_channels = [
            channel_id
            for channel_id, cached_url in self._channel_webhook_urls.items()
            if cached_url == url
        ]
        for channel_id in stale_channels:
            self._channel_webhook_urls.pop(channel_id, None)

    async def get_or_create_webhook(
        self,
        channel: discord.TextChannel,
        *,
        force_refresh: bool = False,
    ) -> Optional[str]:
        cached = None if force_refresh else self._channel_webhook_urls.get(channel.id)
        if cached:
            return cached
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user == self.bot.user and wh.name == "Fliphone":
                    self._channel_webhook_urls[channel.id] = wh.url
                    return wh.url
            wh = await channel.create_webhook(name="Fliphone")
            self._channel_webhook_urls[channel.id] = wh.url
            return wh.url
        except discord.Forbidden:
            return None
        except Exception as exc:
            print(f"[webhook] {exc}")
            return None

    @staticmethod
    def relay_permission_issues(channel: discord.TextChannel) -> list[str]:
        bot_member = channel.guild.me
        if bot_member is None:
            return ["Bot member unavailable"]
        perms = channel.permissions_for(bot_member)
        required = (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
            ("Manage Webhooks", perms.manage_webhooks),
            ("Attach Files", perms.attach_files),
            ("Add Reactions", perms.add_reactions),
        )
        return [name for name, allowed in required if not allowed]

    async def ensure_relay_webhook(
        self,
        channel: discord.TextChannel,
        *,
        force_refresh: bool = False,
    ) -> tuple[Optional[str], list[str]]:
        """Validate required permissions and refresh the channel's stored webhook URL."""
        issues = self.relay_permission_issues(channel)
        if issues:
            return None, issues

        webhook_url = await self.get_or_create_webhook(
            channel,
            force_refresh=force_refresh,
        )
        if not webhook_url:
            return None, ["Webhook access failed"]

        asyncio.create_task(self.db.update_webhook(channel.id, webhook_url))
        cfg = self._cfg_by_channel.get(channel.id)
        if cfg is not None:
            cfg["webhook_url"] = webhook_url
        return webhook_url, []

    async def rebuild_relay_webhook(
        self,
        channel: discord.TextChannel,
    ) -> tuple[Optional[str], list[str]]:
        """Delete old Fliphone webhooks and create one clean replacement."""
        issues = self.relay_permission_issues(channel)
        if issues:
            return None, issues

        try:
            webhooks = await channel.webhooks()
            other_webhook_count = 0
            for webhook in webhooks:
                if webhook.user == self.bot.user and webhook.name == "Fliphone":
                    self._wh_obj_cache.pop(webhook.url, None)
                    await webhook.delete(reason="Fliphone setup reset")
                else:
                    other_webhook_count += 1
            if other_webhook_count >= 15:
                return None, ["This channel already has Discord's maximum of 15 webhooks"]
            webhook_url = (await channel.create_webhook(name="Fliphone")).url
        except discord.Forbidden:
            return None, ["Manage Webhooks"]
        except discord.HTTPException as exc:
            if exc.code == 30007:
                return None, ["This channel already has Discord's maximum number of webhooks"]
            return None, ["Discord webhook creation failed"]

        self._channel_webhook_urls[channel.id] = webhook_url
        await self.db.update_webhook(channel.id, webhook_url)
        cfg = self._cfg_by_channel.get(channel.id)
        if cfg is not None:
            cfg["webhook_url"] = webhook_url
        return webhook_url, []

    async def _get_user_relay_policy(self, user_id: int) -> tuple[bool, bool]:
        now = time.monotonic()
        cached = self._user_policy_cache.get(user_id)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        banned, anonymous = await asyncio.gather(
            self.db.is_user_banned(user_id),
            self.db.is_user_anonymous(user_id),
        )
        self._user_policy_cache[user_id] = (now + 60, banned, anonymous)
        return banned, anonymous

    async def _connected_message_for(self, user_id: int) -> str:
        return _CONNECTED_MSG

    async def _notify_anon_mode_start(
        self,
        channel: discord.abc.Messageable,
        user_id: int,
    ) -> None:
        _, anonymous = await self._get_user_relay_policy(user_id)
        if not anonymous:
            return
        try:
            await channel.send(f"<@{user_id}> {_ANON_NOTICE}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def probe_webhook_avatar(
        self,
        channel: discord.TextChannel,
        member: discord.Member | discord.User,
    ) -> tuple[bool, str]:
        """Send and delete a webhook probe, verifying Discord applied an avatar."""
        webhook_url, issues = await self.ensure_relay_webhook(channel)
        if not webhook_url:
            return False, ", ".join(issues)

        probe = await self._send_webhook(
            webhook_url,
            "Fliphone avatar relay diagnostic",
            "Fliphone Avatar Test",
            _get_avatar_url(member),
            [],
            wait=True,
            silent=True,
        )
        if not isinstance(probe, discord.WebhookMessage):
            return False, "Webhook test message failed"

        avatar_applied = probe.author.avatar is not None
        try:
            await probe.delete()
        except discord.HTTPException:
            pass
        return avatar_applied, "" if avatar_applied else "Discord did not apply the webhook avatar"

    async def _get_valid_queue_match(self, guild_id: int, channel_id: int) -> Optional[dict]:
        """Discard stale queue entries until a server with a working webhook is found."""
        while True:
            match = await self.db.get_queue_match(guild_id, channel_id)
            if not match:
                return None

            partner_channel = self.bot.get_channel(match["channel_id"])
            if isinstance(partner_channel, discord.TextChannel):
                webhook_url, issues = await self.ensure_relay_webhook(partner_channel)
                if webhook_url:
                    match["webhook_url"] = webhook_url
                    return match
            else:
                issues = ["Configured channel is missing"]

            await self.db.remove_from_queue(match["channel_id"])
            self._cancel_timeout(match["channel_id"])
            self._cancel_queue_nudge(match["channel_id"])
            if partner_channel:
                try:
                    await partner_channel.send(
                        "⚠️ Fliphone removed this server from the queue because webhook relay is unavailable.\n"
                        f"Missing or broken: **{', '.join(issues)}**\n"
                        "A server admin should run `f.setup` in this channel."
                    )
                except discord.HTTPException:
                    pass

    async def _claim_valid_queue_match(
        self,
        *,
        guild_id: int,
        channel_id: int,
        webhook_url: str,
    ) -> Optional[dict]:
        """Validate and atomically claim one queued partner."""
        for _ in range(3):
            match = await self._get_valid_queue_match(guild_id, channel_id)
            if not match:
                return None
            claimed = await self.db.claim_queue_connection(
                channel_a=channel_id,
                guild_a=guild_id,
                webhook_a=webhook_url,
                channel_b=match["channel_id"],
                webhook_b=match["webhook_url"],
            )
            if claimed:
                return {"connection": claimed, "match": match}
        return None

    async def _end_broken_connection(self, conn: dict) -> None:
        """End a call instead of exposing relay content through plain bot fallback."""
        conn_id = int(conn["id"])
        if conn_id in self._ending_broken_connections:
            return
        self._ending_broken_connections.add(conn_id)
        try:
            current = await self.db.get_connection(conn["channel_a"])
            if not current or int(current["id"]) != conn_id:
                return
            await self.db.remove_connection(conn_id)
            self._invalidate_connection(conn)
            self._call_reported_gifs.pop(conn_id, None)
            self._cancel_inactivity(conn_id)
            self._clear_rl_state(conn["channel_a"])
            self._clear_rl_state(conn["channel_b"])
            report_cog = self.bot.get_cog("Report")
            if report_cog:
                report_cog.clear_log(conn_id)

            notice = (
                "⚠️ **Call ended because webhook relay became unavailable.**\n"
                "No messages were sent using the plain bot fallback. "
                "Automatic repair failed. A server admin should run `f.repair` in this channel."
            )
            for channel_id in (conn["channel_a"], conn["channel_b"]):
                channel = self.bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(notice)
                    except discord.HTTPException:
                        pass
        finally:
            self._ending_broken_connections.discard(conn_id)

    async def _end_rate_limited_connection(
        self,
        conn: dict,
        offender_channel_id: int,
        offender_user_id: int,
    ) -> None:
        """End a call and apply the 5-minute call cooldown to one source channel."""
        conn_id = int(conn["id"])
        current = await self.db.get_connection(conn["channel_a"])
        if not current or int(current["id"]) != conn_id:
            return

        self._call_rate_cooldowns[offender_channel_id] = (
            time.monotonic() + self._CALL_RL_COOLDOWN_SECONDS
        )
        self._last_calls[conn["guild_a"]] = {
            "other_guild_id": conn["guild_b"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        self._last_calls[conn["guild_b"]] = {
            "other_guild_id": conn["guild_a"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        await self.db.remove_connection(conn_id, ended_by=offender_user_id)
        self._invalidate_connection(conn)
        self._call_reported_gifs.pop(conn_id, None)
        self._cancel_inactivity(conn_id)
        self._clear_rl_state(conn["channel_a"])
        self._clear_rl_state(conn["channel_b"])
        report_cog = self.bot.get_cog("Report")
        if report_cog:
            report_cog.clear_log(conn_id)

        other_channel_id = (
            conn["channel_b"] if offender_channel_id == conn["channel_a"] else conn["channel_a"]
        )
        offender_channel = self.bot.get_channel(offender_channel_id)
        if offender_channel:
            try:
                await offender_channel.send(
                    "Call ended because this channel hit the relay rate limit.\n"
                    "You can start another 1:1 call in 5 minutes."
                )
            except discord.HTTPException:
                pass
        other_channel = self.bot.get_channel(other_channel_id)
        if other_channel:
            try:
                await other_channel.send(
                    "The other server hit the relay rate limit, so the call ended.\n"
                    "Use `f.call` to start another call."
                )
            except discord.HTTPException:
                pass

    async def _send_webhook(
        self,
        url: str,
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        reply_embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> discord.WebhookMessage | bool | None:
        """
        Send a webhook message using the persistent session and cached Webhook objects.
        If wait=True, returns the WebhookMessage. If wait=False, returns True on success.
        """
        try:
            session = self._session
            if session is None or session.closed:
                session = aiohttp.ClientSession()
                self._session = session
            wh = self._wh_obj_cache.get(url)
            if wh is None:
                wh = discord.Webhook.from_url(url, session=session)
                self._wh_obj_cache[url] = wh

            async def _send_once(
                send_content: Optional[str],
                send_embed: Optional[discord.Embed],
                *,
                send_avatar_url: Optional[str] = avatar_url,
            ) -> discord.WebhookMessage | bool:
                msg = await wh.send(
                    content=send_content or None,
                    username=username[:80],
                    avatar_url=send_avatar_url,
                    embeds=[send_embed] if send_embed else discord.utils.MISSING,
                    files=files if files else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                    wait=wait,
                )
                return msg if wait else True

            msg = await _send_once(content, reply_embed)
            return msg if wait else True
        except discord.HTTPException as exc:
            status = getattr(exc, "status", None)
            if status == 400:
                fallback = await self._send_webhook_bad_content_fallback(
                    wh,
                    content,
                    username,
                    avatar_url,
                    files,
                    reply_embed=reply_embed,
                    wait=wait,
                    silent=silent,
                )
                if fallback:
                    return fallback
                print(f"[relay-webhook-content] status={status} code={getattr(exc, 'code', None)} {exc}")
                return None if wait else False
            print(f"[relay-webhook] status={status} code={getattr(exc, 'code', None)} {exc}")
            self._invalidate_webhook_url(url)
            return None if wait else False
        except Exception as exc:
            print(f"[relay-webhook] {exc}")
            self._invalidate_webhook_url(url)
            return None if wait else False

    async def _send_webhook_bad_content_fallback(
        self,
        wh: discord.Webhook,
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        *,
        reply_embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> discord.WebhookMessage | bool | None:
        def _downgrade_embed(embed: Optional[discord.Embed]) -> tuple[Optional[discord.Embed], list[int]]:
            if not embed or not embed.description:
                return embed, []
            updated, ids = downgrade_animated_custom_emoji_markup(embed.description)
            if not ids:
                return embed, []
            clone = embed.copy()
            clone.description = updated
            return clone, ids

        downgraded_content, content_ids = downgrade_animated_custom_emoji_markup(content or "")
        downgraded_embed, embed_ids = _downgrade_embed(reply_embed)
        if content_ids or embed_ids:
            try:
                msg = await wh.send(
                    content=downgraded_content or None,
                    username=username[:80],
                    avatar_url=avatar_url,
                    embeds=[downgraded_embed] if downgraded_embed else discord.utils.MISSING,
                    files=files if files else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                    wait=wait,
                )
                asyncio.create_task(self.db.mark_app_emojis_static(content_ids + embed_ids))
                return msg if wait else True
            except discord.HTTPException:
                pass

        plain_content = plain_custom_emoji_fallback(content or "").strip()
        plain_embed = reply_embed
        if reply_embed and reply_embed.description:
            plain_embed = reply_embed.copy()
            plain_embed.description = plain_custom_emoji_fallback(reply_embed.description).strip() or "emoji"
        if not plain_content and not plain_embed and not files:
            plain_content = "emoji"
        try:
            msg = await wh.send(
                content=plain_content or None,
                username=username[:80],
                avatar_url=avatar_url,
                embeds=[plain_embed] if plain_embed else discord.utils.MISSING,
                files=files if files else discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=silent,
                wait=wait,
            )
            return msg if wait else True
        except discord.HTTPException as exc:
            try:
                msg = await wh.send(
                    content=plain_content or None,
                    username=username[:80],
                    avatar_url=None,
                    embeds=[plain_embed] if plain_embed else discord.utils.MISSING,
                    files=files if files else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                    wait=wait,
                )
                return msg if wait else True
            except discord.HTTPException:
                pass
            print(f"[relay-webhook-content-fallback] status={getattr(exc, 'status', None)} code={getattr(exc, 'code', None)} {exc}")
            return None

    async def _repair_relay_webhook(
        self,
        channel: discord.abc.GuildChannel,
        conn: dict,
    ) -> Optional[str]:
        if not isinstance(channel, discord.TextChannel):
            return None
        old_webhook_url = None
        if channel.id == conn["channel_a"]:
            old_webhook_url = conn.get("webhook_a")
        elif channel.id == conn["channel_b"]:
            old_webhook_url = conn.get("webhook_b")
        self._invalidate_webhook_url(old_webhook_url)

        # A failed webhook can still appear in channel.webhooks(), so merely
        # refreshing it may return the same unusable token. Rebuild it exactly
        # as f.repair does, but without tearing down the active call.
        webhook_url, repair_issues = await self.rebuild_relay_webhook(channel)
        if not webhook_url:
            print(
                f"[relay-webhook-repair] channel={channel.id} failed: "
                f"{', '.join(repair_issues) or 'unknown error'}"
            )
            return None

        await self.db.update_connection_webhook(channel.id, webhook_url)
        if channel.id == conn["channel_a"]:
            conn["webhook_a"] = webhook_url
        elif channel.id == conn["channel_b"]:
            conn["webhook_b"] = webhook_url
        self._cache_connection(conn)
        return webhook_url

    # ── Message relay ─────────────────────────────────────────────────────────

    async def _send_call_webhook_with_repair(
        self,
        target_channel: Optional[discord.abc.GuildChannel],
        conn: dict,
        webhook_url: Optional[str],
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        *,
        reply_embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> discord.WebhookMessage | bool | None:
        if webhook_url:
            sent = await self._send_webhook(
                webhook_url,
                content,
                username,
                avatar_url,
                files,
                reply_embed=reply_embed,
                wait=wait,
                silent=silent,
            )
            if sent:
                return sent

        if not isinstance(target_channel, discord.TextChannel):
            return None if wait else False

        for attempt in range(self._WEBHOOK_REPAIR_ATTEMPTS):
            repaired_url = await self._repair_relay_webhook(target_channel, conn)
            if not repaired_url:
                if attempt + 1 < self._WEBHOOK_REPAIR_ATTEMPTS:
                    await asyncio.sleep(0.5)
                continue

            sent = await self._send_webhook(
                repaired_url,
                content,
                username,
                avatar_url,
                files,
                reply_embed=reply_embed,
                wait=wait,
                silent=silent,
            )
            if sent:
                return sent

        return None if wait else False

    async def _relay(self, message: discord.Message, conn: dict) -> None:
        is_side_a  = message.channel.id == conn["channel_a"]
        target_cid = conn["channel_b"] if is_side_a else conn["channel_a"]
        target_wh  = conn["webhook_b"] if is_side_a else conn["webhook_a"]
        target_gid = conn["guild_b"]   if is_side_a else conn["guild_a"]

        async def _send_gif_report_card(send_channel, gif_url: str, msg_id: Optional[int]) -> None:
            if await self.db.check_gif_url(gif_url) == "whitelist":
                return
            report_id = await self.db.add_gif_report(
                url=gif_url,
                msg_id=msg_id,
                channel_id=target_cid,
                guild_id=target_gid,
                session_type="call",
                session_id=conn["id"],
                sender_user_id=message.author.id,
                source_guild_id=message.guild.id,
                source_channel_id=message.channel.id,
            )
            prompt = await send_channel.send(view=GifReportView(), silent=True)
            await self.db.set_gif_report_prompt(report_id, prompt.id, prompt.channel.id)

        # ── Rate limiting (sync — no DB needed) ──────────────────────────────
        is_rate_limited, _, cap, speakers = self._check_call_rate_limit(
            message.channel.id,
            message.author.id,
        )
        if is_rate_limited:
            warn = self._rl_warns.get(message.channel.id, 0) + 1
            self._rl_warns[message.channel.id] = warn
            if warn <= self._RL_WARN_MAX:
                try:
                    await message.channel.send(
                        f"{message.author.mention} This channel is sending messages too fast "
                        f"for this call. Current cap: **{cap}/{int(self._RL_WINDOW)}s** "
                        f"with **{speakers}** active speaker(s). "
                        f"(Warning **{warn}/{self._RL_WARN_MAX}**)",
                        delete_after=8,
                    )
                except discord.HTTPException:
                    pass
            if warn >= self._RL_WARN_MAX:
                await self._end_rate_limited_connection(
                    conn,
                    message.channel.id,
                    message.author.id,
                )
            return
        self._rl_warns.pop(message.channel.id, None)

        # ── Ban check + config fetch in parallel ──────────────────────────────
        is_banned, anon = await self._get_user_relay_policy(message.author.id)
        if is_banned:
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} You are banned from using Fliphone.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return

        # ── Identity ──────────────────────────────────────────────────────────
        if anon:
            seed = conn["id"] * 100000 + message.author.id
            display_name, avatar_url = _anon_identity(seed)
        else:
            member       = message.author
            display_name = _relay_display_name(member)
            avatar_url = _get_avatar_url(member)

        # ── Reply context embed ───────────────────────────────────────────────
        reply_embed: Optional[discord.Embed] = None
        reply_context: Optional[str] = None
        if message.reference:
            ref_msg = message.reference.resolved
            if isinstance(ref_msg, discord.Message):
                ref_author = _relay_display_name(ref_msg.author)
                # For relayed webhook messages, display_avatar IS the user's pfp
                # because we update the webhook avatar on every send.
                # For regular messages use the normal helper.
                try:
                    ref_avatar = str(ref_msg.author.display_avatar.with_static_format("png").with_size(64).url).split("?")[0]
                except Exception:
                    ref_avatar = None
                ref_text = (ref_msg.content or "").strip()
                if is_local_only(ref_text):
                    ref_text = "message"
                else:
                    ref_text = " ".join(
                        l for l in ref_text.splitlines() if not l.startswith("http")
                    ).strip()
                if len(ref_text) > 100:
                    ref_text = ref_text[:100] + "…"
                elif not ref_text:
                    if ref_msg.attachments:
                        ext = ref_msg.attachments[0].filename.rsplit(".", 1)[-1].lower()
                        ref_text = "GIF" if ext == "gif" else "image"
                    elif ref_msg.embeds:
                        ref_text = "image"
                    else:
                        ref_text = "message"
                embed_color = random.randint(0x100000, 0xFFFFFF)
                ref_text, _ = filter_message(ref_text)
                had_custom_emoji = bool(CUSTOM_EMOJI_PATTERN.search(ref_text))
                ref_text, _ = await replace_approved_custom_emojis(ref_text, self.db)
                ref_text = ref_text.strip() or ("emoji" if had_custom_emoji else "")
                ref_text, _ = _limit_unicode_emojis(ref_text)
                ref_text = _render_user_mentions(ref_text, message.guild)
                reply_context = (
                    f"> Replying to **{discord.utils.escape_markdown(ref_author)}**: "
                    f"{discord.utils.escape_markdown(ref_text)}"
                )
                reply_embed = discord.Embed(description=ref_text, color=embed_color)
                reply_embed.set_author(name=f"Replying to {ref_author}", icon_url=ref_avatar)

        # ── Content ───────────────────────────────────────────────────────────
        raw_content = (message.content or "")

        if is_local_only(raw_content):
            return

        content, was_censored = filter_message(raw_content)
        if was_censored:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Your message contained a blocked word "
                    f"and was censored before being sent.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        content, used_custom_emoji_ids = await replace_approved_custom_emojis(content, self.db)
        content = content.strip()
        content, emojis_trimmed = _limit_unicode_emojis(content)
        if emojis_trimmed:
            content = content.strip()
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Too many emojis at once. "
                    f"Only the first **{MAX_EMOJIS_PER_MESSAGE}** were sent.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
        content = _render_user_mentions(content, message.guild)

        # ── Strip non-GIF links ───────────────────────────────────────────────
        # GIF URLs (Tenor/Giphy/.gif) are handled separately below.
        # Every other link is removed — no external URLs get relayed.
        non_gif_links = [
            url for url in LINK_PATTERN.findall(content)
            if not is_provider_gif(url) and not is_direct_gif_url(url)
        ]
        if non_gif_links:
            for url in non_gif_links:
                content = content.replace(url, "")
            content = content.strip()
            try:
                await message.channel.send(
                    f"🔗 {message.author.mention} Links aren't allowed in calls.",
                    delete_after=6,
                )
            except discord.HTTPException:
                pass

        # ── Detect GIF URLs in text content ───────────────────────────────────
        inline_gif_urls = [
            url for url in extract_urls(content)
            if is_provider_gif(url) or is_direct_gif_url(url)
        ]

        # ── Attachments ───────────────────────────────────────────────────────
        GIF_EXT = {".gif"}
        files: list[discord.File] = []
        attachment_gif_urls: list[str] = []
        blocked_attachment_count = len(message.stickers)

        for att in message.attachments:
            ext = ("." + att.filename.rsplit(".", 1)[-1].lower()) if "." in att.filename else ""
            if ext in GIF_EXT:
                content += f"\n{att.url}"
                attachment_gif_urls.append(att.url)
            else:
                blocked_attachment_count += 1

        # ── All GIF URLs (inline + attachments) — deduplicate by stripped URL ──
        def _norm_dedup(u: str) -> str:
            return u.split("?")[0].rstrip("/").lower()

        seen_norms: set[str] = set()
        all_gif_urls: list[str] = []
        for u in inline_gif_urls + attachment_gif_urls:
            n = _norm_dedup(u)
            if n not in seen_norms:
                seen_norms.add(n)
                all_gif_urls.append(u)

        safe_urls: list[str] = []
        gif_statuses = await asyncio.gather(
            *[self.db.check_gif_url(url) for url in all_gif_urls],
            return_exceptions=True,
        ) if all_gif_urls else []
        gif_status_by_url = {
            url: (None if isinstance(status, Exception) else status)
            for url, status in zip(all_gif_urls, gif_statuses)
        }
        for url, status in zip(all_gif_urls, gif_statuses):
            if status == "blacklist":
                continue
            if is_provider_gif(url) or status == "whitelist":
                safe_urls.append(url)

        for gif_url in [u for u in all_gif_urls if u not in safe_urls]:
            content = content.replace(gif_url, "")
        content = content.strip()
        all_gif_urls = safe_urls

        if inline_gif_urls and not safe_urls:
            try:
                await message.channel.send(
                    "That GIF source is not approved. Run `f.addgif`, then send it again within 60 seconds for review.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        # ── Blacklist + per-call reported check ──────────────────────────────
        def _norm(u: str) -> str:
            return u.split("?")[0].rstrip("/").lower()

        call_reported = self._call_reported_gifs.get(conn["id"], set())
        blocked_urls: set[str] = set()
        if all_gif_urls:
            for gif_url in all_gif_urls:
                status = gif_status_by_url.get(gif_url)
                norm_url = _norm(gif_url)
                if status == "blacklist" or norm_url in call_reported:
                    blocked_urls.add(gif_url)
                    content = content.replace(gif_url, "")
        if blocked_urls:
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} A blocked GIF was removed.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        # GIFs that are not blocked — these get report cards
        reportable_gif_urls = []
        for url in all_gif_urls:
            if url in blocked_urls:
                continue
            if gif_status_by_url.get(url) != "whitelist":
                reportable_gif_urls.append(url)

        # Keep GIF replies in one webhook message so reply context stays attached.
        if reply_embed and reportable_gif_urls:
            text_content = "\n".join(part for part in (reply_context, content.strip()) if part)
            reply_embed = None
        else:
            text_content = content.strip() or None

        # ── Check if message is now empty ───────────────────────────────────
        if not text_content and not files:
            return

        conn["msg_count"] = int(conn.get("msg_count", 0)) + 1
        asyncio.create_task(self.db.increment_message_count(conn["id"]))
        # Reset inactivity timer — someone is talking
        self._reset_inactivity(conn["id"], conn["channel_a"], conn["channel_b"])

        report_ch_id = int(config.REPORT_LOG_CHANNEL_ID) if config.REPORT_LOG_CHANNEL_ID else 0

        # ── Send via webhook ──────────────────────────────────────────────────
        report_cog = self.bot.get_cog("Report")
        if report_cog:
            report_cog.record_message(
                conn_id=conn["id"],
                user_id=message.author.id,
                username=str(message.author),
                guild_id=message.guild.id,
                guild_name=message.guild.name,
                content=content.strip(),
            )
        need_id = bool(reportable_gif_urls)
        target_channel = self.bot.get_channel(target_cid)

        main_wh_msg = await self._send_call_webhook_with_repair(
            target_channel,
            conn,
            target_wh,
            text_content,
            display_name,
            avatar_url,
            files,
            reply_embed=reply_embed,
            wait=True,
            silent=bool(reportable_gif_urls),
        )
        if main_wh_msg:
            self._reaction_routes[(message.channel.id, message.id)] = (target_cid, main_wh_msg.id)
            self._reaction_routes[(target_cid, main_wh_msg.id)] = (message.channel.id, message.id)
            if used_custom_emoji_ids:
                asyncio.create_task(self.db.increment_emoji_usage(used_custom_emoji_ids))
            asyncio.create_task(
                self.db.add_chat_xp(
                    message.author.id,
                    message.guild.id,
                    random.randint(CHAT_XP_MIN, CHAT_XP_MAX),
                    CHAT_XP_COOLDOWN_SECONDS,
                )
            )
            # GIF report cards
            if reportable_gif_urls:
                target_ch = self.bot.get_channel(target_cid)
                if target_ch:
                    report_tasks = [
                        _send_gif_report_card(
                            target_ch,
                            gif_url,
                            main_wh_msg.id,
                        )
                        for gif_url in reportable_gif_urls
                    ]
                    if report_tasks:
                        await asyncio.gather(*report_tasks, return_exceptions=True)
            return

        await self._end_broken_connection(conn)

    async def _relay_reaction(self, payload: discord.RawReactionActionEvent, *, remove: bool) -> None:
        if not self.bot.user or payload.user_id == self.bot.user.id or payload.emoji.id is not None:
            return
        route = self._reaction_routes.get((payload.channel_id, payload.message_id))
        if not route:
            return
        now = time.monotonic()
        times = self._reaction_times.setdefault(payload.user_id, deque())
        while times and now - times[0] > 10:
            times.popleft()
        if len(times) >= 5:
            return
        times.append(now)
        channel = self.bot.get_channel(route[0])
        if not channel:
            return
        try:
            target = await channel.fetch_message(route[1])
            if remove:
                await target.remove_reaction(str(payload.emoji), self.bot.user)
            else:
                await target.add_reaction(str(payload.emoji))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._relay_reaction(payload, remove=False)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._relay_reaction(payload, remove=True)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        gif_cog = self.bot.get_cog("GifSubmission")
        if gif_cog and (gif_cog.is_consumed(message.id) or await gif_cog.consume_capture(message)):
            return
        if is_local_only(message.content or ""):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        conn = await self._get_connection_cached(message.channel.id)
        if not conn:
            return
        target_channel_id = (
            int(conn["channel_b"])
            if message.channel.id == int(conn["channel_a"])
            else int(conn["channel_a"])
        )
        lock = self._relay_locks.setdefault(target_channel_id, asyncio.Lock())
        async with lock:
            await self._relay(message, conn)

    # ── f.call ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="call", aliases=["c", "dial", "connect"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def call(self, ctx: commands.Context) -> None:
        """Dial into the queue, or connect instantly."""
        is_banned, guild_banned, cfg, conn, room_member, q = await asyncio.gather(
            self.db.is_user_banned(ctx.author.id),
            self.db.is_guild_banned(ctx.guild.id),
            self._get_guild_config_cached(ctx.guild.id),
            self._get_connection_cached(ctx.channel.id),
            self.db.get_room_member(ctx.channel.id),
            self.db.get_queue_entry(ctx.channel.id),
        )

        if is_banned:
            await ctx.send("🚫 You are banned from using Fliphone.")
            return
        if guild_banned:
            await ctx.send("🚫 This server is banned from using Fliphone.")
            return
        cooldown_remaining = self._call_cooldown_remaining(ctx.channel.id)
        if cooldown_remaining:
            await ctx.send(
                "This channel is on a 1:1 call cooldown for another "
                f"**{cooldown_remaining // 60}m {cooldown_remaining % 60}s**."
            )
            return

        if not cfg:
            await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` once in this server.")
            return

        if conn:
            await ctx.send(f"📞 Already in a call ({_duration_str(conn['started_at'])}). Use `f.hangup` to end it first.")
            return

        # Block joining a 1:1 call while the channel is in a group room
        if room_member:
            await ctx.send("📡 This channel is currently in a group room. Use `f.roomleave` first.")
            return

        if q:
            await ctx.send(f"⏳ Already waiting ({_duration_str(q['joined_at'])}). Use `f.hangup` to cancel.")
            return

        wh_url, permission_issues = await self.ensure_relay_webhook(ctx.channel)
        if not wh_url:
            await ctx.send(
                "❌ Fliphone cannot start a call because webhook relay is unavailable.\n"
                f"Missing or broken: **{', '.join(permission_issues)}**\n"
                "A server admin should run `f.setup` in this channel."
            )
            return
        claimed = await self._claim_valid_queue_match(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            webhook_url=wh_url,
        )

        if claimed:
            match = claimed["match"]
            new_conn = claimed["connection"]
            self._cancel_timeout(match["channel_id"])
            self._cancel_queue_nudge(match["channel_id"])
            conn_id = int(new_conn["id"])
            self._cache_connection(new_conn)
            await ctx.send(await self._connected_message_for(ctx.author.id))
            partner_channel = self.bot.get_channel(match["channel_id"])
            if partner_channel:
                try:
                    await partner_channel.send(await self._connected_message_for(match["user_id"]))
                except discord.HTTPException:
                    pass
            await asyncio.gather(
                self._notify_anon_mode_start(ctx.channel, ctx.author.id),
                self._notify_anon_mode_start(partner_channel, match["user_id"]) if partner_channel else asyncio.sleep(0),
            )
            # Start inactivity timer for this call
            self._reset_inactivity(conn_id, ctx.channel.id, match["channel_id"])
        else:
            current = await self.db.get_connection(ctx.channel.id)
            if current:
                self._cache_connection(current)
                await ctx.send("☎️ This channel was already connected by another call request.")
                return
            search_msg = await ctx.send("📳 **Searching for someone to talk to...**")
            await self.db.add_to_queue(
                channel_id=ctx.channel.id, guild_id=ctx.guild.id,
                user_id=ctx.author.id, webhook_url=wh_url,
            )
            self._start_timeout(ctx.channel.id)
            self._start_queue_nudge(ctx.channel.id, ctx.author.id)
            queue_size, active = await asyncio.gather(
                self.db.get_queue_size(),
                self.db.get_active_connection_count(),
            )
            await search_msg.edit(
                content=(
                f"📳 **Searching for someone to talk to...** ({queue_size} waiting, {active} active calls)\n"
                f"Use `f.hangup` to cancel. Auto-cancels in {config.QUEUE_TIMEOUT} min."
                )
            )
            # Notify opted-in subscribers that someone is waiting
            asyncio.create_task(self._fire_notify(ctx.author.id))

    # ── f.hangup ──────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="hangup", aliases=["h", "disconnect", "bye"])
    @commands.guild_only()
    async def hangup(self, ctx: commands.Context) -> None:
        """End an active call or leave the queue."""
        q = await self.db.get_queue_entry(ctx.channel.id)
        if q:
            await self.db.remove_from_queue(ctx.channel.id)
            self._cancel_timeout(ctx.channel.id)
            self._cancel_queue_nudge(ctx.channel.id)
            await ctx.send("📵 Left the queue. Use `f.call` to dial again.")
            return

        conn = await self._get_connection_cached(ctx.channel.id)
        if not conn:
            await ctx.send("📵 Not in a call or queue. Use `f.call` to connect!")
            return

        other_cid = conn["channel_b"] if ctx.channel.id == conn["channel_a"] else conn["channel_a"]
        duration  = _duration_str(conn["started_at"])
        msg_count = conn["msg_count"]
        conn_id = conn["id"]
        report_cog = self.bot.get_cog("Report")
        if report_cog:
            report_cog.clear_log(conn["id"])
        self._last_calls[conn["guild_a"]] = {
            "other_guild_id": conn["guild_b"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        self._last_calls[conn["guild_b"]] = {
            "other_guild_id": conn["guild_a"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        await self.db.remove_connection(conn_id, ended_by=ctx.author.id)
        self._invalidate_connection(conn)
        self._call_reported_gifs.pop(conn_id, None)
        self._cancel_inactivity(conn_id)
        self._clear_rl_state(conn["channel_a"])
        self._clear_rl_state(conn["channel_b"])
        await ctx.send(
            f"📵 Call ended. Duration: **{duration}** · Messages: **{msg_count}**\n"
            "Use `f.call` to start a new call."
        )
        other = self.bot.get_channel(other_cid)
        if other:
            try:
                await other.send(
                    "📵 The other server ended the call.\n"
                    "Use `f.call` to start a new call."
                )
            except discord.HTTPException:
                pass

    # ── f.skip ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def skip(self, ctx: commands.Context) -> None:
        """End the current call and immediately search for a new one."""
        cfg = await self._get_guild_config_cached(ctx.guild.id)
        if not cfg:
            await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` once in this server.")
            return
        cooldown_remaining = self._call_cooldown_remaining(ctx.channel.id)
        if cooldown_remaining:
            await ctx.send(
                "This channel is on a 1:1 call cooldown for another "
                f"**{cooldown_remaining // 60}m {cooldown_remaining % 60}s**."
            )
            return

        conn = await self._get_connection_cached(ctx.channel.id)
        if conn:
            other_cid = conn["channel_b"] if ctx.channel.id == conn["channel_a"] else conn["channel_a"]
            skip_conn_id = conn["id"]
            report_cog = self.bot.get_cog("Report")
            if report_cog:
                report_cog.clear_log(conn["id"])
            self._last_calls[conn["guild_a"]] = {
                "other_guild_id": conn["guild_b"],
                "started_at": conn["started_at"],
                "ended_at": datetime.utcnow().isoformat(),
                "active": False,
                "conn_id": conn["id"],
            }
            self._last_calls[conn["guild_b"]] = {
                "other_guild_id": conn["guild_a"],
                "started_at": conn["started_at"],
                "ended_at": datetime.utcnow().isoformat(),
                "active": False,
                "conn_id": conn["id"],
            }
            await self.db.remove_connection(skip_conn_id, ended_by=ctx.author.id)
            self._invalidate_connection(conn)
            self._call_reported_gifs.pop(skip_conn_id, None)
            self._cancel_inactivity(skip_conn_id)
            self._clear_rl_state(conn["channel_a"])
            self._clear_rl_state(conn["channel_b"])
            other_ch = self.bot.get_channel(other_cid)
            if other_ch:
                try:
                    await other_ch.send(
                        "📵 The other user skipped.\n"
                        "Use `f.call` to start a new call."
                    )
                except discord.HTTPException:
                    pass
        else:
            q = await self.db.get_queue_entry(ctx.channel.id)
            if q:
                await self.db.remove_from_queue(ctx.channel.id)
                self._cancel_timeout(ctx.channel.id)
                self._cancel_queue_nudge(ctx.channel.id)

        await ctx.send("⏭️ you have skipped this caller.")

        wh_url, permission_issues = await self.ensure_relay_webhook(ctx.channel)
        if not wh_url:
            await ctx.send(
                "❌ Fliphone cannot search for a new call because webhook relay is unavailable.\n"
                f"Missing or broken: **{', '.join(permission_issues)}**\n"
                "A server admin should run `f.setup` in this channel."
            )
            return
        claimed = await self._claim_valid_queue_match(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            webhook_url=wh_url,
        )

        if claimed:
            match = claimed["match"]
            new_conn = claimed["connection"]
            self._cancel_timeout(match["channel_id"])
            self._cancel_queue_nudge(match["channel_id"])
            conn_id = int(new_conn["id"])
            self._cache_connection(new_conn)
            await ctx.send(await self._connected_message_for(ctx.author.id))
            partner_channel = self.bot.get_channel(match["channel_id"])
            if partner_channel:
                try:
                    await partner_channel.send(await self._connected_message_for(match["user_id"]))
                except discord.HTTPException:
                    pass
            await asyncio.gather(
                self._notify_anon_mode_start(ctx.channel, ctx.author.id),
                self._notify_anon_mode_start(partner_channel, match["user_id"]) if partner_channel else asyncio.sleep(0),
            )
            self._reset_inactivity(conn_id, ctx.channel.id, match["channel_id"])
        else:
            current = await self.db.get_connection(ctx.channel.id)
            if current:
                self._cache_connection(current)
                await ctx.send("☎️ This channel was already connected by another call request.")
                return
            await self.db.add_to_queue(
                channel_id=ctx.channel.id, guild_id=ctx.guild.id,
                user_id=ctx.author.id, webhook_url=wh_url,
            )
            self._start_timeout(ctx.channel.id)
            self._start_queue_nudge(ctx.channel.id, ctx.author.id)
            queue_size = await self.db.get_queue_size()
            await ctx.send(
                f"📳 **Searching for someone to talk to...** ({queue_size} waiting)\n"
                f"Use `f.skip` again to re-roll. Auto-cancels in {config.QUEUE_TIMEOUT} min."
            )

    # ── f.status ──────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="status", aliases=["pbstatus"])
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current Fliphone status for this channel."""
        conn = await self._get_connection_cached(ctx.channel.id)
        if conn:
            is_a      = ctx.channel.id == conn["channel_a"]
            other_gid = conn["guild_b"] if is_a else conn["guild_a"]
            other_g   = self.bot.get_guild(other_gid)
            embed = discord.Embed(title="📞 In a Call", color=config.COLOR_OK)
            embed.add_field(name="Duration",  value=_duration_str(conn["started_at"]), inline=True)
            embed.add_field(name="Messages",  value=str(conn["msg_count"]),            inline=True)
            embed.add_field(name="Call ID",   value=f"#{conn['id']}",                 inline=True)
            if other_g:
                embed.add_field(name="Connected To", value=other_g.name, inline=False)
            embed.set_footer(text=config.FOOTER)
            await ctx.send(embed=embed)
            return

        q = await self.db.get_queue_entry(ctx.channel.id)
        if q:
            queue_size = await self.db.get_queue_size()
            embed = discord.Embed(
                title="⏳ Waiting in Queue",
                description=f"**Wait time:** {_duration_str(q['joined_at'])}\n**Queue size:** {queue_size}",
                color=config.COLOR_WAIT,
            )
            embed.set_footer(text=config.FOOTER)
            await ctx.send(embed=embed)
            return

        embed = discord.Embed(title="📴 Idle", description="Not connected. Use `f.call` to connect!", color=config.COLOR_WAIT)
        active_calls, queue_size, total_calls = await asyncio.gather(
            self.db.get_active_connection_count(),
            self.db.get_queue_size(),
            self.db.get_total_calls(),
        )
        embed.add_field(name="Active Calls",   value=str(active_calls), inline=True)
        embed.add_field(name="In Queue",       value=str(queue_size),   inline=True)
        embed.add_field(name="All-Time Calls", value=str(total_calls),  inline=True)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    # ── f.block ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="block")
    @commands.guild_only()
    async def block(self, ctx: commands.Context, station: Optional[str] = None) -> None:
        """Block the server you're currently connected to."""
        conn = await self._get_connection_cached(ctx.channel.id)
        if not conn:
            room_member = await self.db.get_room_member(ctx.channel.id)
            if not room_member:
                await ctx.send("❌ You can only block a server while in an active call or room.")
                return
            if not station:
                await ctx.send("Specify a station: `f.block <station>`.")
                return
            members = await self.db.get_room_members(room_member["room_id"])
            wanted = station.strip().capitalize()
            target = next((item for item in members if item["station"] == wanted), None)
            if not target or target["channel_id"] == ctx.channel.id:
                await ctx.send("❌ That station is not available in this room.")
                return
            await self.db.block_guild(ctx.guild.id, target["guild_id"], ctx.author.id)
            room_cog = self.bot.get_cog("Room")
            if room_cog:
                await room_cog._remove_member(
                    ctx.channel.id,
                    room_member["room_id"],
                    broadcast_reason=f"📡 **Station {room_member['station']}** left the room.",
                    notify_leaver=False,
                )
            await ctx.send(f"🚫 Station {wanted} was blocked. You have left this room.")
            return

        is_a        = ctx.channel.id == conn["channel_a"]
        other_gid   = conn["guild_b"] if is_a else conn["guild_a"]
        other_cid   = conn["channel_b"] if is_a else conn["channel_a"]
        other_guild = self.bot.get_guild(other_gid)
        other_name  = other_guild.name if other_guild else f"Server {other_gid}"

        await self.db.block_guild(ctx.guild.id, other_gid, ctx.author.id)
        block_conn_id = conn["id"]
        report_cog = self.bot.get_cog("Report")
        if report_cog:
            report_cog.clear_log(conn["id"])
        self._last_calls[conn["guild_a"]] = {
            "other_guild_id": conn["guild_b"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        self._last_calls[conn["guild_b"]] = {
            "other_guild_id": conn["guild_a"],
            "started_at": conn["started_at"],
            "ended_at": datetime.utcnow().isoformat(),
            "active": False,
            "conn_id": conn["id"],
        }
        await self.db.remove_connection(block_conn_id, ended_by=ctx.author.id)
        self._invalidate_connection(conn)
        self._call_reported_gifs.pop(block_conn_id, None)
        self._cancel_inactivity(block_conn_id)
        self._clear_rl_state(conn["channel_a"])
        self._clear_rl_state(conn["channel_b"])
        await ctx.send(f"🚫 **{other_name}** has been blocked & the call has ended.")

        other_ch = self.bot.get_channel(other_cid)
        if other_ch:
            try:
                await other_ch.send("📵 Other server has ended the call!")
            except discord.HTTPException:
                pass

    # ── f.notify ─────────────────────────────────────────────────────────────

    @commands.command(name="notify", aliases=["notifications"])
    async def notify(self, ctx: commands.Context) -> None:
        """Toggle queue call notifications. The bot DMs you when someone is waiting."""
        enabled = await self.db.toggle_notify(ctx.author.id)
        if enabled:
            await ctx.send(
                embed=discord.Embed(
                    title="🔔 Notifications Enabled",
                    description=(
                        "You'll now receive a DM whenever someone enters the queue "
                        "with no one to connect to.\n\n"
                        "Run `f.notify` again to turn it off."
                    ),
                    color=config.COLOR_OK,
                )
            )
        else:
            await ctx.send(
                embed=discord.Embed(
                    title="🔕 Notifications Disabled",
                    description=(
                        "You will no longer receive queue notification DMs.\n\n"
                        "Run `f.notify` to turn them back on."
                    ),
                    color=config.COLOR_WARN,
                )
            )

    @commands.command(name="profile", aliases=["settings", "me"])
    async def profile(self, ctx: commands.Context) -> None:
        """Show your Fliphone user settings and current server/channel state."""
        guild_id = ctx.guild.id if ctx.guild else None
        notify_enabled, is_banned, chat_stats = await asyncio.gather(
            self.db.get_notify_status(ctx.author.id),
            self.db.is_user_banned(ctx.author.id),
            self.db.get_chat_profile(ctx.author.id, guild_id),
        )

        view, banner_file = await self._profile_components(
            ctx.author,
            chat_stats,
            notify_enabled,
            is_banned,
            show_server_rank=ctx.guild is not None,
        )
        if banner_file:
            await ctx.send(view=view, file=banner_file)
        else:
            await ctx.send(view=view)

    async def _profile_banner_path(self, user_id: int) -> Optional[Path]:
        banners = _profile_banner_files()
        if not banners:
            return None
        saved_index = await self.db.get_profile_banner(user_id)
        index = saved_index if saved_index is not None else _fallback_banner_index(user_id, len(banners))
        if index < 0 or index >= len(banners):
            index = _fallback_banner_index(user_id, len(banners))
        return banners[index]

    async def _attach_profile_banner(self, user_id: int, embed: discord.Embed) -> Optional[discord.File]:
        banner = await self._profile_banner_path(user_id)
        if not banner:
            return None
        filename = f"profile_banner{banner.suffix.lower()}"
        embed.set_image(url=f"attachment://{filename}")
        return discord.File(banner, filename=filename)

    async def _profile_components(
        self,
        member: discord.Member | discord.User,
        chat_stats: dict,
        notify_enabled: bool,
        is_banned: bool,
        show_server_rank: bool = False,
    ) -> tuple[discord.ui.LayoutView, Optional[discord.File]]:
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(
            accent_colour=config.COLOR_ERR if is_banned else config.COLOR_WAIT
        )

        banner_file = None
        banner = await self._profile_banner_path(member.id)
        if banner:
            banner_file = discord.File(banner, filename=f"profile_banner{banner.suffix.lower()}")
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=banner_file, description="Profile banner")
            container.add_item(gallery)

        level, _, _ = _level_from_xp(int(chat_stats["xp"]))
        rank_text = _rank_label(chat_stats.get("global_rank"))
        stat_lines = [
            f"**Level {level}**",
            f"XP Gained: **{int(chat_stats['xp']):,}**",
            f"Global Rank: **{rank_text}**",
        ]
        if show_server_rank:
            stat_lines.append(f"Server Rank: **{_rank_label(chat_stats.get('server_rank'))}**")
        stat_lines.append(f"Chats Sent: **{int(chat_stats['message_count']):,}**")
        stat_lines.append(f"Notify: **{'On' if notify_enabled else 'Off'}**")

        display_name = _relay_display_name(member)
        container.add_item(discord.ui.TextDisplay(f"# {display_name}"))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.Section(
                "\n".join(stat_lines)
                + "\n"
                "-# `f.banner` rerolls your banner",
                accessory=discord.ui.Thumbnail(_get_avatar_url(member), description=f"{display_name}'s avatar"),
            )
        )
        view.add_item(container)
        return view, banner_file

    def _user_leaderboard_embed(self, rows: list[dict], *, title: str) -> discord.Embed:
        embed = discord.Embed(title=title, color=config.COLOR_WAIT)
        if not rows:
            embed.description = "No chat XP yet. Start talking in Fliphone calls to rank up."
            return embed

        lines = []
        for index, row in enumerate(rows, start=1):
            level, _, _ = _level_from_xp(int(row["xp"]))
            lines.append(
                f"**#{index}** <@{int(row['user_id'])}> "
                f"• Level **{level}** "
                f"• **{int(row['xp']):,} XP** "
                f"• {int(row['message_count']):,} chats"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text="XP is earned from real relayed call messages.")
        return embed

    def _server_leaderboard_embed(self, rows: list[dict]) -> discord.Embed:
        embed = discord.Embed(title="Fliphone Server Leaderboard", color=config.COLOR_WAIT)
        if not rows:
            embed.description = "No server XP yet. Servers earn XP when members chat in Fliphone calls."
            return embed

        lines = []
        for index, row in enumerate(rows, start=1):
            xp = int(row["xp"] or 0)
            level, _, _ = _level_from_xp(xp)
            guild = self.bot.get_guild(int(row["guild_id"]))
            raw_name = guild.name if guild else "Unnamed Server"
            filtered_name, _ = filter_message(raw_name)
            name = re.sub(r"[\s\-_|:•]+", " ", filtered_name.replace("[censored]", " ")).strip()
            name = name or "Unnamed Server"
            lines.append(
                f"**#{index}** {discord.utils.escape_markdown(name)} "
                f"• Level **{level}** "
                f"• **{xp:,} XP** "
                f"• {int(row['message_count'] or 0):,} chats"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text="Server XP comes from relayed Fliphone chat activity.")
        return embed

    @commands.command(name="leaderboard", aliases=["lb", "levels", "rankings"])
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Show the global Fliphone chat XP leaderboard."""
        rows = await self.db.get_chat_leaderboard(limit=10)
        await ctx.send(embed=self._user_leaderboard_embed(rows, title="Fliphone User Leaderboard"))

    @commands.command(name="serverlb", aliases=["slb", "serverleaderboard"])
    async def serverlb(self, ctx: commands.Context) -> None:
        """Show the global Fliphone server XP leaderboard."""
        rows = await self.db.get_server_leaderboard(limit=10)
        await ctx.send(embed=self._server_leaderboard_embed(rows))

    @commands.command(name="banner", aliases=["profilebanner"])
    async def banner(self, ctx: commands.Context, action: Optional[str] = None) -> None:
        """Reroll or reset your Fliphone profile banner."""
        banners = _profile_banner_files()
        if not banners:
            await ctx.send("❌ No profile banners are available yet.")
            return

        if action and action.lower() in {"reset", "default"}:
            await self.db.reset_profile_banner(ctx.author.id)
            embed = discord.Embed(
                title="Profile Banner Reset",
                description="Your profile banner is back to your stable default.",
                color=config.COLOR_OK,
            )
            banner_file = await self._attach_profile_banner(ctx.author.id, embed)
            embed.set_footer(text=config.FOOTER)
            if banner_file:
                await ctx.send(embed=embed, file=banner_file)
            else:
                await ctx.send(embed=embed)
            return

        if action:
            await ctx.send("Use `f.banner` to reroll, or `f.banner reset` to return to your default.")
            return

        current = await self.db.get_profile_banner(ctx.author.id)
        if current is None:
            current = _fallback_banner_index(ctx.author.id, len(banners))
        choices = [idx for idx in range(len(banners)) if idx != current]
        new_index = random.choice(choices) if choices else current
        await self.db.set_profile_banner(ctx.author.id, new_index)

        embed = discord.Embed(
            title="Profile Banner Updated",
            description="Your new banner has been saved.",
            color=config.COLOR_OK,
        )
        banner_file = await self._attach_profile_banner(ctx.author.id, embed)
        embed.set_footer(text="Run f.profile to see your full profile.")
        if banner_file:
            await ctx.send(embed=embed, file=banner_file)
        else:
            await ctx.send(embed=embed)

    async def _fire_notify(self, caller_id: int) -> None:
        """DM all opted-in subscribers that someone is waiting in the queue."""
        if caller_id in config.NOTIFY_IGNORE_IDS:
            return
        subscribers = await self.db.get_notify_subscribers()
        for uid in subscribers:
            if uid == caller_id:
                continue
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    await user.send(
                        embed=discord.Embed(
                            title="📞 Someone is waiting for a call!",
                            description=(
                                "A server just joined the Fliphone queue with nobody to connect to.\n\n"
                                "Head to your phonebooth channel and run `f.call` to connect!"
                            ),
                            color=config.COLOR_WAIT,
                        )
                    )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ── f.anon ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="anon", aliases=["mask", "anonymous"])
    @commands.guild_only()
    async def anon(self, ctx: commands.Context) -> None:
        """Toggle your personal tarot anon identity."""
        # Check this is a configured phonebooth channel
        cfg, guild_cfg = await asyncio.gather(
            self._get_config_by_channel_cached(ctx.channel.id),
            self._get_guild_config_cached(ctx.guild.id),
        )
        if not cfg and not guild_cfg:
            await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` first.")
            return
        is_anon = await self.db.toggle_user_anonymous(ctx.author.id)
        self._user_policy_cache.pop(ctx.author.id, None)
        if is_anon:
            await ctx.send("🎭 **Anon mode ON** — your relayed messages will use a stable tarot identity in each conversation.")
        else:
            await ctx.send("👤 **Anon mode OFF** — your relayed messages will show your filtered display name and avatar.")

    # ── f.fr / f.friendrequest ────────────────────────────────────────────────

    @commands.hybrid_command(name="friendrequest", aliases=["fr"])
    @commands.guild_only()
    async def friendrequest(self, ctx: commands.Context, station: Optional[str] = None) -> None:
        """Share your Discord username with the person you're talking to."""
        conn = await self._get_connection_cached(ctx.channel.id)
        member    = ctx.author

        def _fr_embed() -> discord.Embed:
            embed = discord.Embed(
                title="👋 Friend Request",
                description=(
                    "⚠️ **Stay safe online!**\n"
                    "We cannot moderate users outside this bot. Accept friend requests at your own risk.\n"
                    "Never share passwords or personal info & avoid clicking suspicious links.\n"
                    "Report any misconduct to Discord immediately & remember to stay safe."
                ),
                color=0x5865F2,
            )
            embed.add_field(name="Username",     value=f"`{member.name}`",  inline=True)
            embed.set_thumbnail(url=_get_avatar_url(member))
            embed.set_footer(text="Copy the username above to send a friend request.")
            return embed

        target_channels: list[int] = []
        if conn:
            is_a = ctx.channel.id == conn["channel_a"]
            target_channels.append(conn["channel_b"] if is_a else conn["channel_a"])
        else:
            room_member = await self.db.get_room_member(ctx.channel.id)
            if not room_member:
                await ctx.send("❌ You can only share friend-request info during an active call or room.")
                return
            room_members = await self.db.get_room_members(room_member["room_id"])
            if station:
                wanted = station.strip().capitalize()
                target = next((item for item in room_members if item["station"] == wanted), None)
                if not target or target["channel_id"] == ctx.channel.id:
                    await ctx.send("❌ That station is not available in this room.")
                    return
                target_channels.append(target["channel_id"])
            else:
                target_channels.extend(
                    item["channel_id"] for item in room_members
                    if item["channel_id"] != ctx.channel.id
                )

        await ctx.send(embed=_fr_embed())
        for channel_id in target_channels:
            other_ch = self.bot.get_channel(channel_id)
            if other_ch:
                try:
                    await other_ch.send(embed=_fr_embed())
                except discord.HTTPException:
                    pass


async def setup(bot):
    await bot.add_cog(Phonebooth(bot))
