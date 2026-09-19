"""Contracts shared by every source (native or MCP).

A source is READ-ONLY by construction: the protocol has no write methods, and credentials are
requested with read scopes. Sources return Signals (deterministic facts) and SourceTexts
(prose the LLM must read to find commitments). They never call the model themselves."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from nbrain.vault.schema import Item, ItemType

Role = Literal["email", "calendar", "chat", "tickets", "code", "notes", "knowledge", "other"]


@dataclass(frozen=True)
class Window:
    today: date
    email_lookback_days: int
    commitment_lookback_days: int
    awaiting_reply_hours: int
    ticket_stale_days: int
    review_age_days: int
    tz: str

    @property
    def email_since(self) -> date:
        from datetime import timedelta

        return self.today - timedelta(days=self.email_lookback_days)

    @property
    def commitment_since(self) -> date:
        from datetime import timedelta

        return self.today - timedelta(days=self.commitment_lookback_days)


class Signal(BaseModel):
    """A deterministic fact a collector found. Becomes (or updates) an Item."""

    type: ItemType
    title: str
    source: str
    source_id: str
    url: str | None = None
    person: str | None = None  # plain name; store converts to wikilink
    person_email: str | None = None
    project_hint: str | None = None  # free text used for project attribution
    due: date | None = None
    promised_on: date | None = None
    observed_at: datetime | None = None
    cause: str | None = None
    evidence: str | None = None
    priority: int = 3
    confidence: float = 1.0  # < 0.6 goes to watch instead of open
    meeting: str | None = None

    def to_item(self) -> Item:
        return Item(
            title=self.title,
            type=self.type,
            source=self.source,
            source_id=self.source_id,
            url=self.url,
            promised_to=self.person,
            due=self.due,
            promised_on=self.promised_on,
            cause=self.cause,
            evidence=self.evidence,
            priority=self.priority,
            meeting=self.meeting,
        )


class SourceText(BaseModel):
    """Prose the LLM reads to extract commitments. Kept small and attributed."""

    source: str
    source_id: str
    url: str | None = None
    kind: Literal["chat", "email", "notes", "ticket-comment", "other"] = "other"
    title: str = ""
    author: str | None = None  # account-derived, never inferred from prose
    author_email: str | None = None
    author_is_me: bool = False
    participants: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    text: str
    meeting: str | None = None
    unopened_by_me: bool = False  # notes doc assigning me items I never opened -> high priority


class CalendarEvent(BaseModel):
    source: str
    source_id: str
    title: str
    start: datetime
    end: datetime
    url: str | None = None
    organiser_is_me: bool = False
    organiser: str | None = None
    attendees: list[str] = Field(default_factory=list)
    attendee_emails: list[str] = Field(default_factory=list)
    declined: list[str] = Field(default_factory=list)
    has_agenda: bool = False
    external: bool = False
    my_response: str | None = None
    is_focus_block: bool = False


class VerifyResult(BaseModel):
    state: Literal["open", "resolved", "unknown"]
    note: str | None = None
    url: str | None = None


_BLANKS = re.compile(r"[ \t\u00a0]+")
_GAPS = re.compile(r"\n{3,}")


class ContextMessage(BaseModel):
    """One message/comment/event in an item's live surrounding context."""

    author: str | None = None
    author_email: str | None = None
    is_me: bool = False
    at: datetime | None = None
    text: str = ""
    kind: Literal["message", "comment", "status", "commit", "note"] = "message"

    @field_validator("text", mode="before")
    @classmethod
    def _tidy(cls, v: object) -> str:
        """Collapse the whitespace that HTML-to-text extraction leaves behind.

        A marketing email can arrive as a few sentences buried in hundreds of blank lines; left
        alone it wastes the model's context and renders as a wall of gaps in the drawer."""
        if not v:
            return ""
        text = str(v).replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(_BLANKS.sub(" ", line).strip() for line in text.split("\n"))
        return _GAPS.sub("\n\n", text).strip()


class ItemContext(BaseModel):
    """Live state pulled from the source at the moment the user asks for help with an item.

    Deliberately re-fetched rather than reusing what the sweep stored: the sweep ran hours ago
    and the thread may have moved. `fingerprint` is what changed-detection keys on, so a cached
    draft is thrown away as soon as the source does anything new."""

    available: bool = True
    kind: Literal["thread", "ticket", "review", "document", "none"] = "none"
    title: str = ""
    url: str | None = None
    status: str | None = None  # "open · assigned to X · updated 3d ago"
    participants: list[str] = Field(default_factory=list)
    messages: list[ContextMessage] = Field(default_factory=list)
    facts: dict[str, str] = Field(default_factory=dict)  # labels -> values for the drawer
    awaiting_me: bool | None = None  # is the last word theirs?
    fingerprint: str = ""  # changes whenever the source moves; drives cache invalidation
    note: str | None = None  # why context is unavailable, shown honestly in the drawer

    def last_inbound(self) -> ContextMessage | None:
        for m in reversed(self.messages):
            if not m.is_me:
                return m
        return None

    @classmethod
    def unavailable(cls, why: str) -> ItemContext:
        return cls(available=False, kind="none", note=why)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    detail: str = ""
    checked_at: datetime | None = None
    write_capable: bool = False  # honesty: the token could technically write


@dataclass
class CollectResult:
    signals: list[Signal] = field(default_factory=list)
    texts: list[SourceText] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # coverage caveats for "Not checked"
    findings: dict[str, str] = field(default_factory=dict)  # e.g. jira: "no due dates exist"

    def extend(self, other: CollectResult) -> None:
        self.signals += other.signals
        self.texts += other.texts
        self.events += other.events
        self.notes += other.notes
        self.findings.update(other.findings)


@runtime_checkable
class Source(Protocol):
    name: str
    roles: set[str]
    verifiable: bool  # False -> items from here are always "unconfirmed"

    async def healthcheck(self) -> SourceStatus: ...

    async def collect(self, window: Window) -> CollectResult: ...

    async def verify(self, item: Item) -> VerifyResult: ...

    async def fetch_context(self, item: Item) -> ItemContext: ...


class BaseSource:
    """Convenience base with a no-op verify for sources that cannot re-check state."""

    name = "base"
    roles: set[str] = set()
    verifiable = False

    async def healthcheck(self) -> SourceStatus:
        return SourceStatus(self.name, True)

    async def collect(self, window: Window) -> CollectResult:
        return CollectResult()

    async def verify(self, item: Item) -> VerifyResult:
        return VerifyResult(state="unknown", note=f"{self.name} cannot verify live state")

    async def fetch_context(self, item: Item) -> ItemContext:
        """Re-read the item at its source. Overridden per source; the default is honest."""
        return ItemContext.unavailable(f"{self.name} cannot fetch live context for this item")
