"""Small shared helpers: slugs, wikilinks, dates."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "item"


def wikilink(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip()
    if name.startswith("[[") and name.endswith("]]"):
        return name
    return f"[[{name}]]"


def unlink(value: str | None) -> str | None:
    """'[[Name|alias]]' -> 'Name'."""
    if not value:
        return None
    m = _WIKILINK.fullmatch(value.strip())
    return m.group(1).strip() if m else value.strip()


def find_wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK.finditer(text or "")]


def safe_filename(name: str) -> str:
    """Obsidian-safe note title: strip characters that break filenames or links."""
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "untitled"


def now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def today_in(tz: str) -> date:
    return now_in(tz).date()


def parse_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def days_between(a: date | None, b: date | None) -> int | None:
    if a is None or b is None:
        return None
    return (b - a).days


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def humanize_age(days: int | None) -> str:
    if days is None:
        return "?"
    if days == 0:
        return "today"
    if days == 1:
        return "1d"
    return f"{days}d"


def window_start(today: date, days: int) -> date:
    return today - timedelta(days=days)
