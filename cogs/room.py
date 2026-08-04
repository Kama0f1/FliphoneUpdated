"""
cogs/room.py – Multi-server group room feature for Fliphone.

Public commands use Discord slash commands such as /room, /roomleave, and /roomskip.

How rooms work
--------------
Each server in a room is assigned a station name (Alpha–Echo).
Server names are never revealed — only station names appear in join/leave
notices and as webhook prefixes. Rooms become active once 2+ servers have
joined, and new servers can slot in to existing active rooms up to the
max of 5.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

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
# GifReportView lives in phonebooth; import lazily via bot.get_cog to avoid circular imports.

logger = logging.getLogger("fliphone.room")

# ── Constants ─────────────────────────────────────────────────────────────────

# Station names used to identify servers — never their real names.
STATION_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
ROOM_MAX_SIZE = 5
ROOM_INACTIVITY_MINUTES = 10        # no relayed text/GIF → close the room
ROOM_QUEUE_TIMEOUT_MINUTES = 10     # waiting room dissolves if nobody joins

# Rate limiting - dynamic per source channel so busy rooms get more headroom.
RL_BASE_MSGS = 30
RL_PER_EXTRA_SPEAKER = 15
RL_MAX_MSGS = 120
RL_WINDOW = 15.0
RL_WARNS = 3
WEBHOOK_REPAIR_ATTEMPTS = 2

# Vote kick
VK_DURATION = 60    # seconds the voting window stays open
VK_COOLDOWN = 300   # seconds a kicked guild must wait before rejoining

# Matches custom Discord emojis — <:name:id> and <a:name:id> (animated).
# Stripped silently — they won't render in other servers.
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

# This catches Tenor, Giphy, Klipy, and ANY link that ends in .gif
GIF_LINK_PATTERN = re.compile(
    r"https?://(?:\S*\.)?(?:tenor\.com|giphy\.com|klipy\.com|static\.klipy\.com)\S*"
    r"|https?://\S+\.gif(?:\?\S*)?",
    re.IGNORECASE,
)

# Matches any URL that is NOT an allowed GIF link — stripped from room messages.
_ANY_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")

# Maximum characters / lines allowed in a single room message before it is rejected.
ROOM_MAX_MSG_LEN = 500
ROOM_MAX_LINES    = 10


# ── Shared helpers (inlined to avoid circular import from phonebooth) ─────────

def _anon_identity(seed: int) -> tuple[str, str]:
    rng = random.Random(seed)
    name = rng.choice(config.ANON_NAMES)
    avatar = f"https://robohash.org/{seed}.png?set=set4&size=128x128"
    return name, avatar


_ANON_NOTICE = "🎭 **Anon mode is ON**"


def _get_avatar_url(member: discord.Member | discord.User) -> str:
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


# ── Vote-kick state + view ────────────────────────────────────────────────────

class VoteKickState:
    """Tracks a single active vote-kick within a room."""

    def __init__(
        self,
        room_id: int,
        target_channel: int,
        target_station: str,
        target_guild: int,
        initiator_channel: int,
        total_members: int,
    ) -> None:
        self.room_id          = room_id
        self.target_channel   = target_channel
        self.target_station   = target_station
        self.target_guild     = target_guild
        self.initiator_channel = initiator_channel
        self.total_members    = total_members
        # channel_id → True (kick) / False (keep)
        self.votes: dict[int, bool] = {initiator_channel: True}


class VoteKickView(discord.ui.View):
    """Buttons sent to every room channel during a vote-kick."""

    def __init__(self, state: VoteKickState, cog: "Room") -> None:
        super().__init__(timeout=VK_DURATION)
        self.state = state
        self.cog   = cog

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _needed(self) -> int:
        return (self.state.total_members // 2) + 1

    async def _try_early_resolve(self) -> None:
        s = self.state
        kick_votes = sum(1 for v in s.votes.values() if v)
        keep_votes = sum(1 for v in s.votes.values() if not v)
        needed = self._needed()
        if kick_votes >= needed:
            await self._resolve(True)
        elif keep_votes > s.total_members - needed:
            await self._resolve(False)

    async def _resolve(self, passed: bool) -> None:
        self.stop()
        s = self.state
        self.cog._active_votekicks.pop(s.room_id, None)

        if passed:
            await self.cog._execute_kick(s, by_vote=True)
        else:
            members = await self.cog.db.get_room_members(s.room_id)
            embed = discord.Embed(
                description=f"🗳️ Vote to kick **Station {s.target_station}** did not pass.",
                color=config.COLOR_WARN,
            )
            for m in members:
                ch = self.cog.bot.get_channel(m["channel_id"])
                if ch:
                    try:
                        await ch.send(embed=embed)
                    except discord.HTTPException:
                        pass

    async def on_timeout(self) -> None:
        s = self.state
        self.cog._active_votekicks.pop(s.room_id, None)
        kick_votes = sum(1 for v in s.votes.values() if v)
        await self._resolve(kick_votes >= self._needed())

    # ── Buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="✅ Kick", style=discord.ButtonStyle.danger)
    async def btn_kick(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cast(interaction, True)

    @discord.ui.button(label="❌ Keep", style=discord.ButtonStyle.secondary)
    async def btn_keep(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cast(interaction, False)

    async def _cast(self, interaction: discord.Interaction, vote: bool) -> None:
        s = self.state
        rm = await self.cog.db.get_room_member(interaction.channel.id)
        if not rm or rm["room_id"] != s.room_id:
            await interaction.response.send_message(
                "❌ You're not in this room.", ephemeral=True
            )
            return
        if rm["channel_id"] == s.target_channel:
            await interaction.response.send_message(
                "❌ You can't vote on your own kick.", ephemeral=True
            )
            return
        if rm["channel_id"] in s.votes:
            await interaction.response.send_message(
                "⚠️ Your station has already voted.", ephemeral=True
            )
            return

        s.votes[rm["channel_id"]] = vote
        label = f"Station {s.target_station}"
        reply = f"✅ Voted to **kick** {label}." if vote else f"❌ Voted to **keep** {label}."
        await interaction.response.send_message(reply, ephemeral=True)
        await self._try_early_resolve()


# ── Room cog ──────────────────────────────────────────────────────────────────

class Room(commands.Cog):
    """Group room commands — up to 5 servers talking together."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db

        # Rate-limiting: channel_id -> deque of (monotonic timestamp, user_id)
        self._msg_times:   dict[int, deque]   = {}
        # In-memory warn counts (reset when member leaves)
        self._warn_counts: dict[int, int]      = {}
        # Whole-room inactivity tasks, keyed by room ID.
        self._inactivity_tasks: dict[int, asyncio.Task] = {}
        # Waiting-room timeout tasks per room_id
        self._waiting_tasks:    dict[int, asyncio.Task] = {}
        # Kick cooldowns: guild_id → monotonic expiry timestamp
        self._kick_cooldowns:   dict[int, float]        = {}
        # Active vote-kicks: room_id → VoteKickState
        self._active_votekicks: dict[int, VoteKickState] = {}
        self._broken_webhook_notified: set[int] = set()
        self._reaction_routes: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self._reaction_times: dict[int, deque] = {}
        self._relay_locks: dict[int, asyncio.Lock] = {}
        self._relay_context_by_channel: dict[
            int, tuple[dict, dict, list[dict]]
        ] = {}
        self._relay_context_miss_until: dict[int, float] = {}
        self._join_locks: dict[int, asyncio.Lock] = {}
        self._recovery_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._recovery_task = asyncio.create_task(self._recover_runtime_state())

    def cog_unload(self) -> None:
        if self._recovery_task:
            self._recovery_task.cancel()
        for t in self._inactivity_tasks.values():
            t.cancel()
        for t in self._waiting_tasks.values():
            t.cancel()

    def _cache_relay_context(self, room: dict, members: list[dict]) -> None:
        for member in members:
            channel_id = int(member["channel_id"])
            self._relay_context_miss_until.pop(channel_id, None)
            self._relay_context_by_channel[channel_id] = (
                member,
                room,
                members,
            )

    def _invalidate_relay_context(
        self, *, room_id: Optional[int] = None, channel_id: Optional[int] = None
    ) -> None:
        if room_id is None and channel_id is not None:
            cached = self._relay_context_by_channel.get(int(channel_id))
            if cached:
                room_id = int(cached[1]["id"])
        if room_id is not None:
            stale = [
                cid
                for cid, (_, room, _) in self._relay_context_by_channel.items()
                if int(room["id"]) == int(room_id)
            ]
            for cid in stale:
                self._relay_context_by_channel.pop(cid, None)
        elif channel_id is not None:
            self._relay_context_by_channel.pop(int(channel_id), None)
        if channel_id is not None:
            self._relay_context_miss_until.pop(int(channel_id), None)

    async def _get_relay_context(
        self, channel_id: int
    ) -> tuple[Optional[dict], Optional[dict], list[dict]]:
        cached = self._relay_context_by_channel.get(int(channel_id))
        if cached:
            return cached
        if self._relay_context_miss_until.get(int(channel_id), 0.0) > time.monotonic():
            return None, None, []
        member, room, members = await self.db.get_room_relay_context(channel_id)
        if member and room and room["status"] == "active":
            self._cache_relay_context(room, members)
        elif not member:
            self._relay_context_miss_until[int(channel_id)] = time.monotonic() + 30.0
        return member, room, members

    # ── Rate-limit / flood detection ──────────────────────────────────────────

    @staticmethod
    def _dynamic_rl_limit(active_speakers: int) -> int:
        speakers = max(1, active_speakers)
        return min(
            RL_MAX_MSGS,
            RL_BASE_MSGS + (speakers - 1) * RL_PER_EXTRA_SPEAKER,
        )

    def _check_rate(self, channel_id: int, user_id: int) -> tuple[bool, int, int, int]:
        """Return (limited, count, limit, active_speakers) for this source channel."""
        now = time.monotonic()
        dq  = self._msg_times.setdefault(channel_id, deque())
        dq.append((now, user_id))
        cutoff = now - RL_WINDOW
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        active_speakers = len({uid for _, uid in dq})
        limit = self._dynamic_rl_limit(active_speakers)
        return len(dq) > limit, len(dq), limit, active_speakers

    # ── Broadcast helper ──────────────────────────────────────────────────────

    async def _broadcast(
        self,
        room_id: int,
        *,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        exclude: Optional[int] = None,
    ) -> None:
        """Send to every room channel, optionally excluding one channel_id."""
        for m in await self.db.get_room_members(room_id):
            if exclude and m["channel_id"] == exclude:
                continue
            ch = self.bot.get_channel(m["channel_id"])
            if ch:
                try:
                    await ch.send(content=content, embed=embed)
                except discord.HTTPException:
                    pass

    # ── Inactivity per member ─────────────────────────────────────────────────

    def _reset_inactivity(
        self,
        channel_id: int,
        room_id: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        old = self._inactivity_tasks.pop(room_id, None)
        if old:
            old.cancel()
        self._inactivity_tasks[room_id] = asyncio.create_task(
            self._inactivity_timer(channel_id, room_id, delay_seconds)
        )

    def _cancel_inactivity(self, channel_id: int) -> None:
        # Member removal must not cancel the room-wide activity timer.
        return

    def _cancel_room_inactivity(self, room_id: int) -> None:
        task = self._inactivity_tasks.pop(room_id, None)
        if task:
            task.cancel()

    async def _close_room(self, room_id: int) -> None:
        """Close a room and start the same report-log expiry used by calls."""
        await self.db.close_room(room_id)

    async def _inactivity_timer(
        self,
        channel_id: int,
        room_id: int,
        delay_seconds: Optional[float] = None,
    ) -> None:
        delay = ROOM_INACTIVITY_MINUTES * 60 if delay_seconds is None else max(0.0, delay_seconds)
        await asyncio.sleep(delay)
        room = await self.db.get_room_by_id(room_id)
        if not room or room["status"] != "active":
            return
        members = await self.db.get_room_members(room_id)
        self._invalidate_relay_context(room_id=room_id)
        for member in members:
            ch = self.bot.get_channel(member["channel_id"])
            if ch:
                try:
                    await ch.send(
                        f"📵 Room closed after **{ROOM_INACTIVITY_MINUTES} minutes** without a relayed message."
                    )
                except discord.HTTPException:
                    pass
            await self.db.remove_room_member(member["channel_id"])
        await self._close_room(room_id)
        self._cancel_room_inactivity(room_id)

    # ── Waiting-room timeout ──────────────────────────────────────────────────

    def _start_waiting_timeout(self, room_id: int, delay_seconds: Optional[float] = None) -> None:
        self._cancel_waiting_timeout(room_id)
        self._waiting_tasks[room_id] = asyncio.create_task(
            self._waiting_timer(room_id, delay_seconds)
        )

    def _cancel_waiting_timeout(self, room_id: int) -> None:
        t = self._waiting_tasks.pop(room_id, None)
        if t:
            t.cancel()

    async def _waiting_timer(self, room_id: int, delay_seconds: Optional[float] = None) -> None:
        """Dissolve a waiting room if nobody else joins in time."""
        delay = ROOM_QUEUE_TIMEOUT_MINUTES * 60 if delay_seconds is None else max(0.0, delay_seconds)
        await asyncio.sleep(delay)
        room = await self.db.get_room_by_id(room_id)
        if not room or room["status"] != "waiting":
            return
        self._invalidate_relay_context(room_id=room_id)
        for m in await self.db.get_room_members(room_id):
            ch = self.bot.get_channel(m["channel_id"])
            if ch:
                try:
                    await ch.send(
                        f"📵 No other servers joined within **{ROOM_QUEUE_TIMEOUT_MINUTES} minutes**. "
                        f"Use `/room` to try again!"
                    )
                except discord.HTTPException:
                    pass
            await self.db.remove_room_member(m["channel_id"])
            self._cancel_inactivity(m["channel_id"])
        await self._close_room(room_id)

    async def _recover_runtime_state(self) -> None:
        """Resume or expire persisted room timers after a bot restart."""
        await self.bot.wait_until_ready()
        try:
            await self._recover_open_rooms()
        except Exception as exc:
            print(f"[recovery] room recovery failed: {exc}")

    async def _recover_open_rooms(self) -> None:
        waiting_timeout = ROOM_QUEUE_TIMEOUT_MINUTES * 60
        inactivity_timeout = ROOM_INACTIVITY_MINUTES * 60

        for room in await self.db.get_open_rooms():
            room_id = int(room["id"])
            members = await self.db.get_room_members(room_id)
            if not members:
                await self._close_room(room_id)
                continue

            if room["status"] == "waiting":
                if len(members) >= 2:
                    await self.db.activate_room(room_id)
                    room["status"] = "active"
                else:
                    elapsed = _elapsed_seconds(room.get("created_at"))
                    if elapsed >= waiting_timeout:
                        await self._waiting_timer(room_id, 0)
                        continue
                    self._start_waiting_timeout(room_id, waiting_timeout - elapsed)
            elif len(members) < 2:
                for member in members:
                    channel = self.bot.get_channel(member["channel_id"])
                    if channel:
                        try:
                            await channel.send(
                                "📵 **Room closed** — not enough servers remaining.\n"
                                "Use `/room` to start a new one!"
                            )
                        except discord.HTTPException:
                            pass
                    await self.db.remove_room_member(member["channel_id"])
                    self._cancel_inactivity(member["channel_id"])
                await self._close_room(room_id)
                continue

            if room["status"] == "active":
                self._cache_relay_context(room, members)
                elapsed = _elapsed_seconds(room.get("last_activity_at") or room.get("created_at"))
                first_channel = int(members[0]["channel_id"])
                self._reset_inactivity(
                    first_channel, room_id, max(0.0, inactivity_timeout - elapsed)
                )

    # ── Core: remove a member and handle room collapse ────────────────────────

    async def _remove_member(
        self,
        channel_id: int,
        room_id: int,
        *,
        broadcast_reason: str,
        notify_leaver: bool = True,
        leaver_msg: str = "📵 You have been removed from the room. Use `/room` to join a new one!",
    ) -> None:
        """
        Remove one member, clean up their in-memory state, notify the room,
        and collapse the room if fewer than 2 servers remain.
        """
        row = await self.db.remove_room_member(channel_id)
        if not row:
            return
        self._invalidate_relay_context(room_id=room_id)

        self._cancel_inactivity(channel_id)
        self._msg_times.pop(channel_id, None)
        self._warn_counts.pop(channel_id, None)

        if notify_leaver:
            ch = self.bot.get_channel(channel_id)
            if ch:
                try:
                    await ch.send(leaver_msg)
                except discord.HTTPException:
                    pass

        remaining = await self.db.get_room_members(room_id)
        if len(remaining) < 2:
            # Room collapses — tell everyone and close
            for rm in remaining:
                rch = self.bot.get_channel(rm["channel_id"])
                if rch:
                    try:
                        await rch.send(
                            "📵 **Room closed** — not enough servers remaining.\n"
                            "Use `/room` to start a new one!"
                        )
                    except discord.HTTPException:
                        pass
                await self.db.remove_room_member(rm["channel_id"])
                self._cancel_inactivity(rm["channel_id"])
            await self._close_room(room_id)
            self._cancel_waiting_timeout(room_id)
            self._cancel_room_inactivity(room_id)
            return

        # Broadcast departure to remaining members
        await self._broadcast(
            room_id,
            embed=discord.Embed(
                description=f"{broadcast_reason} ({len(remaining)}/{ROOM_MAX_SIZE} in room)",
                color=config.COLOR_WARN,
            ),
        )

    # ── Core: execute a kick (by vote or by flood) ────────────────────────────

    async def _execute_kick(self, state: VoteKickState, *, by_vote: bool) -> None:
        """Apply kick cooldown, remove member, collapse room if needed."""
        self._kick_cooldowns[state.target_guild] = time.monotonic() + VK_COOLDOWN

        reason_str = "a majority vote" if by_vote else "flooding / spamming"

        # Tell the kicked channel first
        kicked_ch = self.bot.get_channel(state.target_channel)
        if kicked_ch:
            try:
                await kicked_ch.send(
                    embed=discord.Embed(
                        title="🔨 Removed from Room",
                        description=(
                            f"Your server was removed by {reason_str}.\n"
                            f"You can rejoin rooms after a **5-minute cooldown**."
                        ),
                        color=config.COLOR_ERR,
                    )
                )
            except discord.HTTPException:
                pass

        # Remove from DB + clean up
        await self.db.remove_room_member(state.target_channel)
        self._invalidate_relay_context(room_id=state.room_id)
        self._cancel_inactivity(state.target_channel)
        self._msg_times.pop(state.target_channel, None)
        self._warn_counts.pop(state.target_channel, None)

        remaining = await self.db.get_room_members(state.room_id)
        if len(remaining) < 2:
            for rm in remaining:
                rch = self.bot.get_channel(rm["channel_id"])
                if rch:
                    try:
                        await rch.send(
                            "📵 **Room closed** — not enough servers remaining.\n"
                            "Use `/room` to start a new one!"
                        )
                    except discord.HTTPException:
                        pass
                await self.db.remove_room_member(rm["channel_id"])
                self._cancel_inactivity(rm["channel_id"])
            await self._close_room(state.room_id)
            return

        await self._broadcast(
            state.room_id,
            embed=discord.Embed(
                description=(
                    f"🔨 **Station {state.target_station}** was removed by {reason_str}. "
                    f"({len(remaining)}/{ROOM_MAX_SIZE} in room)"
                ),
                color=config.COLOR_ERR,
            ),
        )

    # ── Webhook helpers ───────────────────────────────────────────────────────

    async def get_or_create_webhook(self, channel: discord.TextChannel) -> Optional[str]:
        try:
            for wh in await channel.webhooks():
                if wh.user == self.bot.user and wh.name == "Fliphone":
                    return wh.url
            return (await channel.create_webhook(name="Fliphone")).url
        except discord.Forbidden:
            return None
        except Exception as exc:
            print(f"[room-webhook] {exc}")
            return None

    async def ensure_relay_webhook(
        self,
        channel: discord.TextChannel,
        *,
        force_refresh: bool = False,
    ) -> tuple[Optional[str], list[str]]:
        phonebooth = self.bot.get_cog("Phonebooth")
        if phonebooth and hasattr(phonebooth, "ensure_relay_webhook"):
            return await phonebooth.ensure_relay_webhook(
                channel,
                force_refresh=force_refresh,
            )

        bot_member = channel.guild.me
        if bot_member is None:
            return None, ["Bot member unavailable"]
        perms = channel.permissions_for(bot_member)
        required = (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
            ("Manage Webhooks", perms.manage_webhooks),
        )
        issues = [name for name, allowed in required if not allowed]
        if issues:
            return None, issues
        webhook_url = await self.get_or_create_webhook(channel)
        if not webhook_url:
            return None, ["Webhook access failed"]
        await self.db.update_webhook(channel.id, webhook_url)
        return webhook_url, []

    async def rebuild_relay_webhook(
        self,
        channel: discord.TextChannel,
    ) -> tuple[Optional[str], list[str]]:
        phonebooth = self.bot.get_cog("Phonebooth")
        if phonebooth and hasattr(phonebooth, "rebuild_relay_webhook"):
            return await phonebooth.rebuild_relay_webhook(channel)

        bot_member = channel.guild.me
        if bot_member is None:
            return None, ["Bot member unavailable"]
        perms = channel.permissions_for(bot_member)
        required = (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
            ("Manage Webhooks", perms.manage_webhooks),
        )
        issues = [name for name, allowed in required if not allowed]
        if issues:
            return None, issues

        try:
            webhooks = await channel.webhooks()
            other_webhook_count = 0
            for webhook in webhooks:
                if webhook.user == self.bot.user and webhook.name == "Fliphone":
                    await webhook.delete(reason="Fliphone room relay reset")
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
        return webhook_url, []

    async def _send_webhook(
        self,
        url: str,
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        *,
        embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> discord.WebhookMessage | bool | None:
        """Send via webhook. Returns the message when requested, otherwise success."""
        session = getattr(self.bot, "http_session", None)
        own_session = session is None or session.closed
        try:
            if own_session:
                session = aiohttp.ClientSession()
            wh = discord.Webhook.from_url(url, session=session)
            msg = await wh.send(
                content=content or None,
                username=username[:80],
                avatar_url=avatar_url,
                files=files if files else discord.utils.MISSING,
                embed=embed if embed is not None else discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=silent,
                wait=wait,
            )
            return msg if wait else True
        except discord.HTTPException as exc:
            status = getattr(exc, "status", None)
            if status == 400 and "wh" in locals():
                fallback = await self._send_webhook_bad_content_fallback(
                    wh,
                    content,
                    username,
                    avatar_url,
                    files,
                    embed=embed,
                    wait=wait,
                    silent=silent,
                )
                if fallback:
                    return fallback
                print(f"[room-relay-content] status={status} code={getattr(exc, 'code', None)} {exc}")
                return None
            print(f"[room-relay] status={status} code={getattr(exc, 'code', None)} {exc}")
            return None
        except Exception as exc:
            print(f"[room-relay] {exc}")
            return None
        finally:
            if own_session and session:
                await session.close()

    async def _send_webhook_bad_content_fallback(
        self,
        wh: discord.Webhook,
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        *,
        embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> discord.WebhookMessage | bool | None:
        def _downgrade_embed(value: Optional[discord.Embed]) -> tuple[Optional[discord.Embed], list[int]]:
            if not value or not value.description:
                return value, []
            updated, ids = downgrade_animated_custom_emoji_markup(value.description)
            if not ids:
                return value, []
            clone = value.copy()
            clone.description = updated
            return clone, ids

        downgraded_content, content_ids = downgrade_animated_custom_emoji_markup(content or "")
        downgraded_embed, embed_ids = _downgrade_embed(embed)
        if content_ids or embed_ids:
            try:
                msg = await wh.send(
                    content=downgraded_content or None,
                    username=username[:80],
                    avatar_url=avatar_url,
                    files=files if files else discord.utils.MISSING,
                    embed=downgraded_embed if downgraded_embed else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                    wait=wait,
                )
                asyncio.create_task(self.db.mark_app_emojis_static(content_ids + embed_ids))
                return msg if wait else True
            except discord.HTTPException:
                pass

        plain_content = plain_custom_emoji_fallback(content or "").strip()
        plain_embed = embed
        if embed and embed.description:
            plain_embed = embed.copy()
            plain_embed.description = plain_custom_emoji_fallback(embed.description).strip() or "emoji"
        if not plain_content and not plain_embed and not files:
            plain_content = "emoji"
        try:
            msg = await wh.send(
                content=plain_content or None,
                username=username[:80],
                avatar_url=avatar_url,
                files=files if files else discord.utils.MISSING,
                embed=plain_embed if plain_embed else discord.utils.MISSING,
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
                    files=files if files else discord.utils.MISSING,
                    embed=plain_embed if plain_embed else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                    wait=wait,
                )
                return msg if wait else True
            except discord.HTTPException:
                pass
            print(f"[room-relay-content-fallback] status={getattr(exc, 'status', None)} code={getattr(exc, 'code', None)} {exc}")
            return None

    # ── Message relay (one sender → all others) ───────────────────────────────

    async def _send_room_webhook_with_repair(
        self,
        channel: Optional[discord.abc.GuildChannel],
        member: dict,
        webhook_url: Optional[str],
        content: Optional[str],
        username: str,
        avatar_url: Optional[str],
        files: list[discord.File],
        *,
        embed: Optional[discord.Embed] = None,
        wait: bool = False,
        silent: bool = False,
    ) -> tuple[Optional[str], discord.WebhookMessage | bool | None]:
        if webhook_url:
            sent = await self._send_webhook(
                webhook_url,
                content,
                username,
                avatar_url,
                files,
                embed=embed,
                wait=wait,
                silent=silent,
            )
            if sent:
                return webhook_url, sent

        if not isinstance(channel, discord.TextChannel):
            return webhook_url, None if wait else False

        for attempt in range(WEBHOOK_REPAIR_ATTEMPTS):
            repaired_url, _ = await self.rebuild_relay_webhook(channel)
            if not repaired_url:
                if attempt + 1 < WEBHOOK_REPAIR_ATTEMPTS:
                    await asyncio.sleep(0.5)
                continue

            member["webhook_url"] = repaired_url
            await self.db.update_room_member_webhook(member["channel_id"], repaired_url)
            sent = await self._send_webhook(
                repaired_url,
                content,
                username,
                avatar_url,
                files,
                embed=embed,
                wait=wait,
                silent=silent,
            )
            if sent:
                return repaired_url, sent

        return webhook_url, None if wait else False

    async def _relay_to_room(
        self,
        message: discord.Message,
        room: dict,
        member: dict,
        members: list[dict],
    ) -> None:
        # ── User ban check ────────────────────────────────────────────────────
        phonebooth = self.bot.get_cog("Phonebooth")
        if phonebooth and hasattr(phonebooth, "_get_user_relay_policy"):
            is_banned, anon, content_opt_out = await phonebooth._get_user_relay_policy(
                message.author.id
            )
        else:
            is_banned, anon, content_opt_out = await asyncio.gather(
                self.db.is_user_banned(message.author.id),
                self.db.is_user_anonymous(message.author.id),
                self.db.is_content_opted_out(message.author.id),
            )

        if content_opt_out:
            return
        if is_banned:
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} You are banned from using Fliphone.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return

        # ── Rate limit / flood check ──────────────────────────────────────────
        is_rate_limited, _, cap, speakers = self._check_rate(
            message.channel.id,
            message.author.id,
        )

        if is_rate_limited:
            warn = self._warn_counts.get(message.channel.id, 0) + 1
            self._warn_counts[message.channel.id] = warn
            try:
                await message.channel.send(
                    f"{message.author.mention} This channel is sending messages too fast "
                    f"for this room. Current cap: **{cap}/{int(RL_WINDOW)}s** "
                    f"with **{speakers}** active speaker(s). "
                    f"(Warning **{warn}/{RL_WARNS}**)",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            if warn >= RL_WARNS:
                state = VoteKickState(
                    room_id=room["id"],
                    target_channel=message.channel.id,
                    target_station=member["station"],
                    target_guild=member["guild_id"],
                    initiator_channel=message.channel.id,
                    total_members=len(members),
                )
                await self._execute_kick(state, by_vote=False)
            return
        self._warn_counts.pop(message.channel.id, None)

        # ── Identity ──────────────────────────────────────────────────────────
        if anon:
            seed = room["id"] * 100000 + message.author.id
            display_name, avatar_url = _anon_identity(seed)
        else:
            author       = message.author
            display_name = _relay_display_name(author)
            avatar_url = _get_avatar_url(author)

        # Webhook username always shows station so servers are identifiable.
        webhook_name = f"Station {member['station']} · {display_name}"

        # ── Reply embed ───────────────────────────────────────────────────────
        reply_embed: Optional[discord.Embed] = None
        reply_context: Optional[str] = None
        if message.reference:
            ref_msg = message.reference.resolved
            if isinstance(ref_msg, discord.Message):
                ref_author = _relay_display_name(ref_msg.author)
                try:
                    ref_avatar = str(
                        ref_msg.author.display_avatar.with_static_format("png").with_size(64).url
                    ).split("?")[0]
                except Exception:
                    ref_avatar = None
                ref_text = (ref_msg.content or "").strip()
                if is_local_only(ref_text):
                    ref_text = "message"
                else:
                    # Strip lines that are bare URLs (GIF links etc.)
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
                reply_embed = discord.Embed(description=ref_text, color=0x5865F2)
                reply_embed.set_author(name=f"Replying to {ref_author}", icon_url=ref_avatar)

        # ── Content filter ────────────────────────────────────────────────────
        raw = message.content or ""

        if is_local_only(raw):
            return

        # ── Anti text-wall: check raw content BEFORE filtering ────────────────
        raw_lines = raw.splitlines()
        if len(raw) > ROOM_MAX_MSG_LEN:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Your message is too long "
                    f"(max **{ROOM_MAX_MSG_LEN}** characters).",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return
        if len(raw_lines) > ROOM_MAX_LINES:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Too many lines "
                    f"(max **{ROOM_MAX_LINES}**).",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return

        content, was_censored = filter_message(raw)
        if was_censored:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Your message was censored before being sent.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        # Mirror approved submitted emojis; strip every other custom emoji.
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
        # Collect allowed GIF URLs first, then remove all other URLs from content.
        inline_gif_urls = [
            url for url in extract_urls(content)
            if is_provider_gif(url) or is_direct_gif_url(url)
        ]
        allowed_gif_set: set[str] = set(inline_gif_urls)

        def _strip_non_gif_urls(text: str) -> tuple[str, bool]:
            """Remove any URL that isn't an allowed GIF link. Returns (new_text, had_links)."""
            had_links = False
            def _replacer(m: re.Match) -> str:
                nonlocal had_links
                if m.group(0) in allowed_gif_set:
                    return m.group(0)
                had_links = True
                return ""
            new_text = _ANY_URL_PATTERN.sub(_replacer, text).strip()
            return new_text, had_links

        content, had_unsafe_links = _strip_non_gif_urls(content)
        if had_unsafe_links:
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Links are not allowed in rooms and were removed.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        # ── Attachments ───────────────────────────────────────────────────────
        GIF_EXT = {".gif"}
        attachment_gif_urls: list[str] = []

        for att in message.attachments:
            ext = ("." + att.filename.rsplit(".", 1)[-1].lower()) if "." in att.filename else ""
            if ext in GIF_EXT:
                content += f"\n{att.url}"
                attachment_gif_urls.append(att.url)

        # Deduplicate GIFs
        def _norm(u: str) -> str:
            return u.split("?")[0].rstrip("/").lower()

        seen: set[str] = set()
        all_gif_urls: list[str] = []
        for u in inline_gif_urls + attachment_gif_urls:
            n = _norm(u)
            if n not in seen:
                seen.add(n)
                all_gif_urls.append(u)

        # GIF blacklist check (all in parallel, not one-by-one)
        gif_status_by_url: dict[str, Optional[str]] = {}
        if all_gif_urls:
            gif_statuses = await asyncio.gather(
                *[self.db.check_gif_url(u) for u in all_gif_urls],
                return_exceptions=True,
            )
            gif_status_by_url = {
                url: (None if isinstance(status, Exception) else status)
                for url, status in zip(all_gif_urls, gif_statuses)
            }
            blocked_count = 0
            safe_gifs: list[str] = []
            for gif_url, status in zip(all_gif_urls, gif_statuses):
                if status == "blacklist":
                    content = content.replace(gif_url, "")
                    blocked_count += 1
                elif is_provider_gif(gif_url) or status == "whitelist":
                    safe_gifs.append(gif_url)
                else:
                    content = content.replace(gif_url, "")
            unapproved = len(all_gif_urls) - blocked_count - len(safe_gifs)
            all_gif_urls = safe_gifs
            if blocked_count > 0:
                try:
                    await message.channel.send(
                        f"🚫 {message.author.mention} {blocked_count} blocked GIF(s) were removed.",
                        delete_after=8,
                    )
                except discord.HTTPException:
                    pass
            if unapproved:
                try:
                    await message.channel.send(
                        "That GIF source is not approved. Submit its URL with `/addgif` for review.",
                        delete_after=8,
                    )
                except discord.HTTPException:
                    pass

        if not content.strip() and not all_gif_urls:
            return

        # ── Counters + inactivity reset ───────────────────────────────────────
        asyncio.create_task(self.db.increment_room_msg_count(room["id"]))
        asyncio.create_task(self.db.increment_room_member_msg_count(message.channel.id))
        self._reset_inactivity(message.channel.id, room["id"])
        asyncio.create_task(
            self.db.add_chat_xp(message.author.id, message.guild.id, random.randint(12, 22), 60)
        )

        # ── Resolve GifReportView once for the whole relay ───────────────────
        import sys as _sys
        _pb_mod = _sys.modules.get("cogs.phonebooth")
        GifReportView = getattr(_pb_mod, "GifReportView", None) if _pb_mod else None

        members_without_self = [m for m in members if m["channel_id"] != message.channel.id]
        message_copies: list[tuple[int, int]] = [(message.channel.id, message.id)]

        # ── Relay to every other member ───────────────────────────────────────
        for other in members_without_self:
            wh_url = other.get("webhook_url")
            other_ch = self.bot.get_channel(other["channel_id"])

            recipient_content = content
            recipient_gif_urls = list(all_gif_urls)

            recipient_text_content = recipient_content.strip() or None
            recipient_reply_embed = reply_embed
            if recipient_gif_urls and reply_embed:
                recipient_text_content = "\n".join(
                    part for part in (reply_context, recipient_text_content) if part
                )
                recipient_reply_embed = None

            if not recipient_text_content:
                continue

            send_files: list[discord.File] = []

            sent_msg_id: Optional[int] = None

            wh_url, wh_msg = await self._send_room_webhook_with_repair(
                other_ch,
                other,
                wh_url,
                recipient_text_content,
                webhook_name,
                avatar_url,
                send_files,
                embed=recipient_reply_embed,
                wait=True,
                silent=bool(recipient_gif_urls),
            )
            if not wh_msg:
                if other_ch and other["channel_id"] not in self._broken_webhook_notified:
                    try:
                        await other_ch.send(
                            "⚠️ Room relay stopped for this server because webhook repair failed. "
                            "An admin should run `/repair` in this channel."
                        )
                        self._broken_webhook_notified.add(other["channel_id"])
                    except discord.HTTPException:
                        pass
                continue
            self._broken_webhook_notified.discard(other["channel_id"])
            if isinstance(wh_msg, discord.WebhookMessage):
                sent_msg_id = wh_msg.id
                message_copies.append((other["channel_id"], wh_msg.id))

            # ── GIF report cards (batched checks, not one-by-one) ──────────────
            if recipient_gif_urls and other_ch:
                # Create report tasks for non-whitelisted GIFs
                report_tasks = []
                for gif_url in recipient_gif_urls:
                    status = gif_status_by_url.get(gif_url)
                    if status == "whitelist":
                        continue
                    async def _create_report(url=gif_url):
                        try:
                            report_id = await self.db.add_gif_report(
                                url=url,
                                msg_id=sent_msg_id,
                                channel_id=other["channel_id"],
                                guild_id=other["guild_id"],
                                session_type="room",
                                session_id=room["id"],
                                sender_user_id=message.author.id,
                                source_guild_id=message.guild.id,
                                source_channel_id=message.channel.id,
                            )
                            if GifReportView:
                                prompt = await other_ch.send(view=GifReportView(), silent=True)
                                await self.db.set_gif_report_prompt(
                                    report_id, prompt.id, prompt.channel.id
                                )
                        except discord.HTTPException:
                            pass
                        except Exception as e:
                            print(f"ERROR: GIF report failed: {e}")
                    report_tasks.append(_create_report())
                
                if report_tasks:
                    await asyncio.gather(*report_tasks, return_exceptions=True)

        if len(message_copies) > 1:
            if used_custom_emoji_ids:
                asyncio.create_task(self.db.increment_emoji_usage(used_custom_emoji_ids))
            for copy in message_copies:
                self._reaction_routes[copy] = [item for item in message_copies if item != copy]

    async def _relay_reaction(self, payload: discord.RawReactionActionEvent, *, remove: bool) -> None:
        if not self.bot.user or payload.user_id == self.bot.user.id or payload.emoji.id is not None:
            return
        routes = self._reaction_routes.get((payload.channel_id, payload.message_id))
        if not routes:
            return
        now = time.monotonic()
        times = self._reaction_times.setdefault(payload.user_id, deque())
        while times and now - times[0] > 10:
            times.popleft()
        if len(times) >= 5:
            return
        times.append(now)
        for channel_id, message_id in routes:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue
            try:
                target = await channel.fetch_message(message_id)
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
        if message.author.bot or not message.guild:
            return
        rm, room, members = await self._get_relay_context(message.channel.id)
        if not rm:
            return
        if not room or room["status"] != "active":
            return
        phonebooth = self.bot.get_cog("Phonebooth")
        if phonebooth and hasattr(phonebooth, "_get_user_relay_policy"):
            _, _, content_opt_out = await phonebooth._get_user_relay_policy(message.author.id)
        else:
            content_opt_out = await self.db.is_content_opted_out(message.author.id)
        if content_opt_out:
            return
        if is_local_only(message.content or ""):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        lock = self._relay_locks.setdefault(int(room["id"]), asyncio.Lock())
        async with lock:
            await self._relay_to_room(message, room, rm, members)

    # ── Internal join logic (shared by f.room and f.roomskip) ────────────────

    async def _do_join(
        self,
        ctx: commands.Context,
        *,
        excluded_room_id: Optional[int] = None,
        force_new: bool = False,
        progress: discord.Message,
    ) -> None:
        """Core join logic — find or create a room for this channel."""
        async def finish(content: str = "", *, embed: discord.Embed | None = None) -> None:
            await progress.edit(content=content or None, embed=embed)

        if await self.db.is_user_banned(ctx.author.id):
            await finish("🚫 You are banned from using Fliphone.")
            return
        if await self.db.is_guild_banned(ctx.guild.id):
            await finish("🚫 This server is banned from using Fliphone.")
            return
        if await self.db.is_content_opted_out(ctx.author.id):
            await finish("Message relay is disabled for your account. Use `/privacy optin` before joining.")
            return

        cfg = await self.db.get_guild_config(ctx.guild.id)
        if not cfg:
            await finish("❌ Fliphone isn't set up in this server. An admin should run `/setup` first.")
            return

        if await self.db.get_connection(ctx.channel.id):
            await finish("📞 This channel is in an active 1:1 call. Use `/hangup` first.")
            return

        if await self.db.get_room_member(ctx.channel.id):
            await finish("📡 Already in a room! Use `/roomleave` to leave first.")
            return

        # Kick-cooldown check
        expiry = self._kick_cooldowns.get(ctx.guild.id, 0)
        if time.monotonic() < expiry:
            secs = int(expiry - time.monotonic())
            await finish(
                f"⏳ Your server is on a room cooldown for another "
                f"**{secs // 60}m {secs % 60}s**."
            )
            return

        if not isinstance(ctx.channel, discord.TextChannel):
            await finish("❌ Group rooms require a normal text channel.")
            return
        try:
            async with asyncio.timeout(15):
                wh_url, permission_issues = await self.ensure_relay_webhook(ctx.channel)
        except TimeoutError:
            await finish(
                "❌ Room setup timed out while checking this channel's webhook. "
                "Ask an admin to run `/repair` here, then try again."
            )
            return
        if not wh_url:
            await finish(
                "❌ Fliphone cannot join a room because webhook relay is unavailable.\n"
                f"Missing or broken: **{', '.join(permission_issues)}**\n"
                "A server admin should run `/check` in this channel."
            )
            return

        is_anon = await self.db.is_user_anonymous(ctx.author.id)

        # ── Try to slot into an existing room ─────────────────────────────────
        room = None
        if not force_new:
            room = await self.db.claim_room_slot(
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
                webhook_url=wh_url,
                station_names=STATION_NAMES,
                excluded_room_id=excluded_room_id,
            )

        if room:
            station = room["station"]
            self._relay_context_miss_until.pop(int(ctx.channel.id), None)
            self._invalidate_relay_context(room_id=room["id"])
            count = await self.db.get_room_member_count(room["id"])

            # Promote waiting → active once a second server joins
            if room["status"] == "waiting" and count >= 2:
                await self.db.activate_room(room["id"])
                self._cancel_waiting_timeout(room["id"])

            await self.db.touch_room(room["id"])
            self._reset_inactivity(ctx.channel.id, room["id"])

            # Confirm the join first, then notify the other members.
            all_members = await self.db.get_room_members(room["id"])
            if count >= 2:
                active_room = {**room, "status": "active"}
                self._cache_relay_context(active_room, all_members)
            others = [
                f"**Station {member['station']}**"
                for member in all_members
                if member["channel_id"] != ctx.channel.id
            ]
            others_str = ", ".join(others) if others else "nobody yet"
            await finish(
                embed=discord.Embed(
                    title=f"📡 You joined as Station {station}!",
                    description=(
                        f"There are **{count}** server(s) here right now: {others_str}.\n\n"
                        "Say hello! 👋\n"
                        "**Tips:**\n"
                        "• `/roomstatus` — see who's in the room\n"
                        "• `/roomkick` — start a vote to remove a station\n"
                        "• `/roomleave` — leave quietly  ·  `/roomskip` — skip to a new room\n\n"
                        "*By continuing you agree to be respectful.*"
                    ),
                    color=config.COLOR_OK,
                )
            )
            if is_anon:
                await ctx.send(f"{ctx.author.mention} {_ANON_NOTICE}")
            notices = []
            for m in all_members:
                if m["channel_id"] == ctx.channel.id:
                    continue
                ch = self.bot.get_channel(m["channel_id"])
                if not ch:
                    continue
                notices.append(
                    ch.send(
                        embed=discord.Embed(
                            description=f"📡 **Station {station}** has joined the room! ({count}/{ROOM_MAX_SIZE})",
                            color=config.COLOR_OK,
                        )
                    )
                )
            if notices:
                await asyncio.gather(*notices, return_exceptions=True)
            return

        # ── No suitable room found — create a new one ─────────────────────────
        room_id = await self.db.create_room(max_size=ROOM_MAX_SIZE)
        await self.db.add_room_member(
            room_id=room_id,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            webhook_url=wh_url,
            station="Alpha",
        )
        self._relay_context_miss_until.pop(int(ctx.channel.id), None)
        self._reset_inactivity(ctx.channel.id, room_id)
        self._start_waiting_timeout(room_id)

        await finish(
            embed=discord.Embed(
                title="📡 Room Created — Waiting for others…",
                description=(
                    "You joined as **Station Alpha**!\n"
                    f"Waiting for up to **{ROOM_QUEUE_TIMEOUT_MINUTES} minutes** for other servers.\n"
                    "When a 2nd server joins, the room goes live automatically.\n\n"
                    "Use `/roomleave` to cancel."
                ),
                color=config.COLOR_WAIT,
            )
        )
        if is_anon:
            await ctx.send(f"{ctx.author.mention} {_ANON_NOTICE}")

    # ── f.room ────────────────────────────────────────────────────────────────

    async def _guarded_join(self, ctx: commands.Context, **kwargs) -> None:
        lock = self._join_locks.setdefault(ctx.channel.id, asyncio.Lock())
        if lock.locked():
            await ctx.send("📡 A room search is already running in this channel.")
            return
        async with lock:
            progress = await ctx.send("📡 **Looking for a room...**")
            try:
                async with asyncio.timeout(30):
                    await self._do_join(ctx, progress=progress, **kwargs)
            except TimeoutError:
                logger.warning(
                    "Room join timed out for guild=%s channel=%s",
                    ctx.guild.id,
                    ctx.channel.id,
                )
                await progress.edit(
                    content=(
                        "❌ The room search timed out. Please try `/room` again. "
                        "If it repeats, ask an admin to run `/check`."
                    ),
                    embed=None,
                )
            except Exception:
                logger.exception(
                    "Room join failed for guild=%s channel=%s",
                    ctx.guild.id,
                    ctx.channel.id,
                )
                await progress.edit(
                    content=(
                        "❌ Something interrupted the room search. Please try `/room` again. "
                        "If it repeats, ask an admin to run `/check`."
                    ),
                    embed=None,
                )

    @commands.hybrid_command(name="room", aliases=["r"])
    @commands.guild_only()
    async def room(self, ctx: commands.Context) -> None:
        """Join a group room of up to 5 servers."""
        await self._guarded_join(ctx)

    @commands.hybrid_command(name="roomcreate", aliases=["rc"])
    @commands.guild_only()
    async def roomcreate(self, ctx: commands.Context) -> None:
        """Create a fresh room instead of joining an available one."""
        await self._guarded_join(ctx, force_new=True)

    # ── f.roomleave ───────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roomleave", aliases=["rl"])
    @commands.guild_only()
    async def roomleave(self, ctx: commands.Context) -> None:
        """Leave the current room without re-queuing."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `/room` to join one!")
            return
        await self._remove_member(
            ctx.channel.id,
            rm["room_id"],
            broadcast_reason=f"📡 **Station {rm['station']}** has left the room.",
            notify_leaver=False,
        )
        await ctx.send(
            "📵 You left the room. Use `/room` to join another one!"
        )

    # ── f.roomskip ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roomskip", aliases=["rs"])
    @commands.guild_only()
    async def roomskip(self, ctx: commands.Context) -> None:
        """Leave the current room and immediately search for a new one."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `/room` to join one!")
            return
        await self._remove_member(
            ctx.channel.id,
            rm["room_id"],
            broadcast_reason=f"📡 **Station {rm['station']}** has left the room.",
            notify_leaver=False,
        )
        await ctx.send("⏭️ Skipping to a new room…")
        # Directly invoke the join logic (no cooldown hit since we call internal method)
        await self._guarded_join(ctx, excluded_room_id=rm["room_id"])

    # ── f.roomstatus ─────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roomstatus", aliases=["rst"])
    @commands.guild_only()
    async def roomstatus(self, ctx: commands.Context) -> None:
        """Show current room info."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `/room` to join one!")
            return
        room    = await self.db.get_room_by_id(rm["room_id"])
        members = await self.db.get_room_members(rm["room_id"])

        stations = "  ·  ".join(
            f"**Station {m['station']}**" + (" *(you)*" if m["channel_id"] == ctx.channel.id else "")
            for m in members
        )
        embed = discord.Embed(title="📡 Room Status", color=config.COLOR_OK)
        embed.add_field(name="Your Station", value=f"Station {rm['station']}", inline=True)
        embed.add_field(name="Servers",      value=f"{len(members)}/{ROOM_MAX_SIZE}", inline=True)
        embed.add_field(name="Status",       value=room["status"].capitalize(),  inline=True)
        embed.add_field(name="Duration",     value=_duration_str(room["created_at"]), inline=True)
        embed.add_field(name="Messages",     value=str(room["msg_count"]),       inline=True)
        embed.add_field(name="Stations",     value=stations, inline=False)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    # ── f.roomkick ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roomkick", aliases=["rk"])
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def roomkick(self, ctx: commands.Context, *, station_name: str = "") -> None:
        """
        Start a majority vote to kick a station from the room.
        Usage: /roomkick station:Bravo
        """
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room.")
            return
        room = await self.db.get_room_by_id(rm["room_id"])
        if not room or room["status"] != "active":
            await ctx.send("📡 The room isn't active yet.")
            return
        if rm["room_id"] in self._active_votekicks:
            await ctx.send("⚠️ There's already an active vote kick in this room. Wait for it to finish.")
            return

        members = await self.db.get_room_members(rm["room_id"])
        if len(members) < 3:
            await ctx.send("⚠️ Vote kicks require at least **3** servers in the room.")
            return

        if not station_name:
            valid = ", ".join(
                f"**{m['station']}**"
                for m in members
                if m["channel_id"] != ctx.channel.id
            )
            await ctx.send(
                f"❌ Please specify a station to kick. Available: {valid}\n"
                "Use the `station_name` option in `/roomkick`."
            )
            return

        target_name = station_name.strip().capitalize()
        target = next(
            (m for m in members if m["station"] == target_name), None
        )
        if not target:
            valid = ", ".join(
                f"**{m['station']}**"
                for m in members
                if m["channel_id"] != ctx.channel.id
            )
            await ctx.send(
                f"❌ No station named **{target_name}**. Valid targets: {valid}"
            )
            return
        if target["channel_id"] == ctx.channel.id:
            await ctx.send("❌ You can't vote-kick your own station.")
            return

        state = VoteKickState(
            room_id=rm["room_id"],
            target_channel=target["channel_id"],
            target_station=target_name,
            target_guild=target["guild_id"],
            initiator_channel=ctx.channel.id,
            total_members=len(members),
        )
        view  = VoteKickView(state, self)
        self._active_votekicks[rm["room_id"]] = state

        needed = (len(members) // 2) + 1
        embed = discord.Embed(
            title=f"🗳️ Vote Kick — Station {target_name}",
            description=(
                f"**Station {rm['station']}** started a vote to remove **Station {target_name}**.\n"
                f"**{needed}/{len(members)}** votes needed to pass.\n"
                f"Voting closes in **{VK_DURATION} seconds**.\n\n"
                f"*The initiator automatically votes to kick. Each server gets one vote.*"
            ),
            color=config.COLOR_WARN,
        )
        embed.set_footer(text=config.FOOTER)

        # Send the vote embed to every room channel
        for m in members:
            ch = self.bot.get_channel(m["channel_id"])
            if ch:
                try:
                    await ch.send(embed=embed, view=view)
                except discord.HTTPException:
                    pass


async def setup(bot) -> None:
    await bot.add_cog(Room(bot))
