"""messaging.py - Send long messages to Telegram safely (4096-character limit)."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900  # headroom below the hard limit
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # keep the brief compact


def _hard_split(paragraph: str, limit: int) -> list[str]:
    """Split an over-long paragraph on line breaks (and, as a last resort, mid-line)."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces, current = [], ""
    for line in paragraph.split("\n"):
        while len(line) > limit:  # a single giant line: cut it
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split text into chunks <= limit, preferring paragraph (blank-line) boundaries."""
    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        for piece in _hard_split(paragraph, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _strip_tags(text: str) -> str:
    """Plain-text fallback used if Telegram rejects the HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def send_long_message(bot: Bot, chat_id: int, text: str) -> None:
    """Send text as one or more HTML messages; fall back to plain text on parse errors."""
    for chunk in split_message(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML,
                                   link_preview_options=NO_PREVIEW)
        except BadRequest as exc:  # e.g. malformed tag produced by the LLM
            log.warning("HTML rejected by Telegram (%s); resending as plain text", exc)
            await bot.send_message(chat_id=chat_id, text=_strip_tags(chunk),
                                   link_preview_options=NO_PREVIEW)
        await asyncio.sleep(0.5)  # gentle on rate limits
