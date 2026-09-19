"""Google sources against a fake discovery client. No network, no real credentials."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from nbrain.config.schema import Config
from nbrain.sources.base import Window
from nbrain.sources.google import (
    CalendarSource,
    ChatSource,
    DriveNotesSource,
    GmailSource,
    GoogleAuth,
    build_google_sources,
    required_scopes,
)
from nbrain.sources.google.auth import GMAIL_COMPOSE, GMAIL_READONLY, GMAIL_SEND
from nbrain.sources.google.drive_notes import _action_section, _doc_text, _meeting_name
from nbrain.vault.schema import Item, ItemType, Person
from nbrain.vault.store import VaultStore

Handler = Callable[[str, dict[str, Any]], Any]


class _Call:
    """Chainable stand-in for googleapiclient resources: svc.users().threads().get(...).execute()."""

    def __init__(self, handler: Handler, path: list[str], kwargs: dict[str, Any]):
        self._h, self._path, self._kw = handler, path, kwargs

    def __getattr__(self, name: str) -> Callable[..., _Call]:
        return lambda **kw: _Call(self._h, [*self._path, name], kw)

    def execute(self) -> Any:
        return self._h(".".join(self._path), self._kw)


def fake_service(handler: Handler) -> _Call:
    return _Call(handler, [], {})


def not_found() -> HttpError:
    return HttpError(httplib2.Response({"status": 404}), b"not found")


def patch_services(services: dict[str, _Call]):  # type: ignore[no-untyped-def]
    return patch.object(GoogleAuth, "service", lambda self, api, version: services[api])


@pytest.fixture
def window(today: date) -> Window:
    return Window(
        today=today,
        email_lookback_days=7,
        commitment_lookback_days=30,
        awaiting_reply_hours=48,
        ticket_stale_days=5,
        review_age_days=3,
        tz="Europe/London",
    )


@pytest.fixture
def auth(cfg: Config) -> GoogleAuth:
    cfg.sources.google.enabled = True
    return GoogleAuth(cfg)


def _ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


# ---------- scopes ----------


def test_required_scopes_read_only_by_default(cfg: Config) -> None:
    scopes = required_scopes(cfg)
    assert scopes and all(s.endswith(".readonly") for s in scopes)
    assert GMAIL_READONLY in scopes


def test_required_scopes_add_compose_and_send_only_when_delivery_enabled(cfg: Config) -> None:
    cfg.delivery.gmail_draft.enabled = True
    scopes = required_scopes(cfg)
    assert GMAIL_COMPOSE in scopes and GMAIL_SEND not in scopes
    cfg.delivery.gmail_send.enabled = True
    assert GMAIL_SEND in required_scopes(cfg)
    cfg.sources.google.gmail = False
    assert GMAIL_READONLY not in required_scopes(cfg)


def test_build_google_sources_respects_enabled(cfg: Config, vault: VaultStore) -> None:
    assert build_google_sources(cfg, vault) == []
    cfg.sources.google.enabled = True
    cfg.sources.google.chat = False
    names = [s.name for s in build_google_sources(cfg, vault)]
    assert names == ["gmail", "google-calendar", "meeting-notes"]


def test_auth_status_reports_write_capability(auth: GoogleAuth) -> None:
    assert auth.status().ok is False
    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(json.dumps({"token": "x", "scopes": [*auth.scopes, GMAIL_SEND]}))
    st = auth.status()
    assert st.ok and st.write_capable


# ---------- gmail ----------


def _msg(
    *, mid: str, frm: str, to: str, subject: str, snippet: str, at: datetime
) -> dict[str, Any]:
    return {
        "id": mid,
        "snippet": snippet,
        "internalDate": str(int(at.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ]
        },
    }


def _gmail_service(threads: dict[str, dict[str, Any]]) -> _Call:
    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "users.threads.list":
            return {"threads": [{"id": t} for t in threads]}
        if path == "users.threads.get":
            assert kw["format"] == "metadata"
            if kw["id"] not in threads:
                raise not_found()
            return threads[kw["id"]]
        raise AssertionError(f"unexpected call {path}")

    return fake_service(handler)


async def test_gmail_waiting_on_me_only_for_tiered_people_past_threshold(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window
) -> None:
    vault.save(Person(title="Alice Boss", email="alice@example.com", tier=1))
    vault.save(Person(title="Carol Peer", email="carol@example.com", tier=3))
    threads = {
        "t-old-tier1": {
            "id": "t-old-tier1",
            "messages": [
                _msg(mid="m1", frm="Alice Boss <alice@example.com>", to="test@example.com",
                     subject="Budget sign-off", snippet="Can you approve?", at=_ago(72))
            ],
        },
        "t-fresh-tier1": {
            "id": "t-fresh-tier1",
            "messages": [
                _msg(mid="m2", frm="Alice Boss <alice@example.com>", to="test@example.com",
                     subject="Quick one", snippet="ping", at=_ago(2))
            ],
        },
        "t-old-tier3": {
            "id": "t-old-tier3",
            "messages": [
                _msg(mid="m3", frm="carol@example.com", to="test@example.com",
                     subject="FYI", snippet="see attached", at=_ago(100))
            ],
        },
        "t-unknown": {
            "id": "t-unknown",
            "messages": [
                _msg(mid="m4", frm="Dave <dave@other.org>", to="test@example.com",
                     subject="Intro", snippet="hello there", at=_ago(100))
            ],
        },
        "t-i-asked": {
            "id": "t-i-asked",
            "messages": [
                _msg(mid="m5", frm="test@example.com", to="Bob <bob@example.com>",
                     subject="ETA?", snippet="When can you ship this?", at=_ago(60))
            ],
        },
    }
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": _gmail_service(threads)}):
        res = await src.collect(window)

    waiting = [s for s in res.signals if s.type == ItemType.waiting_on_me]
    assert [s.source_id for s in waiting] == ["t-old-tier1"]
    assert waiting[0].priority == 1
    assert waiting[0].person == "Alice Boss"
    assert waiting[0].title == "Reply to Alice Boss: Budget sign-off"
    assert waiting[0].url == "https://mail.google.com/mail/u/0/#inbox/t-old-tier1"
    them = [s for s in res.signals if s.type == ItemType.waiting_on_them]
    assert [s.source_id for s in them] == ["t-i-asked"] and them[0].priority == 3
    # inbound human threads not covered by a signal become LLM texts
    assert {t.source_id for t in res.texts} == {"t-fresh-tier1", "t-old-tier3", "t-unknown"}
    assert all(t.kind == "email" and not t.author_is_me for t in res.texts)
    text = next(t for t in res.texts if t.source_id == "t-unknown")
    assert text.text == "Intro\nhello there" and text.author_email == "dave@other.org"


async def test_gmail_noise_suppressed_automation_counted_phishing_flagged(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window
) -> None:
    cfg.noise.suppress = ["newsletter.example"]
    cfg.noise.mine = ["jira@atlassian.net"]
    cfg.noise.phishing = ["payroll-update.biz"]
    threads = {
        "t-news": {"id": "t-news", "messages": [
            _msg(mid="a", frm="news@newsletter.example", to="test@example.com",
                 subject="Weekly digest", snippet="read", at=_ago(80))]},
        "t-jira": {"id": "t-jira", "messages": [
            _msg(mid="b", frm="jira@atlassian.net", to="test@example.com",
                 subject="[JIRA] assigned", snippet="you were assigned", at=_ago(80))]},
        "t-phish": {"id": "t-phish", "messages": [
            _msg(mid="c", frm="HR <hr@payroll-update.biz>", to="test@example.com",
                 subject="Update your bank details", snippet="urgent", at=_ago(3))]},
    }
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": _gmail_service(threads)}):
        res = await src.collect(window)
    assert res.texts == []
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.type == ItemType.watch and sig.priority == 3
    assert sig.title == "Phishing-shaped: Update your bank details"
    assert "1 automated" in res.findings["gmail"]


async def test_gmail_verify(cfg: Config, vault: VaultStore, auth: GoogleAuth) -> None:
    threads = {
        "t1": {"id": "t1", "messages": [
            _msg(mid="a", frm="alice@example.com", to="test@example.com", subject="s",
                 snippet="?", at=_ago(80)),
            _msg(mid="b", frm="Test User <test@example.com>", to="alice@example.com",
                 subject="Re: s", snippet="done", at=_ago(1))]},
        "t2": {"id": "t2", "messages": [
            _msg(mid="c", frm="alice@example.com", to="test@example.com", subject="s",
                 snippet="?", at=_ago(80))]},
    }
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": _gmail_service(threads)}):
        replied = await src.verify(Item(title="x", source="gmail", source_id="t1"))
        still = await src.verify(Item(title="x", source="gmail", source_id="t2"))
        gone = await src.verify(Item(title="x", source="gmail", source_id="t-missing"))
    assert replied.state == "resolved" and replied.note == "you replied"
    assert still.state == "open"
    assert gone.state == "resolved" and gone.note == "thread gone"


# ---------- calendar ----------


async def test_calendar_agenda_signal_and_event_flags(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window, today: date
) -> None:
    day = today.isoformat()
    events = [
        {
            "id": "ev-no-agenda", "summary": "Roadmap sync", "htmlLink": "https://cal/ev1",
            "start": {"dateTime": f"{day}T10:00:00+01:00"},
            "end": {"dateTime": f"{day}T10:30:00+01:00"},
            "organizer": {"email": "test@example.com", "self": True},
            "attendees": [
                {"email": "test@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "accepted"},
                {"email": "eve@client.io", "responseStatus": "declined"},
            ],
        },
        {
            "id": "ev-agenda", "summary": "1:1", "htmlLink": "https://cal/ev2",
            "description": "1. review last week\n2. blockers\n3. growth topics",
            "start": {"dateTime": f"{day}T14:00:00+01:00"},
            "end": {"dateTime": f"{day}T14:30:00+01:00"},
            "organizer": {"email": "test@example.com"},
            "attendees": [{"email": "test@example.com", "self": True},
                          {"email": "bob@example.com"}],
        },
        {
            "id": "ev-focus", "summary": "Focus", "htmlLink": "https://cal/ev3",
            "start": {"dateTime": f"{day}T15:00:00+01:00"},
            "end": {"dateTime": f"{day}T17:00:00+01:00"},
            "organizer": {"email": "test@example.com", "self": True},
        },
        {
            "id": "ev-allday", "summary": "Holiday", "start": {"date": day}, "end": {"date": day},
        },
        {
            "id": "ev-declined", "summary": "Optional", "htmlLink": "https://cal/ev5",
            "start": {"dateTime": f"{day}T16:00:00+01:00"},
            "end": {"dateTime": f"{day}T16:30:00+01:00"},
            "organizer": {"email": "bob@example.com"},
            "attendees": [{"email": "test@example.com", "self": True,
                           "responseStatus": "declined"}],
        },
    ]

    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "events.list":
            assert kw["singleEvents"] is True and kw["orderBy"] == "startTime"
            return {"items": events}
        if path == "events.get":
            for ev in events:
                if ev["id"] == kw["eventId"]:
                    return ev
            raise not_found()
        raise AssertionError(path)

    src = CalendarSource(cfg, vault, auth)
    with patch_services({"calendar": fake_service(handler)}):
        res = await src.collect(window)
        gone = await src.verify(Item(title="x", source=src.name, source_id="nope"))
        open_ = await src.verify(Item(title="x", source=src.name, source_id="ev-no-agenda",
                                      type=ItemType.waiting_on_me))

    assert [e.source_id for e in res.events] == ["ev-no-agenda", "ev-agenda", "ev-focus"]
    first = res.events[0]
    assert first.organiser_is_me and not first.has_agenda and first.external
    assert first.declined == ["eve@client.io"] and "Bob" in first.attendees
    assert res.events[1].has_agenda and not res.events[1].external
    assert res.events[2].is_focus_block
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.type == ItemType.waiting_on_me and sig.priority == 2
    assert sig.title == "Add an agenda: Roadmap sync" and sig.due == today
    assert sig.meeting == "Roadmap sync" and sig.source_id == "ev-no-agenda"
    assert gone.state == "resolved" and open_.state == "open"


# ---------- chat ----------


def _chat_msg(name: str, sender: dict[str, Any], text: str, at: datetime,
              mentions: list[str] | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "name": name, "sender": sender, "text": text,
        "createTime": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "thread": {"name": name.rsplit("/messages/", 1)[0] + "/threads/T"},
    }
    if mentions:
        m["annotations"] = [
            {"type": "USER_MENTION", "userMention": {"user": {"name": u}}} for u in mentions
        ]
    return m


async def test_chat_author_is_me_and_mention_signal(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window
) -> None:
    me = {"name": "users/111", "displayName": "Test User", "email": "test@example.com",
          "type": "HUMAN"}
    bob = {"name": "users/222", "displayName": "Bob", "email": "bob@example.com", "type": "HUMAN"}
    bot = {"name": "users/999", "displayName": "Jira", "type": "BOT"}
    spaces = [{"name": "spaces/AAA", "displayName": "Team", "spaceType": "SPACE"},
              {"name": "spaces/BBB", "spaceType": "DIRECT_MESSAGE"}]
    messages = {
        "spaces/AAA": [
            _chat_msg("spaces/AAA/messages/m3", bob, "@Test can you review the doc?", _ago(72),
                      mentions=["users/111"]),
            _chat_msg("spaces/AAA/messages/m2", me, "I'll send the numbers by Friday", _ago(90)),
            _chat_msg("spaces/AAA/messages/m1", bot, "Issue moved", _ago(95)),
        ],
        "spaces/BBB": [
            _chat_msg("spaces/BBB/messages/d2", me, "yes, on it", _ago(5)),
            _chat_msg("spaces/BBB/messages/d1", bob, "did you get my note?", _ago(70)),
        ],
    }

    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "spaces.list":
            return {"spaces": spaces}
        if path == "spaces.messages.list":
            assert 'createTime > "' in kw["filter"]
            return {"messages": messages[kw["parent"]]}
        raise AssertionError(path)

    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": fake_service(handler)}):
        res = await src.collect(window)

    by_id = {t.source_id: t for t in res.texts}
    assert by_id["spaces/AAA/messages/m2"].author_is_me is True
    assert by_id["spaces/BBB/messages/d2"].author_is_me is True
    assert by_id["spaces/AAA/messages/m3"].author_is_me is False
    assert by_id["spaces/AAA/messages/m3"].author == "Bob"
    assert by_id["spaces/AAA/messages/m3"].url == "https://chat.google.com/room/AAA/m3"
    # the DM question was answered by me later -> no text, no signal
    assert "spaces/BBB/messages/d1" not in by_id
    assert "spaces/AAA/messages/m1" not in by_id  # bot noise
    assert [s.source_id for s in res.signals] == ["spaces/AAA/messages/m3"]
    assert res.signals[0].type == ItemType.waiting_on_me and res.signals[0].priority == 2


async def test_chat_identifies_me_by_display_name_when_email_absent(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window
) -> None:
    me = {"name": "users/111", "displayName": "Test User", "type": "HUMAN"}
    spaces = [{"name": "spaces/AAA", "displayName": "Team", "spaceType": "SPACE"}]

    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "spaces.list":
            return {"spaces": spaces}
        return {"messages": [_chat_msg("spaces/AAA/messages/m1", me, "will do", _ago(10))]}

    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": fake_service(handler)}):
        res = await src.collect(window)
    assert len(res.texts) == 1 and res.texts[0].author_is_me is True


# ---------- drive notes ----------


def _para(text: str, style: str | None = None, bullet: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {"elements": [{"textRun": {"content": text + "\n"}}]}
    if style:
        p["paragraphStyle"] = {"namedStyleType": style}
    if bullet:
        p["bullet"] = {"listId": "l1"}
    return {"paragraph": p}


DOC = {
    "title": "Roadmap sync - 2026/09/16 10:00 BST - Notes by Gemini",
    "body": {"content": [
        _para("Summary", "HEADING_1"),
        _para("We discussed the roadmap at length."),
        _para("Suggested next steps", "HEADING_2"),
        _para("Test User will send the numbers by Friday.", bullet=True),
        _para("Bob to book the venue.", bullet=True),
        _para("Details", "HEADING_1"),
        _para("Lots of detail here."),
    ]},
}


def test_doc_text_and_next_steps_section() -> None:
    text = _doc_text(DOC)
    assert text.startswith("# Summary\nWe discussed")
    assert "## Suggested next steps\n- Test User will send" in text
    section = _action_section(text)
    assert section == (
        "## Suggested next steps\n- Test User will send the numbers by Friday.\n- Bob to book the venue."
    )
    assert _action_section("# Summary\nnothing") is None
    assert _meeting_name(DOC["title"]) == "Roadmap sync"
    assert _meeting_name("Weekly 1:1 Meeting notes") == "Weekly 1:1"


async def test_drive_notes_collect_and_verify(
    cfg: Config, vault: VaultStore, auth: GoogleAuth, window: Window
) -> None:
    files = [
        {"id": "doc1", "name": DOC["title"], "modifiedTime": "2026-09-16T11:00:00.000Z",
         "webViewLink": "https://docs/doc1", "owners": [{"displayName": "Gemini"}]},
        {"id": "doc2", "name": "Standup Meeting notes", "modifiedTime": "2026-09-16T12:00:00Z",
         "webViewLink": "https://docs/doc2", "viewedByMeTime": "2026-09-16T12:30:00Z"},
    ]

    def drive(path: str, kw: dict[str, Any]) -> Any:
        if path == "files.list":
            assert "mimeType='application/vnd.google-apps.document'" in kw["q"]
            assert "Notes by Gemini" in kw["q"] and kw["pageSize"] == 6
            return {"files": files}
        if path == "files.get":
            if kw["fileId"] == "doc1":
                return {"id": "doc1", "webViewLink": "https://docs/doc1"}
            raise not_found()
        raise AssertionError(path)

    def docs(path: str, kw: dict[str, Any]) -> Any:
        assert path == "documents.get"
        if kw["documentId"] == "doc1":
            return DOC
        return {"title": "Standup", "body": {"content": [_para("Nothing much happened today.")]}}

    src = DriveNotesSource(cfg, vault, auth)
    with patch_services({"drive": fake_service(drive), "docs": fake_service(docs)}):
        res = await src.collect(window)
        v1 = await src.verify(Item(title="x", source=src.name, source_id="doc1"))
        v2 = await src.verify(Item(title="x", source=src.name, source_id="doc2"))

    assert res.signals == []
    assert [t.source_id for t in res.texts] == ["doc1", "doc2"]
    t1, t2 = res.texts
    assert t1.kind == "notes" and t1.unopened_by_me is True and t1.meeting == "Roadmap sync"
    assert t1.text.startswith("## Suggested next steps") and "Lots of detail" not in t1.text
    assert t1.observed_at is not None and t1.observed_at.tzinfo is not None
    assert t1.url == "https://docs/doc1"
    assert t2.unopened_by_me is False and t2.text == "Nothing much happened today."
    assert t2.meeting == "Standup"
    assert v1.state == "unknown" and v1.note == "notes docs cannot confirm completion"
    assert v2.state == "unknown"


def test_no_scopes_fails_with_a_useful_message(tmp_path):
    """Google rejects a scope-less auth URL with an opaque 400; we must fail earlier and clearer."""
    import pytest

    from nbrain.config.schema import Config
    from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, required_scopes

    cfg = Config(vault_path=tmp_path)
    cfg.sources.google.enabled = True
    cfg.sources.google.gmail = False
    cfg.sources.google.calendar = False
    cfg.sources.google.chat = False
    cfg.sources.google.drive_notes = False
    assert required_scopes(cfg) == []

    with pytest.raises(GoogleAuthError) as e:
        GoogleAuth(cfg).credentials(interactive=True)
    msg = str(e.value)
    assert "sub-source" in msg and "nbrain config set sources.google.gmail true" in msg

    cfg.sources.google.calendar = True
    assert required_scopes(cfg) == ["https://www.googleapis.com/auth/calendar.readonly"]
