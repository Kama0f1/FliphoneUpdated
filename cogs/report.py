"""
cogs/report.py - General call/conversation report system for Fliphone.

Commands
--------
/report                     - Report your most recent or active call
/userreports                - List open call reports [owner + trusted mods]
/resolvereport              - Mark a report as resolved [owner + trusted mods]

How it works
------------
When a report is submitted:
  1. The reporter provides a reason and optionally attaches media.
  2. The bot looks up who they were connected to via active connection,
     the Phonebooth cog's last_calls cache, or call_history in the DB.
  3. The full eligible call window is fetched on demand from the reporting Discord channel.
  4. The context is sent to the private report channel and discarded locally.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
from access_control import is_trusted_moderator, trusted_moderator_only
from database import Database
from relay_policy import is_local_only

# Channel to send report log embeds to
REPORT_LOG_CHANNEL_ID = int(os.getenv("USER_REPORT_LOG_CHANNEL_ID", 1497205915089371186))

REPORT_TRANSCRIPT_CHUNK_BYTES = 7_500_000
REPORT_EVIDENCE_RETENTION_DAYS = 30
REPORT_VIEWER_PAGE_SIZE = 5

log = logging.getLogger("fliphone")


# ── Report Modal (slash command) ──────────────────────────────────────────────

class ReportModal(discord.ui.Modal, title="Report a Call"):
    reason = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe the issue — harassment, slurs, NSFW content, etc.",
        min_length=10,
        max_length=500,
    )
    media_url = discord.ui.TextInput(
        label="Media link (optional)",
        style=discord.TextStyle.short,
        placeholder="Paste an image or video link as evidence",
        required=False,
        max_length=500,
    )

    def __init__(self, cog: "Report", station: Optional[str] = None) -> None:
        super().__init__()
        self.cog = cog
        self.station = station

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog._process_report(
            interaction=interaction,
            guild=interaction.guild,
            channel_id=interaction.channel_id,
            user=interaction.user,
            reason=self.reason.value.strip(),
            media_url=self.media_url.value.strip() or None,
            attachments=[],
            station=self.station,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class UserReportSelect(discord.ui.Select):
    def __init__(
        self,
        view: "UserReportPanelView",
        reports: list[dict],
        selected_report_id: Optional[int] = None,
    ) -> None:
        self.panel_view = view
        options = []
        for report in reports[:25]:
            label = f"Report #{report['id']}"
            description = (report["reason"] or "")[:90]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(report["id"]),
                    description=description,
                    default=int(report["id"]) == selected_report_id,
                )
            )
        super().__init__(placeholder="Choose a call report", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel_view.selected_report_id = int(self.values[0])
        report = self.panel_view.get_selected_report()
        if not report:
            await interaction.response.send_message(
                "That report is no longer open. Refresh the panel.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=self.panel_view.cog._build_userreport_detail_embed(report),
            view=UserReportPanelView(
                self.panel_view.cog,
                self.panel_view.reports,
                selected_report_id=self.panel_view.selected_report_id,
            ),
        )


class UserReportPanelView(discord.ui.View):
    def __init__(
        self,
        cog: "Report",
        reports: list[dict],
        selected_report_id: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.reports = reports
        self.selected_report_id = selected_report_id
        if reports:
            self.add_item(UserReportSelect(self, reports, selected_report_id))

    def get_selected_report(self) -> Optional[dict]:
        if self.selected_report_id is None:
            return None
        return next(
            (
                report
                for report in self.reports
                if int(report["id"]) == self.selected_report_id
            ),
            None,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        admin_cog = interaction.client.get_cog("Admin")
        if admin_cog and await admin_cog._is_global_mod(interaction.user, interaction.guild):
            return True
        await interaction.response.send_message("You do not have permission to use this panel.", ephemeral=True)
        return False

    async def _refresh(self, interaction: discord.Interaction, message: str | None = None) -> None:
        reports = await self.cog.db.get_open_call_reports()
        selected = next(
            (
                report
                for report in reports
                if int(report["id"]) == self.selected_report_id
            ),
            None,
        )
        embed = (
            self.cog._build_userreport_detail_embed(selected)
            if selected
            else self.cog._build_userreports_embed(reports)
        )
        view = (
            UserReportPanelView(
                self.cog,
                reports,
                selected_report_id=int(selected["id"]) if selected else None,
            )
            if reports
            else None
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if message:
            await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Resolve", style=discord.ButtonStyle.success)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected_report_id:
            await interaction.response.send_message("No call report selected.", ephemeral=True)
            return
        await interaction.response.defer()
        report = await self.cog.db.get_call_report(self.selected_report_id)
        success = await self.cog.db.resolve_call_report(self.selected_report_id)
        if not success:
            await self._refresh(interaction, "That report was already resolved or no longer exists.")
            return
        if report:
            await self.cog._delete_call_report_review_message(report)
        await self._refresh(interaction, f"Call report #{self.selected_report_id} resolved.")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._refresh(interaction)


class ReportConversationButton(discord.ui.Button):
    def __init__(self, cog: "Report", report_id: int) -> None:
        super().__init__(
            label="Open Conversation",
            style=discord.ButtonStyle.primary,
            custom_id=f"report:conversation:{report_id}",
        )
        self.cog = cog
        self.report_id = report_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog._open_conversation_viewer(interaction, self.report_id)


class ReportConversationLaunchView(discord.ui.View):
    def __init__(self, cog: "Report", report_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ReportConversationButton(cog, report_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await is_trusted_moderator(interaction.client, interaction.user):
            return True
        await interaction.response.send_message(
            "You do not have permission to view report evidence.",
            ephemeral=True,
        )
        return False


class ReportConversationPager(discord.ui.View):
    def __init__(self, cog: "Report", report_id: int, entries: list[dict], user_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.report_id = report_id
        self.entries = entries
        self.user_id = user_id
        self.pages = self._paginate(entries)
        self.page = 0
        self.page_count = len(self.pages)
        self._update_buttons()

    @staticmethod
    def _paginate(entries: list[dict]) -> list[list[tuple[int, dict]]]:
        pages: list[list[tuple[int, dict]]] = []
        current: list[tuple[int, dict]] = []
        current_chars = 0
        for position, entry in enumerate(entries, start=1):
            estimated_chars = min(len(str(entry.get("content") or "")), 3_900) + 250
            if current and (
                len(current) >= REPORT_VIEWER_PAGE_SIZE
                or current_chars + estimated_chars > 5_500
            ):
                pages.append(current)
                current = []
                current_chars = 0
            current.append((position, entry))
            current_chars += estimated_chars
        if current:
            pages.append(current)
        return pages or [[]]

    def _update_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def render(self) -> tuple[str, list[discord.Embed]]:
        page_entries = self.pages[self.page]
        embeds = [
            self.cog._build_conversation_message_embed(
                entry,
                report_id=self.report_id,
                position=position,
                total=len(self.entries),
            )
            for position, entry in page_entries
        ]
        return (
            f"**Report #{self.report_id} conversation** | "
            f"Page {self.page + 1}/{self.page_count} | {len(self.entries)} messages",
            embeds,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This private conversation viewer belongs to another moderator.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._update_buttons()
        content, embeds = self.render()
        await interaction.response.edit_message(content=content, embeds=embeds, view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._update_buttons()
        content, embeds = self.render()
        await interaction.response.edit_message(content=content, embeds=embeds, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Conversation viewer closed.",
            embeds=[],
            view=None,
        )


class Report(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db

    async def cog_load(self) -> None:
        for report in await self.db.get_open_call_reports():
            message_id = report.get("review_msg_id")
            if message_id:
                self.bot.add_view(
                    ReportConversationLaunchView(self, int(report["id"])),
                    message_id=int(message_id),
                )
        self._cleanup_expired_reports.start()

    def cog_unload(self) -> None:
        self._cleanup_expired_reports.cancel()

    async def _delete_call_report_review_message(self, report: dict) -> None:
        channel_id = report.get("review_channel_id")
        if not channel_id:
            return
        message_ids: list[int] = []
        stored_ids = str(report.get("review_message_ids") or "")
        for value in stored_ids.split(","):
            try:
                message_ids.append(int(value.strip()))
            except (TypeError, ValueError):
                continue
        legacy_message_id = report.get("review_msg_id")
        if legacy_message_id and int(legacy_message_id) not in message_ids:
            message_ids.insert(0, int(legacy_message_id))
        if not message_ids:
            return
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            return
        for message_id in message_ids:
            try:
                await channel.get_partial_message(message_id).delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                continue

    # ── Private review evidence retention ─────────────────────────────────────

    @tasks.loop(hours=6)
    async def _cleanup_expired_reports(self) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=REPORT_EVIDENCE_RETENTION_DAYS)).isoformat()
        for report in await self.db.get_expired_call_reports(cutoff):
            if await self.db.expire_call_report(int(report["id"])):
                await self._delete_call_report_review_message(report)

    @_cleanup_expired_reports.before_loop
    async def _before_cleanup_expired_reports(self) -> None:
        await self.bot.wait_until_ready()

    # ── Find the call to report against ──────────────────────────────────────

    async def _get_reportable_call(
        self, guild_id: int, channel_id: int, station: Optional[str] = None
    ) -> Optional[dict]:
        """
        Returns a dict with other_guild_id, started_at, ended_at, active, conn_id.
        Checks active connection first, then last_calls cache, then call_history DB.
        """
        # 1. Active call right now
        conn = await self.db.get_connection(channel_id)
        if conn:
            is_a = channel_id == conn["channel_a"]
            return {
                "other_guild_id": conn["guild_b"] if is_a else conn["guild_a"],
                "started_at":     conn["started_at"],
                "ended_at":       None,
                "active":         True,
                "conn_id":        conn["id"],
            }

        room_member = await self.db.get_room_member(channel_id)
        if room_member:
            if not station:
                return {"needs_station": True}
            members = await self.db.get_room_members(room_member["room_id"])
            wanted = station.strip().capitalize()
            target = next((item for item in members if item["station"] == wanted), None)
            if not target or target["channel_id"] == channel_id:
                return {"invalid_station": True}
            room = await self.db.get_room_by_id(room_member["room_id"])
            return {
                "other_guild_id": target["guild_id"],
                "started_at": room_member.get("joined_at") or (room["created_at"] if room else None),
                "ended_at": None,
                "active": True,
                "conn_id": -int(room_member["room_id"]),
                "station_guild_ids": {
                    str(item["station"]): int(item["guild_id"])
                    for item in members
                },
            }

        # 2. In-memory cache — survives skips and instant hangups
        pb = self.bot.get_cog("Phonebooth")
        if pb and hasattr(pb, "_last_calls"):
            cached = pb._last_calls.get(guild_id)
            if cached:
                return cached

        # 3. DB call history — survives restarts
        recent = await self.db.get_recent_call_for_guild(guild_id)
        if recent:
            return {
                "other_guild_id": recent["guild_b"] if recent["guild_a"] == guild_id else recent["guild_a"],
                "started_at":     recent["started_at"],
                "ended_at":       recent["ended_at"],
                "active":         False,
                "conn_id":        None,
            }

        return None

    @staticmethod
    def _parse_utc(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _fetch_report_context(
        self,
        channel_id: int,
        call: dict,
        reporting_guild_id: int,
    ) -> list[dict]:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return []
        if not hasattr(channel, "history"):
            return []

        started_at = self._parse_utc(call.get("started_at"))
        ended_at = self._parse_utc(call.get("ended_at"))
        history_kwargs: dict[str, object] = {
            "limit": None,
            "oldest_first": True,
        }
        if started_at:
            history_kwargs["after"] = started_at - timedelta(seconds=1)
        if ended_at:
            history_kwargs["before"] = ended_at + timedelta(seconds=1)

        try:
            messages = [message async for message in channel.history(**history_kwargs)]
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            return []
        local_user_ids = {
            int(message.author.id)
            for message in messages
            if message.webhook_id is None
            and not message.author.bot
            and (not self.bot.user or message.author.id != self.bot.user.id)
        }
        opted_out_results = await asyncio.gather(
            *(self.db.is_content_opted_out(user_id) for user_id in local_user_ids),
            return_exceptions=True,
        )
        opted_out_ids = {
            user_id
            for user_id, result in zip(local_user_ids, opted_out_results)
            if result is True
        }

        entries: list[dict] = []
        for message in messages:
            is_webhook = message.webhook_id is not None
            if not is_webhook and message.author.bot:
                continue
            if not is_webhook and int(message.author.id) in opted_out_ids:
                continue

            content = message.content or ""
            if is_local_only(content):
                continue
            attachment_lines = [
                f"[attachment: {attachment.filename}] {attachment.url}"
                for attachment in message.attachments
            ]
            sticker_lines = [
                f"[sticker: {sticker.name}] {sticker.url}"
                for sticker in getattr(message, "stickers", [])
            ]
            embed_lines: list[str] = []
            for embed in getattr(message, "embeds", []):
                embed_author = getattr(getattr(embed, "author", None), "name", None)
                embed_footer = getattr(getattr(embed, "footer", None), "text", None)
                parts = [embed_author, embed.title, embed.description, embed.url]
                parts.extend(
                    f"{field.name}: {field.value}" for field in getattr(embed, "fields", [])
                )
                if embed_footer:
                    parts.append(embed_footer)
                image_url = getattr(getattr(embed, "image", None), "url", None)
                if image_url:
                    parts.append(image_url)
                embed_text = " | ".join(str(part).strip() for part in parts if part)
                if embed_text:
                    embed_lines.append(f"[embed] {embed_text}")
            reaction_parts = [
                f"{reaction.emoji} x{reaction.count}"
                for reaction in getattr(message, "reactions", [])
            ]
            reaction_lines = [f"[reactions] {', '.join(reaction_parts)}"] if reaction_parts else []
            reference = getattr(message, "reference", None)
            reference_id = getattr(reference, "message_id", None)
            reply_lines = [f"[reply to Discord message {reference_id}]"] if reference_id else []
            combined = "\n".join(
                part
                for part in (
                    content.strip(),
                    *reply_lines,
                    *attachment_lines,
                    *sticker_lines,
                    *embed_lines,
                    *reaction_lines,
                )
                if part
            )
            if not combined:
                continue

            username = str(getattr(message.author, "display_name", message.author))
            display_avatar = getattr(message.author, "display_avatar", None)
            avatar_url = str(getattr(display_avatar, "url", "") or "")
            webhook_guild_id = int(call["other_guild_id"])
            webhook_guild_name = "Relayed side"
            station_guild_ids = call.get("station_guild_ids") or {}
            if is_webhook and station_guild_ids:
                station = username.split(" ", 2)[1] if username.startswith("Station ") else None
                webhook_guild_id = int(station_guild_ids.get(station, 0))
                webhook_guild_name = f"Station {station}" if station else "Unknown room station"

            entries.append(
                {
                    "user_id": None if is_webhook else int(message.author.id),
                    "username": username,
                    "guild_id": (
                        webhook_guild_id
                        if is_webhook
                        else int(reporting_guild_id)
                    ),
                    "guild_name": webhook_guild_name if is_webhook else "Reporting side",
                    "avatar_url": avatar_url,
                    "timestamp": message.created_at.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "content": combined,
                }
            )
        return entries

    @staticmethod
    def _split_transcript(text: str, max_bytes: int = REPORT_TRANSCRIPT_CHUNK_BYTES) -> list[bytes]:
        """Split a transcript on line boundaries into valid UTF-8 Discord attachments."""
        if max_bytes < 4:
            raise ValueError("max_bytes must be at least 4")
        chunks: list[bytes] = []
        current = bytearray()
        for line in text.splitlines(keepends=True):
            encoded = line.encode("utf-8")
            if len(encoded) > max_bytes:
                if current:
                    chunks.append(bytes(current))
                    current.clear()
                while encoded:
                    boundary = min(max_bytes, len(encoded))
                    while boundary > 0:
                        try:
                            encoded[:boundary].decode("utf-8")
                            break
                        except UnicodeDecodeError:
                            boundary -= 1
                    if boundary == 0:
                        raise ValueError("unable to split transcript at a UTF-8 boundary")
                    chunks.append(encoded[:boundary])
                    encoded = encoded[boundary:]
                continue
            if current and len(current) + len(encoded) > max_bytes:
                chunks.append(bytes(current))
                current.clear()
            current.extend(encoded)
        if current or not chunks:
            chunks.append(bytes(current))
        return chunks

    @staticmethod
    def _entry_side(entry: dict, reported_guild_id: int, reporting_guild_id: int) -> str:
        if int(entry["guild_id"]) == int(reported_guild_id):
            return "REPORTED"
        if int(entry["guild_id"]) == int(reporting_guild_id):
            return "REPORTING"
        return "OTHER ROOM STATION"

    def _build_transcript(
        self,
        *,
        report_id: int,
        reason: str,
        reported_name: str,
        reported_guild_id: int,
        reporting_name: str,
        reporting_guild_id: int,
        entries: list[dict],
    ) -> str:
        header = (
            f"FLIPHONE CALL REPORT #{report_id}\n"
            f"Reason: {' '.join(reason.split())}\n"
            f"Reported side: {reported_name} (server {reported_guild_id})\n"
            f"Reporting side: {reporting_name} (server {reporting_guild_id})\n"
            "Legend: REPORTED = the selected side; REPORTING = the side that filed it; "
            "OTHER ROOM STATION = another participant included for context.\n"
            + ("=" * 72)
        )
        blocks: list[str] = []
        for index, entry in enumerate(entries, start=1):
            username = " ".join(str(entry.get("username") or "Unknown").split())
            user_id = str(entry.get("user_id") or "unavailable")
            timestamp = str(entry.get("timestamp") or "unknown").replace("\n", " ")
            content = str(entry.get("content") or "").replace("\r\n", "\n").replace("\r", "\n")
            blocks.append(
                f"----- FLIPHONE MESSAGE {index:06d} -----\n"
                f"Time: {timestamp} UTC\n"
                f"Side: {self._entry_side(entry, reported_guild_id, reporting_guild_id)}\n"
                f"Speaker: {username}\n"
                f"User ID: {user_id}\n"
                f"Message Length: {len(content)}\n"
                f"Message:\n{content}"
            )
        return f"{header}\n\n" + "\n\n".join(blocks)

    def _build_viewer_data(
        self,
        entries: list[dict],
        reported_guild_id: int,
        reporting_guild_id: int,
    ) -> str:
        lines: list[str] = []
        for entry in entries:
            lines.append(
                json.dumps(
                    {
                        "timestamp": str(entry.get("timestamp") or "unknown"),
                        "side": self._entry_side(
                            entry,
                            reported_guild_id,
                            reporting_guild_id,
                        ),
                        "username": str(entry.get("username") or "Unknown"),
                        "user_id": entry.get("user_id"),
                        "avatar_url": str(entry.get("avatar_url") or ""),
                        "content": str(entry.get("content") or ""),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_viewer_data(text: str) -> list[dict]:
        entries: list[dict] = []
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(entry, dict) and entry.get("content"):
                entries.append(entry)
        return entries

    @staticmethod
    def _parse_transcript(text: str) -> list[dict]:
        marker = re.compile(r"^----- FLIPHONE MESSAGE \d+ -----$", re.MULTILINE)
        entries: list[dict] = []
        cursor = 0
        while match := marker.search(text, cursor):
            metadata_start = match.end()
            message_separator = text.find("\nMessage:\n", metadata_start)
            if message_separator < 0:
                break
            metadata = text[metadata_start:message_separator].strip("\n")
            values: dict[str, str] = {}
            for line in metadata.splitlines():
                key, found, value = line.partition(": ")
                if found:
                    values[key] = value
            content_start = message_separator + len("\nMessage:\n")
            try:
                content_length = int(values.get("Message Length", ""))
            except ValueError:
                next_match = marker.search(text, content_start)
                content_end = next_match.start() if next_match else len(text)
            else:
                content_end = min(len(text), content_start + max(0, content_length))
            content = text[content_start:content_end]
            entries.append(
                {
                    "timestamp": values.get("Time", "unknown").removesuffix(" UTC"),
                    "side": values.get("Side", "OTHER"),
                    "username": values.get("Speaker", "Unknown"),
                    "user_id": None if values.get("User ID") == "unavailable" else values.get("User ID"),
                    "avatar_url": "" if values.get("Avatar URL") == "none" else values.get("Avatar URL", ""),
                    "content": content,
                }
            )
            cursor = max(content_end, match.end())
        return entries

    @staticmethod
    def _report_message_ids(report: dict) -> list[int]:
        message_ids: list[int] = []
        for value in str(report.get("review_message_ids") or "").split(","):
            try:
                message_ids.append(int(value.strip()))
            except (TypeError, ValueError):
                continue
        legacy_id = report.get("review_msg_id")
        if legacy_id and int(legacy_id) not in message_ids:
            message_ids.insert(0, int(legacy_id))
        return message_ids

    async def _load_report_transcript(self, report: dict) -> list[dict]:
        channel_id = report.get("review_channel_id")
        if not channel_id:
            return []
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))

        report_id = int(report["id"])
        transcript_parts: list[bytes] = []
        viewer_data_parts: list[bytes] = []
        for message_id in self._report_message_ids(report):
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                continue
            for attachment in message.attachments:
                filename = attachment.filename.removeprefix("SPOILER_")
                is_transcript = (
                    filename.startswith(f"report-{report_id}-full-context")
                    and filename.endswith(".txt")
                )
                is_viewer_data = (
                    filename.startswith(f"report-{report_id}-viewer-data")
                    and filename.endswith(".jsonl")
                )
                if not is_transcript and not is_viewer_data:
                    continue
                try:
                    data = await attachment.read()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    try:
                        data = await attachment.read(use_cached=True)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                        log.warning(
                            "Report viewer could not download attachment | report=%s | file=%s | error=%s",
                            report_id,
                            attachment.filename,
                            exc,
                        )
                        continue
                if is_viewer_data:
                    viewer_data_parts.append(data)
                else:
                    transcript_parts.append(data)
        if viewer_data_parts:
            return self._parse_viewer_data(
                b"".join(viewer_data_parts).decode("utf-8", errors="replace")
            )
        if not transcript_parts:
            return []
        return self._parse_transcript(b"".join(transcript_parts).decode("utf-8", errors="replace"))

    def _build_conversation_message_embed(
        self,
        entry: dict,
        *,
        report_id: int,
        position: int,
        total: int,
    ) -> discord.Embed:
        username = self._safe_report_text(entry.get("username") or "Unknown", 120)
        side = self._safe_report_text(entry.get("side") or "OTHER", 40).title()
        content = discord.utils.escape_mentions(str(entry.get("content") or "No message content."))
        if len(content) > 3_900:
            content = f"{content[:3_897]}..."
        embed = discord.Embed(description=content, color=config.COLOR_WAIT)
        avatar_url = str(entry.get("avatar_url") or "")
        author_kwargs = {"name": f"{side} | {username}"}
        if avatar_url.startswith(("https://", "http://")):
            author_kwargs["icon_url"] = avatar_url
        embed.set_author(**author_kwargs)
        embed.set_footer(
            text=(
                f"{self._format_report_time(entry.get('timestamp'))} | "
                f"Message {position}/{total} | Report #{report_id}"
            )
        )
        return embed

    async def _open_conversation_viewer(
        self,
        interaction: discord.Interaction,
        report_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.db.get_call_report(report_id)
        if not report or report.get("status") != "open":
            await interaction.edit_original_response(
                content="This report is no longer open.",
                view=None,
            )
            return
        try:
            entries = await self._load_report_transcript(report)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            entries = []
        if not entries:
            await interaction.edit_original_response(
                content="No readable conversation context is available for this report.",
                view=None,
            )
            return
        viewer = ReportConversationPager(self, report_id, entries, interaction.user.id)
        content, embeds = viewer.render()
        await interaction.edit_original_response(content=content, embeds=embeds, view=viewer)

    @staticmethod
    def _get_recent_senders(entries: list[dict]) -> list[dict]:
        """
        Return the latest entry for each sender in fetched report context.
        """
        # Deduplicate — keep latest entry per user
        seen: dict[str, dict] = {}
        for entry in entries:
            key = str(entry.get("user_id") or entry.get("username") or "unknown")
            seen[key] = entry
        return list(seen.values())

    @staticmethod
    def _safe_report_text(value: object, limit: int = 300) -> str:
        text = " ".join(str(value or "").split())
        text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)]}..."

    @staticmethod
    def _format_report_time(value: Optional[str], fallback: str = "Unknown") -> str:
        if not value:
            return fallback
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError):
            return str(value)[:19].replace("T", " ")

    def _build_readable_excerpt(
        self,
        entries: list[dict],
        reported_guild_id: int,
        reporting_guild_id: int,
        *,
        only_reported: bool = False,
        max_messages: int = 8,
        max_chars: int = 1_000,
    ) -> str:
        candidates = [
            entry
            for entry in entries
            if entry.get("content")
            and (
                not only_reported
                or int(entry["guild_id"]) == int(reported_guild_id)
            )
        ]
        blocks: list[str] = []
        for entry in candidates[-max_messages:]:
            timestamp = str(entry.get("timestamp") or "")
            time_label = timestamp[11:19] if len(timestamp) >= 19 else "unknown"
            if int(entry["guild_id"]) == int(reported_guild_id):
                side = "REPORTED"
            elif int(entry["guild_id"]) == int(reporting_guild_id):
                side = "REPORTING"
            else:
                side = "OTHER"
            username = self._safe_report_text(entry.get("username") or "Unknown", 40)
            content = self._safe_report_text(entry.get("content"), 220)
            blocks.append(
                f"**{username}** | `{side}` | `{time_label}`\n"
                f"> {content}"
            )

        while len("\n\n".join(blocks)) > max_chars and len(blocks) > 1:
            blocks.pop(0)
        if blocks and len(blocks[0]) > max_chars:
            blocks[0] = f"{blocks[0][: max_chars - 3]}..."
        return "\n\n".join(blocks)

    # ── Core report logic ─────────────────────────────────────────────────────

    async def _process_report(
        self,
        interaction,
        guild:       discord.Guild,
        channel_id:  int,
        user:        discord.User | discord.Member,
        reason:      str,
        media_url:   Optional[str],
        attachments: list[discord.Attachment],
        station: Optional[str] = None,
    ) -> None:

        call = await self._get_reportable_call(guild.id, channel_id, station)
        if call and call.get("needs_station"):
            await interaction.followup.send("Choose the station using the `station` option in `/report`.", ephemeral=True)
            return
        if call and call.get("invalid_station"):
            await interaction.followup.send("That station is not in this room.", ephemeral=True)
            return
        if not call:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=(
                        "❌ No recent call found for this server.\n"
                        "Reports must be submitted during or shortly after a call."
                    ),
                    color=config.COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        raw_log = await self._fetch_report_context(channel_id, call, guild.id)
        senders = self._get_recent_senders(raw_log)

        # Collect all media links
        media_links: list[str] = []
        if media_url:
            media_links.append(media_url)
        for att in attachments:
            media_links.append(att.url)

        # Store in DB
        report_id = await self.db.add_call_report(
            reporter_guild_id=guild.id,
            reporter_user_id=user.id,
            reported_guild_id=call["other_guild_id"],
            reason=reason,
            call_started_at=call.get("started_at"),
            call_ended_at=call.get("ended_at"),
        )
        # Confirm to reporter
        confirm_embed = discord.Embed(
            title="Report Submitted",
            description=(
                f"Your report has been logged and will be reviewed by the Fliphone team.\n\n"
                f"**Report ID:** #{report_id}\n"
                f"**Summary:** {reason[:200]}"
            ),
            color=config.COLOR_OK,
        )
        confirm_embed.set_footer(text=config.FOOTER)
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

        # Send log embed
        log_ch = self.bot.get_channel(REPORT_LOG_CHANNEL_ID)
        if not log_ch:
            return

        reported_guild = self.bot.get_guild(call["other_guild_id"])
        reported_name  = reported_guild.name if reported_guild else "Unknown Server"

        safe_reason = self._safe_report_text(reason, 500)
        log_embed = discord.Embed(
            title=f"Call Report #{report_id}",
            description=f"**Reason provided**\n{safe_reason}",
            color=config.COLOR_ERR,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(
            name="Reported Side",
            value=(
                f"**{self._safe_report_text(reported_name, 100)}**\n"
                f"Server ID: `{call['other_guild_id']}`"
            ),
            inline=False,
        )
        log_embed.add_field(
            name="Reporting Side",
            value=(
                f"**{self._safe_report_text(guild.name, 100)}**\n"
                f"Server ID: `{guild.id}`\n"
                f"Reporter: <@{user.id}> (`{user.id}`)"
            ),
            inline=False,
        )
        call_state = "Active when submitted" if call["active"] else "Already ended"
        log_embed.add_field(
            name="Timeline",
            value=(
                f"Started: **{self._format_report_time(call.get('started_at'))}**\n"
                f"State: **{call_state}**"
            ),
            inline=False,
        )

        # Recent senders — filter out the reporter's own guild
        other_senders = [
            sender
            for sender in senders
            if int(sender["guild_id"]) == int(call["other_guild_id"])
        ]
        if other_senders:
            sender_lines = []
            for sender in other_senders[:10]:
                label = f"- **{self._safe_report_text(sender['username'], 60)}**"
                if sender.get("user_id"):
                    label += f" (`{sender['user_id']}`)"
                sender_lines.append(label)
            log_embed.add_field(
                name=f"People Seen on Reported Side ({len(other_senders)})",
                value="\n".join(sender_lines),
                inline=False,
            )
        else:
            log_embed.add_field(
                name="Recent Senders",
                value="No relayed messages were available in this channel for the selected call.",
                inline=False,
            )

        captured = [entry for entry in raw_log if entry.get("content")]
        transcript_parts: list[bytes] = []
        viewer_data_parts: list[bytes] = []
        if captured:
            capture_end = self._format_report_time(captured[-1].get("timestamp"))
            log_embed.add_field(
                name="Conversation Capture",
                value=(
                    f"**{len(captured)} messages** captured from the start of this call "
                    f"through **{capture_end}**. The sections below are short previews; "
                    "use **Open Conversation** or the attached transcript for the full capture."
                ),
                inline=False,
            )
        reported_excerpt = self._build_readable_excerpt(
            list(raw_log), call["other_guild_id"], guild.id, only_reported=True
        )
        if reported_excerpt:
            log_embed.add_field(
                name="Latest Messages from Reported Side",
                value=reported_excerpt,
                inline=False,
            )

        context_excerpt = self._build_readable_excerpt(
            list(raw_log), call["other_guild_id"], guild.id
        )
        if context_excerpt:
            log_embed.add_field(
                name="Latest Two-Sided Context",
                value=context_excerpt,
                inline=False,
            )

        if captured:
            transcript = self._build_transcript(
                report_id=report_id,
                reason=reason,
                reported_name=reported_name,
                reported_guild_id=int(call["other_guild_id"]),
                reporting_name=guild.name,
                reporting_guild_id=guild.id,
                entries=captured,
            )
            transcript_parts = self._split_transcript(transcript)
            viewer_data = self._build_viewer_data(
                captured,
                int(call["other_guild_id"]),
                guild.id,
            )
            viewer_data_parts = self._split_transcript(viewer_data)

        if media_links:
            log_embed.add_field(
                name="Evidence",
                value="\n".join(media_links),
                inline=False,
            )
            # Embed image if it's a single image link
            if len(media_links) == 1 and any(
                media_links[0].lower().endswith(ext)
                for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")
            ):
                log_embed.set_image(url=media_links[0])

        log_embed.set_footer(
            text=(
                f"Full available context was fetched when this report was submitted | "
                f"/resolvereport {report_id} to close"
            )
        )

        evidence_message_ids: list[int] = []
        total_parts = max(len(transcript_parts), len(viewer_data_parts))
        try:
            for part_index in range(total_parts or 1):
                index = part_index + 1
                files: list[discord.File] = []
                if part_index < len(transcript_parts):
                    transcript_name = (
                        f"report-{report_id}-full-context.txt"
                        if len(transcript_parts) == 1
                        else (
                            f"report-{report_id}-full-context-part-{part_index + 1}"
                            f"-of-{len(transcript_parts)}.txt"
                        )
                    )
                    files.append(
                        discord.File(
                            io.BytesIO(transcript_parts[part_index]),
                            filename=transcript_name,
                        )
                    )
                if part_index < len(viewer_data_parts):
                    viewer_name = (
                        f"report-{report_id}-viewer-data.jsonl"
                        if len(viewer_data_parts) == 1
                        else (
                            f"report-{report_id}-viewer-data-part-{part_index + 1}"
                            f"-of-{len(viewer_data_parts)}.jsonl"
                        )
                    )
                    files.append(
                        discord.File(
                            io.BytesIO(viewer_data_parts[part_index]),
                            filename=viewer_name,
                            spoiler=True,
                        )
                    )

                if part_index == 0:
                    evidence_message = await log_ch.send(
                        embed=log_embed,
                        files=files,
                        view=ReportConversationLaunchView(self, report_id),
                    )
                else:
                    evidence_message = await log_ch.send(
                        content=(
                            f"Call report #{report_id} evidence, part {index} of {total_parts}."
                        ),
                        files=files,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                evidence_message_ids.append(evidence_message.id)
                await self.db.set_call_report_review_messages(
                    report_id, evidence_message.channel.id, evidence_message_ids
                )
        except discord.HTTPException as exc:
            log.warning("Could not send complete report evidence | report=%s | error=%s", report_id, exc)

    # ── /report ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="report")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def report(self, ctx: commands.Context, station: Optional[str] = None) -> None:
        """Report the server you are currently in a call with, or your most recent call."""
        cfg = await self.db.get_guild_config(ctx.guild.id)
        if not cfg:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Fliphone is not set up in this server.",
                    color=config.COLOR_ERR,
                )
            )
            return

        # Slash — open modal
        if ctx.interaction:
            await ctx.interaction.response.send_modal(ReportModal(self, station))
            return

        await ctx.send("Use `/report` so Discord can collect the report reason privately.")
        return

    # ── f.userreports ─────────────────────────────────────────────────────────

    @commands.hybrid_command(name="userreports")
    @trusted_moderator_only()
    async def userreports(self, ctx: commands.Context) -> None:
        """[Mods] List all open call reports."""
        admin_cog = self.bot.get_cog("Admin")
        if not admin_cog or not await admin_cog._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return

        reports = await self.db.get_open_call_reports()
        embed = self._build_userreports_embed(reports)
        view = UserReportPanelView(self, reports) if reports else None
        await ctx.send(embed=embed, view=view)

    # ── f.resolvereport ───────────────────────────────────────────────────────

    def _build_userreport_detail_embed(self, report: dict) -> discord.Embed:
        reported_guild = self.bot.get_guild(report["reported_guild_id"])
        reporter_guild = self.bot.get_guild(report["reporter_guild_id"])
        reported_name = reported_guild.name if reported_guild else "Unknown Server"
        reporter_name = reporter_guild.name if reporter_guild else "Unknown Server"

        embed = discord.Embed(
            title=f"Call Report #{report['id']}",
            description=(
                "**Reason provided**\n"
                f"{self._safe_report_text(report.get('reason') or 'No reason provided.', 500)}"
            ),
            color=config.COLOR_WARN,
        )
        embed.add_field(
            name="Reported Side",
            value=(
                f"**{self._safe_report_text(reported_name, 100)}**\n"
                f"Server ID: `{report['reported_guild_id']}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Reporting Side",
            value=(
                f"**{self._safe_report_text(reporter_name, 100)}**\n"
                f"Server ID: `{report['reporter_guild_id']}`\n"
                f"Reporter: <@{report['reporter_user_id']}> (`{report['reporter_user_id']}`)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Timeline",
            value=(
                f"Started: **{self._format_report_time(report.get('call_started_at'))}**\n"
                f"Ended: **{self._format_report_time(report.get('call_ended_at'), 'Active when reported')}**\n"
                f"Submitted: **{self._format_report_time(report.get('created_at'))}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Evidence",
            value="Open the original private report message to view its full context attachment parts.",
            inline=False,
        )
        embed.set_footer(text="Use Resolve to close this report, or choose another report above.")
        return embed

    def _build_userreports_embed(self, reports: list[dict]) -> discord.Embed:
        if not reports:
            embed = discord.Embed(
                title="Call Report Panel",
                description="No open call reports.",
                color=config.COLOR_OK,
            )
            embed.set_footer(text=config.FOOTER)
            return embed

        lines = []
        for report in reports[:20]:
            reported_guild = self.bot.get_guild(report["reported_guild_id"])
            reported_name = reported_guild.name if reported_guild else f"Server {report['reported_guild_id']}"
            reason = report["reason"] or ""
            short_reason = reason[:70] + "..." if len(reason) > 70 else reason
            created = report["created_at"][:10]
            lines.append(
                f"**#{report['id']}** - {created} - {reported_name}\n"
                f"{short_reason}"
            )

        embed = discord.Embed(
            title=f"Call Report Panel ({len(reports)} open)",
            description="\n".join(lines),
            color=config.COLOR_WARN,
        )
        embed.set_footer(text="Use the select menu/button, or /resolvereport.")
        return embed

    @commands.hybrid_command(name="resolvereport")
    @trusted_moderator_only()
    async def resolvereport(self, ctx: commands.Context, report_id: int) -> None:
        """[Mods] Mark a call report as resolved."""
        admin_cog = self.bot.get_cog("Admin")
        if not admin_cog or not await admin_cog._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return

        report = await self.db.get_call_report(report_id)
        success = await self.db.resolve_call_report(report_id)
        if not success:
            await ctx.send(f"❌ No open report found with ID `{report_id}`.")
            return

        if report:
            await self._delete_call_report_review_message(report)
        await ctx.send(
            embed=discord.Embed(
                description=f"✅ Report #{report_id} marked as resolved.",
                color=config.COLOR_OK,
            )
        )

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_command_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=discord.Embed(
                    description=f"⏳ You can submit one report every 30 seconds. Try again in **{error.retry_after:.0f}s**.",
                    color=config.COLOR_WARN,
                )
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Bad argument: `{error}`")
        else:
            raise error


async def setup(bot) -> None:
    await bot.add_cog(Report(bot))
