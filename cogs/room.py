"""
cogs/room.py – Multi-server group room feature for Fliphone.

Commands
--------
f.room / f.r           – Join a group room of up to 6 servers
f.roomleave / f.rl     – Leave the current room
f.roomskip / f.rs      – Leave and immediately re-queue for a new room
f.roomstatus / f.rst   – Show current room info
f.roomkick / f.rk      – Start a majority vote to kick a station

How rooms work
--------------
Each server in a room is assigned a NATO station name (Alpha–Foxtrot).
Server names are never revealed — only station names appear in join/leave
notices and as webhook prefixes. Rooms become active once 2+ servers have
joined, and new servers can slot in to existing active rooms up to the
max of 6.
"""

from __future__ import annotations

import asyncio
import io
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
# GifReportView lives in phonebooth; import lazily via bot.get_cog to avoid circular imports.

# ── Constants ─────────────────────────────────────────────────────────────────

# Station names used to identify servers — never their real names.
STATION_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
ROOM_MAX_SIZE = 6
ROOM_INACTIVITY_MINUTES = 15        # per-member idle → auto-removed
ROOM_QUEUE_TIMEOUT_MINUTES = 10     # waiting room dissolves if nobody joins

# Rate limiting — intentionally lax (people type fast).
RL_MSGS   = 10      # messages allowed within the window
RL_WINDOW = 15.0    # seconds
RL_WARNS  = 3       # warnings before auto-kick for rate limiting

# Flood detection — instant kick, no warning.
FLOOD_MSGS   = 20
FLOOD_WINDOW = 30.0

# Vote kick
VK_DURATION = 60    # seconds the voting window stays open
VK_COOLDOWN = 600   # seconds a kicked guild must wait before rejoining

# Matches custom Discord emojis — <:name:id> and <a:name:id> (animated).
# Stripped silently — they won't render in other servers.
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[a-zA-Z0-9_]+:[0-9]+>")

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
    name = f"Stranger {rng.choice(config.ANON_NAMES)}"
    avatar = f"https://robohash.org/{seed}?set=set4&size=256x256"
    return name, avatar


def _get_avatar_url(member: discord.Member | discord.User) -> str:
    if isinstance(member, discord.Member) and member.guild_avatar:
        asset = member.guild_avatar
    else:
        asset = member.display_avatar
    try:
        url = str(asset.with_static_format("png").with_size(256).url)
        return url.split("?")[0]
    except Exception:
        return str(member.default_avatar.url)


def _render_user_mentions(text: str, guild: discord.Guild | None) -> str:
    if not text or guild is None:
        return text

    def _replace(match: re.Match[str]) -> str:
        member = guild.get_member(int(match.group(1)))
        if member:
            return f"@{member.display_name}"
        return "@user"

    return MENTION_PATTERN.sub(_replace, text)


