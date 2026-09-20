"""Every source emits Interaction records: who I dealt with, where, and how often.

One record per person per thread / event / issue — never per message, or a single chatty thread
would outweigh ten real relationships. No network: the fakes come from the per-source test
modules so the shapes stay honest."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import respx
from test_gitlab_source import FakeGitlab
from test_gitlab_source import issue as gl_issue
from test_gitlab_source import make_source as make_gitlab
from test_gitlab_source import mr as gl_mr
from test_google_sources import _chat_msg, _gmail_service, _msg, fake_service, patch_services
from test_jira_source import BASE as JIRA_BASE
from test_jira_source import BOB as JIRA_BOB
from test_jira_source import ME as JIRA_ME
from test_jira_source import issue as jira_issue
from test_jira_source import mock_search as mock_jira_search
from test_slack_source import ME as SLACK_ME
from test_slack_source import basic_client, hours_ago, match
from test_slack_source import make_source as make_slack
from test_slack_source import window as slack_window

from nbrain.config.schema import Config
from nbrain.sources.base import Interaction, Window
from nbrain.sources.gitlab import GitLabSource
from nbrain.sources.google import CalendarSource, ChatSource, GmailSource, GoogleAuth
from nbrain.sources.jira import JiraSource
from nbrain.sources.slack import SlackSource
from nbrain.vault.store import VaultStore

TODAY = date(2026, 9, 17)


def window(**kw: Any) -> Window:
    base: dict[str, Any] = dict(
        today=TODAY,
        email_lookback_days=7,
        commitment_lookback_days=30,
        awaiting_reply_hours=48,
        ticket_stale_days=5,
        review_age_days=3,
        tz="Europe/London",
    )
    base.update(kw)
    return Window(**base)


def _ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


@pytest.fixture
def auth(cfg: Config) -> GoogleAuth:
    cfg.sources.google.enabled = True
    return GoogleAuth(cfg)


def by_person(interactions: list[Interaction]) -> dict[str, Interaction]:
    return {(i.person_email or i.person_name or "?"): i for i in interactions}


# ---------- gmail ----------


def _thread(tid: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": tid, "messages": messages}


GMAIL_THREADS: dict[str, dict[str, Any]] = {
    "t-pair": _thread(
        "t-pair",
        [
            _msg(mid="a1", frm="Alice <alice@example.com>", to="test@example.com",
                 subject="Budget", snippet="thoughts?", at=_ago(50)),
            _msg(mid="a2", frm="Test User <test@example.com>", to="alice@example.com",
                 subject="Budget", snippet="sure", at=_ago(49)),
            _msg(mid="a3", frm="Alice <alice@example.com>", to="test@example.com",
                 subject="Budget", snippet="and one more thing", at=_ago(48)),
        ],
    ),
    "t-wide": _thread(
        "t-wide",
        [
            _msg(mid="b1", frm="Bob <bob@example.com>",
                 to="test@example.com, Carol <carol@example.com>",
                 subject="Launch plan", snippet="all hands", at=_ago(20)),
        ],
    ),
    "t-robot": _thread(
        "t-robot",
        [
            _msg(mid="c1", frm="no-reply@build.example", to="test@example.com",
                 subject="Build failed", snippet="see log", at=_ago(9)),
        ],
    ),
}


async def collect_gmail(cfg: Config, vault: VaultStore, auth: GoogleAuth,
                        threads: dict[str, dict[str, Any]] | None = None) -> Any:
    src = GmailSource(cfg, vault, auth)
    with patch_services({"gmail": _gmail_service(threads if threads is not None else GMAIL_THREADS)}):
        return await src.collect(window())


async def test_gmail_one_interaction_per_person_per_thread(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_gmail(cfg, vault, auth)
    pair = [i for i in res.interactions if i.ref == "t-pair"]
    # Alice sent two of the three messages; she is still exactly one record.
    assert [i.person_email for i in pair] == ["alice@example.com"]
    assert pair[0].channel == "email" and pair[0].subject == "Budget"
    assert pair[0].group is False and pair[0].with_me is True
    assert pair[0].inbound is True  # Alice had the last word
    assert pair[0].person_name == "Alice"
    assert pair[0].at is not None and pair[0].at.tzinfo is not None


async def test_gmail_never_records_me_and_marks_wide_threads_as_group(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_gmail(cfg, vault, auth)
    assert "test@example.com" not in {i.person_email for i in res.interactions}
    wide = [i for i in res.interactions if i.ref == "t-wide"]
    assert {i.person_email for i in wide} == {"bob@example.com", "carol@example.com"}
    assert all(i.group is True for i in wide)  # Bob, Carol and me is more than two humans
    assert all(i.subject == "Launch plan" for i in wide)


async def test_gmail_skips_automated_and_noise_listed_senders(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    cfg.noise.suppress = ["newsletter.example"]
    cfg.noise.mine = ["jira@atlassian.net"]
    threads = {
        "t-robot": GMAIL_THREADS["t-robot"],
        "t-news": _thread("t-news", [
            _msg(mid="n1", frm="news@newsletter.example", to="test@example.com",
                 subject="Digest", snippet="read", at=_ago(9))]),
        "t-jira": _thread("t-jira", [
            _msg(mid="j1", frm="jira@atlassian.net", to="test@example.com",
                 subject="[JIRA] assigned", snippet="you", at=_ago(9))]),
        "t-bounce": _thread("t-bounce", [
            _msg(mid="d1", frm="Mail Delivery <MAILER-DAEMON@example.com>", to="test@example.com",
                 subject="Undelivered", snippet="failed", at=_ago(9))]),
    }
    res = await collect_gmail(cfg, vault, auth, threads)
    assert res.interactions == []


async def test_gmail_ref_is_stable_so_two_reads_dedupe(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    first = await collect_gmail(cfg, vault, auth)
    second = await collect_gmail(cfg, vault, auth)
    assert [i.ref for i in first.interactions] == [i.ref for i in second.interactions]
    keys = {i.key() for i in first.interactions}
    assert keys == {i.key() for i in [*first.interactions, *second.interactions]}
    assert "email:t-pair:alice@example.com" in keys


async def test_gmail_interaction_failure_still_returns_signals(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = GmailSource(cfg, vault, auth)
    threads = {"t-wide": GMAIL_THREADS["t-wide"]}
    with (
        patch_services({"gmail": _gmail_service(threads)}),
        patch.object(GmailSource, "_thread_interactions", side_effect=RuntimeError("boom")),
    ):
        res = await src.collect(window())
    assert res.interactions == []
    assert [t.source_id for t in res.texts] == ["t-wide"]  # collection carried on


# ---------- google calendar ----------


def _calendar_service(events: list[dict[str, Any]]) -> Any:
    def handler(path: str, kw: dict[str, Any]) -> Any:
        assert path == "events.list"
        return {"items": events}

    return fake_service(handler)


def CAL_EVENTS() -> list[dict[str, Any]]:
    day = TODAY.isoformat()
    return [
        {
            "id": "ev-1to1", "summary": "1:1 with Bob",
            "start": {"dateTime": f"{day}T09:00:00+01:00"},
            "end": {"dateTime": f"{day}T09:30:00+01:00"},
            "organizer": {"email": "test@example.com", "self": True},
            "description": "agenda: last week, blockers, growth",
            "attendees": [
                {"email": "test@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "accepted"},
            ],
        },
        {
            "id": "ev-wide", "summary": "Roadmap sync",
            "start": {"dateTime": f"{day}T10:00:00+01:00"},
            "end": {"dateTime": f"{day}T11:00:00+01:00"},
            "organizer": {"email": "bob@example.com"},
            "description": "agenda for the roadmap review",
            "attendees": [
                {"email": "test@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "accepted"},
                {"email": "carol@example.com", "displayName": "Carol",
                 "responseStatus": "needsAction"},
                {"email": "eve@client.io", "displayName": "Eve", "responseStatus": "declined"},
                {"email": "room-7@resource.calendar.google.com", "displayName": "Room 7",
                 "resource": True, "responseStatus": "accepted"},
            ],
        },
        {
            "id": "ev-i-declined", "summary": "Optional",
            "start": {"dateTime": f"{day}T12:00:00+01:00"},
            "end": {"dateTime": f"{day}T12:30:00+01:00"},
            "organizer": {"email": "dave@example.com"},
            "attendees": [
                {"email": "test@example.com", "self": True, "responseStatus": "declined"},
                {"email": "dave@example.com", "displayName": "Dave"},
            ],
        },
    ]


async def collect_calendar(cfg: Config, vault: VaultStore, auth: GoogleAuth) -> Any:
    src = CalendarSource(cfg, vault, auth)
    with patch_services({"calendar": _calendar_service(CAL_EVENTS())}):
        return await src.collect(window())


async def test_calendar_one_interaction_per_attendee_per_event(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_calendar(cfg, vault, auth)
    one_to_one = [i for i in res.interactions if i.ref == "ev-1to1"]
    assert [i.person_email for i in one_to_one] == ["bob@example.com"]
    i = one_to_one[0]
    assert i.channel == "meeting" and i.subject == "1:1 with Bob"
    assert i.group is False and i.with_me is True and i.person_name == "Bob"
    assert i.at is not None and i.at.hour == 9


async def test_calendar_skips_me_rooms_decliners_and_events_i_declined(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_calendar(cfg, vault, auth)
    assert "test@example.com" not in {i.person_email for i in res.interactions}
    assert "ev-i-declined" not in {i.ref for i in res.interactions}
    wide = [i for i in res.interactions if i.ref == "ev-wide"]
    assert {i.person_email for i in wide} == {"bob@example.com", "carol@example.com"}
    assert all(i.group is True for i in wide)  # five on the invite, two of them not company


async def test_calendar_ref_is_stable_across_reads(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    first = await collect_calendar(cfg, vault, auth)
    second = await collect_calendar(cfg, vault, auth)
    assert {i.key() for i in first.interactions} == {i.key() for i in second.interactions}
    assert "meeting:ev-1to1:bob@example.com" in {i.key() for i in first.interactions}


async def test_calendar_interaction_failure_still_returns_events(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = CalendarSource(cfg, vault, auth)
    with (
        patch_services({"calendar": _calendar_service(CAL_EVENTS())}),
        patch.object(CalendarSource, "_event_interactions", side_effect=RuntimeError("boom")),
    ):
        res = await src.collect(window())
    assert res.interactions == []
    assert [e.source_id for e in res.events] == ["ev-1to1", "ev-wide"]


# ---------- google chat ----------


CHAT_ME = {"name": "users/111", "displayName": "Test User", "email": "test@example.com",
           "type": "HUMAN"}
CHAT_BOB = {"name": "users/222", "displayName": "Bob", "email": "bob@example.com",
            "type": "HUMAN"}
CHAT_CAROL = {"name": "users/333", "displayName": "Carol", "type": "HUMAN"}  # no email exposed
CHAT_BOT = {"name": "users/999", "displayName": "Jira", "type": "BOT"}

CHAT_SPACES = [
    {"name": "spaces/AAA", "displayName": "Team", "spaceType": "SPACE"},
    {"name": "spaces/BBB", "spaceType": "DIRECT_MESSAGE"},
]


def _chat_service() -> Any:
    messages = {
        "spaces/AAA": [
            _chat_msg("spaces/AAA/messages/m4", CHAT_BOB, "and another thing", _ago(10)),
            _chat_msg("spaces/AAA/messages/m3", CHAT_BOB, "ping?", _ago(12)),
            _chat_msg("spaces/AAA/messages/m2", CHAT_CAROL, "here is the doc", _ago(14)),
            _chat_msg("spaces/AAA/messages/m1", CHAT_BOT, "Issue moved", _ago(16)),
        ],
        "spaces/BBB": [
            _chat_msg("spaces/BBB/messages/d2", CHAT_ME, "on it", _ago(4)),
            _chat_msg("spaces/BBB/messages/d1", CHAT_BOB, "did you see my note?", _ago(6)),
        ],
    }

    def handler(path: str, kw: dict[str, Any]) -> Any:
        if path == "spaces.list":
            return {"spaces": CHAT_SPACES}
        return {"messages": messages[kw["parent"]]}

    return fake_service(handler)


async def collect_chat(cfg: Config, vault: VaultStore, auth: GoogleAuth) -> Any:
    src = ChatSource(cfg, vault, auth)
    with patch_services({"chat": _chat_service()}):
        return await src.collect(window())


async def test_chat_one_interaction_per_person_per_space(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_chat(cfg, vault, auth)
    team = [i for i in res.interactions if i.ref == "spaces/AAA"]
    # Bob spoke twice in the space and is still one record; the bot is none.
    assert sorted(i.person_name or "" for i in team) == ["Bob", "Carol"]
    bob = by_person(team)["bob@example.com"]
    assert bob.channel == "chat" and bob.subject == "Team" and bob.group is True
    assert bob.with_me is True and bob.inbound is True
    carol = by_person(team)["Carol"]
    assert carol.person_email is None  # Chat gave a display name only; nothing was invented


async def test_chat_dm_is_not_a_group_and_i_am_never_my_own_contact(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    res = await collect_chat(cfg, vault, auth)
    dm = [i for i in res.interactions if i.ref == "spaces/BBB"]
    assert [i.person_email for i in dm] == ["bob@example.com"]
    assert dm[0].group is False and dm[0].inbound is False  # I had the last word
    assert "test@example.com" not in {i.person_email for i in res.interactions}
    assert "Jira" not in {i.person_name for i in res.interactions}


async def test_chat_ref_is_stable_across_reads(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    first = await collect_chat(cfg, vault, auth)
    second = await collect_chat(cfg, vault, auth)
    assert {i.key() for i in first.interactions} == {i.key() for i in second.interactions}
    assert "chat:spaces/AAA:bob@example.com" in {i.key() for i in first.interactions}


async def test_chat_interaction_failure_still_returns_texts(
    cfg: Config, vault: VaultStore, auth: GoogleAuth
) -> None:
    src = ChatSource(cfg, vault, auth)
    with (
        patch_services({"chat": _chat_service()}),
        patch.object(ChatSource, "_space_interactions", side_effect=RuntimeError("boom")),
    ):
        res = await src.collect(window())
    assert res.interactions == []
    assert res.texts  # the space was still read for commitments


# ---------- slack ----------


def slack_client(mine: list[dict[str, Any]], mentions: list[dict[str, Any]]) -> Any:
    client = basic_client()
    client.users_info.side_effect = lambda user: {
        "ok": True,
        "user": {"id": user, "name": user.lower(), "real_name": user,
                 "profile": {"display_name": user.lower()}},
    }

    def search(query: str, count: int, page: int, sort: str) -> dict[str, Any]:
        matches = mine if query.startswith("from:") else mentions
        return {"ok": True, "messages": {"matches": matches, "paging": {"pages": 1}}}

    client.search_messages.side_effect = search
    client.conversations_replies.side_effect = lambda channel, ts, limit: {
        "ok": True,
        "messages": [{"type": "message", "text": "orig", "ts": ts, "user": "U_BOB"}],
    }
    client.conversations_history.return_value = {"ok": True, "messages": []}
    return client


SLACK_MENTIONS = [
    match(f"<@{SLACK_ME}> can you look?", hours_ago(72), "U_BOB"),
    match(f"<@{SLACK_ME}> and also this", hours_ago(71), "U_BOB"),
    match(f"<@{SLACK_ME}> my turn", hours_ago(70), "U_CAROL"),
    match(f"<@{SLACK_ME}> dm question", hours_ago(69), "U_DAN", channel="D9", name="dan"),
    {**match(f"<@{SLACK_ME}> deploy done", hours_ago(68), "U_BOT"), "bot_id": "B1"},
]


async def collect_slack(cfg: Config, vault: VaultStore) -> Any:
    client = slack_client([match("I'll do it", hours_ago(30), SLACK_ME)], SLACK_MENTIONS)
    return await make_slack(cfg, vault, client).collect(slack_window())


@pytest.fixture
def slack_cfg(cfg: Config) -> Config:
    cfg.sources.slack.enabled = True
    cfg.sources.slack.user_id = SLACK_ME
    return cfg


async def test_slack_one_interaction_per_person_per_channel(
    slack_cfg: Config, vault: VaultStore
) -> None:
    res = await collect_slack(slack_cfg, vault)
    general = [i for i in res.interactions if i.ref == "C1"]
    # Bob wrote twice in #general; one record, and never one per message.
    assert sorted(i.person_name or "" for i in general) == ["u_bob", "u_carol"]
    bob = by_person(general)["u_bob"]
    assert bob.channel == "slack" and bob.subject == "general" and bob.group is True
    assert bob.with_me is True and bob.person_email is None  # no email in the profile fake


async def test_slack_dm_is_not_a_group_and_bots_and_i_are_excluded(
    slack_cfg: Config, vault: VaultStore
) -> None:
    res = await collect_slack(slack_cfg, vault)
    dm = [i for i in res.interactions if i.ref == "D9"]
    assert [i.person_name for i in dm] == ["u_dan"] and dm[0].group is False
    assert SLACK_ME not in {i.person_name for i in res.interactions}
    assert "u_bot" not in {i.person_name for i in res.interactions}


async def test_slack_ref_is_stable_across_reads(slack_cfg: Config, vault: VaultStore) -> None:
    first = await collect_slack(slack_cfg, vault)
    second = await collect_slack(slack_cfg, vault)
    assert {i.key() for i in first.interactions} == {i.key() for i in second.interactions}
    assert "slack:C1:u_bob" in {i.key() for i in first.interactions}


async def test_slack_interaction_failure_still_returns_signals(
    slack_cfg: Config, vault: VaultStore
) -> None:
    client = slack_client([], SLACK_MENTIONS)
    src = make_slack(slack_cfg, vault, client)
    with patch.object(SlackSource, "_tally", side_effect=RuntimeError("boom")):
        res = await src.collect(slack_window())
    assert res.interactions == []
    assert {s.source_id for s in res.signals} >= {f"C1:{SLACK_MENTIONS[0]['ts']}",
                                                  f"D9:{SLACK_MENTIONS[3]['ts']}"}


# ---------- jira ----------


@pytest.fixture
def jira_cfg(cfg: Config) -> Config:
    cfg.sources.jira.enabled = True
    cfg.sources.jira.url = JIRA_BASE
    cfg.sources.jira.email = "test@example.com"
    return cfg


@pytest.fixture(autouse=True)
def jira_token():
    with patch("nbrain.sources.jira.get_secret", return_value="tok") as p:
        yield p


def jira_window(**kw: Any) -> Window:
    return window(tz="UTC", **kw)


@respx.mock
async def test_jira_records_assignee_and_reporter_once_per_issue(
    jira_cfg: Config, vault: VaultStore
) -> None:
    mock_jira_search(
        [jira_issue("APP-1", updated_days=2, assignee=JIRA_ME, reporter=JIRA_BOB)],
        [jira_issue("APP-2", updated_days=1, assignee=JIRA_BOB, reporter=JIRA_ME)],
    )
    res = await JiraSource(jira_cfg, vault).collect(jira_window())
    assert [i.ref for i in res.interactions] == ["APP-1", "APP-2"]
    one = res.interactions[0]
    assert one.channel == "ticket" and one.subject == "APP-1" and one.group is False
    assert one.person_email == "bob@example.com" and one.person_name == "Bob Builder"
    assert one.with_me is True and one.at is not None


@respx.mock
async def test_jira_never_records_me_and_dedupes_an_issue_in_both_lists(
    jira_cfg: Config, vault: VaultStore
) -> None:
    both = jira_issue("APP-9", updated_days=8, assignee=JIRA_ME, reporter=JIRA_ME)
    mock_jira_search([both], [both])
    res = await JiraSource(jira_cfg, vault).collect(jira_window())
    assert res.interactions == []  # both roles are me


@respx.mock
async def test_jira_ref_is_stable_across_reads(jira_cfg: Config, vault: VaultStore) -> None:
    mock_jira_search([jira_issue("APP-1", updated_days=2, reporter=JIRA_BOB)], [])
    src = JiraSource(jira_cfg, vault)
    first = await src.collect(jira_window())
    second = await src.collect(jira_window())
    assert {i.key() for i in first.interactions} == {"ticket:APP-1:bob@example.com"}
    assert {i.key() for i in first.interactions} == {i.key() for i in second.interactions}


@respx.mock
async def test_jira_interaction_failure_still_returns_signals(
    jira_cfg: Config, vault: VaultStore
) -> None:
    mock_jira_search([jira_issue("APP-2", updated_days=9, reporter=JIRA_BOB)], [])
    src = JiraSource(jira_cfg, vault)
    with patch.object(JiraSource, "_interactions", side_effect=RuntimeError("boom")):
        res = await src.collect(jira_window())
    assert res.interactions == []
    assert [s.source_id for s in res.signals] == ["APP-2"]


# ---------- gitlab ----------


@pytest.fixture
def gl_cfg(cfg: Config) -> Config:
    cfg.sources.gitlab.enabled = True
    return cfg


def gl_fake() -> FakeGitlab:
    return FakeGitlab(
        review=[
            gl_mr(1, updated_at="2026-09-16T09:00:00.000Z",
                  author={"name": "Ana Author", "username": "ana"},
                  reviewers=[{"username": "me", "name": "Test User"}]),
            gl_mr(2, updated_at="2026-09-15T09:00:00.000Z",
                  author={"name": "Ana Author", "username": "ana"},
                  assignees=[{"username": "dev", "name": "Dev Dan"}],
                  reviewers=[{"username": "me"}, {"username": "rex", "name": "Rex Review"}]),
        ],
        issues=[
            gl_issue(4, updated_at="2026-09-14T09:00:00.000Z",
                     author={"name": "Ana Author", "username": "ana"},
                     assignees=[{"username": "me", "name": "Test User"}]),
        ],
    )


async def collect_gitlab(gl_cfg: Config, vault: VaultStore) -> Any:
    return await make_gitlab(gl_cfg, vault, gl_fake()).collect(window(tz="UTC"))


async def test_gitlab_one_interaction_per_person_per_mr_or_issue(
    gl_cfg: Config, vault: VaultStore
) -> None:
    res = await collect_gitlab(gl_cfg, vault)
    mr1 = [i for i in res.interactions if i.ref == "mr:7:1"]
    assert [i.person_name for i in mr1] == ["Ana Author"]
    assert mr1[0].channel == "review" and mr1[0].subject == "!1"
    assert mr1[0].group is False and mr1[0].with_me is True
    assert mr1[0].person_email is None  # GitLab listings carry no email; none was invented
    assert mr1[0].at == datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    issue4 = [i for i in res.interactions if i.ref == "issue:7:4"]
    assert [i.person_name for i in issue4] == ["Ana Author"] and issue4[0].subject == "#4"


async def test_gitlab_never_records_me_and_flags_wide_reviews_as_group(
    gl_cfg: Config, vault: VaultStore
) -> None:
    res = await collect_gitlab(gl_cfg, vault)
    assert "me" not in {i.person_name for i in res.interactions}
    assert "Test User" not in {i.person_name for i in res.interactions}
    mr2 = [i for i in res.interactions if i.ref == "mr:7:2"]
    assert sorted(i.person_name or "" for i in mr2) == ["Ana Author", "Dev Dan", "Rex Review"]
    assert all(i.group is True for i in mr2)


async def test_gitlab_respects_the_project_filter(gl_cfg: Config, vault: VaultStore) -> None:
    gl_cfg.sources.gitlab.projects = ["grp/app"]
    fake = FakeGitlab(review=[gl_mr(1), gl_mr(2, project="other/repo")])
    res = await make_gitlab(gl_cfg, vault, fake).collect(window(tz="UTC"))
    assert {i.ref for i in res.interactions} == {"mr:7:1"}


async def test_gitlab_ref_is_stable_across_reads(gl_cfg: Config, vault: VaultStore) -> None:
    first = await collect_gitlab(gl_cfg, vault)
    second = await collect_gitlab(gl_cfg, vault)
    assert {i.key() for i in first.interactions} == {i.key() for i in second.interactions}
    assert "review:mr:7:1:ana author" in {i.key() for i in first.interactions}


async def test_gitlab_interaction_failure_still_returns_signals(
    gl_cfg: Config, vault: VaultStore
) -> None:
    src = make_gitlab(gl_cfg, vault, gl_fake())
    with patch.object(GitLabSource, "_interactions", side_effect=RuntimeError("boom")):
        res = await src.collect(window(tz="UTC"))
    assert res.interactions == []
    assert {s.source_id for s in res.signals} == {"mr:7:1", "mr:7:2"}


async def test_gitlab_malformed_item_costs_only_its_own_interaction(
    gl_cfg: Config, vault: VaultStore
) -> None:
    broken = SimpleNamespace(attributes={"iid": 99})  # no project_id
    fake = FakeGitlab(review=[broken, gl_mr(1)])
    res = await make_gitlab(gl_cfg, vault, fake).collect(window(tz="UTC"))
    assert {i.ref for i in res.interactions} == {"mr:7:1"}
    assert [s.source_id for s in res.signals] == ["mr:7:1"]


# ---------- the contract itself ----------


def test_key_dedupes_on_channel_ref_and_person() -> None:
    a = Interaction(person_email="Bob@Example.com", channel="email", ref="t1", subject="x")
    b = Interaction(person_email="bob@example.com", channel="email", ref="t1", subject="y")
    other_thread = Interaction(person_email="bob@example.com", channel="email", ref="t2")
    assert a.key() == b.key()
    assert a.key() != other_thread.key()


def test_key_falls_back_to_the_name_when_there_is_no_email() -> None:
    named = Interaction(person_name="Carol", channel="chat", ref="spaces/AAA")
    assert named.key() == "chat:spaces/AAA:carol"
