"""Shared relay policy for calls, rooms, and GIF submissions."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

import discord


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d+>")
PROVIDER_HOSTS = {
    "tenor.com",
    "giphy.com",
    "klipy.com",
    "static.klipy.com",
}


def is_local_only(content: str) -> bool:
    return content.startswith(("x ", "X "))


def contains_custom_emoji(content: str) -> bool:
    return bool(CUSTOM_EMOJI_RE.search(content))


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