def _duration_str(started_at: str) -> str:
    delta = datetime.utcnow() - datetime.fromisoformat(started_at)
    total = int(delta.total_seconds())
    return f"{total // 60}m {total % 60}s"


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
    """Group room commands — up to 6 servers talking together."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db

        # Rate-limiting: channel_id → deque of monotonic timestamps
        self._msg_times:   dict[int, deque]   = {}
        # In-memory warn counts (reset when member leaves)
        self._warn_counts: dict[int, int]      = {}
        # Inactivity tasks per room-member channel
        self._inactivity_tasks: dict[int, asyncio.Task] = {}
        # Waiting-room timeout tasks per room_id
        self._waiting_tasks:    dict[int, asyncio.Task] = {}
        # Kick cooldowns: guild_id → monotonic expiry timestamp
        self._kick_cooldowns:   dict[int, float]        = {}
        # Active vote-kicks: room_id → VoteKickState
        self._active_votekicks: dict[int, VoteKickState] = {}

    def cog_unload(self) -> None:
        for t in self._inactivity_tasks.values():
            t.cancel()
        for t in self._waiting_tasks.values():
            t.cancel()

    # ── Rate-limit / flood detection ──────────────────────────────────────────

    def _check_rate(self, channel_id: int) -> tuple[bool, bool]:
        """Returns (is_flooding, is_rate_limited)."""
        now = time.monotonic()
        dq  = self._msg_times.setdefault(channel_id, deque())
        dq.append(now)
        # Prune anything older than the larger window
        cutoff = now - max(RL_WINDOW, FLOOD_WINDOW)
        while dq and dq[0] < cutoff:
            dq.popleft()
        flood_count = sum(1 for t in dq if now - t <= FLOOD_WINDOW)
        rl_count    = sum(1 for t in dq if now - t <= RL_WINDOW)
        return flood_count >= FLOOD_MSGS, rl_count >= RL_MSGS

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

    def _reset_inactivity(self, channel_id: int, room_id: int) -> None:
        old = self._inactivity_tasks.pop(channel_id, None)
        if old:
            old.cancel()
        self._inactivity_tasks[channel_id] = asyncio.create_task(
            self._inactivity_timer(channel_id, room_id)
        )

    def _cancel_inactivity(self, channel_id: int) -> None:
        t = self._inactivity_tasks.pop(channel_id, None)
        if t:
            t.cancel()

    async def _inactivity_timer(self, channel_id: int, room_id: int) -> None:
        await asyncio.sleep(ROOM_INACTIVITY_MINUTES * 60)
        rm = await self.db.get_room_member(channel_id)
        if not rm or rm["room_id"] != room_id:
            return
        await self._remove_member(
            channel_id, room_id,
            broadcast_reason=f"📵 **Station {rm['station']}** was removed for inactivity.",
            notify_leaver=True,
            leaver_msg=(
                f"📵 You were removed from the room due to **{ROOM_INACTIVITY_MINUTES} minutes** "
                f"of inactivity. Use `f.room` to join a new one!"
            ),
        )

    # ── Waiting-room timeout ──────────────────────────────────────────────────

    def _start_waiting_timeout(self, room_id: int) -> None:
        self._cancel_waiting_timeout(room_id)
        self._waiting_tasks[room_id] = asyncio.create_task(
            self._waiting_timer(room_id)
        )

    def _cancel_waiting_timeout(self, room_id: int) -> None:
        t = self._waiting_tasks.pop(room_id, None)
        if t:
            t.cancel()

    async def _waiting_timer(self, room_id: int) -> None:
        """Dissolve a waiting room if nobody else joins in time."""
        await asyncio.sleep(ROOM_QUEUE_TIMEOUT_MINUTES * 60)
        room = await self.db.get_room_by_id(room_id)
        if not room or room["status"] != "waiting":
            return
        for m in await self.db.get_room_members(room_id):
            ch = self.bot.get_channel(m["channel_id"])
            if ch:
                try:
                    await ch.send(
                        f"📵 No other servers joined within **{ROOM_QUEUE_TIMEOUT_MINUTES} minutes**. "
                        f"Use `f.room` to try again!"
                    )
                except discord.HTTPException:
                    pass
            await self.db.remove_room_member(m["channel_id"])
            self._cancel_inactivity(m["channel_id"])
        await self.db.close_room(room_id)

    # ── Core: remove a member and handle room collapse ────────────────────────

    async def _remove_member(
        self,
        channel_id: int,
        room_id: int,
        *,
        broadcast_reason: str,
        notify_leaver: bool = True,
        leaver_msg: str = "📵 You have been removed from the room. Use `f.room` to join a new one!",
    ) -> None:
        """
        Remove one member, clean up their in-memory state, notify the room,
        and collapse the room if fewer than 2 servers remain.
        """
        row = await self.db.remove_room_member(channel_id)
        if not row:
            return

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
                            "Use `f.room` to start a new one!"
                        )
                    except discord.HTTPException:
                        pass
                await self.db.remove_room_member(rm["channel_id"])
                self._cancel_inactivity(rm["channel_id"])
            await self.db.close_room(room_id)
            self._cancel_waiting_timeout(room_id)
            return

        # Broadcast departure to remaining members
        await self._broadcast(
            room_id,
            embed=discord.Embed(
                description=f"{broadcast_reason} ({len(remaining)}/6 in room)",
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
                            f"You can rejoin rooms after a **10-minute cooldown**."
                        ),
                        color=config.COLOR_ERR,
                    )
                )
            except discord.HTTPException:
                pass

        # Remove from DB + clean up
        await self.db.remove_room_member(state.target_channel)
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
                            "Use `f.room` to start a new one!"
                        )
                    except discord.HTTPException:
                        pass
                await self.db.remove_room_member(rm["channel_id"])
                self._cancel_inactivity(rm["channel_id"])
            await self.db.close_room(state.room_id)
            return

        await self._broadcast(
            state.room_id,
            embed=discord.Embed(
                description=(
                    f"🔨 **Station {state.target_station}** was removed by {reason_str}. "
                    f"({len(remaining)}/6 in room)"
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
    ) -> Optional[discord.WebhookMessage]:
        """Send via webhook. Returns the WebhookMessage if wait=True, else None."""
        try:
            session = getattr(self.bot, "http_session", None)
            _own_session = session is None or session.closed
            if _own_session:
                session = aiohttp.ClientSession()
            try:
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
            finally:
                if _own_session:
                    await session.close()
            return msg if wait else None
        except Exception as exc:
            print(f"[room-relay] {exc}")
            return None

    # ── Message relay (one sender → all others) ───────────────────────────────

    async def _relay_to_room(
        self,
        message: discord.Message,
        room: dict,
        member: dict,
        members: list[dict],
    ) -> None:
        # ── User ban check ────────────────────────────────────────────────────
        if await self.db.is_user_banned(message.author.id):
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention} You are banned from using Fliphone.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass
            return

        # ── Rate limit / flood check ──────────────────────────────────────────
        is_flooded, is_rate_limited = self._check_rate(message.channel.id)

        if is_flooded:
            # Instant auto-kick, no vote needed
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

        if is_rate_limited:
            warn = self._warn_counts.get(message.channel.id, 0) + 1
            self._warn_counts[message.channel.id] = warn
            try:
                await message.channel.send(
                    f"⚠️ Slow down — you're sending messages too fast. "
                    f"(Warning **{warn}/{RL_WARNS}**)",
                    delete_after=6,
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

        # ── Identity ──────────────────────────────────────────────────────────
        cfg  = await self.db.get_config_by_channel(message.channel.id)
        anon = cfg.get("anonymous", 0) if cfg else 0
        if anon:
            seed = room["id"] * 1000 + member["guild_id"]
            display_name, avatar_url = _anon_identity(seed)
        else:
            author       = message.author
            display_name = (
                author.nick
                if isinstance(author, discord.Member) and author.nick
                else author.display_name
            )
            avatar_url = _get_avatar_url(author)

        # Webhook username always shows station so servers are identifiable.
        webhook_name = f"Station {member['station']} · {display_name}"

        # ── Reply embed ───────────────────────────────────────────────────────
        reply_embed: Optional[discord.Embed] = None
        if message.reference:
            ref_msg = message.reference.resolved
            if isinstance(ref_msg, discord.Message):
                ref_author = ref_msg.author.display_name
                try:
                    ref_avatar = str(
                        ref_msg.author.display_avatar.with_static_format("png").with_size(64).url
                    ).split("?")[0]
                except Exception:
                    ref_avatar = None
                ref_text = (ref_msg.content or "").strip()
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
                ref_text = _render_user_mentions(ref_text, message.guild)
                reply_embed = discord.Embed(description=ref_text, color=0x5865F2)
                reply_embed.set_author(name=f"Replying to {ref_author}", icon_url=ref_avatar)

        # ── Content filter ────────────────────────────────────────────────────
        raw = message.content or ""
        if message.stickers:
            raw = (raw + "\n🎭 *Sticker: " + ", ".join(s.name for s in message.stickers) + "*").strip()

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

        # ── Strip custom emojis silently ─────────────────────────────────────
        content = CUSTOM_EMOJI_PATTERN.sub("", content).strip()
        content = _render_user_mentions(content, message.guild)

        # ── Strip non-GIF links ───────────────────────────────────────────────
        # Collect allowed GIF URLs first, then remove all other URLs from content.
        inline_gif_urls: list[str] = GIF_LINK_PATTERN.findall(content)
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
        GIF_EXT    = {".gif"}
        BLOCK_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".avi", ".mkv"}
        file_bytes: list[tuple[bytes, str]] = []   # (data, filename)
        total_bytes = 0
        LIMIT = 8_000_000
        attachment_gif_urls: list[str] = []

        for att in message.attachments:
            ext = ("." + att.filename.rsplit(".", 1)[-1].lower()) if "." in att.filename else ""
            if ext in GIF_EXT:
                content += f"\n{att.url}"
                attachment_gif_urls.append(att.url)
            elif ext in BLOCK_EXTS:
                pass  # images/videos blocked in rooms same as 1:1 calls
            elif total_bytes + att.size <= LIMIT:
                try:
                    file_bytes.append((await att.read(), att.filename))
                    total_bytes += att.size
                except Exception:
                    pass

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
        if all_gif_urls:
            gif_statuses = await asyncio.gather(
                *[self.db.check_gif_url(u) for u in all_gif_urls],
                return_exceptions=True,
            )
            blocked_count = 0
            for gif_url, status in zip(all_gif_urls, gif_statuses):
                if status == "blacklist":
                    content = content.replace(gif_url, "")
                    all_gif_urls.remove(gif_url)
                    blocked_count += 1
            if blocked_count > 0:
                try:
                    await message.channel.send(
                        f"🚫 {message.author.mention} {blocked_count} blocked GIF(s) were removed.",
                        delete_after=8,
                    )
                except discord.HTTPException:
                    pass

        # ── Counters + inactivity reset ───────────────────────────────────────
        await self.db.increment_room_msg_count(room["id"])
        await self.db.increment_room_member_msg_count(message.channel.id)
        self._reset_inactivity(message.channel.id, room["id"])

        # ── Resolve GifReportView once for the whole relay ───────────────────
        import sys as _sys
        _pb_mod = _sys.modules.get("cogs.phonebooth")
        GifReportView = getattr(_pb_mod, "GifReportView", None) if _pb_mod else None

        # ── Pre-fetch all GIF modes for all recipients in parallel ────────────
        members_without_self = [m for m in members if m["channel_id"] != message.channel.id]
        if members_without_self:
            gif_modes_list = await asyncio.gather(
                *[self.db.get_gif_mode(m["guild_id"]) for m in members_without_self],
                return_exceptions=True,
            )
            members_gif_modes = {
                m["guild_id"]: (gif_modes_list[i] or "").lower()
                for i, m in enumerate(members_without_self)
            }
        else:
            members_gif_modes = {}

        # ── Relay to every other member ───────────────────────────────────────
        for other in members_without_self:
            wh_url = other.get("webhook_url")
            other_ch = self.bot.get_channel(other["channel_id"])

            # Apply GIF mode per recipient server so each station receives
            # content filtered to its own policy.
            recipient_content = content
            recipient_gif_urls = list(all_gif_urls)
            recipient_gif_mode = members_gif_modes.get(other["guild_id"], "").lower()
            if recipient_gif_mode == "disabled":
                for u in recipient_gif_urls:
                    recipient_content = recipient_content.replace(u, "")
                recipient_gif_urls = []
            elif recipient_gif_mode == "limited":
                safe_urls = [
                    u for u in recipient_gif_urls
                    if "tenor.com" in u.lower() or "giphy.com" in u.lower() or "klipy.com" in u.lower()
                ]
                for u in [u for u in recipient_gif_urls if u not in safe_urls]:
                    recipient_content = recipient_content.replace(u, "")
                recipient_gif_urls = safe_urls

            recipient_text_content = recipient_content.strip() or None

            # Fresh file copies for each recipient (file pointers are single-use)
            send_files = [
                discord.File(io.BytesIO(data), filename=fname)
                for data, fname in file_bytes
            ]

            sent_msg_id: Optional[int] = None

            if wh_url:
                wh_msg = await self._send_webhook(
                    wh_url,
                    recipient_text_content,
                    webhook_name,
                    avatar_url,
                    send_files,
                    embed=reply_embed,
                    wait=bool(recipient_gif_urls),
                    silent=bool(recipient_gif_urls),
                )
                if wh_msg:
                    sent_msg_id = wh_msg.id
            elif other_ch:
                body = recipient_text_content or ""
                try:
                    sent = await other_ch.send(
                        content=f"**{webhook_name}**\n{body}" if body else f"**{webhook_name}**",
                        embed=reply_embed if reply_embed else discord.utils.MISSING,
                        files=send_files if send_files else discord.utils.MISSING,
                        allowed_mentions=discord.AllowedMentions.none(),
                        silent=bool(recipient_gif_urls),
                    )
                    sent_msg_id = sent.id
                except discord.HTTPException:
                    continue

            # ── GIF report cards (batched checks, not one-by-one) ──────────────
            if recipient_gif_urls and other_ch:
                # Check whitelist status for all GIFs in parallel
                gif_statuses = await asyncio.gather(
                    *[self.db.check_gif_url(u) for u in recipient_gif_urls],
                    return_exceptions=True,
                )
                # Create report tasks for non-whitelisted GIFs
                report_tasks = []
                for gif_url, status in zip(recipient_gif_urls, gif_statuses):
                    if status == "whitelist":
                        continue
                    async def _create_report(url=gif_url):
                        try:
                            report_id = await self.db.add_gif_report(
                                url=url,
                                msg_id=sent_msg_id,
                                channel_id=other["channel_id"],
                                guild_id=other["guild_id"],
                            )
                            report_embed = discord.Embed(
                                title="🚩 GIF Safety Check",
                                description=(
                                    "A GIF was sent in this room.\n"
                                    "If it contains inappropriate content, tap the button.\n"
                                    "It will be **immediately removed** and flagged for review.\n"
                                    "*(Verified safe GIFs cannot be reported.)*"
                                ),
                                color=0x2b2d31,
                            )
                            report_embed.set_footer(text=f"Report #{report_id} • {config.FOOTER}")
                            if GifReportView:
                                await other_ch.send(embed=report_embed, view=GifReportView())
                            else:
                                await other_ch.send(embed=report_embed)
                        except discord.HTTPException:
                            pass
                        except Exception as e:
                            print(f"ERROR: GIF report failed: {e}")
                    report_tasks.append(_create_report())
                
                if report_tasks:
                    await asyncio.gather(*report_tasks, return_exceptions=True)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        rm = await self.db.get_room_member(message.channel.id)
        if not rm:
            return
        room = await self.db.get_room_by_id(rm["room_id"])
        if not room or room["status"] != "active":
            return
        members = await self.db.get_room_members(rm["room_id"])
        await self._relay_to_room(message, room, rm, members)

    # ── Internal join logic (shared by f.room and f.roomskip) ────────────────

    async def _do_join(self, ctx: commands.Context) -> None:
        """Core join logic — find or create a room for this channel."""
        if await self.db.is_user_banned(ctx.author.id):
            await ctx.send("🚫 You are banned from using Fliphone.")
            return

        cfg = await self.db.get_guild_config(ctx.guild.id)
        if not cfg:
            await ctx.send(
                "❌ Fliphone isn't set up in this server. An admin should run `f.setup` first."
            )
            return

        if await self.db.get_connection(ctx.channel.id):
            await ctx.send(
                "📞 This channel is in an active 1:1 call. Use `f.hangup` first."
            )
            return

        if await self.db.get_room_member(ctx.channel.id):
            await ctx.send("📡 Already in a room! Use `f.roomleave` to leave first.")
            return

        # Kick-cooldown check
        expiry = self._kick_cooldowns.get(ctx.guild.id, 0)
        if time.monotonic() < expiry:
            secs = int(expiry - time.monotonic())
            await ctx.send(
                f"⏳ Your server is on a room cooldown for another "
                f"**{secs // 60}m {secs % 60}s**."
            )
            return

        wh_url = await self.get_or_create_webhook(ctx.channel)

        # ── Try to slot into an existing room ─────────────────────────────────
        room = await self.db.get_available_room(ctx.guild.id)
        if room:
            used    = await self.db.get_used_stations(room["id"])
            station = next((s for s in STATION_NAMES if s not in used), None)
            if station is None:
                room = None  # race condition — all slots taken; fall through

        if room:
            await self.db.add_room_member(
                room_id=room["id"],
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
                webhook_url=wh_url,
                station=station,
            )
            count = await self.db.get_room_member_count(room["id"])

            # Promote waiting → active once a second server joins
            if room["status"] == "waiting" and count >= 2:
                await self.db.activate_room(room["id"])
                self._cancel_waiting_timeout(room["id"])

            self._reset_inactivity(ctx.channel.id, room["id"])

            # Notify every member
            all_members = await self.db.get_room_members(room["id"])
            for m in all_members:
                ch = self.bot.get_channel(m["channel_id"])
                if not ch:
                    continue
                try:
                    if m["channel_id"] == ctx.channel.id:
                        # Greet the new arrival
                        others = [
                            f"**Station {x['station']}**"
                            for x in all_members
                            if x["channel_id"] != ctx.channel.id
                        ]
                        others_str = ", ".join(others) if others else "nobody yet"
                        await ch.send(
                            embed=discord.Embed(
                                title=f"📡 You joined as Station {station}!",
                                description=(
                                    f"There are **{count}** server(s) here right now: {others_str}.\n\n"
                                    "Say hello! 👋\n"
                                    "**Tips:**\n"
                                    "• `f.roomstatus` — see who's in the room\n"
                                    "• `f.roomkick <station>` — start a vote to remove a station\n"
                                    "• `f.roomleave` — leave quietly  ·  `f.roomskip` — skip to a new room\n\n"
                                    "*By continuing you agree to be respectful.*"
                                ),
                                color=config.COLOR_OK,
                            )
                        )
                    else:
                        await ch.send(
                            embed=discord.Embed(
                                description=f"📡 **Station {station}** has joined the room! ({count}/6)",
                                color=config.COLOR_OK,
                            )
                        )
                except discord.HTTPException:
                    pass
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
        self._reset_inactivity(ctx.channel.id, room_id)
        self._start_waiting_timeout(room_id)

        await ctx.send(
            embed=discord.Embed(
                title="📡 Room Created — Waiting for others…",
                description=(
                    "You joined as **Station Alpha**!\n"
                    f"Waiting for up to **{ROOM_QUEUE_TIMEOUT_MINUTES} minutes** for other servers.\n"
                    "When a 2nd server joins, the room goes live automatically.\n\n"
                    "Use `f.roomleave` to cancel."
                ),
                color=config.COLOR_WAIT,
            )
        )

    # ── f.room ────────────────────────────────────────────────────────────────

    @commands.command(name="room", aliases=["r"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def room(self, ctx: commands.Context) -> None:
        """Join a group room of up to 6 servers."""
        await self._do_join(ctx)

    # ── f.roomleave ───────────────────────────────────────────────────────────

    @commands.command(name="roomleave", aliases=["rl"])
    @commands.guild_only()
    async def roomleave(self, ctx: commands.Context) -> None:
        """Leave the current room without re-queuing."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `f.room` to join one!")
            return
        await self._remove_member(
            ctx.channel.id,
            rm["room_id"],
            broadcast_reason=f"📡 **Station {rm['station']}** has left the room.",
            notify_leaver=False,
        )
        await ctx.send(
            "📵 You left the room. Use `f.room` to join another one!"
        )

    # ── f.roomskip ────────────────────────────────────────────────────────────

    @commands.command(name="roomskip", aliases=["rs"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def roomskip(self, ctx: commands.Context) -> None:
        """Leave the current room and immediately search for a new one."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `f.room` to join one!")
            return
        await self._remove_member(
            ctx.channel.id,
            rm["room_id"],
            broadcast_reason=f"📡 **Station {rm['station']}** has left the room.",
            notify_leaver=False,
        )
        await ctx.send("⏭️ Skipping to a new room…")
        # Directly invoke the join logic (no cooldown hit since we call internal method)
        await self._do_join(ctx)

    # ── f.roomstatus ─────────────────────────────────────────────────────────

    @commands.command(name="roomstatus", aliases=["rst"])
    @commands.guild_only()
    async def roomstatus(self, ctx: commands.Context) -> None:
        """Show current room info."""
        rm = await self.db.get_room_member(ctx.channel.id)
        if not rm:
            await ctx.send("📡 You're not in a room. Use `f.room` to join one!")
            return
        room    = await self.db.get_room_by_id(rm["room_id"])
        members = await self.db.get_room_members(rm["room_id"])

        stations = "  ·  ".join(
            f"**Station {m['station']}**" + (" *(you)*" if m["channel_id"] == ctx.channel.id else "")
            for m in members
        )
        embed = discord.Embed(title="📡 Room Status", color=config.COLOR_OK)
        embed.add_field(name="Your Station", value=f"Station {rm['station']}", inline=True)
        embed.add_field(name="Servers",      value=f"{len(members)}/6",         inline=True)
        embed.add_field(name="Status",       value=room["status"].capitalize(),  inline=True)
        embed.add_field(name="Duration",     value=_duration_str(room["created_at"]), inline=True)
        embed.add_field(name="Messages",     value=str(room["msg_count"]),       inline=True)
        embed.add_field(name="Stations",     value=stations, inline=False)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    # ── f.roomkick ────────────────────────────────────────────────────────────

    @commands.command(name="roomkick", aliases=["rk"])
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def roomkick(self, ctx: commands.Context, *, station_name: str = "") -> None:
        """
        Start a majority vote to kick a station from the room.
        Usage: f.roomkick <station>   e.g. f.roomkick Bravo
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
                f"Usage: `f.roomkick <Station>`"
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
