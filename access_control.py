"""Central authorization checks for Fliphone staff tools."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config


async def is_trusted_moderator(bot: commands.Bot, user: discord.abc.User) -> bool:
    """Return whether a user may use global moderation tools."""
    return await bot.is_owner(user) or user.id in config.TRUSTED_MOD_IDS


def trusted_moderator_only():
    """Apply the trusted moderator check to text and application commands."""

    async def command_predicate(ctx: commands.Context) -> bool:
        return await is_trusted_moderator(ctx.bot, ctx.author)

    async def interaction_predicate(interaction: discord.Interaction) -> bool:
        return await is_trusted_moderator(interaction.client, interaction.user)

    def decorator(target):
        target = commands.check(command_predicate)(target)
        target = app_commands.check(interaction_predicate)(target)
        return target

    return decorator


def owner_only():
    """Apply the bot owner check to text and application commands."""

    async def command_predicate(ctx: commands.Context) -> bool:
        return await ctx.bot.is_owner(ctx.author)

    async def interaction_predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    def decorator(target):
        target = commands.check(command_predicate)(target)
        target = app_commands.check(interaction_predicate)(target)
        return target

    return decorator
