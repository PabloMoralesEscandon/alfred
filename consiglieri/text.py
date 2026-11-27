"""Text helpers for Telegram's message size limit."""

from __future__ import annotations

from typing import List

TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = 4000) -> List[str]:
    """Split on paragraph, then line, then word boundaries so no chunk exceeds `limit`."""
    text = text.strip()
    chunks: List[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = window.rfind(separator)
            if cut > limit // 2:
                break
            cut = -1
        if cut == -1:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
