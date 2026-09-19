"""Assemble the brief from metrics + LLM draft, enforce caps, render markdown and email HTML."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nbrain.brief import caps
from nbrain.config.schema import RoleTrack
from nbrain.llm.tasks import Action, BriefDraft
from nbrain.sources.base import CalendarEvent
from nbrain.sweep.metrics import Metrics
from nbrain.util import humanize_age, unlink
from nbrain.vault.schema import Item, Verified

TEMPLATES = Path(__file__).parent / "templates"

PALETTE = {
    # The portal's light-mode tokens, as literal hex: email has no CSS variables and no
    # reliable dark mode, so this is deliberately a light-only palette.
    "page_bg": "#F6F7F9", "card": "#FFFFFF", "surface": "#F1F3F6",
    "border": "#E4E7EC", "border_strong": "#CFD4DC",
    "text": "#101828", "text2": "#475467", "muted": "#667085",
    "accent": "#4F46E5", "accent_bg": "#EEF0FF", "accent_border": "#C7CBFB",
    "danger_fg": "#B42318", "danger_bg": "#FEF3F2", "danger_border": "#FECDCA", "danger_line": "#D92D20",
    "warn_fg": "#B54708", "warn_bg": "#FFFAEB", "warn_border": "#FEDF89", "warn_line": "#F79009",
    "ok_fg": "#067647", "ok_bg": "#ECFDF3", "ok_border": "#ABEFC6", "ok_line": "#17B26A",
    "info_fg": "#175CD3", "info_bg": "#EFF8FF", "info_border": "#B2DDFF", "info_line": "#2E90FA",
}


@dataclass
class MeetingLine:
    title: str
    time: str
    why: str
    url: str | None = None
    external: bool = False


@dataclass
class BriefData:
    day: date
    user_first: str
    role_track: RoleTrack
    tz: str
    metrics: Metrics
    one_thing: str
    assessment: str
    actions: list[Action]
    due_today: list[Item]
    due_overflow: int
    bottleneck: list[Item]
    waiting: list[Item]
    waiting_hidden: int
    slipping: list[Item]
    slipping_hidden: int
    commitments: list[Item]
    commitments_hidden: int
    meetings_action: list[MeetingLine]
    meetings_other: int
    meeting_notes: list[str]
    tomorrow: list[str]
    changed_lines: list[str]
    retractions: list[str]
    not_checked: str
    extras: list[str]
    subject: str = ""
    warnings: list[str] = field(default_factory=list)

    def with_trim(self, level: int) -> BriefData:
        """Progressively tighter version used to satisfy the total word cap."""
        d = BriefData(**self.__dict__)
        if level >= 1:
            d.meeting_notes = []
        if level >= 2:
            d.waiting, extra = caps.top_n(self.waiting, 3)
            d.waiting_hidden = self.waiting_hidden + extra
        if level >= 3:
            d.slipping, extra = caps.top_n(self.slipping, 2)
            d.slipping_hidden = self.slipping_hidden + extra
            d.commitments, extra = caps.top_n(self.commitments, 2)
            d.commitments_hidden = self.commitments_hidden + extra
        if level >= 4:
            d.actions = self.actions[:3]
            d.assessment = caps.cap_sentences(self.assessment, 3)
        if level >= 5:
            d.tomorrow = self.tomorrow[:1]
            d.changed_lines = self.changed_lines[:1]
            d.waiting, extra = caps.top_n(self.waiting, 2)
            d.waiting_hidden = self.waiting_hidden + extra
        return d


_WORD = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def resolve_action_items(actions: list[Action], items: list[Item]) -> list[Action]:
    """Point each action at the item it is about.

    The model is asked for an `item_id` but a hallucinated or missing one would give the reader a
    dead link, so an id is kept only when it names a real item. Otherwise the action is matched to
    the item whose title it most overlaps, and left unlinked when nothing is a clear match."""
    by_id = {i.id: i for i in items}
    out: list[Action] = []
    for a in actions:
        if a.item_id and a.item_id in by_id:
            out.append(a)
            continue
        action_words = _words(a.text)
        best: tuple[float, str] | None = None
        for item in items:
            title_words = _words(item.title)
            if not title_words:
                continue
            score = len(title_words & action_words) / len(title_words)
            if best is None or score > best[0]:
                best = (score, item.id)
        out.append(Action(text=a.text, effort_hint=a.effort_hint, item_id=best[1] if best and best[0] >= 0.5 else None))
    return out


def _badge(item: Item, today: date) -> str:
    if item.is_overdue(today):
        return "overdue"
    if item.is_due_today(today):
        return "today"
    if item.verified == Verified.live:
        return "verified"
    return "unconfirmed"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(enabled_extensions=("html",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["age"] = lambda item, today: humanize_age(item.age_days(today))
    env.filters["badge"] = _badge
    env.filters["unlink"] = unlink
    env.filters["oneline"] = caps.one_line
    env.globals["palette"] = PALETTE
    return env


def meetings_needing_action(events: list[CalendarEvent], items: list[Item], tz: str) -> tuple[list[MeetingLine], int]:
    """Meetings today that need something: no agenda and I organise, external, or I owe an attendee."""
    from zoneinfo import ZoneInfo

    owed_to = {unlink(i.promised_to) or "" for i in items if i.promised_to}
    lines: list[MeetingLine] = []
    other = 0
    for ev in sorted(events, key=lambda e: e.start):
        if ev.is_focus_block:
            continue
        reasons = []
        if ev.organiser_is_me and not ev.has_agenda and len(ev.attendees) > 1:
            reasons.append("no agenda, you organise")
        if ev.external:
            reasons.append("external attendees")
        owed = [a for a in ev.attendees if a in owed_to]
        if owed:
            reasons.append("you owe " + ", ".join(owed[:2]) + " something")
        if ev.declined:
            reasons.append(f"{len(ev.declined)} declined")
        if reasons:
            t = ev.start.astimezone(ZoneInfo(tz)).strftime("%H:%M")
            lines.append(MeetingLine(ev.title, t, "; ".join(reasons), ev.url, ev.external))
        else:
            other += 1
    return lines, other


def tomorrow_lines(events: list[CalendarEvent], today: date, tz: str) -> list[str]:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tomorrow = today + timedelta(days=1)
    evs = [e for e in events if e.start.astimezone(ZoneInfo(tz)).date() == tomorrow and not e.is_focus_block]
    if not evs:
        return ["No meetings tomorrow."]
    hours = sum((e.end - e.start).total_seconds() for e in evs) / 3600
    lines = [f"{len(evs)} meetings, {hours:.1f}h booked."]
    mine = [e.title for e in evs if e.organiser_is_me]
    if mine:
        lines.append("You organise: " + ", ".join(mine[:3]) + ".")
    no_agenda = [e.title for e in evs if e.organiser_is_me and not e.has_agenda and len(e.attendees) > 1]
    if no_agenda:
        lines.append("No agenda yet: " + ", ".join(no_agenda[:2]) + ".")
    ext = [e.title for e in evs if e.external]
    if ext and len(lines) < caps.TOMORROW_LINES:
        lines.append("External: " + ", ".join(ext[:2]) + " — prep tonight.")
    return lines[: caps.TOMORROW_LINES]


def changed_lines(metrics: Metrics) -> list[str]:
    ch = metrics.changed
    if ch.is_empty():
        return ["No movement since yesterday."]
    out = []
    if ch.new:
        out.append(f"New: {len(ch.new)} — " + "; ".join(caps.one_line(i.title, 60) for i in ch.new[:3]) + (f" +{len(ch.new) - 3}" if len(ch.new) > 3 else ""))
    if ch.moved:
        out.append(f"Moved: {len(ch.moved)} — " + "; ".join(f"{caps.one_line(i.title, 40)} ({why})" for i, why in ch.moved[:2]))
    if ch.cleared:
        out.append(f"Cleared: {len(ch.cleared)} — " + "; ".join(caps.one_line(i.title, 60) for i in ch.cleared[:3]))
    return out[: caps.CHANGED_LINES]


def fallback_draft(metrics: Metrics, today: date, first_name: str) -> BriefDraft:
    """Used when the model is unavailable: still a truthful, if plain, brief."""
    urgent = metrics.urgent
    if urgent:
        one = f"{len(urgent)} item(s) due or overdue today; start with \"{urgent[0].title}\"."
    elif metrics.waiting_on_me:
        one = f"{len(metrics.waiting_on_me)} people are waiting on you; the oldest is {humanize_age(metrics.waiting_on_me[0].age_days(today))}."
    else:
        one = "Nothing is due today and nobody is waiting on you."
    assessment = (
        f"{metrics.open_count} open items, {len(metrics.slipping)} slipping, commitments delivered {metrics.delivery_score}. "
        "The model was unavailable this run, so this is the numbers-only version."
    )
    actions = [Action(text=i.title, effort_hint="", item_id=i.id) for i in (urgent + metrics.waiting_on_me)[:5]]
    return BriefDraft(one_thing=one, assessment=assessment, actions=actions)


def _linkable(metrics: Metrics) -> list[Item]:
    """Every item an action could plausibly refer to."""
    seen: dict[str, Item] = {}
    for group in (metrics.urgent, metrics.waiting_on_me, metrics.slipping, metrics.commitments_open, metrics.waiting_on_them, metrics.watch):
        for i in group:
            seen.setdefault(i.id, i)
    return list(seen.values())


def build_brief(
    *,
    today: date,
    first_name: str,
    role_track: RoleTrack,
    tz: str,
    metrics: Metrics,
    draft: BriefDraft,
    events: list[CalendarEvent],
    not_checked: list[str],
    gaps: list[str],
    extras: list[str],
    retractions: list[str],
    subject: str,
) -> BriefData:
    due, due_overflow = caps.top_n(metrics.urgent, caps.DUE_TODAY_MAX)
    if len(metrics.urgent) > caps.DUE_TODAY_MAX:
        due, due_overflow = [], len(metrics.urgent)  # planning problem: say it in one line instead
    waiting, w_hidden = caps.top_n(metrics.waiting_on_me, caps.WAITING_MAX)
    slipping, s_hidden = caps.top_n(metrics.slipping, caps.SLIPPING_MAX)
    commitments, c_hidden = caps.top_n([i for i in metrics.commitments_open if i not in metrics.urgent], 3)
    meetings, other = meetings_needing_action(events, metrics.commitments_open, tz)
    bottleneck = metrics.waiting_on_me[:3] if role_track == RoleTrack.lead else []
    parts: list[str] = []
    if not_checked:
        parts.append("Not checked: " + "; ".join(not_checked) + ".")
    if gaps:
        parts.append(" ".join(g.rstrip(".") + "." for g in gaps))
    nc = " ".join(parts) or "All enabled sources answered this run."
    data = BriefData(
        day=today,
        user_first=first_name,
        role_track=role_track,
        tz=tz,
        metrics=metrics,
        one_thing=caps.cap_sentences(draft.one_thing, caps.ONE_THING_SENTENCES),
        assessment=caps.cap_sentences(draft.assessment, caps.ASSESSMENT_SENTENCES),
        actions=resolve_action_items(draft.actions[: caps.ACTIONS_MAX], _linkable(metrics)),
        due_today=due,
        due_overflow=due_overflow,
        bottleneck=bottleneck,
        waiting=waiting,
        waiting_hidden=w_hidden,
        slipping=slipping,
        slipping_hidden=s_hidden,
        commitments=commitments,
        commitments_hidden=c_hidden,
        meetings_action=meetings,
        meetings_other=other,
        meeting_notes=draft.meeting_notes[:3],
        tomorrow=(draft.tomorrow_note[: caps.TOMORROW_LINES] or tomorrow_lines(events, today, tz)) if "tomorrow_preview" in extras else [],
        changed_lines=changed_lines(metrics) if "changed_since_yesterday" in extras else [],
        retractions=list(dict.fromkeys(retractions + draft.retractions)),
        not_checked=caps.one_line(nc, 220),
        extras=extras,
        subject=subject,
    )
    return data


_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")


def to_plain_text(markdown_text: str) -> str:
    """Strip portal-only syntax for the email's plain-text part.

    `[[id|details]]` is a link the portal turns into the assist drawer; in a text/plain body it is
    just noise, so the label survives and the target does not."""
    out = _WIKI.sub(lambda m: (m.group(2) or m.group(1)).strip(), markdown_text)
    out = re.sub(r"\s+\bdetails\b(?=\s*$)", "", out, flags=re.MULTILINE)
    return out


def render_markdown(data: BriefData) -> str:
    env = _env()
    tpl = env.get_template("brief.md.j2")
    for level in range(0, 6):
        d = data.with_trim(level) if level else data
        md = tpl.render(b=d, today=d.day, m=d.metrics)
        if caps.word_count(md) <= caps.TOTAL_WORDS:
            if level:
                data.warnings.append(f"brief trimmed (level {level}) to respect the {caps.TOTAL_WORDS}-word cap")
            return md
    data.warnings.append("brief still over the word cap after maximum trimming")
    return md


def render_html(data: BriefData) -> str:
    env = _env()
    tpl = env.get_template("brief.html.j2")
    for level in range(0, 6):
        d = data.with_trim(level) if level else data
        html = tpl.render(b=d, today=d.day, m=d.metrics)
        if len(html.encode()) <= caps.HTML_MAX_BYTES:
            return html
    return html


def brief_input_for_llm(metrics: Metrics, events: list[CalendarEvent], today: date, tz: str, not_checked: list[str], verify_retractions: list[str]) -> dict[str, Any]:
    def row(i: Item) -> dict[str, Any]:
        return {
            "id": i.id,
            "type": i.type.value,
            "title": i.title,
            "person": unlink(i.promised_to),
            "project": unlink(i.project),
            "due": i.due.isoformat() if i.due else None,
            "age_days": i.age_days(today),
            "verified": i.verified.value,
            "cause": i.cause,
            "priority": i.priority,
            "url": i.url,
        }

    meetings, other = meetings_needing_action(events, metrics.commitments_open, tz)
    return {
        "today": today.isoformat(),
        "metric_strip": {
            "due_today": len(metrics.urgent),
            "waiting_on_me": len(metrics.waiting_on_me),
            "oldest_days": metrics.oldest_days,
            "delivered": metrics.delivery_score,
        },
        "due_or_overdue": [row(i) for i in metrics.urgent[:8]],
        "waiting_on_me": [row(i) for i in metrics.waiting_on_me[:10]],
        "slipping": [row(i) for i in metrics.slipping[:8]],
        "open_commitments": [row(i) for i in metrics.commitments_open[:10]],
        "waiting_on_them": [row(i) for i in metrics.waiting_on_them[:5]],
        "priority_split": metrics.priority_split,
        "imbalance_consecutive_runs": metrics.imbalance_runs,
        "changed_since_yesterday": {
            "new": [i.title for i in metrics.changed.new[:5]],
            "moved": [f"{i.title} ({w})" for i, w in metrics.changed.moved[:5]],
            "cleared": [i.title for i in metrics.changed.cleared[:5]],
        },
        "meetings_needing_action": [m.__dict__ for m in meetings],
        "other_meetings_today": other,
        "tomorrow": tomorrow_lines(events, today, tz),
        "not_checked": not_checked,
        "possible_retractions": verify_retractions,
    }
