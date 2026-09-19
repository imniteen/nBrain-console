from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from nbrain.config.schema import Config
from nbrain.sources.base import Window
from nbrain.sources.jira import NO_DUE_DATES, JiraSource
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

TODAY = date(2026, 9, 17)
BASE = "https://jira.example.com"
ME = {"accountId": "acc-me", "displayName": "Test User", "emailAddress": "test@example.com"}
BOB = {"accountId": "acc-bob", "displayName": "Bob Builder", "emailAddress": "bob@example.com"}


def window(**kw: Any) -> Window:
    base = dict(
        today=TODAY,
        email_lookback_days=7,
        commitment_lookback_days=30,
        awaiting_reply_hours=48,
        ticket_stale_days=5,
        review_age_days=3,
        tz="UTC",
    )
    base.update(kw)
    return Window(**base)


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat() + "T10:00:00.000+0000"


def issue(key: str, *, updated_days: int = 0, due: date | None = None, assignee=ME, reporter=ME) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"Summary {key}",
            "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            "assignee": assignee,
            "reporter": reporter,
            "duedate": due.isoformat() if due else None,
            "updated": days_ago(updated_days),
            "project": {"key": key.split("-")[0]},
        },
    }


@pytest.fixture
def jira_cfg(cfg: Config) -> Config:
    cfg.sources.jira.enabled = True
    cfg.sources.jira.url = BASE
    cfg.sources.jira.email = "test@example.com"
    return cfg


@pytest.fixture(autouse=True)
def token():
    with patch("nbrain.sources.jira.get_secret", return_value="tok") as p:
        yield p


def mock_search(assigned: list[dict[str, Any]], reported: list[dict[str, Any]]) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        jql = request.url.params["jql"]
        assert request.url.params["fields"].startswith("summary,status")
        return httpx.Response(200, json={"issues": assigned if "assignee" in jql else reported})

    respx.get(f"{BASE}/rest/api/3/search/jql").mock(side_effect=responder)


async def test_healthcheck_without_token(jira_cfg: Config, vault: VaultStore, token) -> None:
    token.return_value = None
    status = await JiraSource(jira_cfg, vault).healthcheck()
    assert status.ok is False and "JIRA_TOKEN" in status.detail


@respx.mock
async def test_healthcheck_ok_uses_basic_auth(jira_cfg: Config, vault: VaultStore) -> None:
    route = respx.get(f"{BASE}/rest/api/3/myself").mock(return_value=httpx.Response(200, json={"displayName": "Test User"}))
    status = await JiraSource(jira_cfg, vault).healthcheck()
    assert status.ok and "Test User" in status.detail and status.write_capable
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_healthcheck_http_error_is_not_ok(jira_cfg: Config, vault: VaultStore) -> None:
    respx.get(f"{BASE}/rest/api/3/myself").mock(return_value=httpx.Response(401))
    status = await JiraSource(jira_cfg, vault).healthcheck()
    assert status.ok is False and "401" in status.detail


@respx.mock
async def test_bearer_auth_when_no_email(jira_cfg: Config, vault: VaultStore) -> None:
    jira_cfg.sources.jira.email = ""
    route = respx.get(f"{BASE}/rest/api/3/myself").mock(return_value=httpx.Response(200, json={"name": "me"}))
    await JiraSource(jira_cfg, vault).healthcheck()
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_past_due_and_stale_rules(jira_cfg: Config, vault: VaultStore) -> None:
    mock_search(
        [
            issue("APP-1", due=TODAY - timedelta(days=3), updated_days=0),
            issue("APP-2", updated_days=9),
            issue("APP-3", updated_days=2),
            issue("APP-4", due=TODAY + timedelta(days=1), updated_days=9),  # due future, but stale
        ],
        [],
    )
    res = await JiraSource(jira_cfg, vault).collect(window())
    by = {s.source_id: s for s in res.signals}
    assert set(by) == {"APP-1", "APP-2", "APP-4"}
    assert by["APP-1"].cause == f"past due since {(TODAY - timedelta(days=3)).isoformat()}"
    assert by["APP-1"].priority == 1 and by["APP-1"].type == ItemType.slipping
    assert by["APP-1"].due == TODAY - timedelta(days=3)
    assert by["APP-2"].cause == "untouched 9d" and by["APP-2"].priority == 2
    assert by["APP-2"].url == f"{BASE}/browse/APP-2"
    assert by["APP-2"].project_hint == "APP Summary APP-2"
    assert "jira" not in res.findings  # APP-1 has a due date


