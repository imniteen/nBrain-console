"""Hard caps from brief-format.md. Limits, not guidance."""

from __future__ import annotations

import re
from collections.abc import Sequence

TOTAL_WORDS = 550
ONE_THING_SENTENCES = 2
DUE_TODAY_MAX = 3
ASSESSMENT_SENTENCES = 4
ACTIONS_MAX = 5
WAITING_MAX = 5
SLIPPING_MAX = 3
TOMORROW_LINES = 3
CHANGED_LINES = 3
URGENT_LINES_MAX = 6
HTML_MAX_BYTES = 40_000

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s for s in _SENT.split(text) if s]


def cap_sentences(text: str, n: int) -> str:
    return " ".join(sentences(text)[:n])


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w[\w'’-]*\b", text))


def cap_words(text: str, n: int) -> str:
    words = text.split()
    if len(words) <= n:
        return text
    return " ".join(words[:n]).rstrip(",;:") + "…"


def top_n[T](items: Sequence[T], n: int) -> tuple[list[T], int]:
    """Return (shown, hidden_count)."""
    shown = list(items[:n])
    return shown, max(0, len(items) - n)


def more_note(hidden: int, where: str = "radar.md") -> str:
    return f"+{hidden} more in {where}" if hidden > 0 else ""


def one_line(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
