"""Frontmatter models for vault notes. Everything the sweep reasons about is a property here,
so ages, streaks and scores are computed by code, never by the model re-reading prose."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from nbrain.util import parse_date, unlink, wikilink


class ItemType(StrEnum):
    commitment = "commitment"  # I said I would do X
    waiting_on_me = "waiting-on-me"  # someone needs my reply/review/decision
    waiting_on_them = "waiting-on-them"  # I asked, no answer yet
    slipping = "slipping"  # my ticket/MR is stale or past due
    meeting_action = "meeting-action"  # action item from notes
    watch = "watch"  # low-confidence, keep an eye on it


class ItemStatus(StrEnum):
    open = "open"
    resolved = "resolved"
    dismissed = "dismissed"
    watch = "watch"


class Verified(StrEnum):
    live = "live"  # confirmed against the source this run
    unconfirmed = "unconfirmed"  # source cannot tell us; phrase as "if still open"
    stale = "stale"  # unconfirmed for N runs


def _fm_date(v: Any) -> date | None:
    return parse_date(v)


class Note(BaseModel):
    """Base for anything stored as a note with frontmatter."""

    id: str = ""  # filename stem, set by the store
    title: str
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    body: str = ""

    model_config = {"extra": "allow"}

    FOLDER: ClassVar[str] = ""

    def frontmatter(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"id", "body"}, exclude_none=True, mode="python")
        # Obsidian: keep lists as lists, dates as date objects (YAML dumps them unquoted);
        # enums become plain strings.
        out: dict[str, Any] = {}
        for k, v in data.items():
            if v in ("", [], {}):
                continue
            if isinstance(v, StrEnum):
                v = v.value
            elif isinstance(v, list):
                v = [x.value if isinstance(x, StrEnum) else x for x in v]
            out[k] = v
        return out

    @classmethod
    def from_frontmatter(cls, meta: dict[str, Any], body: str, note_id: str):  # type: ignore[no-untyped-def]
        data = dict(meta)
        data["body"] = body
        data["id"] = note_id
        data.setdefault("title", note_id)
        return cls.model_validate(data)


class Item(Note):
    FOLDER: ClassVar[str] = "Items"

    type: ItemType = ItemType.commitment
    status: ItemStatus = ItemStatus.open
    source: str = ""  # gmail | google-calendar | google-chat | meeting-notes | slack | jira | gitlab | mcp:<name>
    source_id: str = ""  # stable key inside the source, for dedupe and verification
    url: str | None = None
    owner: str = "me"
    promised_to: str | None = None  # "[[Person]]"
    project: str | None = None  # "[[Project]]"
    meeting: str | None = None  # "[[Meeting]]"
    promised_on: date | None = None
    due: date | None = None
    first_seen: date | None = None
    last_seen: date | None = None
    last_verified: date | None = None
    verified: Verified = Verified.unconfirmed
    unconfirmed_runs: int = 0
    resolved_on: date | None = None
    dismissed_on: date | None = None
    dismissed_reason: str | None = None
    priority: int = 3  # 1 = highest
    cause: str | None = None  # why it is slipping, one line
    evidence: str | None = None  # quoted wording, one or two lines
    surfaced_in: list[str] = Field(default_factory=list)  # "[[Briefs/2026-09-17]]"
    tags: list[str] = Field(default_factory=lambda: ["nbrain/item"])

    _date_fields = (
        "promised_on",
        "due",
        "first_seen",
        "last_seen",
        "last_verified",
        "resolved_on",
        "dismissed_on",
    )

    @field_validator(
        "promised_on",
        "due",
        "first_seen",
        "last_seen",
        "last_verified",
        "resolved_on",
        "dismissed_on",
        mode="before",
    )
    @classmethod
    def _dates(cls, v: Any) -> date | None:
        return _fm_date(v)

    @field_validator("promised_to", "project", "meeting", mode="before")
    @classmethod
    def _links(cls, v: Any) -> str | None:
        return wikilink(v) if v else None

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.source, self.source_id)

    @property
    def person(self) -> str | None:
        return unlink(self.promised_to)

    @property
    def project_name(self) -> str | None:
        return unlink(self.project)

    def age_days(self, today: date) -> int | None:
        start = self.promised_on or self.first_seen
        return (today - start).days if start else None

    def is_due_today(self, today: date) -> bool:
        return self.due == today and self.status == ItemStatus.open

    def is_overdue(self, today: date) -> bool:
        return self.due is not None and self.due < today and self.status == ItemStatus.open

    def is_open(self) -> bool:
        return self.status == ItemStatus.open

    def verified_label(self) -> str:
        if self.verified == Verified.live:
            return "✅ live"
        if self.verified == Verified.stale or self.unconfirmed_runs >= 2:
            return f"🕓 stale {self.unconfirmed_runs}"
        return "⚠️ unconfirmed"


class PersonKind(StrEnum):
    """What this address actually is. Guessed from behaviour, correctable by hand."""

    colleague = "colleague"  # a human on your own domain you exchange messages with
    external = "external"  # a human elsewhere: client, vendor, candidate
    automation = "automation"  # no-reply, build bots, ticket notifications
    suspicious = "suspicious"  # phishing-shaped; never treated as a contact
    unknown = "unknown"  # seen once, not enough to say


class Person(Note):
    FOLDER: ClassVar[str] = "People"

    email: str | None = None
    tier: int = 2  # 1 = never wait, 2 = team, 3 = occasional
    external: bool = False
    kind: PersonKind = PersonKind.unknown
    kind_evidence: str | None = None  # why, in one line, so a wrong guess is visible
    sla_hours: int | None = None
    relationship: str | None = None  # manager, report, peer, client...
    role: str | None = None
    out_of_office_until: date | None = None

    # How you actually work together. Counted from interactions, never from the model.
    emails: int = 0
    chats: int = 0
    group_chats: int = 0
    meetings: int = 0
    tickets: int = 0
    reviews: int = 0
    one_to_one: bool = False  # a recurring meeting with just the two of you
    first_interaction: date | None = None
    last_interaction: date | None = None
    interaction_refs: list[str] = Field(default_factory=list)  # dedupe keys already counted

    tags: list[str] = Field(default_factory=lambda: ["nbrain/person"])

    @field_validator("first_interaction", "last_interaction", mode="before")
    @classmethod
    def _idates(cls, v: Any) -> date | None:
        return _fm_date(v)

    @property
    def total_interactions(self) -> int:
        return self.emails + self.chats + self.group_chats + self.meetings + self.tickets + self.reviews

    def channel_mix(self) -> dict[str, int]:
        """Only the channels with something in them, strongest first."""
        raw = {
            "email": self.emails,
            "chat": self.chats,
            "group chat": self.group_chats,
            "meetings": self.meetings,
            "tickets": self.tickets,
            "reviews": self.reviews,
        }
        return {k: v for k, v in sorted(raw.items(), key=lambda kv: -kv[1]) if v}

    def is_real_person(self) -> bool:
        return self.kind in (PersonKind.colleague, PersonKind.external)

    @field_validator("out_of_office_until", mode="before")
    @classmethod
    def _d(cls, v: Any) -> date | None:
        return _fm_date(v)


class Project(Note):
    FOLDER: ClassVar[str] = "Projects"

    weight: float = 1.0  # relative priority; equal weights trigger starvation flags
    status: str | None = None
    role: str | None = None  # owner | contributor
    jira_keys: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    chat_spaces: list[str] = Field(default_factory=list)
    slack_channels: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # for attributing signals to this project
    slipping_means: str | None = None  # what late looks like here, in the user's words
    milestone: str | None = None
    milestone_date: date | None = None
    tags: list[str] = Field(default_factory=lambda: ["nbrain/project"])

    @field_validator("milestone_date", mode="before")
    @classmethod
    def _d(cls, v: Any) -> date | None:
        return _fm_date(v)


class Meeting(Note):
    FOLDER: ClassVar[str] = "Meetings"

    cadence: str | None = None  # weekly, biweekly, daily
    time: str | None = None
    organiser_is_me: bool = False
    purpose: str | None = None
    attendees: list[str] = Field(default_factory=list)  # "[[Person]]"
    agenda_required: bool = True
    focus_block: bool = False
    tags: list[str] = Field(default_factory=lambda: ["nbrain/meeting"])

    @field_validator("attendees", mode="before")
    @classmethod
    def _links(cls, v: Any) -> list[str]:
        return [wikilink(x) or "" for x in (v or []) if x]


class SweepLog(Note):
    FOLDER: ClassVar[str] = "Sweeps"

    kind: str = "daily"
    run_at: str = ""
    duration_seconds: float = 0
    sources_ok: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)
    items_new: int = 0
    items_resolved: int = 0
    items_open: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    model: str = ""
    dry_run: bool = False
    tags: list[str] = Field(default_factory=lambda: ["nbrain/sweep"])
