"""
cogs/report.py - General call/conversation report system for Fliphone.

Commands
--------
/report                     - Report your most recent or active call
@Fliphone userreports       - List open call reports [owner + trusted mods]
@Fliphone resolvereport     - Mark a report as resolved [owner + trusted mods]

How it works
------------
When a report is submitted:
  1. The reporter provides a reason and optionally attaches media.
  2. The bot looks up who they were connected to via active connection,
     the Phonebooth cog's last_calls cache, or call_history in the DB.
  3. Recent context is fetched on demand from the reporting Discord channel.
  4. The context is sent to the private report channel and discarded locally.
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
from database import Database
from relay_policy import is_local_only

# Channel to send report log embeds to
REPORT_LOG_CHANNEL_ID = int(os.getenv("USER_REPORT_LOG_CHANNEL_ID", 1497205915089371186))

MAX_CONTEXT_MESSAGES = 50
MAX_CONTEXT_SCAN = 100
REPORT_EVIDENCE_RETENTION_DAYS = 30


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


class Report(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db

    async def cog_load(self) -> None:
        self._cleanup_expired_reports.start()

    def cog_unload(self) -> None:
        self._cleanup_expired_reports.cancel()

    async def _delete_call_report_review_message(self, report: dict) -> None:
        message_id = report.get("review_msg_id")
        channel_id = report.get("review_channel_id")
        if not message_id or not channel_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            await channel.get_partial_message(int(message_id)).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            pass

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
                "started_at": room["created_at"] if room else None,
                "ended_at": None,
                "active": True,
                "conn_id": -int(room_member["room_id"]),
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
            "limit": MAX_CONTEXT_SCAN,
            "oldest_first": False,
        }
        if started_at:
            history_kwargs["after"] = started_at - timedelta(seconds=1)
        if ended_at:
            history_kwargs["before"] = ended_at + timedelta(seconds=1)

        try:
            messages = [message async for message in channel.history(**history_kwargs)]
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            return []
        messages.reverse()

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
            combined = "\n".join(part for part in (content.strip(), *attachment_lines) if part)
            if not combined:
                continue

            entries.append(
                {
                    "user_id": None if is_webhook else int(message.author.id),
                    "username": str(getattr(message.author, "display_name", message.author)),
                    "guild_id": (
                        int(call["other_guild_id"])
                        if is_webhook
                        else int(reporting_guild_id)
                    ),
                    "guild_name": "Relayed side" if is_webhook else "Reporting side",
                    "timestamp": message.created_at.astimezone(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "content": combined[:2_000],
                }
            )
        return entries[-MAX_CONTEXT_MESSAGES:]

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
        lines: list[str] = []
        for entry in candidates[-max_messages:]:
            timestamp = str(entry.get("timestamp") or "")
            time_label = timestamp[11:19] if len(timestamp) >= 19 else "unknown"
            side = (
                "REPORTED"
                if int(entry["guild_id"]) == int(reported_guild_id)
                else "REPORTING"
            )
            username = self._safe_report_text(entry.get("username") or "Unknown", 40)
            content = self._safe_report_text(entry.get("content"), 220)
            lines.append(f"`{time_label}` `{side}` **{username}:** {content}")

        while len("\n".join(lines)) > max_chars and len(lines) > 1:
            lines.pop(0)
        if lines and len(lines[0]) > max_chars:
            lines[0] = f"{lines[0][: max_chars - 3]}..."
        return "\n".join(lines)

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
        other_senders = [s for s in senders if s["guild_id"] != guild.id]
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

        transcript_file = None
        reported_excerpt = self._build_readable_excerpt(
            list(raw_log), call["other_guild_id"], only_reported=True
        )
        if reported_excerpt:
            log_embed.add_field(
                name="Latest Messages from Reported Side",
                value=reported_excerpt,
                inline=False,
            )

        context_excerpt = self._build_readable_excerpt(
            list(raw_log), call["other_guild_id"]
        )
        if context_excerpt:
            log_embed.add_field(
                name="Latest Two-Sided Context",
                value=context_excerpt,
                inline=False,
            )

        captured = [entry for entry in raw_log if entry.get("content")]
        if captured:
            transcript_header = (
                f"FLIPHONE CALL REPORT #{report_id}\n"
                f"Reason: {' '.join(reason.split())}\n"
                f"Reported side: {reported_name} (server {call['other_guild_id']})\n"
                f"Reporting side: {guild.name} (server {guild.id})\n"
                "Legend: REPORTED = the side being reported; REPORTING = the side that filed it.\n"
                "=" * 72
            )
            transcript_lines = []
            for entry in captured:
                side = (
                    "REPORTED"
                    if int(entry["guild_id"]) == int(call["other_guild_id"])
                    else "REPORTING"
                )
                content = " ".join(str(entry.get("content") or "").split())
                sender_id = f" (user {entry['user_id']})" if entry.get("user_id") else ""
                transcript_lines.append(
                    f"[{entry['timestamp']} UTC] [{side}] {entry['username']}{sender_id}:\n  {content}"
                )
            transcript = f"{transcript_header}\n\n" + "\n\n".join(transcript_lines)
            transcript_file = discord.File(
                io.BytesIO(transcript.encode("utf-8")),
                filename=f"report-{report_id}-excerpt.txt",
            )

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
                f"Context was fetched only when this report was submitted | "
                f"@Fliphone resolvereport {report_id} to close"
            )
        )

        try:
            review_message = await log_ch.send(embed=log_embed, file=transcript_file)
            await self.db.set_call_report_review_message(
                report_id, review_message.id, review_message.channel.id
            )
        except discord.HTTPException:
            pass

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

    @commands.command(name="userreports")
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
            value="Open the original private report message to view its on-demand context attachment.",
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
        embed.set_footer(text="Use the select menu/button, or @Fliphone resolvereport <id>.")
        return embed

    @commands.command(name="resolvereport")
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
