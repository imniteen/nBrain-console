"""`fetch_context` for every source: a live re-read of one item, with no network.

Each source is covered for four things: a populated context (with `is_me` and `awaiting_me`
computed), a fingerprint that moves only when the source moves, an honest `unavailable` note on
the failure paths, and the size caps holding on an oversized thread."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from gitlab.exceptions import GitlabGetError

# The client doubles the per-source suites already use; no new fakes invented here.
from test_gitlab_source import FakeGitlab
from test_gitlab_source import obj as gl_obj
from test_google_sources import fake_service, not_found, patch_services
from test_jira_source import BASE
from test_jira_source import ME as JIRA_ME
from test_slack_source import ME as SLACK_ME
from test_slack_source import api_error, basic_client, msg

from nbrain.config.schema import Config
from nbrain.sources.gitlab import GitLabSource
from nbrain.sources.google import (
    CalendarSource,
    ChatSource,
    DriveNotesSource,
    GmailSource,
    GoogleAuth,
)
from nbrain.sources.jira import JiraSource
from nbrain.sources.slack import SlackSource
from nbrain.vault.schema import Item
from nbrain.vault.store import VaultStore

NOW = datetime.now(UTC)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def item(source: str, source_id: str, **kw: Any) -> Item:
    return Item(title="x", source=source, source_id=source_id, **kw)


@pytest.fixture(autouse=True)
def _tokens():  # type: ignore[no-untyped-def]
    """Every source thinks it has a credential; nothing here ever reaches a network."""
    with (
        patch("nbrain.sources.jira.get_secret", return_value="tok"),
        patch("nbrain.sources.gitlab.get_secret", return_value="glpat-x"),
        patch("nbrain.sources.slack.get_secret", return_value="xoxp-x"),
    ):
        yield


@pytest.fixture
def auth(cfg: Config) -> GoogleAuth:
    cfg.sources.google.enabled = True
    return GoogleAuth(cfg)


@pytest.fixture
def jira_cfg(cfg: Config) -> Config:
    cfg.sources.jira.enabled = True
    cfg.sources.jira.url = BASE
    cfg.sources.jira.email = "test@example.com"
    return cfg


@pytest.fixture
def gl_cfg(cfg: Config) -> Config:
    cfg.sources.gitlab.enabled = True
    return cfg


@pytest.fixture
def slack_cfg(cfg: Config) -> Config:
    cfg.sources.slack.enabled = True
    cfg.sources.slack.user_id = SLACK_ME
    return cfg


def make_gitlab(cfg: Config, vault: VaultStore, fake: FakeGitlab) -> GitLabSource:
    src = GitLabSource(cfg, vault)
    src._gl = fake  # type: ignore[assignment]
    return src


def make_slack(cfg: Config, vault: VaultStore, client: MagicMock) -> SlackSource:
    src = SlackSource(cfg, vault)
    src._client = client
    return src


# ---------------------------------------------------------------- gmail


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def full_msg(
    mid: str, frm: str, to: str, subject: str, body: str, at: datetime, html: str | None = None
) -> dict[str, Any]:
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(body)}}] if body else []
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": mid,
        "snippet": body[:50],
        "internalDate": str(int(at.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
            "parts": parts,
        },
    }


def gmail_full_service(threads: dict[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    def handler(path: str, kw: dict[str, Any]) -> Any:
        assert path == "users.threads.get" and kw["format"] == "full"
        if kw["id"] not in threads:
            raise not_found()
        return threads[kw["id"]]

    return fake_service(handler)


ALICE = "Alice Boss <alice@example.com>"
MY_ADDR = "Test User <TEST@example.com>"  # upper-cased on purpose: is_me is case-insensitive


def gmail_thread(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {"t1": {"id": "t1", "messages": messages}}


async def test_gmail_context_reads_bodies_and_computes_awaiting_me(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    threads = gmail_thread(
        [
            full_msg("m1", ALICE, "test@example.com", "Budget sign-off", "Can you approve?", ago(50)),
            full_msg("m2", MY_ADDR, "alice@example.com", "Re: Budget sign-off", "Looking now.", ago(30)),
            full_msg(
                "m3",
                ALICE,
                "test@example.com, Bob <bob@example.com>",
                "Re: Budget sign-off",
                "Any update?\n\n> Looking now.\nOn Tue someone wrote:\nold noise",
                ago(4),
            ),
        ]
    )
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service(threads)}):
        ctx = await src.fetch_context(item("gmail", "t1"))

    assert ctx.available and ctx.kind == "thread"
    assert [m.is_me for m in ctx.messages] == [False, True, False]
    assert ctx.messages[0].author == "Alice Boss" and ctx.messages[0].author_email == "alice@example.com"
    assert ctx.messages[2].text == "Any update?"  # quoted chain trimmed
    assert ctx.awaiting_me is True
    assert ctx.title == "Budget sign-off" and ctx.facts["subject"] == "Budget sign-off"
    assert ctx.facts["messages"] == "3" and "Alice Boss" in ctx.facts["last inbound"]
    assert ctx.url == "https://mail.google.com/mail/u/0/#inbox/t1"
    assert "awaiting your reply" in (ctx.status or "")
    assert {"alice@example.com", "bob@example.com"} <= {p.split("<")[-1].strip(">") for p in ctx.participants}
    assert ctx.fingerprint == "m3:3"


async def test_gmail_context_html_only_body_is_stripped(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    threads = gmail_thread(
        [full_msg("m1", ALICE, "test@example.com", "Hi", "", ago(2), html="<p>Hello <b>you</b></p>")]
    )
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service(threads)}):
        ctx = await src.fetch_context(item("gmail", "t1"))
    assert ctx.messages[0].text == "Hello you"


async def test_gmail_context_last_word_mine_is_not_awaiting_me(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    threads = gmail_thread(
        [
            full_msg("m1", ALICE, "test@example.com", "Q", "well?", ago(9)),
            full_msg("m2", MY_ADDR, "alice@example.com", "Re: Q", "done", ago(1)),
        ]
    )
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service(threads)}):
        ctx = await src.fetch_context(item("gmail", "t1"))
    assert ctx.awaiting_me is False and ctx.messages[-1].is_me is True
    assert "you replied last" in (ctx.status or "")


async def test_gmail_fingerprint_moves_only_when_the_thread_moves(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    messages = [full_msg("m1", ALICE, "test@example.com", "Q", "well?", ago(9))]
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service(gmail_thread(messages))}):
        first = await src.fetch_context(item("gmail", "t1"))
        again = await src.fetch_context(item("gmail", "t1"))
    assert first.fingerprint == again.fingerprint

    messages.append(full_msg("m2", ALICE, "test@example.com", "Q", "still waiting", ago(1)))
    with patch_services({"gmail": gmail_full_service(gmail_thread(messages))}):
        moved = await src.fetch_context(item("gmail", "t1"))
    assert moved.fingerprint != first.fingerprint and moved.fingerprint == "m2:2"


async def test_gmail_context_missing_thread_is_unavailable(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service({})}):
        ctx = await src.fetch_context(item("gmail", "gone"))
    assert ctx.available is False and ctx.kind == "none"
    assert ctx.note and "no longer exists" in ctx.note
    assert ctx.messages == [] and ctx.fingerprint == ""


async def test_gmail_context_caps_an_oversized_thread(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    messages = [
        full_msg(f"m{i}", ALICE, "test@example.com", "Long", "x" * 5000, ago(40 - i))
        for i in range(20)
    ]
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": gmail_full_service(gmail_thread(messages))}):
        ctx = await src.fetch_context(item("gmail", "t1"))
    assert len(ctx.messages) == 8  # newest-biased
    assert [m.author for m in ctx.messages] and ctx.messages[-1].at is not None
    assert all(len(m.text) <= 2000 for m in ctx.messages)
    assert ctx.facts["messages"] == "20" and ctx.facts["shown"] == "last 8 of 20 messages"
    assert ctx.fingerprint == "m19:20"


# ---------------------------------------------------------------- google chat

ME_SENDER = {"name": "users/111", "displayName": "Test User", "email": "test@example.com", "type": "HUMAN"}
BOB_SENDER = {"name": "users/222", "displayName": "Bob", "email": "bob@example.com", "type": "HUMAN"}


def chat_msg(mid: str, sender: dict[str, Any], text: str, at: datetime) -> dict[str, Any]:
    return {
        "name": f"spaces/AAA/messages/{mid}",
        "sender": sender,
        "text": text,
        "createTime": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "thread": {"name": "spaces/AAA/threads/T"},
    }


def chat_service(messages: list[dict[str, Any]], *, missing: bool = False):  # type: ignore[no-untyped-def]
    by_name = {m["name"]: m for m in messages}

    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "spaces.messages.get":
            if missing or kw["name"] not in by_name:
                raise not_found()
            return by_name[kw["name"]]
        if path == "spaces.get":
            return {"name": "spaces/AAA", "displayName": "Team", "spaceType": "SPACE"}
        if path == "spaces.messages.list":
            ordered = sorted(messages, key=lambda m: m["createTime"], reverse=True)
            return {"messages": ordered[: kw["pageSize"]]}
        raise AssertionError(path)

    return fake_service(handler)


async def test_chat_context_surrounding_messages_and_identity(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    messages = [
        chat_msg("m1", BOB_SENDER, "can you review the doc?", ago(30)),
        chat_msg("m2", ME_SENDER, "on it", ago(20)),
        chat_msg("m3", BOB_SENDER, "any luck?", ago(2)),
    ]
    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": chat_service(messages)}):
        ctx = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m1"))

    assert ctx.available and ctx.kind == "thread"
    assert [m.is_me for m in ctx.messages] == [False, True, False]
    assert ctx.messages[1].author == "Test User" and ctx.messages[1].author_email == "test@example.com"
    assert ctx.awaiting_me is True
    assert ctx.facts["space"] == "Team" and ctx.facts["messages"] == "3"
    assert ctx.participants == ["Bob", "Test User"]
    assert ctx.fingerprint == "spaces/AAA/messages/m3:3"


async def test_chat_context_fingerprint_and_unavailable(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    messages = [chat_msg("m1", BOB_SENDER, "ping", ago(5))]
    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": chat_service(messages)}):
        first = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m1"))
        again = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m1"))
    assert first.fingerprint == again.fingerprint

    messages.append(chat_msg("m2", BOB_SENDER, "still there?", ago(1)))
    with patch_services({"chat": chat_service(messages)}):
        moved = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m1"))
    assert moved.fingerprint != first.fingerprint

    with patch_services({"chat": chat_service(messages, missing=True)}):
        gone = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m1"))
    assert gone.available is False and gone.note and "no longer exists" in gone.note


async def test_chat_context_caps_a_busy_space(cfg: Config, vault: VaultStore, auth: GoogleAuth) -> None:
    messages = [chat_msg(f"m{i}", BOB_SENDER, "y" * 4000, ago(60 - i)) for i in range(40)]
    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": chat_service(messages)}):
        ctx = await src.fetch_context(item("google-chat", "spaces/AAA/messages/m0"))
    assert len(ctx.messages) == 15
    assert all(len(m.text) <= 2000 for m in ctx.messages)


# ---------------------------------------------------------------- calendar


def calendar_service(event: dict[str, Any] | None):  # type: ignore[no-untyped-def]
    def handler(path: str, kw: dict[str, Any]) -> Any:
        assert path == "events.get"
        if event is None:
            raise not_found()
        return event

    return fake_service(handler)


EVENT = {
    "id": "ev1",
    "summary": "Roadmap sync",
    "htmlLink": "https://cal/ev1",
    "updated": "2026-09-17T08:00:00.000Z",
    "start": {"dateTime": "2026-09-17T10:00:00+01:00"},
    "end": {"dateTime": "2026-09-17T10:30:00+01:00"},
    "organizer": {"email": "test@example.com", "self": True, "displayName": "Test User"},
    "attendees": [
        {"email": "test@example.com", "self": True, "responseStatus": "accepted"},
        {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "declined"},
        {"email": "room-1", "resource": True, "responseStatus": "accepted"},
    ],
}


async def test_calendar_context_describes_the_event(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = CalendarSource(cfg, vault, auth)
    with patch_services({"calendar": calendar_service(EVENT)}):
        ctx = await src.fetch_context(item("google-calendar", "ev1"))
    assert ctx.available and ctx.kind == "document"
    assert ctx.title == "Roadmap sync" and ctx.url == "https://cal/ev1"
    assert ctx.facts["attendees"] == "2" and ctx.facts["organiser"] == "you"
    assert ctx.facts["your response"] == "accepted" and ctx.facts["agenda"] == "none"
    assert "Bob (declined)" in ctx.participants
    assert ctx.awaiting_me is True  # my meeting, still no agenda
    assert ctx.messages == [] and "no agenda" in (ctx.status or "")
    assert ctx.fingerprint == "2026-09-17T08:00:00.000Z:2:0"


async def test_calendar_context_agenda_and_unavailable(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    with_agenda = {**EVENT, "description": "a" * 3000, "updated": "2026-09-17T09:00:00.000Z"}
    src = CalendarSource(cfg, vault, auth)
    with patch_services({"calendar": calendar_service(with_agenda)}):
        ctx = await src.fetch_context(item("google-calendar", "ev1"))
    assert ctx.awaiting_me is False and len(ctx.messages) == 1
    assert len(ctx.messages[0].text) == 2000 and ctx.messages[0].kind == "note"
    assert ctx.fingerprint != EVENT["updated"]

    with patch_services({"calendar": calendar_service(None)}):
        gone = await src.fetch_context(item("google-calendar", "ev1"))
    assert gone.available is False and gone.note and "no longer exists" in gone.note

    with patch_services({"calendar": calendar_service({**EVENT, "status": "cancelled"})}):
        cancelled = await src.fetch_context(item("google-calendar", "ev1"))
    assert cancelled.available is False and cancelled.note and "cancelled" in cancelled.note


# ---------------------------------------------------------------- meeting notes


def _para(text: str, style: str | None = None, bullet: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {"elements": [{"textRun": {"content": text + "\n"}}]}
    if style:
        p["paragraphStyle"] = {"namedStyleType": style}
    if bullet:
        p["bullet"] = {"listId": "l1"}
    return {"paragraph": p}


NOTES_DOC = {
    "title": "Roadmap sync - Notes by Gemini",
    "body": {
        "content": [
            _para("Summary", "HEADING_1"),
            _para("We discussed the roadmap."),
            _para("Next steps", "HEADING_2"),
            _para("Test User will send the numbers by Friday.", bullet=True),
        ]
    },
}


def notes_services(file: dict[str, Any] | None, doc: dict[str, Any] | None = NOTES_DOC):  # type: ignore[no-untyped-def]
    def drive(path: str, kw: dict[str, Any]) -> Any:
        assert path == "files.get"
        if file is None:
            raise not_found()
        return file

    def docs(path: str, kw: dict[str, Any]) -> Any:
        assert path == "documents.get"
        if doc is None:
            raise not_found()
        return doc

    return {"drive": fake_service(drive), "docs": fake_service(docs)}


FILE = {
    "id": "doc1",
    "name": "Roadmap sync - Notes by Gemini",
    "modifiedTime": "2026-09-16T11:00:00.000Z",
    "webViewLink": "https://docs/doc1",
    "owners": [{"displayName": "Gemini"}],
}


async def test_meeting_notes_context_returns_the_action_section(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = DriveNotesSource(cfg, vault, auth)
    with patch_services(notes_services(FILE)):
        ctx = await src.fetch_context(item("meeting-notes", "doc1"))
    assert ctx.available and ctx.kind == "document"
    assert len(ctx.messages) == 1 and ctx.messages[0].kind == "note"
    assert ctx.messages[0].text.startswith("## Next steps")
    assert "We discussed" not in ctx.messages[0].text
    assert ctx.facts["opened by you"] == "no, never" and ctx.facts["meeting"] == "Roadmap sync"
    assert ctx.facts["action items"] == "next-steps section"
    assert ctx.awaiting_me is True  # never opened it
    assert ctx.participants == ["Gemini"] and ctx.url == "https://docs/doc1"
    assert ctx.fingerprint.startswith("2026-09-16T11:00:00.000Z:")


async def test_meeting_notes_context_fingerprint_viewed_and_failures(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = DriveNotesSource(cfg, vault, auth)
    viewed = {**FILE, "viewedByMeTime": "2026-09-16T12:30:00Z"}
    with patch_services(notes_services(viewed)):
        first = await src.fetch_context(item("meeting-notes", "doc1"))
        again = await src.fetch_context(item("meeting-notes", "doc1"))
    assert first.fingerprint == again.fingerprint and first.awaiting_me is False

    edited = {**viewed, "modifiedTime": "2026-09-17T09:00:00.000Z"}
    with patch_services(notes_services(edited)):
        moved = await src.fetch_context(item("meeting-notes", "doc1"))
    assert moved.fingerprint != first.fingerprint

    with patch_services(notes_services(None)):
        gone = await src.fetch_context(item("meeting-notes", "doc1"))
    assert gone.available is False and gone.note and "no longer exists" in gone.note

    with patch_services(notes_services({**FILE, "trashed": True})):
        trashed = await src.fetch_context(item("meeting-notes", "doc1"))
    assert trashed.available is False and trashed.note and "trash" in trashed.note

    with patch_services(notes_services(FILE, doc=None)):
        unreadable = await src.fetch_context(item("meeting-notes", "doc1"))
    assert unreadable.available is False and unreadable.note


async def test_meeting_notes_context_caps_a_long_doc(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    big = {"title": "Big", "body": {"content": [_para("z" * 400) for _ in range(40)]}}
    src = DriveNotesSource(cfg, vault, auth)
    with patch_services(notes_services(FILE, doc=big)):
        ctx = await src.fetch_context(item("meeting-notes", "doc1"))
    assert len(ctx.messages[0].text) == 2000
    assert ctx.facts["action items"].startswith("none found")


# ---------------------------------------------------------------- slack


def slack_client(messages: list[dict[str, Any]], *, threaded: bool = True) -> MagicMock:
    client = basic_client()
    client.conversations_info.return_value = {"ok": True, "channel": {"id": "C1", "name": "general"}}
    client.conversations_replies.return_value = {"ok": True, "messages": messages if threaded else messages[:1]}
    client.conversations_history.return_value = {"ok": True, "messages": [] if threaded else messages}
    client.users_info.side_effect = lambda user: {
        "ok": True,
        "user": {"id": user, "name": user.lower(), "profile": {"display_name": user.lower()}},
    }
    return client


async def test_slack_context_uses_the_thread_and_resolves_names(
    slack_cfg: Config, vault: VaultStore
) -> None:
    ts = f"{NOW.timestamp() - 7200:.6f}"
    messages = [
        msg("can you look at the deploy?", ts, "U_BOB"),
        msg("looking", f"{NOW.timestamp() - 3600:.6f}", SLACK_ME, thread_ts=ts),
        msg("still broken", f"{NOW.timestamp() - 60:.6f}", "U_BOB", thread_ts=ts),
    ]
    src = make_slack(slack_cfg, vault, slack_client(messages))
    ctx = await src.fetch_context(item("slack", f"C1:{ts}"))

    assert ctx.available and ctx.kind == "thread"
    assert [m.is_me for m in ctx.messages] == [False, True, False]
    assert [m.author for m in ctx.messages] == ["u_bob", "u_me", "u_bob"]
    assert ctx.awaiting_me is True
    assert ctx.facts["channel"] == "#general" and ctx.facts["shape"] == "thread"
    assert ctx.facts["messages"] == "3"
    assert ctx.fingerprint == f"{messages[-1]['ts']}:3"


async def test_slack_context_falls_back_to_channel_history(
    slack_cfg: Config, vault: VaultStore
) -> None:
    ts = f"{NOW.timestamp() - 7200:.6f}"
    messages = [
        msg("standalone question", ts, "U_BOB"),
        msg("unrelated chatter", f"{NOW.timestamp() - 600:.6f}", "U_ZOE"),
    ]
    src = make_slack(slack_cfg, vault, slack_client(messages, threaded=False))
    ctx = await src.fetch_context(item("slack", f"C1:{ts}"))
    assert ctx.facts["shape"] == "channel around the message"
    assert len(ctx.messages) == 2 and ctx.awaiting_me is True


async def test_slack_context_fingerprint_and_missing_scope(
    slack_cfg: Config, vault: VaultStore
) -> None:
    ts = f"{NOW.timestamp() - 7200:.6f}"
    messages = [msg("q", ts, "U_BOB"), msg("a", f"{NOW.timestamp() - 60:.6f}", SLACK_ME, thread_ts=ts)]
    src = make_slack(slack_cfg, vault, slack_client(list(messages)))
    first = await src.fetch_context(item("slack", f"C1:{ts}"))
    again = await src.fetch_context(item("slack", f"C1:{ts}"))
    assert first.fingerprint == again.fingerprint and first.awaiting_me is False

    grown = [*messages, msg("one more", f"{NOW.timestamp() - 5:.6f}", "U_BOB", thread_ts=ts)]
    moved = await make_slack(slack_cfg, vault, slack_client(grown)).fetch_context(item("slack", f"C1:{ts}"))
    assert moved.fingerprint != first.fingerprint

    client = basic_client()
    client.conversations_replies.side_effect = api_error("missing_scope")
    denied = await make_slack(slack_cfg, vault, client).fetch_context(item("slack", f"C1:{ts}"))
    assert denied.available is False and denied.note and "missing_scope" in denied.note

    client = basic_client()
    client.conversations_replies.side_effect = api_error("thread_not_found")
    gone = await make_slack(slack_cfg, vault, client).fetch_context(item("slack", f"C1:{ts}"))
    assert gone.available is False and gone.note and "gone" in gone.note

    bad = await make_slack(slack_cfg, vault, basic_client()).fetch_context(item("slack", "nonsense"))
    assert bad.available is False and bad.note and "unrecognised" in bad.note


async def test_slack_context_caps_a_huge_thread(slack_cfg: Config, vault: VaultStore) -> None:
    ts = f"{NOW.timestamp() - 90000:.6f}"
    messages = [
        msg("w" * 5000, f"{float(ts) + i:.6f}", "U_BOB", thread_ts=ts) for i in range(60)
    ]
    src = make_slack(slack_cfg, vault, slack_client(messages))
    ctx = await src.fetch_context(item("slack", f"C1:{ts}"))
    assert len(ctx.messages) == 15
    assert all(len(m.text) <= 2000 for m in ctx.messages)
    assert ctx.messages[-1].at is not None


# ---------------------------------------------------------------- jira


def jira_issue(updated: str = "2026-09-16T10:00:00.000+0000", **fields: Any) -> dict[str, Any]:
    base = {
        "summary": "Ship the importer",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": JIRA_ME,
        "reporter": {"accountId": "acc-bob", "displayName": "Bob Builder", "emailAddress": "bob@example.com"},
        "priority": {"name": "High"},
        "duedate": "2026-09-20",
        "updated": updated,
        "created": "2026-09-01T10:00:00.000+0000",
        "issuetype": {"name": "Story"},
        "description": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Import the CSVs."}]}],
        },
    }
    base.update(fields)
    return {"key": "APP-1", "fields": base}


def jira_comment(cid: str, author: dict[str, Any], text: str, created: str) -> dict[str, Any]:
    return {
        "id": cid,
        "author": author,
        "created": created,
        "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]},
    }


def mock_jira(issue: dict[str, Any] | int, comments: list[dict[str, Any]] | None = None) -> None:
    if isinstance(issue, int):
        respx.get(f"{BASE}/rest/api/3/issue/APP-1").mock(return_value=httpx.Response(issue))
    else:
        respx.get(f"{BASE}/rest/api/3/issue/APP-1").mock(return_value=httpx.Response(200, json=issue))
    body = {"comments": comments or [], "total": len(comments or [])}
    respx.get(f"{BASE}/rest/api/3/issue/APP-1/comment").mock(return_value=httpx.Response(200, json=body))


@respx.mock
async def test_jira_context_description_then_comments(jira_cfg: Config, vault: VaultStore) -> None:
    bob = {"displayName": "Bob Builder", "emailAddress": "bob@example.com"}
    comments = [
        jira_comment("1", bob, "Blocked on the schema.", "2026-09-15T10:00:00.000+0000"),
        jira_comment("2", JIRA_ME, "Unblocked it.", "2026-09-16T09:00:00.000+0000"),
    ]
    mock_jira(jira_issue(), comments)
    ctx = await JiraSource(jira_cfg, vault).fetch_context(item("jira", "APP-1"))

    assert ctx.available and ctx.kind == "ticket"
    assert ctx.title == "APP-1: Ship the importer" and ctx.url == f"{BASE}/browse/APP-1"
    assert [m.kind for m in ctx.messages] == ["note", "comment", "comment"]
    assert ctx.messages[0].text == "Import the CSVs."  # ADF flattened
    assert [m.is_me for m in ctx.messages] == [False, False, True]
    assert ctx.status is not None
    assert ctx.status.startswith("In Progress · assignee Test User · updated ")
    assert ctx.facts == {
        "key": "APP-1",
        "type": "Story",
        "priority": "High",
        "due date": "2026-09-20",
        "reporter": "Bob Builder",
        "comments": "2",
    }
    assert ctx.awaiting_me is True  # assigned to me
    assert ctx.fingerprint == "2026-09-16T10:00:00.000+0000:2"


@respx.mock
async def test_jira_context_fingerprint_moves_with_updated_and_comments(
    jira_cfg: Config, vault: VaultStore
) -> None:
    src = JiraSource(jira_cfg, vault)
    mock_jira(jira_issue(), [])
    first = await src.fetch_context(item("jira", "APP-1"))
    again = await src.fetch_context(item("jira", "APP-1"))
    assert first.fingerprint == again.fingerprint

    mock_jira(
        jira_issue(updated="2026-09-17T11:00:00.000+0000"),
        [jira_comment("9", {"displayName": "Bob Builder"}, "any news?", "2026-09-17T11:00:00.000+0000")],
    )
    moved = await src.fetch_context(item("jira", "APP-1"))
    assert moved.fingerprint != first.fingerprint
    assert moved.awaiting_me is True


@respx.mock
async def test_jira_context_unassigned_awaits_on_the_last_word(
    jira_cfg: Config, vault: VaultStore
) -> None:
    mock_jira(
        jira_issue(assignee=None, status={"name": "Open"}),
        [jira_comment("1", JIRA_ME, "I'll take it", "2026-09-16T09:00:00.000+0000")],
    )
    ctx = await JiraSource(jira_cfg, vault).fetch_context(item("jira", "APP-1"))
    assert ctx.status.startswith("Open · unassigned")
    assert ctx.awaiting_me is False


@respx.mock
async def test_jira_context_failures_are_explained(jira_cfg: Config, vault: VaultStore) -> None:
    src = JiraSource(jira_cfg, vault)
    mock_jira(404)
    gone = await src.fetch_context(item("jira", "APP-1"))
    assert gone.available is False and gone.note and "no longer exists" in gone.note

    mock_jira(403)
    denied = await src.fetch_context(item("jira", "APP-1"))
    assert denied.available is False and denied.note and "permission" in denied.note

    mock_jira(500)
    broken = await src.fetch_context(item("jira", "APP-1"))
    assert broken.available is False and broken.note and "500" in broken.note

    respx.get(f"{BASE}/rest/api/3/issue/APP-1").mock(side_effect=httpx.ConnectError("no route"))
    offline = await src.fetch_context(item("jira", "APP-1"))
    assert offline.available is False and offline.note and "could not reach Jira" in offline.note


@respx.mock
async def test_jira_context_caps_comments_and_length(jira_cfg: Config, vault: VaultStore) -> None:
    comments = [
        jira_comment(str(i), {"displayName": "Bob Builder"}, "c" * 5000, f"2026-09-{i + 1:02d}T10:00:00.000+0000")
        for i in range(25)
    ]
    mock_jira(jira_issue(), comments)
    ctx = await JiraSource(jira_cfg, vault).fetch_context(item("jira", "APP-1"))
    assert len([m for m in ctx.messages if m.kind == "comment"]) == 10
    assert all(len(m.text) <= 2000 for m in ctx.messages)
    assert ctx.facts["comments"] == "25"  # the cap is on what we show, not what we report


@respx.mock
async def test_jira_context_survives_unreadable_comments(jira_cfg: Config, vault: VaultStore) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/APP-1").mock(return_value=httpx.Response(200, json=jira_issue()))
    respx.get(f"{BASE}/rest/api/3/issue/APP-1/comment").mock(return_value=httpx.Response(403))
    ctx = await JiraSource(jira_cfg, vault).fetch_context(item("jira", "APP-1"))
    assert ctx.available and [m.kind for m in ctx.messages] == ["note"]
    assert ctx.facts["comments"] == "0"


# ---------------------------------------------------------------- gitlab


def gl_note(nid: int, username: str, body: str, created: str, **extra: Any) -> dict[str, Any]:
    base = {
        "id": nid,
        "body": body,
        "author": {"name": username.title(), "username": username},
        "created_at": created,
        "system": False,
    }
    base.update(extra)
    return base


def with_notes(target: SimpleNamespace, notes: list[dict[str, Any]]) -> SimpleNamespace:
    target.notes = SimpleNamespace(list=lambda **kw: list(notes))
    return target


def ctx_mr(**extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        iid=1,
        project_id=7,
        title="Add the importer",
        description="Imports the CSVs.",
        web_url="https://gitlab.example/grp/app/-/merge_requests/1",
        author={"name": "Ana Author", "username": "ana"},
        assignees=[{"name": "Ana Author", "username": "ana"}],
        reviewers=[{"username": "me", "name": "Test User"}],
        source_branch="feat/import",
        target_branch="main",
        changes_count="12",
        created_at="2026-09-10T09:00:00.000Z",
        updated_at="2026-09-16T09:00:00.000Z",
        state="opened",
        draft=False,
        head_pipeline={"status": "failed"},
        blocking_discussions_resolved=False,
        references={"full": "grp/app!1"},
    )
    base.update(extra)
    return gl_obj(**base)


def ctx_issue(**extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        iid=4,
        project_id=7,
        title="Fix the parser",
        description="It drops rows.",
        web_url="https://gitlab.example/grp/app/-/issues/4",
        author={"name": "Ana Author", "username": "ana"},
        assignees=[{"name": "Test User", "username": "me"}],
        labels=["bug", "p1"],
        due_date="2026-09-20",
        created_at="2026-09-10T09:00:00.000Z",
        updated_at="2026-09-16T09:00:00.000Z",
        state="opened",
        references={"full": "grp/app#4"},
    )
    base.update(extra)
    return gl_obj(**base)


NOTES = [
    gl_note(1, "ana", "Ready for another look.", "2026-09-14T09:00:00.000Z"),
    gl_note(2, "ci-bot", "changed the description", "2026-09-15T09:00:00.000Z", system=True),
    gl_note(3, "me", "Left a few comments.", "2026-09-15T10:00:00.000Z", resolvable=True, resolved=True),
    gl_note(4, "ana", "Fixed, please re-check.", "2026-09-16T09:00:00.000Z", resolvable=True, resolved=False),
]


async def test_gitlab_mr_context_status_and_notes(gl_cfg: Config, vault: VaultStore) -> None:
    fake = FakeGitlab(fetched=with_notes(ctx_mr(), NOTES), approved=[])
    ctx = await make_gitlab(gl_cfg, vault, fake).fetch_context(item("gitlab", "mr:7:1"))

    assert ctx.available and ctx.kind == "review"
    assert ctx.title == "MR !1: Add the importer"
    assert ctx.status == "opened · pipeline failed · 0 approvals · 1 unresolved threads"
    assert [m.kind for m in ctx.messages] == ["note", "comment", "comment", "comment"]
    assert [m.is_me for m in ctx.messages] == [False, False, True, False]  # system note skipped
    assert ctx.messages[0].text == "Imports the CSVs."
    assert ctx.facts["source branch"] == "feat/import" and ctx.facts["target branch"] == "main"
    assert ctx.facts["reviewers"] == "Test User" and ctx.facts["changes"] == "12"
    assert ctx.awaiting_me is True  # I am a reviewer and have not approved
    assert ctx.fingerprint == "2026-09-16T09:00:00.000Z:4:failed"


async def test_gitlab_mr_context_approved_is_not_awaiting_me(
    gl_cfg: Config, vault: VaultStore
) -> None:
    mr = with_notes(ctx_mr(head_pipeline={"status": "success"}, draft=True), [])
    ctx = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=mr, approved=["me"])).fetch_context(
        item("gitlab", "mr:7:1")
    )
    assert ctx.status == "opened · draft · pipeline success · 1 approvals · unresolved discussions"
    assert ctx.awaiting_me is False
    assert ctx.fingerprint.endswith(":0:success")


async def test_gitlab_fingerprint_moves_on_a_new_note_or_pipeline(
    gl_cfg: Config, vault: VaultStore
) -> None:
    first = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=with_notes(ctx_mr(), NOTES))).fetch_context(
        item("gitlab", "mr:7:1")
    )
    again = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=with_notes(ctx_mr(), NOTES))).fetch_context(
        item("gitlab", "mr:7:1")
    )
    assert first.fingerprint == again.fingerprint

    grown = with_notes(
        ctx_mr(updated_at="2026-09-17T09:00:00.000Z"),
        [*NOTES, gl_note(5, "ana", "bump", "2026-09-17T09:00:00.000Z")],
    )
    moved = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=grown)).fetch_context(item("gitlab", "mr:7:1"))
    assert moved.fingerprint != first.fingerprint

    repiped = with_notes(ctx_mr(head_pipeline={"status": "success"}), NOTES)
    pipeline = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=repiped)).fetch_context(
        item("gitlab", "mr:7:1")
    )
    assert pipeline.fingerprint != first.fingerprint


async def test_gitlab_issue_context(gl_cfg: Config, vault: VaultStore) -> None:
    issue = with_notes(ctx_issue(), [gl_note(1, "ana", "Still happening.", "2026-09-16T09:00:00.000Z")])
    ctx = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=issue)).fetch_context(item("gitlab", "issue:7:4"))
    assert ctx.title == "Issue #4: Fix the parser"
    assert ctx.status.startswith("opened · assigned to Test User · updated ")
    assert ctx.facts["labels"] == "bug, p1" and ctx.facts["due date"] == "2026-09-20"
    assert ctx.facts["comments"] == "1"
    assert ctx.awaiting_me is True and ctx.messages[-1].is_me is False


async def test_gitlab_context_failures_are_explained(gl_cfg: Config, vault: VaultStore) -> None:
    missing = await make_gitlab(gl_cfg, vault, FakeGitlab(missing=True)).fetch_context(
        item("gitlab", "mr:7:1")
    )
    assert missing.available is False and missing.note and "no longer exists" in missing.note

    bad = await make_gitlab(gl_cfg, vault, FakeGitlab()).fetch_context(item("gitlab", "nonsense"))
    assert bad.available is False and bad.note and "unrecognised" in bad.note

    forbidden = FakeGitlab()
    forbidden._project = lambda pid, lazy=False: SimpleNamespace(  # type: ignore[assignment]
        mergerequests=SimpleNamespace(
            get=_raise(GitlabGetError("403 Forbidden", response_code=403))
        ),
        issues=SimpleNamespace(get=_raise(GitlabGetError("403 Forbidden", response_code=403))),
    )
    forbidden.projects = SimpleNamespace(get=forbidden._project)
    denied = await make_gitlab(gl_cfg, vault, forbidden).fetch_context(item("gitlab", "mr:7:1"))
    assert denied.available is False and denied.note and "read_api" in denied.note


def _raise(err: Exception):  # type: ignore[no-untyped-def]
    def fn(*args: Any, **kwargs: Any) -> Any:
        raise err

    return fn


async def test_gitlab_context_caps_notes_and_length(gl_cfg: Config, vault: VaultStore) -> None:
    notes = [gl_note(i, "ana", "n" * 5000, f"2026-09-{i + 1:02d}T09:00:00.000Z") for i in range(25)]
    ctx = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=with_notes(ctx_mr(), notes))).fetch_context(
        item("gitlab", "mr:7:1")
    )
    assert len([m for m in ctx.messages if m.kind == "comment"]) == 10
    assert all(len(m.text) <= 2000 for m in ctx.messages)


async def test_gitlab_notes_failure_still_returns_a_context(gl_cfg: Config, vault: VaultStore) -> None:
    broken = ctx_mr()
    broken.notes = SimpleNamespace(list=_raise(RuntimeError("notes API is off")))
    ctx = await make_gitlab(gl_cfg, vault, FakeGitlab(fetched=broken)).fetch_context(item("gitlab", "mr:7:1"))
    assert ctx.available and [m.kind for m in ctx.messages] == ["note"]


# ---------------------------------------------------------------- the default


async def test_unimplemented_sources_are_honest_about_it() -> None:
    from nbrain.sources.base import BaseSource

    ctx = await BaseSource().fetch_context(item("base", "x"))
    assert ctx.available is False and ctx.kind == "none" and ctx.note
    assert ctx.messages == [] and ctx.fingerprint == ""