@respx.mock
async def test_no_due_dates_finding(jira_cfg: Config, vault: VaultStore) -> None:
    mock_search([issue("APP-1", updated_days=1), issue("APP-2", updated_days=2)], [])
    res = await JiraSource(jira_cfg, vault).collect(window())
    assert res.findings["jira"] == NO_DUE_DATES
    assert res.signals == []


@respx.mock
async def test_reported_issues_waiting_on_them(jira_cfg: Config, vault: VaultStore) -> None:
    mock_search(
        [issue("APP-9", updated_days=8)],  # also in reported; must not duplicate
        [
            issue("APP-9", updated_days=8),
            issue("APP-10", updated_days=8, assignee=BOB),
            issue("APP-11", updated_days=1, assignee=BOB),  # fresh
            issue("APP-12", updated_days=8, assignee=None),  # unassigned
        ],
    )
    res = await JiraSource(jira_cfg, vault).collect(window())
    by = {s.source_id: s for s in res.signals}
    assert set(by) == {"APP-9", "APP-10"}
    assert by["APP-10"].type == ItemType.waiting_on_them
    assert by["APP-10"].person == "Bob Builder" and by["APP-10"].priority == 3
    assert by["APP-10"].cause == "untouched 8d"


@respx.mock
async def test_fallback_to_legacy_search(jira_cfg: Config, vault: VaultStore) -> None:
    respx.get(f"{BASE}/rest/api/3/search/jql").mock(return_value=httpx.Response(410))
    legacy = respx.get(f"{BASE}/rest/api/3/search").mock(
        return_value=httpx.Response(200, json={"issues": [issue("APP-1", updated_days=9)]})
    )
    res = await JiraSource(jira_cfg, vault).collect(window())
    assert legacy.called and len(res.signals) == 1


@respx.mock
async def test_auth_failure_raises_from_collect(jira_cfg: Config, vault: VaultStore) -> None:
    respx.get(f"{BASE}/rest/api/3/search/jql").mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await JiraSource(jira_cfg, vault).collect(window())


@respx.mock
async def test_verify_paths(jira_cfg: Config, vault: VaultStore) -> None:
    src = JiraSource(jira_cfg, vault)
    item = Item(title="x", type=ItemType.slipping, source="jira", source_id="APP-1", last_seen=TODAY - timedelta(days=2))
    url = f"{BASE}/rest/api/3/issue/APP-1"

    respx.get(url).mock(return_value=httpx.Response(200, json={"fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}}))
    res = await src.verify(item)
    assert res.state == "resolved" and res.note == "Done" and res.url == f"{BASE}/browse/APP-1"

    respx.get(url).mock(return_value=httpx.Response(404))
    res = await src.verify(item)
    assert res.state == "resolved" and res.note == "issue gone"

    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={"fields": {"status": {"statusCategory": {"key": "indeterminate"}}, "updated": days_ago(0)}},
        )
    )
    res = await src.verify(item)
    assert res.state == "open" and res.note == "updated since last run"

    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={"fields": {"status": {"statusCategory": {"key": "indeterminate"}}, "updated": days_ago(4)}},
        )
    )
    res = await src.verify(item)
    assert res.state == "open" and res.note is None

    respx.get(url).mock(return_value=httpx.Response(500))
    assert (await src.verify(item)).state == "unknown"
