"""Shared relay policy for calls, rooms, and GIF submissions."""

from __future__ import annotations

import re
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import discord


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d+)>")
ANIMATED_CUSTOM_EMOJI_RE = re.compile(r"<a:(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d+)>")
PROVIDER_HOSTS = {
    "tenor.com",
    "giphy.com",
    "klipy.com",
    "static.klipy.com",
}


class CustomEmojiCandidate(NamedTuple):
    markup: str
    name: str
    emoji_id: int
    animated: bool


def is_local_only(content: str) -> bool:
    return content.startswith(("x ", "X "))


def contains_custom_emoji(content: str) -> bool:
    return bool(CUSTOM_EMOJI_RE.search(content))


def extract_custom_emojis(content: str, *, limit: Optional[int] = None) -> list[CustomEmojiCandidate]:
    emojis: list[CustomEmojiCandidate] = []
    seen: set[int] = set()
    for match in CUSTOM_EMOJI_RE.finditer(content or ""):
        emoji_id = int(match.group("id"))
        if emoji_id in seen:
            continue
        seen.add(emoji_id)
        emojis.append(
            CustomEmojiCandidate(
                markup=match.group(0),
                name=match.group("name"),
                emoji_id=emoji_id,
                animated=bool(match.group("animated")),
            )
        )
        if limit is not None and len(emojis) >= limit:
            break
    return emojis


def custom_emoji_asset_url(emoji_id: int, animated: bool) -> str:
    ext = "gif" if animated else "png"
    return f"https://cdn.discordapp.com/emojis/{int(emoji_id)}.{ext}"


def custom_emoji_preview_url(emoji_id: int, animated: bool) -> str:
    suffix = "?animated=true&size=128&quality=lossless" if animated else "?size=128&quality=lossless"
    return f"https://cdn.discordapp.com/emojis/{int(emoji_id)}.webp{suffix}"


def downgrade_animated_custom_emoji_markup(content: str) -> tuple[str, list[int]]:
    app_emoji_ids: list[int] = []

    def _replacement(match: re.Match[str]) -> str:
        app_emoji_ids.append(int(match.group("id")))
        return f"<:{match.group('name')}:{match.group('id')}>"

    return ANIMATED_CUSTOM_EMOJI_RE.sub(_replacement, content or ""), app_emoji_ids


def plain_custom_emoji_fallback(content: str, replacement: str = "emoji") -> str:
    return CUSTOM_EMOJI_RE.sub(replacement, content or "")


async def replace_approved_custom_emojis(content: str, db: object) -> tuple[str, list[int]]:
    """Replace approved submitted emoji with mirrored application emoji markup.

    Unknown, pending, rejected, blacklisted, or deleted emoji are stripped here.
    Keeping this centralized makes future premium gating a single policy change.
    """
    if not content or not CUSTOM_EMOJI_RE.search(content):
        return content, []

    emoji_ids = [
        int(match.group("id"))
        for match in CUSTOM_EMOJI_RE.finditer(content)
    ]
    approved_by_original = await db.get_approved_emoji_submissions(emoji_ids)
    app_lookup_ids = [
        emoji_id for emoji_id in dict.fromkeys(emoji_ids)
        if emoji_id not in approved_by_original
    ]
    approved_by_app = await db.get_approved_emoji_submissions_by_app_ids(app_lookup_ids)
    used_ids: list[int] = []
    seen_used: set[int] = set()

    def _is_animated(value: object) -> bool:
        if isinstance(value, str):
            return value.lower() in {"1", "true", "t", "yes"}
        return bool(value)

    def _replacement(match: re.Match[str]) -> str:
        emoji_id = int(match.group("id"))
        row = approved_by_original.get(emoji_id) or approved_by_app.get(emoji_id)
        if not row or not row.get("app_emoji_id"):
            return ""
        original_id = int(row["original_emoji_id"])
        if original_id not in seen_used:
            used_ids.append(original_id)
            seen_used.add(original_id)
        prefix = "a" if _is_animated(row.get("app_emoji_animated")) else ""
        name = str(row.get("app_emoji_name") or row.get("original_name") or match.group("name"))
        return f"<{prefix}:{name}:{int(row['app_emoji_id'])}>"

    return CUSTOM_EMOJI_RE.sub(_replacement, content), used_ids


def extract_urls(content: str) -> list[str]:
    return [match.rstrip(".,);]") for match in URL_RE.findall(content or "")]


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{host}{port}{path}{query}"


def is_provider_gif(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in PROVIDER_HOSTS)


def is_direct_gif_url(url: str) -> bool:
    try:
        return urlsplit(url).path.lower().endswith(".gif")
    except ValueError:
        return False


def animated_gif_attachment(message: discord.Message) -> Optional[discord.Attachment]:
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        if content_type == "image/gif" or attachment.filename.lower().endswith(".gif"):
            return attachment
    return None


def extract_gif_candidate(message: discord.Message) -> Optional[str]:
    for url in extract_urls(message.content):
        if is_provider_gif(url) or is_direct_gif_url(url):
            return url
    attachment = animated_gif_attachment(message)
    return attachment.url if attachment else None
