"""Markdown -> HTML for vault notes, with Obsidian wikilinks turned into local routes."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

import markdown

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_EXTENSIONS = ["tables", "fenced_code"]


def note_url(target: str) -> str:
    """Route for a wikilink target: briefs, reviews, radar/memory, else a note by id."""
    t = target.strip()
    low = t.lower()
    if low.startswith("briefs/"):
        return f"/briefs/{quote(t.split('/', 1)[1])}"
    if low.startswith("reviews/"):
        return f"/reviews/{quote(t.split('/', 1)[1])}"
    if low in ("radar", "radar.md"):
        return "/radar"
    if low in ("memory", "memory.md"):
        return "/memory"
    if "/" in t:  # Items/xyz, People/Alice ...
        t = t.rsplit("/", 1)[1]
    return f"/note/{quote(t)}"


def wikilinks_to_html(text: str, item_ids: set[str] | None = None) -> str:
    """Wikilinks become local links. A link whose target is a tracked item is tagged so the
    portal can open the assist drawer over it instead of navigating away."""

    def repl(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        label = (m.group(2) or target).strip()
        stem = target.rsplit("/", 1)[-1]
        attrs = ""
        if item_ids and stem in item_ids:
            attrs = f' data-item="{html.escape(stem, quote=True)}"'
        return f'<a class="wikilink" href="{note_url(target)}"{attrs}>{html.escape(label)}</a>'

    return _WIKILINK.sub(repl, text or "")


_CALLOUT = re.compile(r"^(>\s*)\[!(\w+)\][+-]?\s*(.*)$", re.MULTILINE)


def _callouts(text: str) -> str:
    """Obsidian `> [!info] Title` -> a bold title line inside the blockquote."""
    return _CALLOUT.sub(lambda m: f"{m.group(1)}**{m.group(3) or m.group(2).title()}**  ", text)


def render_markdown(text: str | None, item_ids: set[str] | None = None) -> str:
    """Render note markdown. Wikilinks become links; callouts become titled blockquotes.

    Pass `item_ids` (the ids of tracked items) so links to them open the assist drawer."""
    if not text:
        return ""
    return markdown.markdown(wikilinks_to_html(_callouts(text), item_ids), extensions=_EXTENSIONS)


def render_value(value: Any) -> str:
    """Frontmatter value -> inline HTML (wikilinks linked, lists comma-joined)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(render_value(v) for v in value)
    if isinstance(value, dict):
        return html.escape(", ".join(f"{k}: {v}" for k, v in value.items()))
    s = str(value)
    if _WIKILINK.fullmatch(s.strip()):
        return wikilinks_to_html(s)
    if s.startswith(("http://", "https://")):
        return f'<a href="{html.escape(s)}" rel="noopener" target="_blank">{html.escape(s)}</a>'
    return html.escape(s)
