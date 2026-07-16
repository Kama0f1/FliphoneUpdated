"""
cogs/admin.py – Admin/setup/moderation commands for Phonebooth V2.

f.setup          – Register current channel as phonebooth
f.teardown       – Remove phonebooth from this server
f.stats          – Global statistics
f.invite         – Show bot invite link
f.blocklist      – List blocked servers
f.unblock <id>   – Unblock a server
f.kick           – Force-disconnect the active call
f.ban <user_id>  – Ban a user from using the bot (bot-wide)  [owner]
f.unban <user_id>– Unban a user                              [owner]
f.reports        – List pending GIF reports                  [owner + trusted mods]
f.gifbl          – Blacklist a GIF URL or report             [owner + trusted mods]
f.gifwl          – Whitelist a GIF URL or report             [owner + trusted mods]
f.gifcheck <url> – Check if a URL is listed                  [owner + trusted mods]
f.notifyignore   – Toggle a user off the notify fire list    [owner]
f.pb             – Legacy command group (still works)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Optional

import discord
from discord.ext import commands

import config
from database import Database, TABLE_ORDER


class SetupCheckView(discord.ui.View):
    def __init__(self, cog: "Admin", channel_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.channel_id = channel_id

    @discord.ui.button(label="Reset Setup", style=discord.ButtonStyle.primary)
    async def repair_setup(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("You need Manage Channels to repair setup.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("That configured channel no longer exists.", ephemeral=True)
            return

        embed = await self.cog._repair_setup(interaction.guild, interaction.user, channel)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GifReportSelect(discord.ui.Select):
    def __init__(self, view: "GifReportPanelView", reports: list[dict]) -> None:
        self.panel_view = view
        options = []
        for report in reports[:25]:
            label = f"Report #{report['id']}"
            description = (report["url"] or "")[:90]
            options.append(discord.SelectOption(label=label, value=str(report["id"]), description=description))
        super().__init__(placeholder="Choose a GIF report", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel_view.selected_report_id = int(self.values[0])
        report = await self.panel_view.cog.db.get_gif_report(self.panel_view.selected_report_id)
        if not report:
            await interaction.response.send_message("That GIF report no longer exists.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"GIF Report #{report['id']}",
            color=config.COLOR_WARN,
        )
        embed.add_field(name="URL", value=f"```\n{(report['url'] or '')[:950]}\n```", inline=False)
        embed.add_field(
            name="Reporter",
            value=f"<@{report['reporter_id']}>" if report.get("reporter_id") else "unknown",
            inline=True,
        )
        embed.add_field(name="Channel", value=f"<#{report['channel_id']}>", inline=True)
        sender_id = report.get("sender_user_id")
        source_guild_id = report.get("source_guild_id")
        embed.add_field(
            name="Original Sender",
            value=f"<@{sender_id}> (`{sender_id}`)" if sender_id else "unknown (older report)",
            inline=False,
        )
        embed.add_field(
            name="Source Server",
            value=self.panel_view.cog._format_guild_identity(source_guild_id),
            inline=False,
        )
        embed.add_field(name="Status", value=str(report.get("status") or "pending"), inline=True)
        embed.set_footer(text="Select moderation actions from the main report panel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GifReportPanelView(discord.ui.View):
    def __init__(self, cog: "Admin", reports: list[dict]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_report_id = int(reports[0]["id"]) if reports else None
        if reports:
            self.add_item(GifReportSelect(self, reports))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog._is_global_mod(interaction.user, interaction.guild):
            return True
        await interaction.response.send_message("You do not have permission to use this panel.", ephemeral=True)
        return False

    async def _refresh(self, interaction: discord.Interaction, message: str | None = None) -> None:
        reports = await self.cog.db.get_pending_gif_reports()
        embed = self.cog._build_gif_reports_embed(reports)
        view = GifReportPanelView(self.cog, reports) if reports else None
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if message:
            await interaction.followup.send(message, ephemeral=True)

    async def _act(self, interaction: discord.Interaction, status: str, resolution: str) -> None:
        if not self.selected_report_id:
            await interaction.response.send_message("No GIF report selected.", ephemeral=True)
            return
        report = await self.cog.db.get_gif_report(self.selected_report_id)
        if not report:
            await interaction.response.send_message("That report no longer exists.", ephemeral=True)
            return

        await interaction.response.defer()
        await self.cog.db.set_gif_url_status(report["url"], status, interaction.user.id)
        await self.cog.db.resolve_gif_report(self.selected_report_id, resolution)
        await self.cog._delete_gif_report_review_message(report)
        await self._refresh(interaction, f"GIF report #{self.selected_report_id} marked {resolution}.")

    @discord.ui.button(label="Blacklist", style=discord.ButtonStyle.danger)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, "blacklist", "blacklisted")

    @discord.ui.button(label="Whitelist", style=discord.ButtonStyle.success)
    async def whitelist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, "whitelist", "whitelisted")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._refresh(interaction)

    @discord.ui.button(label="Ban Sender", style=discord.ButtonStyle.danger)
    async def ban_sender(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        report = await self.cog.db.get_gif_report(self.selected_report_id) if self.selected_report_id else None
        sender_id = report.get("sender_user_id") if report else None
        if not sender_id:
            await interaction.response.send_message(
                "This older report does not contain the original sender ID.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.cog.db.ban_user(int(sender_id), interaction.user.id, f"GIF report #{report['id']}")
        await self._refresh(interaction, f"User `{sender_id}` is now banned from Fliphone.")

    @discord.ui.button(label="Ban Server", style=discord.ButtonStyle.danger)
    async def ban_server(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        report = await self.cog.db.get_gif_report(self.selected_report_id) if self.selected_report_id else None
        guild_id = report.get("source_guild_id") if report else None
        if not guild_id:
            await interaction.response.send_message(
                "This older report does not contain the source server ID.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.cog.ban_server_globally(
            int(guild_id), interaction.user.id, f"GIF report #{report['id']}"
        )
        await self._refresh(interaction, f"Server `{guild_id}` is now banned from Fliphone.")


class Admin(commands.Cog, name="Admin"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.db: Database = bot.db
        self._setup_locks: dict[int, asyncio.Lock] = {}

    def create_gif_report_panel_view(self, reports: list[dict]) -> Optional[discord.ui.View]:
        return GifReportPanelView(self, reports) if reports else None

    def _format_guild_identity(self, guild_id: Optional[int]) -> str:
        if not guild_id:
            return "unknown (older report)"
        guild = self.bot.get_guild(int(guild_id))
        return f"{guild.name} (`{guild_id}`)" if guild else f"Server `{guild_id}`"

    async def _delete_gif_report_review_message(self, report: dict) -> None:
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

    async def ban_server_globally(self, guild_id: int, moderator_id: int, reason: str) -> None:
        await self.db.ban_guild(guild_id, moderator_id, reason)
        guild = self.bot.get_guild(guild_id)
        if guild:
            await self._clear_guild_setup_state(guild, notify_partner=True)
            try:
                await guild.leave()
            except discord.HTTPException:
                pass

    def _invite_url(self, guild_id: Optional[int] = None) -> str:
        url = (
            "https://discord.com/api/oauth2/authorize"
            f"?client_id={self.bot.user.id}"
            f"&permissions={config.BOT_PERMISSIONS}"
            "&scope=bot%20applications.commands"
        )
        if guild_id:
            url += f"&guild_id={guild_id}&disable_guild_select=true"
        return url

    def _permission_recovery_guidance(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        target: discord.TextChannel,
        issues: list[str],
    ) -> str:
        permission_attributes = {
            "View Channel": "view_channel",
            "Send Messages": "send_messages",
            "Embed Links": "embed_links",
            "Read Message History": "read_message_history",
            "Manage Webhooks": "manage_webhooks",
            "Attach Files": "attach_files",
            "Add Reactions": "add_reactions",
        }
        bot_member = guild.me
        if bot_member is None:
            return "Fliphone's server member is unavailable. Try again after Discord finishes loading the bot."

        guild_permissions = bot_member.guild_permissions
        guild_missing = [
            issue
            for issue in issues
            if not getattr(guild_permissions, permission_attributes.get(issue, ""), False)
        ]
        override_missing = [issue for issue in issues if issue not in guild_missing]
        guidance: list[str] = []

        if guild_missing:
            guidance.append(
                "Fliphone's server role is missing **"
                + ", ".join(guild_missing)
                + "**. "
                + f"**[Re-invite Fliphone with the required permissions]({self._invite_url(guild.id)})** "
                + "or have the server owner update the Fliphone role."
            )
            if isinstance(user, discord.Member):
                user_permissions = user.guild_permissions
                cannot_grant = [
                    issue
                    for issue in guild_missing
                    if not (
                        user_permissions.administrator
                        or getattr(user_permissions, permission_attributes.get(issue, ""), False)
                    )
                ]
                if cannot_grant:
                    guidance.append(
                        "Your account cannot grant **"
                        + ", ".join(cannot_grant)
                        + "** on Discord's authorization screen. Ask the server owner or an admin who has "
                        "those permissions to re-invite Fliphone."
                    )

        if override_missing:
            scope = (
                f"the **{target.category.name}** category"
                if target.permissions_synced and target.category
                else target.mention
            )
            guidance.append(
                f"Fliphone has **{', '.join(override_missing)}** on its server role, but {scope} "
                "is overriding it. In that channel/category's Permissions, allow those permissions for "
                "the Fliphone role."
            )
            if isinstance(user, discord.Member):
                user_permissions = user.guild_permissions
                if not (user_permissions.administrator or user_permissions.manage_roles):
                    guidance.append(
                        "Your account cannot edit role permission overwrites. Ask the server owner or someone "
                        "with **Manage Roles** to make that change."
                    )

        return "\n\n".join(guidance) or "Run `f.repair` to rebuild the relay webhook."

    async def _delete_fliphone_webhooks(self, channel: Optional[discord.TextChannel]) -> None:
        if not channel:
            return
        pb_cog = self.bot.get_cog("Phonebooth")
        try:
            for webhook in await channel.webhooks():
                if webhook.user == self.bot.user and webhook.name == "Fliphone":
                    if pb_cog and hasattr(pb_cog, "_wh_obj_cache"):
                        pb_cog._wh_obj_cache.pop(webhook.url, None)
                    await webhook.delete(reason="Fliphone setup reset")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _active_setup_activity(self, guild_id: int) -> list[str]:
        connections, queue_entries, room_members = await asyncio.gather(
            self.db.get_guild_connections(guild_id),
            self.db.get_guild_queue_entries(guild_id),
            self.db.get_guild_room_members(guild_id),
        )
        activity: list[str] = []
        if connections:
            activity.append(f"{len(connections)} active 1:1 call(s)")
        if queue_entries:
            activity.append(f"{len(queue_entries)} queue search(es)")
        if room_members:
            activity.append(f"{len(room_members)} active room station(s)")
        return activity

    async def _configured_setup_issues(
        self,
        guild: discord.Guild,
        guild_cfg: dict,
    ) -> tuple[Optional[discord.TextChannel], list[str]]:
        channel_id = int(guild_cfg["channel_id"])
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None, [f"Configured channel `{channel_id}` is missing or inaccessible"]

        pb_cog = self.bot.get_cog("Phonebooth")
        issues = (
            pb_cog.relay_permission_issues(channel)
            if pb_cog and hasattr(pb_cog, "relay_permission_issues")
            else ["Phonebooth relay is unavailable"]
        )
        if issues:
            return channel, issues

        try:
            webhooks = await channel.webhooks()
        except discord.Forbidden:
            return channel, ["Manage Webhooks is denied in the configured channel or category"]
        except discord.HTTPException:
            return channel, ["Discord could not verify the configured channel's webhook"]

        bot_webhook = next(
            (wh for wh in webhooks if wh.user == self.bot.user and wh.name == "Fliphone"),
            None,
        )
        if bot_webhook is None:
            if len(webhooks) >= 15:
                return channel, ["The configured channel has Discord's maximum of 15 webhooks"]
            return channel, ["The configured channel's Fliphone webhook is missing"]
        if guild_cfg.get("webhook_url") != bot_webhook.url:
            return channel, ["The configured channel has a stale stored webhook"]
        return channel, []

    @staticmethod
    def _active_setup_embed(activity: list[str]) -> discord.Embed:
        return discord.Embed(
            title="Setup Change Blocked",
            description=(
                "Fliphone did not reset anything because this server is currently using it:\n"
                + "\n".join(f"- {item}" for item in activity)
                + "\n\nEnd or leave that activity first, then run `f.repair` again."
            ),
            color=config.COLOR_WARN,
        )

    async def _clear_guild_setup_state(self, guild: discord.Guild, *, notify_partner: bool) -> None:
        """Remove active runtime state and stored setup before a clean rebuild."""
        guild_cfg = await self.db.get_guild_config(guild.id)
        if not guild_cfg:
            return

        channel_id = int(guild_cfg["channel_id"])
        pb_cog = self.bot.get_cog("Phonebooth")
        room_cog = self.bot.get_cog("Room")

        for conn in await self.db.get_guild_connections(guild.id):
            active_channel_id = conn["channel_a"] if conn["guild_a"] == guild.id else conn["channel_b"]
            other_id = conn["channel_b"] if active_channel_id == conn["channel_a"] else conn["channel_a"]
            await self.db.remove_connection(conn["id"])
            if pb_cog:
                pb_cog._invalidate_connection(conn)
                pb_cog._cancel_inactivity(conn["id"])
                pb_cog._cancel_timeout(active_channel_id)
                pb_cog._cancel_queue_nudge(active_channel_id)
                pb_cog._call_reported_gifs.pop(conn["id"], None)
                pb_cog._clear_rl_state(conn["channel_a"])
                pb_cog._clear_rl_state(conn["channel_b"])
            report_cog = self.bot.get_cog("Report")
            if report_cog:
                report_cog.clear_log(conn["id"])
            if notify_partner:
                other = self.bot.get_channel(other_id)
                if other:
                    try:
                        await other.send("📵 The other server reset its Fliphone setup, so the call ended.")
                    except discord.HTTPException:
                        pass

        for queue_entry in await self.db.get_guild_queue_entries(guild.id):
            queue_channel_id = int(queue_entry["channel_id"])
            await self.db.remove_from_queue(queue_channel_id)
            if pb_cog:
                pb_cog._cancel_timeout(queue_channel_id)
                pb_cog._cancel_queue_nudge(queue_channel_id)

        for room_member in await self.db.get_guild_room_members(guild.id):
            member_channel_id = int(room_member["channel_id"])
            if room_cog and hasattr(room_cog, "_remove_member"):
                await room_cog._remove_member(
                    member_channel_id,
                    room_member["room_id"],
                    broadcast_reason=f"📡 **Station {room_member['station']}** reset its setup.",
                    notify_leaver=False,
                )
            else:
                await self.db.remove_room_member(member_channel_id)

        old_channel = self.bot.get_channel(channel_id)
        await self._delete_fliphone_webhooks(
            old_channel if isinstance(old_channel, discord.TextChannel) else None
        )
        await self.db.delete_guild(guild.id)
        if pb_cog:
            pb_cog._invalidate_config(guild_id=guild.id, channel_id=channel_id)

    async def send_gif_report_panel_interaction(self, interaction: discord.Interaction) -> None:
        if not await self._is_global_mod(interaction.user, interaction.guild):
            await interaction.response.send_message("You do not have permission to use this panel.", ephemeral=True)
            return
        pending = await self.db.get_pending_gif_reports()
        embed = self._build_gif_reports_embed(pending)
        view = self.create_gif_report_panel_view(pending)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _build_check_embed(
        self,
        guild: discord.Guild,
        invoking_channel: discord.TextChannel,
        probe_user: Optional[discord.Member] = None,
    ) -> tuple[discord.Embed, Optional[int]]:
        guild_cfg = await self.db.get_guild_config(guild.id)
        issues: list[str] = []
        missing: list[str] = []
        ok_lines: list[str] = []
        repair_channel_id: Optional[int] = None

        if not guild_cfg:
            embed = discord.Embed(
                title="Fliphone Setup Check",
                description=(
                    "This server is not set up yet.\n"
                    "Run `f.setup` in the channel you want to use."
                ),
                color=config.COLOR_WARN,
            )
            embed.set_footer(text=config.FOOTER)
            return embed, None

        channel_id = int(guild_cfg["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            issues.append(f"Configured channel `{channel_id}` is missing or inaccessible.")
            channel = invoking_channel
            repair_channel_id = invoking_channel.id
        else:
            ok_lines.append(f"Configured channel: {channel.mention}")
            repair_channel_id = channel.id

        bot_member = guild.me
        if bot_member is None:
            issues.append("Bot member object is not available yet.")
        else:
            perms = channel.permissions_for(bot_member)
            required = {
                "View Channel": perms.view_channel,
                "Send Messages": perms.send_messages,
                "Embed Links": perms.embed_links,
                "Read Message History": perms.read_message_history,
                "Manage Webhooks": perms.manage_webhooks,
                "Attach Files": perms.attach_files,
                "Add Reactions": perms.add_reactions,
            }
            missing = [name for name, has_perm in required.items() if not has_perm]
            if missing:
                issues.append("Missing permissions: " + ", ".join(missing))
            else:
                ok_lines.append("Core channel permissions look good.")

            if perms.manage_webhooks:
                ok_lines.append("Manage Webhooks is available.")
                bot_webhook = None
                try:
                    webhooks = await channel.webhooks()
                    bot_webhook = next(
                        (wh for wh in webhooks if wh.user == self.bot.user and wh.name == "Fliphone"),
                        None,
                    )
                    stored_url = guild_cfg.get("webhook_url")
                    if bot_webhook and stored_url == bot_webhook.url:
                        ok_lines.append("Relay webhook is present.")
                    elif bot_webhook:
                        issues.append("The stored relay webhook is stale. Run `f.repair`.")
                    elif len(webhooks) >= 15:
                        issues.append(
                            "This channel has Discord's maximum of 15 webhooks. Delete one or use another channel."
                        )
                    else:
                        issues.append("No Fliphone webhook found. Run `f.repair`.")
                except discord.Forbidden:
                    issues.append("Cannot inspect webhooks. Check channel/category permissions, then run `f.repair`.")
                except discord.HTTPException:
                    issues.append("Discord failed while checking webhooks. Try `f.check` again.")

                if bot_webhook and probe_user:
                    pb_cog = self.bot.get_cog("Phonebooth")
                    if pb_cog and hasattr(pb_cog, "probe_webhook_avatar"):
                        avatar_ok, avatar_issue = await pb_cog.probe_webhook_avatar(channel, probe_user)
                        if avatar_ok:
                            ok_lines.append("Webhook avatar delivery test passed.")
                        else:
                            issues.append(f"Webhook avatar delivery test failed: {avatar_issue}.")
            else:
                stored_webhook = await self.db.get_relay_webhook(channel.id)
                if stored_webhook:
                    ok_lines.append(
                        "An existing relay webhook is saved and can keep working, but automatic replacement is unavailable."
                    )
        q = await self.db.get_queue_entry(channel_id)
        conn = await self.db.get_connection(channel_id)
        room_member = await self.db.get_room_member(channel_id)
        if conn:
            ok_lines.append(f"Current state: active 1:1 call #{conn['id']}.")
        elif room_member:
            ok_lines.append(f"Current state: group room #{room_member['room_id']}.")
        elif q:
            ok_lines.append("Current state: waiting in queue.")
        else:
            ok_lines.append("Current state: idle.")

        embed = discord.Embed(
            title="Fliphone Setup Check",
            color=config.COLOR_OK if not issues else config.COLOR_WARN,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Healthy", value="\n".join(ok_lines) or "No checks passed.", inline=False)
        if issues:
            embed.add_field(
                name="Needs Attention",
                value="\n".join(f"- {issue}" for issue in issues),
                inline=False,
            )
            embed.add_field(
                name="Next Step",
                value=(
                    self._permission_recovery_guidance(guild, probe_user, channel, missing)
                    if missing and probe_user
                    else "Run `f.repair` to rebuild the damaged setup after active calls, searches, and rooms end."
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Next Step",
                value="Setup looks healthy. Users can run `f.call` in any text channel with the required permissions.",
                inline=False,
            )
        embed.set_footer(text=config.FOOTER)
        return embed, repair_channel_id if issues else None

    async def _reset_and_setup_locked(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        target: discord.TextChannel,
    ) -> discord.Embed:
        pb_cog = self.bot.get_cog("Phonebooth")
        permission_issues = (
            pb_cog.relay_permission_issues(target)
            if pb_cog and hasattr(pb_cog, "relay_permission_issues")
            else ["Phonebooth relay unavailable"]
        )
        if permission_issues:
            return discord.Embed(
                title="Setup Needs Permissions",
                description=(
                    f"Fliphone is missing: **{', '.join(permission_issues)}**\n\n"
                    f"{self._permission_recovery_guidance(guild, user, target, permission_issues)}\n\n"
                    "After permissions are fixed, run `f.setup` again in this channel."
                ),
                color=config.COLOR_ERR,
            )

        await self._clear_guild_setup_state(guild, notify_partner=True)
        webhook_url, webhook_issues = await pb_cog.rebuild_relay_webhook(target)
        if not webhook_url:
            return discord.Embed(
                title="Setup Could Not Finish",
                description=(
                    f"Discord blocked webhook creation: **{', '.join(webhook_issues)}**\n\n"
                    f"**[Re-invite Fliphone]({self._invite_url(guild.id)})**, then run `f.setup` again. "
                    "If it still fails, allow Manage Webhooks for Fliphone in this channel/category or use another channel."
                ),
                color=config.COLOR_ERR,
            )

        await self.db.setup_guild(
            guild_id=guild.id,
            channel_id=target.id,
            webhook_url=webhook_url,
            user_id=user.id,
        )
        pb_cog._invalidate_config(guild_id=guild.id, channel_id=target.id)

        avatar_ok, _ = await pb_cog.probe_webhook_avatar(target, user)
        embed = discord.Embed(title="Fliphone Is Ready", color=config.COLOR_OK, timestamp=datetime.utcnow())
        embed.add_field(name="Channel", value=target.mention, inline=True)
        embed.add_field(name="Relay", value="Fresh webhook created", inline=True)
        embed.add_field(name="Avatar Test", value="Passed" if avatar_ok else "Using safe fallback", inline=True)
        embed.add_field(
            name="Next Step",
            value="Users can run `f.call` in any text channel where Fliphone can view, send messages, and manage webhooks.",
            inline=False,
        )
        embed.add_field(
            name="Policies",
            value=f"[Privacy Policy]({config.PRIVACY_URL}) • [Terms of Service]({config.TOS_URL})",
            inline=False,
        )
        embed.set_footer(text=config.FOOTER)
        return embed

    async def _repair_setup(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        target: discord.TextChannel,
    ) -> discord.Embed:
        lock = self._setup_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            activity = await self._active_setup_activity(guild.id)
            if activity:
                return self._active_setup_embed(activity)
            return await self._reset_and_setup_locked(guild, user, target)

    # ── f.setup ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def setup(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Configure Fliphone once without resetting a healthy existing setup."""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send("❌ Fliphone setup requires a normal text channel.")
            return

        lock = self._setup_locks.setdefault(ctx.guild.id, asyncio.Lock())
        async with lock:
            guild_cfg = await self.db.get_guild_config(ctx.guild.id)
            if guild_cfg:
                configured_channel, issues = await self._configured_setup_issues(ctx.guild, guild_cfg)
                if not issues:
                    channel_label = configured_channel.mention if configured_channel else "the configured channel"
                    embed = discord.Embed(
                        title="Fliphone Is Already Ready",
                        description=(
                            f"This server already has a healthy setup in {channel_label}. "
                            "Nothing was reset.\n\n"
                            "Users can run `f.call` in any text channel where Fliphone has the required permissions. "
                            "Use `f.check` for diagnostics or `f.repair` only when setup is broken."
                        ),
                        color=config.COLOR_OK,
                    )
                else:
                    activity = await self._active_setup_activity(ctx.guild.id)
                    activity_note = (
                        "\n\nA reset is currently blocked because this server has:\n"
                        + "\n".join(f"- {item}" for item in activity)
                        if activity
                        else ""
                    )
                    embed = discord.Embed(
                        title="Setup Needs Repair",
                        description=(
                            "Fliphone found an existing setup and did not overwrite it.\n\n"
                            "**Problem:** " + "; ".join(issues)
                            + activity_note
                            + "\n\nRun `f.repair` after active calls, queue searches, and rooms have ended. "
                            "Use `f.repair #channel` if you need to move setup to a different channel."
                        ),
                        color=config.COLOR_WARN,
                    )
            else:
                activity = await self._active_setup_activity(ctx.guild.id)
                if activity:
                    embed = self._active_setup_embed(activity)
                else:
                    embed = await self._reset_and_setup_locked(ctx.guild, ctx.author, target)
        await ctx.send(embed=embed)

    # ── f.teardown ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="check", aliases=["setupcheck", "doctor"])
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def check(self, ctx: commands.Context) -> None:
        """Check whether this server's Fliphone setup is healthy."""
        embed, repair_channel_id = await self._build_check_embed(ctx.guild, ctx.channel, ctx.author)
        view = SetupCheckView(self, repair_channel_id) if repair_channel_id else None
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="repair", aliases=["fixsetup", "fix"])
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def repair(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Reset and rebuild a damaged setup when the server is idle."""
        guild_cfg = await self.db.get_guild_config(ctx.guild.id)
        target = channel
        if target is None and guild_cfg:
            maybe_channel = self.bot.get_channel(guild_cfg["channel_id"])
            if isinstance(maybe_channel, discord.TextChannel):
                target = maybe_channel
        if target is None:
            target = ctx.channel

        embed = await self._repair_setup(ctx.guild, ctx.author, target)
        await ctx.send(embed=embed)

    @commands.command(name="dbstatus", aliases=["database", "db"])
    @commands.guild_only()
    async def dbstatus(self, ctx: commands.Context) -> None:
        """Show database backend health without exposing private data."""
        if not await self._is_global_mod(ctx.author, ctx.guild):
            await ctx.send("❌ Only the bot owner and trusted mods can use this command.")
            return
        try:
            async def timed(operation):
                started = time.perf_counter()
                result = await operation
                return result, int((time.perf_counter() - started) * 1000)

            total_started = time.perf_counter()
            (ok, ping_ms), (counts, counts_ms) = await asyncio.gather(
                timed(self.db.ping()),
                timed(self.db.table_counts()),
            )
            latency_ms = int((time.perf_counter() - total_started) * 1000)
        except Exception as exc:
            embed = discord.Embed(
                title="Database Status",
                description=f"Database check failed: `{type(exc).__name__}`",
                color=config.COLOR_ERR,
            )
            embed.set_footer(text=config.FOOTER)
            await ctx.send(embed=embed)
            return

        backend = "PostgreSQL" if self.db.backend == "postgres" else "SQLite"
        location = "DATABASE_URL" if self.db.backend == "postgres" else self.db.path
        table_lines = [
            f"`{table}`: {counts.get(table, 0)}"
            for table in TABLE_ORDER
            if counts.get(table, 0)
        ]
        if not table_lines:
            table_lines = ["All tables are currently empty."]

        embed = discord.Embed(
            title="Database Status",
            color=config.COLOR_OK if ok else config.COLOR_ERR,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Backend", value=backend, inline=True)
        embed.add_field(name="Connection", value="OK" if ok else "Failed", inline=True)
        embed.add_field(name="Total Check", value=f"{latency_ms} ms", inline=True)
        embed.add_field(name="Ping", value=f"{ping_ms} ms", inline=True)
        embed.add_field(name="Counts", value=f"{counts_ms} ms", inline=True)
        embed.add_field(name="Location", value=f"`{location}`", inline=False)
        embed.add_field(
            name="Core Counts",
            value=(
                f"Configured servers: **{counts.get('guild_config', 0)}**\n"
                f"Queue: **{counts.get('queue', 0)}**\n"
                f"Active calls: **{counts.get('connections', 0)}**\n"
                f"Call history: **{counts.get('call_history', 0)}**"
            ),
            inline=False,
        )
        embed.add_field(name="Non-empty Tables", value="\n".join(table_lines[:12]), inline=False)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    @commands.command(name="teardown", aliases=["remove"])
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def teardown(self, ctx: commands.Context) -> None:
        """Completely remove Fliphone state so the next f.setup starts clean."""
        await ctx.send("Type `confirm` within 20 seconds to remove all Fliphone setup for this server.")
        try:
            confirmation = await self.bot.wait_for(
                "message",
                timeout=20,
                check=lambda message: message.author == ctx.author and message.channel == ctx.channel,
            )
        except asyncio.TimeoutError:
            await ctx.send("Teardown cancelled.")
            return
        if confirmation.content.strip().lower() != "confirm":
            await ctx.send("Teardown cancelled.")
            return
        lock = self._setup_locks.setdefault(ctx.guild.id, asyncio.Lock())
        async with lock:
            guild_cfg = await self.db.get_guild_config(ctx.guild.id)
            if not guild_cfg:
                await self._delete_fliphone_webhooks(
                    ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
                )
                await ctx.send("📵 Fliphone was already unconfigured. Any webhook in this channel was cleaned up.")
                return

            await self._clear_guild_setup_state(ctx.guild, notify_partner=True)
            await ctx.send("📵 Fliphone was fully removed. Run `f.setup` in the channel you want to use.")

    # ── f.stats ───────────────────────────────────────────────────────────────

    @commands.command(name="stats")
    async def stats(self, ctx: commands.Context) -> None:
        """Display global Fliphone statistics."""
        configured_guild_ids = set(await self.db.get_configured_guild_ids())
        current_guild_ids = {guild.id for guild in self.bot.guilds}
        active_configured = len(configured_guild_ids & current_guild_ids)
        stale_configs = len(configured_guild_ids - current_guild_ids)

        embed = discord.Embed(title="📊 Fliphone — Statistics", color=config.COLOR_WAIT, timestamp=datetime.utcnow())
        embed.add_field(name="🔴 Active Calls",       value=str(await self.db.get_active_connection_count()), inline=True)
        embed.add_field(name="⏳ In Queue",           value=str(await self.db.get_queue_size()),              inline=True)
        embed.add_field(name="📚 All-Time Calls",     value=str(await self.db.get_total_calls()),             inline=True)
        embed.add_field(name="🏠 Configured Servers", value=str(active_configured),                           inline=True)
        embed.add_field(name="🤖 Bot In Servers",     value=str(len(self.bot.guilds)),                       inline=True)
        if stale_configs:
            embed.add_field(name="Old Configs", value=str(stale_configs), inline=True)
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    # ── f.invite ─────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="invite")
    async def invite(self, ctx: commands.Context) -> None:
        """Get the bot invite link."""
        url = self._invite_url(ctx.guild.id if ctx.guild else None)
        embed = discord.Embed(
            title="📞 Add Fliphone to Your Server",
            description=(
                f"**[➕ Click here to invite the bot]({url})**\n\n"
                "Fliphone connects your server to a random server for an anonymous "
                "cross-server chat. Run `f.setup` in any channel after inviting!"
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="Required Permissions",
            value=(
                "• View Channel\n"
                "• Send Messages\n"
                "• Manage Webhooks *(required for relay)*\n"
                "• Embed Links\n"
                "• Attach Files\n"
                "• Read Message History\n"
                "• Add Reactions"
            ),
            inline=False,
        )
        embed.add_field(
            name="Getting Started",
            value=(
                "1. Invite the bot\n"
                "2. Run `f.setup` in your chosen channel\n"
                "3. Type `f.call` to connect!"
            ),
            inline=False,
        )
        embed.add_field(
            name="Permission Note",
            value=(
                "Channel and category overrides can still hide Fliphone after installation. "
                "If setup reports an override, a server owner or someone with Manage Roles must allow "
                "Fliphone in that channel/category."
            ),
            inline=False,
        )
        if isinstance(ctx.author, discord.Member):
            member_permissions = ctx.author.guild_permissions
            requested = {
                "View Channels": member_permissions.view_channel,
                "Send Messages": member_permissions.send_messages,
                "Manage Webhooks": member_permissions.manage_webhooks,
                "Embed Links": member_permissions.embed_links,
                "Attach Files": member_permissions.attach_files,
                "Read Message History": member_permissions.read_message_history,
                "Add Reactions": member_permissions.add_reactions,
            }
            cannot_grant = [name for name, allowed in requested.items() if not allowed]
            if cannot_grant and not member_permissions.administrator:
                embed.add_field(
                    name="You Cannot Grant Every Permission",
                    value=(
                        "Discord will omit **"
                        + ", ".join(cannot_grant)
                        + "** if you authorize this invite. Send the invite link to the server owner or an "
                        "admin who has those permissions instead."
                    ),
                    inline=False,
                )
        embed.add_field(
            name="Support Server",
            value="[Join here](https://discord.gg/t3KHGqPuEP)",
            inline=False,
        )
        embed.set_footer(text=config.FOOTER)
        await ctx.send(embed=embed)

    # ── f.blocklist ───────────────────────────────────────────────────────────

    @commands.command(name="blocklist", aliases=["blocked"])
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def blocklist(self, ctx: commands.Context) -> None:
        """List servers blocked by this server."""
        blocked = await self.db.get_blocked_guilds(ctx.guild.id)
        if not blocked:
            await ctx.send("✅ You haven't blocked any servers.")
            return

        lines = []
        for entry in blocked:
            g    = self.bot.get_guild(entry["blocked_guild_id"])
            name = g.name if g else "Unknown Server"
            lines.append(f"• **{name}** — ID: `{entry['blocked_guild_id']}`")

        embed = discord.Embed(title="🚫 Blocked Servers", description="\n".join(lines), color=config.COLOR_ERR)
        embed.set_footer(text=f"Use f.unblock <id> to unblock  •  {config.FOOTER}")
        await ctx.send(embed=embed)

    # ── f.unblock ─────────────────────────────────────────────────────────────

    @commands.command(name="unblock")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def unblock(self, ctx: commands.Context, server_id: int) -> None:
        """Unblock a server."""
        removed = await self.db.unblock_guild(ctx.guild.id, server_id)
        if not removed:
            await ctx.send(f"❌ Server `{server_id}` isn't in your blocklist.")
            return
        g    = self.bot.get_guild(server_id)
        name = g.name if g else f"Server {server_id}"
        await ctx.send(f"✅ **{name}** has been unblocked.")

    # ── f.kick ────────────────────────────────────────────────────────────────

    @commands.command(name="kick")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def kick(self, ctx: commands.Context) -> None:
        """Force-disconnect the active call."""
        guild_cfg = await self.db.get_guild_config(ctx.guild.id)
        if not guild_cfg:
            await ctx.send("❌ Phonebooth isn't configured. Run `f.setup` first.")
            return

        conn = await self.db.get_connection(ctx.channel.id)
        if not conn:
            await ctx.send("❌ No active call to disconnect.")
            return

        ch_id     = ctx.channel.id
        other_cid = conn["channel_b"] if ch_id == conn["channel_a"] else conn["channel_a"]
        await self.db.remove_connection(conn["id"], ended_by=ctx.author.id)
        pb_cog = self.bot.get_cog("Phonebooth")
        if pb_cog and hasattr(pb_cog, "_invalidate_connection"):
            pb_cog._invalidate_connection(conn)
        await ctx.send("📵 Call force-disconnected by admin.")

        other_ch = self.bot.get_channel(other_cid)
        if other_ch:
            try:
                await other_ch.send("📵 Other server has ended the call!")
            except discord.HTTPException:
                pass

    # ── f.censor ──────────────────────────────────────────────────────────────

    @commands.command(name="censor")
    async def censor(self, ctx, *, word: str) -> None:
        """
        [Bot owner + trusted mods] Add or remove a word from the censor list.
        If the word is already censored, it will be removed (toggle).
        Usage: f.censor <word or phrase>
        """
        if not await self._is_global_mod(ctx.author, ctx.guild):
            await ctx.send("❌ Only the bot owner and trusted mods can use this command.")
            return

        import filter as flt
        word = word.lower().strip()
        current = await self.db.get_custom_words()

        if word in current:
            removed = await self.db.remove_custom_word(word)
            if removed:
                flt.load_custom_words(await self.db.get_custom_words())
                await ctx.send(
                    embed=discord.Embed(
                        title="Censor list updated",
                        description=f"Removed `{word}` from the censor list.",
                        color=config.COLOR_OK,
                    )
                )
            else:
                await ctx.send(f"❌ Could not remove `{word}`.")
        else:
            await self.db.add_custom_word(word, ctx.author.id)
            flt.load_custom_words(await self.db.get_custom_words())
            await ctx.send(
                embed=discord.Embed(
                    title="Censor list updated",
                    description=f"Added `{word}` to the censor list.",
                    color=config.COLOR_OK,
                )
            )

    @commands.command(name="censorlist")
    async def censorlist(self, ctx) -> None:
        """[Bot owner + trusted mods] List all custom censored words."""
        if not await self._is_global_mod(ctx.author, ctx.guild):
            await ctx.send("❌ Only the bot owner and trusted mods can use this command.")
            return

        import filter as flt
        words = await self.db.get_custom_words()
        hardcoded_count = len(flt._BLOCKED)
        if not words:
            await ctx.send(
                embed=discord.Embed(
                    description=f"No custom words added yet. ({hardcoded_count} hardcoded words always active)",
                    color=config.COLOR_WAIT,
                )
            )
            return
        embed = discord.Embed(
            title="Custom censor list",
            description="\n".join(f"`{w}`" for w in words),
            color=config.COLOR_WAIT,
        )
        embed.set_footer(text=f"+{hardcoded_count} hardcoded words always active")
        await ctx.send(embed=embed)

    # ── f.ban ─────────────────────────────────────────────────────────────────

    @commands.command(name="ban")
    async def ban_user(self, ctx: commands.Context, user_id: int, *, reason: str = "No reason given") -> None:
        """[Bot owner + trusted mods] Ban a user ID across all servers."""
        if not await self._is_global_mod(ctx.author, ctx.guild):
            await ctx.send("❌ Only the bot owner and trusted mods can use this command.")
            return
        guild = self.bot.get_guild(user_id)
        if guild:
            await ctx.send(
                f"⚠️ `{user_id}` is the server ID for **{guild.name}**, not a user ID. "
                f"Use `f.serverban {user_id} <reason>` to ban that server."
            )
            return
        await self.db.ban_user(user_id, ctx.author.id, reason)
        user = self.bot.get_user(user_id)
        name = str(user) if user else f"User {user_id}"
        await ctx.send(f"🔨 **{name}** (`{user_id}`) has been banned from Phonebooth.\nReason: {reason}")

    # ── f.unban ───────────────────────────────────────────────────────────────

    @commands.command(name="unban")
    async def unban_user(self, ctx: commands.Context, user_id: int) -> None:
        """[Bot owner + trusted mods] Unban a user from Phonebooth."""
        if not await self._is_global_mod(ctx.author, ctx.guild):
            await ctx.send("❌ Only the bot owner and trusted mods can use this command.")
            return
        removed = await self.db.unban_user(user_id)
        if not removed:
            await ctx.send(f"❌ User `{user_id}` isn't banned.")
            return
        user = self.bot.get_user(user_id)
        name = str(user) if user else f"User {user_id}"
        await ctx.send(f"✅ **{name}** has been unbanned.")

    # ── Shared GIF-mod permission check ──────────────────────────────────────

    async def _is_gif_mod(self, ctx: commands.Context) -> bool:
        """
        Returns True if the caller is allowed to run GIF moderation commands.
        Allowed if:
          - Bot owner
          - User ID is in config.TRUSTED_MOD_IDS
          - The command is run inside a guild in config.TRUSTED_GUILD_IDS
            AND the caller has administrator permission in that guild
        """
        return await self._is_global_mod(ctx.author, ctx.guild)

    async def _is_global_mod(
        self,
        user: discord.abc.User,
        guild: Optional[discord.Guild],
    ) -> bool:
        if await self.bot.is_owner(user):
            return True
        if user.id in config.TRUSTED_MOD_IDS:
            return True
        if not isinstance(user, discord.Member):
            return False
        return False

    # ── f.notifyignore ────────────────────────────────────────────────────────

    @commands.command(name="notifyignore")
    @commands.is_owner()
    async def notifyignore(self, ctx: commands.Context, user_id: int) -> None:
        """[Bot owner only] Toggle a user ID on/off the notify ignore list (e.g. testers)."""
        if user_id in config.NOTIFY_IGNORE_IDS:
            config.NOTIFY_IGNORE_IDS.discard(user_id)
            await ctx.send(f"✅ `{user_id}` removed from notify ignore list — their queues will now trigger notifications.")
        else:
            config.NOTIFY_IGNORE_IDS.add(user_id)
            await ctx.send(f"✅ `{user_id}` added to notify ignore list — their queues will not trigger notifications.")

    # ── f.gifreports ──────────────────────────────────────────────────────────

    @commands.command(name="gifreports")
    async def gifreports(self, ctx: commands.Context) -> None:
        """[GIF mods] List all GIF reports awaiting review."""
        if not await self._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return
        pending = await self.db.get_pending_gif_reports()
        embed = self._build_gif_reports_embed(pending)
        view = self.create_gif_report_panel_view(pending)
        await ctx.send(embed=embed, view=view)
        return
        if not pending:
            await ctx.send("✅ No pending GIF reports.")
            return

        lines = []
        for r in pending[:20]:  # cap at 20 to avoid embed overflow
            reported_at = r["reported_at"][:10] if r["reported_at"] else "?"
            url_short   = r["url"][:60] + "…" if len(r["url"]) > 60 else r["url"]
            reporter    = f"<@{r['reporter_id']}>" if r["reporter_id"] else "unknown"
            lines.append(
                f"**#{r['id']}** — {reported_at} — {reporter}\n"
                f"└ `{url_short}`"
            )

        embed = discord.Embed(
            title=f"🚩 Pending GIF Reports ({len(pending)})",
            description="\n".join(lines),
            color=config.COLOR_WARN,
        )
        embed.set_footer(
            text="f.gifbl <id or url>  →  blacklist  |  f.gifwl <id or url>  →  whitelist"
        )
        await ctx.send(embed=embed)

    # ── f.gifbl ───────────────────────────────────────────────────────────────

    def _build_gif_reports_embed(self, pending: list[dict]) -> discord.Embed:
        if not pending:
            embed = discord.Embed(
                title="GIF Report Panel",
                description="No pending GIF reports.",
                color=config.COLOR_OK,
            )
            embed.set_footer(text=config.FOOTER)
            return embed

        embed = discord.Embed(
            title=f"GIF Report Panel ({len(pending)} pending)",
            description="Select a report below, then blacklist or whitelist it.",
            color=config.COLOR_WARN,
        )
        for report in pending[:10]:
            reported_at = report["reported_at"][:10] if report["reported_at"] else "?"
            url = report["url"] or ""
            reporter = f"<@{report['reporter_id']}>" if report["reporter_id"] else "unknown"
            embed.add_field(
                name=f"#{report['id']} - {reported_at} - {reporter}",
                value=f"```\n{url[:950]}\n```",
                inline=False,
            )
        if len(pending) > 10:
            embed.add_field(
                name="More Reports",
                value=f"{len(pending) - 10} more pending. Use the select menu to inspect them.",
                inline=False,
            )
        embed.set_footer(text="Use the select menu/buttons, or f.gifbl <id/url> and f.gifwl <id/url>.")
        return embed

    @commands.command(name="gifbl")
    async def gifbl(self, ctx: commands.Context, *, id_or_url: str) -> None:
        """[GIF mods] Blacklist a GIF URL. Pass a report ID or full URL."""
        if not await self._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return
        url = await self._resolve_gif_arg(ctx, id_or_url, action="blacklist")
        if not url:
            return

        await self.db.set_gif_url_status(url, "blacklist", ctx.author.id)
        if id_or_url.strip().isdigit():
            report = await self.db.get_gif_report(int(id_or_url.strip()))
            await self.db.resolve_gif_report(int(id_or_url.strip()), "blacklisted")
            if report:
                await self._delete_gif_report_review_message(report)

        embed = discord.Embed(
            title="🚫 GIF Blacklisted",
            description=f"This URL is now blocked. Future relay attempts will be rejected.\n```\n{url[:900]}\n```",
            color=config.COLOR_ERR,
        )
        await ctx.send(embed=embed)

    # ── f.gifwl ───────────────────────────────────────────────────────────────

    @commands.command(name="gifwl")
    async def gifwl(self, ctx: commands.Context, *, id_or_url: str) -> None:
        """[GIF mods] Whitelist a GIF URL. Pass a report ID or full URL."""
        if not await self._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return
        url = await self._resolve_gif_arg(ctx, id_or_url, action="whitelist")
        if not url:
            return

        await self.db.set_gif_url_status(url, "whitelist", ctx.author.id)
        if id_or_url.strip().isdigit():
            report = await self.db.get_gif_report(int(id_or_url.strip()))
            await self.db.resolve_gif_report(int(id_or_url.strip()), "whitelisted")
            if report:
                await self._delete_gif_report_review_message(report)

        embed = discord.Embed(
            title="✅ GIF Whitelisted",
            description=f"This URL is now trusted. The report button will not work for it.\n```\n{url[:900]}\n```",
            color=config.COLOR_OK,
        )
        await ctx.send(embed=embed)

    # ── f.gifcheck ────────────────────────────────────────────────────────────

    @commands.command(name="gifcheck")
    async def gifcheck(self, ctx: commands.Context, *, url: str) -> None:
        """[GIF mods] Check whether a URL is on the blacklist or whitelist."""
        if not await self._is_gif_mod(ctx):
            await ctx.send("❌ You don't have permission to use this command.")
            return
        status = await self.db.check_gif_url(url)
        if status == "blacklist":
            await ctx.send("🚫 **Blacklisted** — this URL will be blocked when relayed.")
        elif status == "whitelist":
            await ctx.send("✅ **Whitelisted** — this URL is trusted and cannot be reported.")
        else:
            await ctx.send("❔ **Not listed** — this URL has no special status.")

    # ── Helper ────────────────────────────────────────────────────────────────

    async def _resolve_gif_arg(self, ctx, id_or_url: str, action: str) -> Optional[str]:
        """
        If the argument looks like a number, treat it as a report_id and fetch
        the URL from the DB.  Otherwise treat the argument as the URL directly.
        Returns the URL string, or None if resolution failed.
        """
        id_or_url = id_or_url.strip()
        if id_or_url.isdigit():
            report = await self.db.get_gif_report(int(id_or_url))
            if not report:
                await ctx.send(f"❌ No report found with ID `{id_or_url}`.")
                return None
            return report["url"]
        elif id_or_url.startswith("http"):
            return id_or_url
        else:
            await ctx.send(
                f"❌ Pass a report ID (number) or a full URL starting with `http`.\n"
                f"Example: `f.gif{action[:2]} 42` or `f.gif{action[:2]} https://tenor.com/view/...`"
            )
            return None
    # ── f.pb (legacy group kept for backwards compat) ─────────────────────────

    @commands.group(name="pb", invoke_without_command=True, case_insensitive=True)
    async def pb(self, ctx: commands.Context) -> None:
        """Phonebooth admin commands. Use f.setup, f.teardown, etc. directly now."""
        await ctx.send(
            "📞 **Phonebooth V2** — Commands:\n"
            "`f.call` `f.hangup` `f.skip` `f.block` `f.fr` `f.anon`\n"
            "`f.setup` `f.check` `f.repair` `f.dbstatus` `f.teardown`\n"
            "`f.stats` `f.invite` `f.blocklist` `f.unblock` `f.kick`"
        )

    @pb.command(name="setup")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pb_setup(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.invoke(self.setup, channel=channel)

    @pb.command(name="check")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pb_check(self, ctx: commands.Context) -> None:
        await ctx.invoke(self.check)

    @pb.command(name="repair")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pb_repair(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        await ctx.invoke(self.repair, channel=channel)

    @pb.command(name="dbstatus")
    @commands.guild_only()
    async def pb_dbstatus(self, ctx: commands.Context) -> None:
        await ctx.invoke(self.dbstatus)

    @pb.command(name="teardown")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pb_teardown(self, ctx: commands.Context) -> None:
        await ctx.invoke(self.teardown)

    @pb.command(name="anon")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def pb_anon(self, ctx: commands.Context) -> None:
        pb_cog = self.bot.get_cog("Phonebooth")
        if pb_cog:
            await ctx.invoke(pb_cog.anon)

    @pb.command(name="stats")
    async def pb_stats(self, ctx: commands.Context) -> None:
        await ctx.invoke(self.stats)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_command_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Manage Channels** permission for that.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("❌ Only the bot owner can use that command.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Bad argument: `{error}`")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Admin(bot))
