"""
cogs/phonebooth.py – Core Phonebooth logic.

Commands
--------
f.call / f.c      – Join queue or connect instantly
f.hangup / f.h    – End call or leave queue
f.skip / f.s      – Hang up and immediately redial
f.status          – Show current status
f.block           – Block the server you're talking to
f.anon / f.mask   – Toggle anonymous mode for YOUR server
f.fr              – Share your username as a friend request card
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from io import BytesIO
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageOps

import config
from database import Database
from filter import filter_message

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
    name = f"Stranger {rng.choice(config.ANON_NAMES)}"
    avatar = f"https://robohash.org/{seed}?set=set4&size=256x256"
    return name, avatar


def _get_avatar_url(member: discord.Member | discord.User) -> str:
    # Guild-specific avatars are not reliable when Discord fetches them for a
    # webhook in another server. Global profile avatars are cross-server assets.
    asset = member.avatar or member.default_avatar
    try:
        return str(asset.with_static_format("png").with_size(256).url)
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
    return sorted(
        path
        for path in PROFILE_BANNER_DIR.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    )


def _fallback_banner_index(user_id: int, banner_count: int) -> int:
    return int(user_id) % banner_count if banner_count else 0


def _profile_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _truncate_to_width(text: str, font: ImageFont.ImageFont, width: int) -> str:
    if not text:
        return ""
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "..."
    usable = max(0, width - int(draw.textlength(suffix, font=font)))
    result = ""
    for char in text:
        if draw.textlength(result + char, font=font) > usable:
            break
        result += char
    return f"{result.rstrip()}{suffix}"


def _rounded_image(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, image.width, image.height), radius=radius, fill=255)
    rounded = Image.new("RGBA", image.size)
    rounded.paste(image.convert("RGBA"), (0, 0), mask)
    return rounded


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
    Persistent button under every relayed GIF.
    report_id is stored in the embed footer so it survives bot restarts.
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

        # ── Parse report_id from footer ───────────────────────────────────────
        report_id = None
        if interaction.message and interaction.message.embeds:
            footer = interaction.message.embeds[0].footer
            if footer and footer.text:
                # Footer format: "Report #42 • Fliphone"
                try:
                    report_id = int(footer.text.split("Report #")[1].split("•")[0].strip())
                except (IndexError, ValueError):
                    pass

        if not report_id:
            await interaction.response.send_message(
                "❌ Couldn't read report ID. This button may be too old.", ephemeral=True
            )
            return

        # ── Fetch report from DB ──────────────────────────────────────────────
        report = await db.get_gif_report(report_id)
        if not report:
            await interaction.response.send_message(
                "❌ Report not found in database.", ephemeral=True
            )
            return

        # ── Already reported / reviewed? ──────────────────────────────────────
        if report["status"] == "reported":
            await interaction.response.send_message(
                "⚠️ This GIF has already been reported and is pending review.", ephemeral=True
            )
            return
        if report["status"] in ("blacklisted", "whitelisted"):
            await interaction.response.send_message(
                "✅ This GIF has already been reviewed by the bot owner.", ephemeral=True
            )
            return

        # ── Whitelisted? ──────────────────────────────────────────────────────
        url_status = await db.check_gif_url(report["url"])
        if url_status == "whitelist":
            await interaction.response.send_message(
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
            try:
                ch = interaction.client.get_channel(report["channel_id"])
                if ch:
                    msg = await ch.fetch_message(report["msg_id"])
                    await msg.delete()
                    deleted = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        # ── Update the report card button ─────────────────────────────────────
        button.disabled = True
        button.label = "✅ Reported"
        new_embed = discord.Embed(
            title="✅ GIF Reported — Removed",
            description=(
                "This GIF has been removed and flagged for review.\n"
                "The bot owner will blacklist or whitelist it."
            ),
            color=0x57F287,
        )
        new_embed.set_footer(text=f"Report #{report_id} • {config.FOOTER}")
        await interaction.response.edit_message(embed=new_embed, view=self)

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
                    await log_ch.send(embed=log_embed, view=GifReportLogView())
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            "✅ GIF removed and flagged for review. Thanks!", ephemeral=True
        )

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
    "To report a user, click on the message and click apps then click "
    "Report Message or reply to the message and do `f.block`\n\n"
    "*By continuing, you agree to be respectful. "
    "To opt out, ask an admin to run `f.setup` in the channel to unconfigure it.*"
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
        # Rate limiting: channel_id -> deque of monotonic send timestamps
        # Lax: 10 messages per 15 s; after 3 warnings the message is silently dropped
        self._rl_times: dict[int, deque] = {}
        self._rl_warns: dict[int, int]   = {}
        # guild_id -> last call info, used by report.py to identify partner after hangup/skip
        self._last_calls: dict[int, dict] = {}
        self._conn_by_channel: dict[int, dict] = {}
        self._cfg_by_channel: dict[int, dict] = {}
        self._cfg_by_guild: dict[int, dict] = {}
        self._session: aiohttp.ClientSession | None = None
        self._wh_obj_cache: dict[str, discord.Webhook] = {}
        self._ending_broken_connections: set[int] = set()
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
        self._conn_by_channel[int(conn["channel_a"])] = conn
        self._conn_by_channel[int(conn["channel_b"])] = conn

    def _invalidate_connection(self, conn: Optional[dict]) -> None:
        if not conn:
            return
        self._conn_by_channel.pop(int(conn["channel_a"]), None)
        self._conn_by_channel.pop(int(conn["channel_b"]), None)

    async def _get_connection_cached(self, channel_id: int) -> Optional[dict]:
        conn = self._conn_by_channel.get(int(channel_id))
        if conn:
            return conn
        conn = await self.db.get_connection(channel_id)
        self._cache_connection(conn)
        return conn

    async def _check_gif_admin(self, ctx: commands.Context) -> bool:
        """Allow only server owner or administrators to manage GIF mode."""
        if not ctx.guild:
            return False
        if ctx.author.id == ctx.guild.owner_id:
            return True
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("❌ Only server administrators or the server owner can use `f.gifmode`.")
        return False

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
            await self._recover_queue_entries()
            await self._recover_active_connections()
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
        for conn in await self.db.get_active_connections():
            conn_id = int(conn["id"])
            last_activity = conn.get("last_activity_at") or conn.get("started_at")
            elapsed = _elapsed_seconds(last_activity)
            if elapsed >= timeout_seconds:
                await self._inactivity_timer(
                    conn_id,
                    int(conn["channel_a"]),
                    int(conn["channel_b"]),
                    0,
                )
                continue

            self._cache_connection(conn)
            self._reset_inactivity(
                conn_id,
                int(conn["channel_a"]),
                int(conn["channel_b"]),
                timeout_seconds - elapsed,
            )

    # ── Rate limiting (1:1 calls) ─────────────────────────────────────────────
    # Lax settings: 10 messages per 15-second rolling window.
    # 3 warnings are shown before messages start being silently dropped.

    _RL_MSGS   = 10
    _RL_WINDOW = 15.0
    _RL_WARN_MAX = 3

    def _call_is_rate_limited(self, channel_id: int) -> bool:
        """Return True if this channel is sending too fast."""
        now = time.monotonic()
        dq  = self._rl_times.setdefault(channel_id, deque())
        dq.append(now)
        cutoff = now - self._RL_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) > self._RL_MSGS

    def _clear_rl_state(self, channel_id: int) -> None:
        self._rl_times.pop(channel_id, None)
        self._rl_warns.pop(channel_id, None)

    @tasks.loop(minutes=30)
    async def _cleanup_loop(self) -> None:
        pass

    # ── Webhook helpers ───────────────────────────────────────────────────────

    async def get_or_create_webhook(self, channel: discord.TextChannel) -> Optional[str]:
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user == self.bot.user and wh.name == "Fliphone":
                    return wh.url
            wh = await channel.create_webhook(name="Fliphone")
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
        )
        return [name for name, allowed in required if not allowed]

    async def ensure_relay_webhook(
        self,
        channel: discord.TextChannel,
    ) -> tuple[Optional[str], list[str]]:
        """Validate required permissions and refresh the channel's stored webhook URL."""
        issues = self.relay_permission_issues(channel)
        if issues:
            return None, issues

        webhook_url = await self.get_or_create_webhook(channel)
        if not webhook_url:
            return None, ["Webhook access failed"]

        await self.db.update_webhook(channel.id, webhook_url)
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

        await self.db.update_webhook(channel.id, webhook_url)
        cfg = self._cfg_by_channel.get(channel.id)
        if cfg is not None:
            cfg["webhook_url"] = webhook_url
        return webhook_url, []

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
                "A server admin should run `f.setup` in the configured channel."
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
            msg = await wh.send(
                content=content or None,
                username=username[:80],
                avatar_url=avatar_url,
                embeds=[reply_embed] if reply_embed else discord.utils.MISSING,
                files=files if files else discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=silent,
                wait=wait,
            )
            return msg if wait else True
        except Exception as exc:
            print(f"[relay-webhook] {exc}")
            # Evict cached webhook on error so it gets rebuilt next send
            self._wh_obj_cache.pop(url, None)
            return None if wait else False

    async def _repair_relay_webhook(
        self,
        channel: discord.abc.GuildChannel,
        conn: dict,
    ) -> Optional[str]:
        if not isinstance(channel, discord.TextChannel):
            return None
        webhook_url, _ = await self.ensure_relay_webhook(channel)
        if not webhook_url:
            return None

        await self.db.update_connection_webhook(channel.id, webhook_url)
        if channel.id == conn["channel_a"]:
            conn["webhook_a"] = webhook_url
        elif channel.id == conn["channel_b"]:
            conn["webhook_b"] = webhook_url
        self._cache_connection(conn)
        return webhook_url

    async def _send_gifmode_connect_notices(
        self,
        channel_a: discord.abc.Messageable,
        guild_a_id: int,
        channel_b: Optional[discord.abc.Messageable],
        guild_b_id: int,
    ) -> None:
        """Send GIF mode notices to both sides right after a call connects."""
        mode_a = (await self.db.get_gif_mode(guild_a_id)).lower()
        mode_b = (await self.db.get_gif_mode(guild_b_id)).lower()

        async def _send(ch, text: str) -> None:
            if not ch:
                return
            try:
                await ch.send(text, delete_after=15)
            except discord.HTTPException:
                pass

        if mode_a == "disabled":
            await _send(
                channel_a,
                "🚫 Your server has GIFs disabled — you won't be able to send or receive GIFs in this call.",
            )
            await _send(
                channel_b,
                "🎭 The other server has GIFs disabled. They may not see everything you send.",
            )
        elif mode_a == "limited":
            await _send(
                channel_a,
                "⚠️ Your server has GIFs limited — only Tenor, Giphy, and Klipy links will be shown.",
            )
            await _send(
                channel_b,
                "🎭 The other server has GIFs limited. They may not see everything you send.",
            )

        if mode_b == "disabled":
            await _send(
                channel_b,
                "🚫 Your server has GIFs disabled — you won't be able to send or receive GIFs in this call.",
            )
            await _send(
                channel_a,
                "🎭 The other server has GIFs disabled. They may not see everything you send.",
            )
        elif mode_b == "limited":
            await _send(
                channel_b,
                "⚠️ Your server has GIFs limited — only Tenor, Giphy, and Klipy links will be shown.",
            )
            await _send(
                channel_a,
                "🎭 The other server has GIFs limited. They may not see everything you send.",
            )

    # ── Message relay ─────────────────────────────────────────────────────────

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
            )
            report_embed = discord.Embed(
                title="🚩 GIF Safety Check",
                description=(
                    "Report this GIF if it breaks the rules.\n"
                    "Safe/approved GIFs cannot be reported."
                ),
                color=0x2b2d31,
            )
            report_embed.set_footer(text=f"Report #{report_id} • {config.FOOTER}")
            await send_channel.send(embed=report_embed, view=GifReportView())

        # ── Rate limiting (sync — no DB needed) ──────────────────────────────
        if self._call_is_rate_limited(message.channel.id):
            warn = self._rl_warns.get(message.channel.id, 0) + 1
            self._rl_warns[message.channel.id] = warn
            if warn <= self._RL_WARN_MAX:
                try:
                    await message.channel.send(
                        f"⚠️ {message.author.mention} You're sending messages too fast — "
                        f"slow down a little! (Warning **{warn}/{self._RL_WARN_MAX}**)",
                        delete_after=6,
                    )
                except discord.HTTPException:
                    pass
            return

        # ── Ban check + config fetch in parallel ──────────────────────────────
        is_banned, cfg = await asyncio.gather(
            self.db.is_user_banned(message.author.id),
            self._get_config_by_channel_cached(message.channel.id),
        )
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
        anon = cfg.get("anonymous", 0) if cfg else 0

        if anon:
            seed = conn["id"] * 1000 + (conn["guild_a"] if is_side_a else conn["guild_b"])
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
                ref_text   = (ref_msg.content or "").strip()
                ref_text   = " ".join(
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
                ref_text = CUSTOM_EMOJI_PATTERN.sub("", ref_text).strip()
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

        content = CUSTOM_EMOJI_PATTERN.sub("", content).strip()
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
            if not GIF_LINK_PATTERN.match(url)
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
        inline_gif_urls: list[str] = GIF_LINK_PATTERN.findall(content)

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

        if blocked_attachment_count:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Only text and GIFs are allowed in calls.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

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

        safe_urls = list(all_gif_urls)
        if all_gif_urls:
            sender_gid = conn["guild_a"] if is_side_a else conn["guild_b"]
            sender_gif_mode, receiver_gif_mode = await self.db.get_gif_modes_bulk(sender_gid, target_gid)
            if sender_gif_mode == "disabled" or receiver_gif_mode == "disabled":
                safe_urls = []
            elif sender_gif_mode == "limited" or receiver_gif_mode == "limited":
                safe_urls = [
                    u for u in safe_urls
                    if "tenor.com" in u.lower() or "giphy.com" in u.lower() or "klipy.com" in u.lower()
                ]

        for gif_url in [u for u in all_gif_urls if u not in safe_urls]:
            content = content.replace(gif_url, "")
        content = content.strip()
        all_gif_urls = safe_urls

        # ── Blacklist + per-call reported check ──────────────────────────────
        def _norm(u: str) -> str:
            return u.split("?")[0].rstrip("/").lower()

        call_reported = self._call_reported_gifs.get(conn["id"], set())
        blocked_urls: set[str] = set()
        if all_gif_urls:
            gif_statuses = await asyncio.gather(
                *[self.db.check_gif_url(u) for u in all_gif_urls],
                return_exceptions=True,
            )
            for gif_url, status in zip(all_gif_urls, gif_statuses):
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
        reportable_gif_urls = [u for u in all_gif_urls if u not in blocked_urls]

        # Keep GIF replies in one webhook message so reply context stays attached.
        if reply_embed and reportable_gif_urls:
            text_content = "\n".join(part for part in (reply_context, content.strip()) if part)
            reply_embed = None
        else:
            text_content = content.strip() or None

        # ── Check if message is now empty ───────────────────────────────────
        if not text_content and not files:
            if not blocked_attachment_count:
                try:
                    await message.channel.send(
                        f"⚠️ {message.author.mention} Your message was not sent because it contained no relayable content.",
                        delete_after=8,
                    )
                except Exception:
                    pass
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
            )
        need_id = bool(reportable_gif_urls)
        target_channel = self.bot.get_channel(target_cid)

        if not target_wh and target_channel:
            target_wh = await self._repair_relay_webhook(target_channel, conn)

        if target_wh:
            main_wh_msg = await self._send_webhook(
                target_wh, text_content, display_name, avatar_url, files,
                reply_embed=reply_embed, wait=need_id, silent=bool(reportable_gif_urls),
            )
            if not main_wh_msg and target_channel:
                repaired_wh = await self._repair_relay_webhook(target_channel, conn)
                if repaired_wh:
                    target_wh = repaired_wh
                    main_wh_msg = await self._send_webhook(
                        target_wh, text_content, display_name, avatar_url, files,
                        reply_embed=reply_embed, wait=need_id, silent=bool(reportable_gif_urls),
                    )
            if main_wh_msg:
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

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        conn = await self._get_connection_cached(message.channel.id)
        if not conn:
            return
        await self._relay(message, conn)

    # ── f.call ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="call", aliases=["c", "dial", "connect"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def call(self, ctx: commands.Context) -> None:
        """Dial into the queue, or connect instantly."""
        is_banned, cfg, conn, room_member, q = await asyncio.gather(
            self.db.is_user_banned(ctx.author.id),
            self._get_config_by_channel_cached(ctx.channel.id),
            self._get_connection_cached(ctx.channel.id),
            self.db.get_room_member(ctx.channel.id),
            self.db.get_queue_entry(ctx.channel.id),
        )

        if is_banned:
            await ctx.send("🚫 You are banned from using Fliphone.")
            return

        if not cfg:
            guild_cfg = await self._get_guild_config_cached(ctx.guild.id)
            if guild_cfg:
                pb_ch = self.bot.get_channel(guild_cfg["channel_id"])
                await ctx.send(f"❌ Use the Fliphone channel: {pb_ch.mention if pb_ch else '#deleted-channel'}")
            else:
                await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` in the target channel.")
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
        match = await self._get_valid_queue_match(ctx.guild.id, ctx.channel.id)

        if match:
            self._cancel_timeout(match["channel_id"])
            self._cancel_queue_nudge(match["channel_id"])
            await self.db.remove_from_queue(match["channel_id"])
            started_at = datetime.utcnow().isoformat()
            conn_id = await self.db.create_connection(
                channel_a=ctx.channel.id, guild_a=ctx.guild.id, webhook_a=wh_url,
                channel_b=match["channel_id"], guild_b=match["guild_id"], webhook_b=match["webhook_url"],
                started_at=started_at,
            )
            new_conn = {
                "id": conn_id,
                "channel_a": ctx.channel.id,
                "guild_a": ctx.guild.id,
                "webhook_a": wh_url,
                "channel_b": match["channel_id"],
                "guild_b": match["guild_id"],
                "webhook_b": match["webhook_url"],
                "started_at": started_at,
                "msg_count": 0,
            }
            self._cache_connection(new_conn)
            await ctx.send(_CONNECTED_MSG)
            partner_channel = self.bot.get_channel(match["channel_id"])
            if partner_channel:
                try:
                    await partner_channel.send(_CONNECTED_MSG)
                except discord.HTTPException:
                    pass
            await self._send_gifmode_connect_notices(
                channel_a=ctx.channel,
                guild_a_id=ctx.guild.id,
                channel_b=partner_channel,
                guild_b_id=match["guild_id"],
            )
            # Start inactivity timer for this call
            self._reset_inactivity(conn_id, ctx.channel.id, match["channel_id"])
            # Anon mode notifications
            caller_cfg  = cfg
            partner_cfg = await self._get_config_by_channel_cached(match["channel_id"])
            caller_anon  = caller_cfg.get("anonymous", 0) if caller_cfg else 0
            partner_anon = partner_cfg.get("anonymous", 0) if partner_cfg else 0
            if caller_anon:
                await ctx.send("🎭 Anonymous mode is enabled — the other server sees you as a Stranger.")
            if partner_anon and partner_channel:
                try:
                    await partner_channel.send("🎭 Anonymous mode is enabled — the other server sees you as a Stranger.")
                except discord.HTTPException:
                    pass
            if partner_anon:
                await ctx.send("🎭 The other server has anonymous mode enabled — you will see them as a Stranger.")
            if caller_anon and partner_channel:
                try:
                    await partner_channel.send("🎭 The other server has anonymous mode enabled — you will see them as a Stranger.")
                except discord.HTTPException:
                    pass
        else:
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
                f"Estimated wait: **instant if someone dials, otherwise up to {config.QUEUE_TIMEOUT} min**.\n"
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
        cfg = await self._get_config_by_channel_cached(ctx.channel.id)
        if not cfg:
            await ctx.send("❌ This isn't a Fliphone channel.")
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
        match = await self._get_valid_queue_match(ctx.guild.id, ctx.channel.id)

        if match:
            self._cancel_timeout(match["channel_id"])
            self._cancel_queue_nudge(match["channel_id"])
            await self.db.remove_from_queue(match["channel_id"])
            started_at = datetime.utcnow().isoformat()
            conn_id = await self.db.create_connection(
                channel_a=ctx.channel.id, guild_a=ctx.guild.id, webhook_a=wh_url,
                channel_b=match["channel_id"], guild_b=match["guild_id"], webhook_b=match["webhook_url"],
                started_at=started_at,
            )
            new_conn = {
                "id": conn_id,
                "channel_a": ctx.channel.id,
                "guild_a": ctx.guild.id,
                "webhook_a": wh_url,
                "channel_b": match["channel_id"],
                "guild_b": match["guild_id"],
                "webhook_b": match["webhook_url"],
                "started_at": started_at,
                "msg_count": 0,
            }
            self._cache_connection(new_conn)
            await ctx.send(_CONNECTED_MSG)
            partner_channel = self.bot.get_channel(match["channel_id"])
            if partner_channel:
                try:
                    await partner_channel.send(_CONNECTED_MSG)
                except discord.HTTPException:
                    pass
            await self._send_gifmode_connect_notices(
                channel_a=ctx.channel,
                guild_a_id=ctx.guild.id,
                channel_b=partner_channel,
                guild_b_id=match["guild_id"],
            )
            self._reset_inactivity(conn_id, ctx.channel.id, match["channel_id"])
        else:
            await self.db.add_to_queue(
                channel_id=ctx.channel.id, guild_id=ctx.guild.id,
                user_id=ctx.author.id, webhook_url=wh_url,
            )
            self._start_timeout(ctx.channel.id)
            self._start_queue_nudge(ctx.channel.id, ctx.author.id)
            queue_size = await self.db.get_queue_size()
            await ctx.send(
                f"📳 **Searching for someone to talk to...** ({queue_size} waiting)\n"
                f"Estimated wait: **instant if someone dials, otherwise up to {config.QUEUE_TIMEOUT} min**.\n"
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
    async def block(self, ctx: commands.Context) -> None:
        """Block the server you're currently connected to."""
        conn = await self._get_connection_cached(ctx.channel.id)
        if not conn:
            await ctx.send("❌ You can only block a server while in an active call.")
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
        notify_enabled, is_banned = await asyncio.gather(
            self.db.get_notify_status(ctx.author.id),
            self.db.is_user_banned(ctx.author.id),
        )

        settings_lines = [
            f"Notifications: {'On' if notify_enabled else 'Off'}",
            f"Access: {'Banned' if is_banned else 'OK'}",
        ]
        server_lines = ["Setup: Not configured", "GIF mode: -", "Anonymous: -"]
        status_text = "Use f.profile in a server to show channel status."
        embed = discord.Embed(
            color=config.COLOR_ERR if is_banned else config.COLOR_WAIT,
            timestamp=datetime.utcnow(),
        )

        if ctx.guild:
            guild_cfg, conn, q, room_member = await asyncio.gather(
                self._get_guild_config_cached(ctx.guild.id),
                self._get_connection_cached(ctx.channel.id),
                self.db.get_queue_entry(ctx.channel.id),
                self.db.get_room_member(ctx.channel.id),
            )
            if guild_cfg:
                channel = self.bot.get_channel(guild_cfg["channel_id"])
                current_mode = await self.db.get_gif_mode(ctx.guild.id)
                setup_text = f"Healthy ({channel.mention})" if channel else "Channel missing"
                server_lines = [
                    f"Setup: {setup_text}",
                    f"GIF mode: {current_mode}",
                    f"Anonymous: {'On' if guild_cfg.get('anonymous') else 'Off'}",
                ]

            if conn:
                status_text = f"In a 1:1 call for {_duration_str(conn['started_at'])}"
            elif room_member:
                status_text = f"In room #{room_member['room_id']} as Station {room_member['station']}"
            elif q:
                status_text = f"Waiting in queue for {_duration_str(q['joined_at'])}"
            else:
                status_text = "Idle"

        banner = await self._profile_banner_path(ctx.author.id)
        avatar_bytes = await self._fetch_profile_avatar(ctx.author)
        card = await asyncio.to_thread(
            self._render_profile_card,
            ctx.author,
            banner,
            avatar_bytes,
            settings_lines,
            server_lines,
            status_text,
            is_banned,
        )
        profile_file = discord.File(BytesIO(card), filename="fliphone_profile.png")
        embed.set_image(url="attachment://fliphone_profile.png")
        embed.set_footer(text=f"Use f.banner to reroll your banner • {config.FOOTER}")
        await ctx.send(embed=embed, file=profile_file)

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

    async def _fetch_profile_avatar(self, member: discord.Member | discord.User) -> Optional[bytes]:
        if not self._session:
            return None
        try:
            async with self._session.get(
                _get_avatar_url(member),
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return None

    def _render_profile_card(
        self,
        member: discord.Member | discord.User,
        banner: Optional[Path],
        avatar_bytes: Optional[bytes],
        settings_lines: list[str],
        server_lines: list[str],
        status_text: str,
        is_banned: bool,
    ) -> bytes:
        width, height = 760, 430
        margin = 14
        banner_h = 150
        bg = (35, 36, 40)
        panel = (43, 45, 49)
        text = (242, 243, 245)
        muted = (188, 191, 198)
        accent = (237, 66, 69) if is_banned else (88, 101, 242)

        card = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(card)
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=12, fill=panel)
        draw.rounded_rectangle((0, 0, 5, height - 1), radius=3, fill=accent)

        if banner and banner.exists():
            with Image.open(banner) as im:
                banner_img = ImageOps.fit(
                    ImageOps.exif_transpose(im).convert("RGB"),
                    (width - margin * 2, banner_h),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        else:
            banner_img = Image.new("RGB", (width - margin * 2, banner_h), (24, 25, 28))
        rounded_banner = _rounded_image(banner_img, 8)
        card.paste(rounded_banner, (margin, margin), rounded_banner)

        name_font = _profile_font(24, bold=True)
        title_font = _profile_font(20, bold=True)
        heading_font = _profile_font(18, bold=True)
        body_font = _profile_font(17)

        avatar_size = 54
        avatar_x = margin + 4
        avatar_y = margin + banner_h + 22
        if avatar_bytes:
            try:
                avatar = Image.open(BytesIO(avatar_bytes))
                avatar = ImageOps.fit(
                    ImageOps.exif_transpose(avatar).convert("RGB"),
                    (avatar_size, avatar_size),
                    method=Image.Resampling.LANCZOS,
                )
            except Exception:
                avatar = None
        else:
            avatar = None
        if avatar is None:
            avatar = Image.new("RGB", (avatar_size, avatar_size), accent)
            avatar_draw = ImageDraw.Draw(avatar)
            initial = (_relay_display_name(member)[:1] or "?").upper()
            bbox = avatar_draw.textbbox((0, 0), initial, font=title_font)
            avatar_draw.text(
                ((avatar_size - (bbox[2] - bbox[0])) / 2, (avatar_size - (bbox[3] - bbox[1])) / 2 - 2),
                initial,
                fill=text,
                font=title_font,
            )
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
        card.paste(avatar, (avatar_x, avatar_y), mask)

        text_x = avatar_x + avatar_size + 14
        draw.text((text_x, avatar_y + 2), _truncate_to_width(_relay_display_name(member), name_font, 520), fill=text, font=name_font)
        draw.text((text_x, avatar_y + 32), "Fliphone Profile", fill=muted, font=title_font)

        left_x = margin + 4
        right_x = 380
        fields_y = avatar_y + avatar_size + 28
        draw.text((left_x, fields_y), "Settings", fill=text, font=heading_font)
        draw.text((right_x, fields_y), "Server", fill=text, font=heading_font)

        line_gap = 23
        for i, line in enumerate(settings_lines[:3]):
            draw.text((left_x, fields_y + 28 + i * line_gap), _truncate_to_width(line, body_font, 320), fill=text, font=body_font)
        for i, line in enumerate(server_lines[:3]):
            draw.text((right_x, fields_y + 28 + i * line_gap), _truncate_to_width(line, body_font, 330), fill=text, font=body_font)

        status_y = fields_y + 92
        draw.text((left_x, status_y), "Status", fill=text, font=heading_font)
        draw.text((left_x, status_y + 28), _truncate_to_width(status_text, body_font, width - margin * 2 - 8), fill=text, font=body_font)

        output = BytesIO()
        card.save(output, "PNG", optimize=True)
        return output.getvalue()

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

    # ── f.anon / f.mask ───────────────────────────────────────────────────────

    @commands.hybrid_command(name="anon", aliases=["mask", "anonymous"])
    @commands.guild_only()
    async def anon(self, ctx: commands.Context) -> None:
        """Toggle anonymous mode for this server. Anyone in the phonebooth channel can use this."""
        # Check this is a configured phonebooth channel
        cfg, guild_cfg = await asyncio.gather(
            self._get_config_by_channel_cached(ctx.channel.id),
            self._get_guild_config_cached(ctx.guild.id),
        )
        if not cfg and not guild_cfg:
            await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` first.")
            return
        is_anon = await self.db.toggle_anonymous(ctx.guild.id)
        self._invalidate_config(guild_id=ctx.guild.id, channel_id=ctx.channel.id)
        if is_anon:
            await ctx.send("🎭 **Anonymous mode ON** — messages from this server will appear as *Stranger [Name]*.")
        else:
            await ctx.send("👤 **Anonymous mode OFF** — messages will show filtered display names and avatars.")

    # ── f.gifmode ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="gifmode")
    @commands.guild_only()
    async def gifmode(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        """Set or view GIF relay mode for this server."""
        if not await self._check_gif_admin(ctx):
            return

        guild_cfg = await self._get_guild_config_cached(ctx.guild.id)
        if not guild_cfg:
            await ctx.send("❌ Fliphone isn't set up. An admin should run `f.setup` first.")
            return

        if mode is None:
            current_mode = await self.db.get_gif_mode(ctx.guild.id)
            await ctx.send(
                "🎞️ GIF mode is currently set to "
                f"**{current_mode}**. Use `f.gifmode <enabled|limited|disabled>` to change it."
            )
            return

        new_mode = mode.lower().strip()
        if new_mode not in {"enabled", "limited", "disabled"}:
            await ctx.send("❌ Invalid mode. Use `enabled`, `limited`, or `disabled`.")
            return

        await self.db.set_gif_mode(ctx.guild.id, new_mode)
        await ctx.send(f"✅ GIF mode updated to **{new_mode}**.")

    # ── f.fr / f.friendrequest ────────────────────────────────────────────────

    @commands.hybrid_command(name="friendrequest", aliases=["fr"])
    @commands.guild_only()
    async def friendrequest(self, ctx: commands.Context) -> None:
        """Share your Discord username with the person you're talking to."""
        conn = await self._get_connection_cached(ctx.channel.id)
        if not conn:
            await ctx.send("❌ You can only share your friend request info during an active call.")
            return

        is_a      = ctx.channel.id == conn["channel_a"]
        other_cid = conn["channel_b"] if is_a else conn["channel_a"]
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

        await ctx.send(embed=_fr_embed())
        other_ch = self.bot.get_channel(other_cid)
        if other_ch:
            try:
                await other_ch.send(embed=_fr_embed())
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Phonebooth(bot))
