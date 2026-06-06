"""
cogs/report.py - General call/conversation report system for Fliphone.

Commands
--------
f.report / /report          - Report your most recent or active call
f.userreports               - List open call reports [owner + trusted mods]
f.resolvereport <id>        - Mark a report as resolved [owner + trusted mods]

How it works
------------
During relay, the bot maintains a rolling log of the last 50 messages per call,
storing { user_id, username, guild_id, guild_name, timestamp } for each.
This works for both 1-on-1 calls and group rooms.

When a report is submitted:
  1. The reporter provides a reason and optionally attaches media.
  2. The bot looks up who they were connected to via active connection,
     the Phonebooth cog's last_calls cache, or call_history in the DB.
  3. A log embed is sent to the report channel showing the reported server,
     the reason, any media, and all recent senders captured from the message log.
"""

from __future__ import annotations

import asyncio
import collections
import os
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands

import config
from database import Database

# Channel to send report log embeds to
REPORT_LOG_CHANNEL_ID = int(os.getenv("USER_REPORT_LOG_CHANNEL_ID", 1497205915089371186))

# How many recent messages to keep in the rolling log per call
MAX_LOG_ENTRIES = 50


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

    def __init__(self, cog: "Report") -> None:
        super().__init__()
        self.cog = cog

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
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class UserReportSelect(discord.ui.Select):
    def __init__(self, view: "UserReportPanelView", reports: list[dict]) -> None:
        self.panel_view = view
        options = []
        for report in reports[:25]:
            label = f"Report #{report['id']}"
            description = (report["reason"] or "")[:90]
            options.append(discord.SelectOption(label=label, value=str(report["id"]), description=description))
        super().__init__(placeholder="Choose a call report", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel_view.selected_report_id = int(self.values[0])
        await interaction.response.send_message(f"Selected call report #{self.values[0]}.", ephemeral=True)


class UserReportPanelView(discord.ui.View):
    def __init__(self, cog: "Report", reports: list[dict]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_report_id = int(reports[0]["id"]) if reports else None
        if reports:
            self.add_item(UserReportSelect(self, reports))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        admin_cog = interaction.client.get_cog("Admin")
        if admin_cog and await admin_cog._is_global_mod(interaction.user, interaction.guild):
            return True
        await interaction.response.send_message("You do not have permission to use this panel.", ephemeral=True)
        return False

    async def _refresh(self, interaction: discord.Interaction, message: str | None = None) -> None:
        reports = await self.cog.db.get_open_call_reports()
        embed = self.cog._build_userreports_embed(reports)
        view = UserReportPanelView(self.cog, reports) if reports else None
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
        success = await self.cog.db.resolve_call_report(self.selected_report_id)
        if not success:
            await self._refresh(interaction, "That report was already resolved or no longer exists.")
            return
        await self._refresh(interaction, f"Call report #{self.selected_report_id} resolved.")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._refresh(interaction)


class Report(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db

        # conn_id -> deque of { user_id, username, guild_id, guild_name, timestamp }
        self._message_log: dict[int, collections.deque] = {}

        # conn_id -> list snapshot kept briefly after a call ends for post-hangup reports
        self._last_logs: dict[int, list] = {}

    # ── Called by Phonebooth cog during every relay ───────────────────────────

    def record_message(
        self,
        conn_id:      int,
        user_id:      int,
        username:     str,
        guild_id:     int,
        guild_name:   str,
    ) -> None:
        """
        Store a sender snapshot in the rolling log for this connection.
        Call this from Phonebooth._relay() right before sending the webhook.
        """
        if conn_id not in self._message_log:
            self._message_log[conn_id] = collections.deque(maxlen=MAX_LOG_ENTRIES)
        self._message_log[conn_id].append({
            "user_id":      user_id,
            "username":     username,
            "guild_id":     guild_id,
            "guild_name":   guild_name,
            "timestamp":    datetime.utcnow().isoformat(timespec="seconds"),
        })

    def clear_log(self, conn_id: int) -> None:
        """
        Move the message log to last_logs when a call ends so post-hangup
        reports can still access it. Call this alongside
        _call_reported_gifs.pop(conn_id, None) in Phonebooth.
        """
        log = self._message_log.pop(conn_id, None)
        if log:
            self._last_logs[conn_id] = list(log)

    # ── Find the call to report against ──────────────────────────────────────

    async def _get_reportable_call(self, guild_id: int, channel_id: int) -> Optional[dict]:
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

    def _get_recent_senders(self, conn_id: Optional[int]) -> list[dict]:
        """
        Return deduplicated recent senders for a connection.
        Checks active log first then the post-hangup snapshot.
        """
        if conn_id is None:
            return []

        log = self._message_log.get(conn_id) or self._last_logs.get(conn_id, [])
        if not log:
            return []

        # Deduplicate — keep latest entry per user
        seen: dict[int, dict] = {}
        for entry in log:
            seen[entry["user_id"]] = entry
        return list(seen.values())

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
    ) -> None:

        call = await self._get_reportable_call(guild.id, channel_id)
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

        senders = self._get_recent_senders(call.get("conn_id"))

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

        log_embed = discord.Embed(
            title=f"New Call Report — #{report_id}",
            color=config.COLOR_ERR,
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(
            name="Reported Server",
            value=f"{reported_name}\n`{call['other_guild_id']}`",
            inline=True,
        )
        log_embed.add_field(
            name="Reported By",
            value=f"{guild.name}\n`{guild.id}`",
            inline=True,
        )
        log_embed.add_field(
            name="Reporter",
            value=f"<@{user.id}>\n`{user.id}`",
            inline=True,
        )
        log_embed.add_field(
            name="Call Started",
            value=call["started_at"][:19] if call.get("started_at") else "Unknown",
            inline=True,
        )
        log_embed.add_field(
            name="Call Active at Report Time",
            value="Yes" if call["active"] else "No",
            inline=True,
        )
        log_embed.add_field(name="\u200b", value="\u200b", inline=True)
        log_embed.add_field(name="Reason", value=reason[:500], inline=False)

        # Recent senders — filter out the reporter's own guild
        other_senders = [s for s in senders if s["guild_id"] != guild.id]
        if other_senders:
            sender_lines = [
                f"<@{s['user_id']}> **@{s['username']}**\n"
                f"`{s['user_id']}` — {s['guild_name']} — {s['timestamp']}"
                for s in other_senders[:10]
            ]
            log_embed.add_field(
                name=f"Recent Senders from Reported Server ({len(other_senders)})",
                value="\n\n".join(sender_lines),
                inline=False,
            )
        else:
            log_embed.add_field(
                name="Recent Senders",
                value="No messages captured from the reported server yet." if call["active"]
                      else "Message log unavailable — call ended before logging began or bot was restarted.",
                inline=False,
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
            text=f"f.resolvereport {report_id} to close  •  {config.FOOTER}"
        )

        try:
            await log_ch.send(embed=log_embed)
        except discord.HTTPException:
            pass

    # ── f.report / /report ────────────────────────────────────────────────────

    @commands.hybrid_command(name="report")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def report(self, ctx: commands.Context) -> None:
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
            await ctx.interaction.response.send_modal(ReportModal(self))
            return

        # Prefix — collect reason
        await ctx.send(
            embed=discord.Embed(
                description=(
                    "Please describe what happened. You have **60 seconds** to reply.\n"
                    "Be as specific as possible — what was said, what rule was broken."
                ),
                color=config.COLOR_WAIT,
            )
        )

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Report cancelled — no response within 60 seconds.")
            return

        reason = reply.content.strip()
        if len(reason) < 10:
            await ctx.send("❌ Please provide more detail (at least 10 characters).")
            return

        attachments = list(reply.attachments)
        media_url: Optional[str] = None

        # Ask for media if none attached
        if not attachments:
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "Do you have any screenshots or video evidence? "
                        "Send them now or type `skip` to submit without."
                    ),
                    color=config.COLOR_WAIT,
                )
            )
            try:
                media_reply = await self.bot.wait_for("message", check=check, timeout=120)
                if media_reply.content.strip().lower() != "skip":
                    if media_reply.attachments:
                        attachments = list(media_reply.attachments)
                    elif media_reply.content.strip().startswith("http"):
                        media_url = media_reply.content.strip()
            except asyncio.TimeoutError:
                pass

        class _Reply:
            async def send(self_, content=None, embed=None, ephemeral=False):
                await ctx.send(content=content, embed=embed)

        class _FakeInteraction:
            followup = _Reply()

        await self._process_report(
            interaction=_FakeInteraction(),
            guild=ctx.guild,
            channel_id=ctx.channel.id,
            user=ctx.author,
            reason=reason,
            media_url=media_url,
            attachments=attachments,
        )

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
        return
        if not reports:
            await ctx.send("✅ No open call reports.")
            return

        lines = []
        for r in reports[:20]:
            reported_guild = self.bot.get_guild(r["reported_guild_id"])
            reported_name  = reported_guild.name if reported_guild else f"Server {r['reported_guild_id']}"
            short_reason   = r["reason"][:70] + "…" if len(r["reason"]) > 70 else r["reason"]
            created        = r["created_at"][:10]
            lines.append(
                f"**#{r['id']}** — {created} — {reported_name}\n"
                f"└ {short_reason}"
            )

        embed = discord.Embed(
            title=f"Open Call Reports ({len(reports)})",
            description="\n".join(lines),
            color=config.COLOR_WARN,
        )
        embed.set_footer(text=f"f.resolvereport <id> to close  •  {config.FOOTER}")
        await ctx.send(embed=embed)

    # ── f.resolvereport ───────────────────────────────────────────────────────

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
        embed.set_footer(text="Use the select menu/button, or f.resolvereport <id>.")
        return embed

    @commands.command(name="resolvereport")
    async def resolvereport(self, ctx: commands.Context, report_id: int) -> None:
        """[Mods] Mark a call report as resolved."""
        admin_cog = self.bot.get_cog("Admin")
        if not admin_cog or not await admin_cog._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return

        success = await self.db.resolve_call_report(report_id)
        if not success:
            await ctx.send(f"❌ No open report found with ID `{report_id}`.")
            return

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
                    description=f"⏳ You can only submit one report per minute. Try again in **{error.retry_after:.0f}s**.",
                    color=config.COLOR_WARN,
                )
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Bad argument: `{error}`")
        else:
            raise error


async def setup(bot) -> None:
    await bot.add_cog(Report(bot))
