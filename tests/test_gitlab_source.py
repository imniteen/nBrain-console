from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from gitlab.exceptions import GitlabGetError

from nbrain.config.schema import Config
from nbrain.sources.base import Window
from nbrain.sources.gitlab import GitLabSource
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

TODAY = date(2026, 9, 17)


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
    return (TODAY - timedelta(days=n)).isoformat() + "T09:00:00.000Z"


def obj(**attrs: Any) -> SimpleNamespace:
    return SimpleNamespace(attributes=attrs)


def mr(iid: int, project: str = "grp/app", **extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        iid=iid,
        project_id=7,
        title=f"MR {iid}",
        web_url=f"https://gitlab.example/{project}/-/merge_requests/{iid}",
        author={"name": "Ana Author", "username": "ana"},
        created_at=days_ago(5),
        references={"full": f"{project}!{iid}"},
        head_pipeline=None,
        blocking_discussions_resolved=True,
        upvotes=0,
        state="opened",
        reviewers=[{"username": "me"}],
    )
    base.update(extra)
    return obj(**base)


def issue(iid: int, project: str = "grp/app", **extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        iid=iid,
        project_id=7,
        title=f"Issue {iid}",
        web_url=f"https://gitlab.example/{project}/-/issues/{iid}",
        references={"full": f"{project}#{iid}"},
        due_date=None,
        updated_at=days_ago(1),
        state="opened",
    )
    base.update(extra)
    return obj(**base)


class FakeGitlab:
    """Just enough of python-gitlab's surface for the source."""

    def __init__(self, review=(), mine=(), issues=(), approved=(), fetched=None, missing=False):
        self.user = obj(username="me")
        self._review, self._mine, self._issues = list(review), list(mine), list(issues)
        self._approved = list(approved)
        self._fetched, self._missing = fetched, missing
        self.mergerequests = SimpleNamespace(list=self._list_mrs)
        self.issues = SimpleNamespace(list=lambda **kw: self._issues)
        self.projects = SimpleNamespace(get=self._project)
        self.calls: list[dict[str, Any]] = []

    def auth(self) -> None:
        pass

    def _list_mrs(self, **kw: Any) -> list[SimpleNamespace]:
        self.calls.append(kw)
        return self._review if "reviewer_username" in kw else self._mine

    def _project(self, pid: Any, lazy: bool = False) -> SimpleNamespace:
        approvals = obj(approved_by=[{"user": {"username": u}} for u in self._approved])
        lazy_mr = SimpleNamespace(approvals=SimpleNamespace(get=lambda: approvals))

        def get(iid: Any, lazy: bool = False) -> Any:
            if lazy:
                return lazy_mr
            if self._missing:
                raise GitlabGetError("404 Not Found", response_code=404)
            return self._fetched

        return SimpleNamespace(mergerequests=SimpleNamespace(get=get), issues=SimpleNamespace(get=get))


@pytest.fixture
def gl_cfg(cfg: Config) -> Config:
    cfg.sources.gitlab.enabled = True
    return cfg


def make_source(cfg: Config, vault: VaultStore, fake: FakeGitlab) -> GitLabSource:
    src = GitLabSource(cfg, vault)
    src._gl = fake  # type: ignore[assignment]
    return src


async def test_healthcheck_without_token_is_not_ok(gl_cfg: Config, vault: VaultStore) -> None:
    with patch("nbrain.sources.gitlab.get_secret", return_value=None):
        status = await GitLabSource(gl_cfg, vault).healthcheck()
    assert status.ok is False
    assert "GITLAB_TOKEN" in status.detail


async def test_healthcheck_reports_user_and_scope_caveat(gl_cfg: Config, vault: VaultStore) -> None:
    src = make_source(gl_cfg, vault, FakeGitlab())
    status = await src.healthcheck()
    assert status.ok and "me" in status.detail and "read_api" in status.detail
    assert status.write_capable is True


async def test_collect_without_token_raises(gl_cfg: Config, vault: VaultStore) -> None:
    with patch("nbrain.sources.gitlab.get_secret", return_value=None), pytest.raises(RuntimeError):
        await GitLabSource(gl_cfg, vault).collect(window())


async def test_review_requests_become_waiting_on_me(gl_cfg: Config, vault: VaultStore) -> None:
    fake = FakeGitlab(review=[mr(1, created_at=days_ago(5)), mr(2, created_at=days_ago(1))])
    res = await make_source(gl_cfg, vault, fake).collect(window())
    by_id = {s.source_id: s for s in res.signals}
    assert by_id["mr:7:1"].type == ItemType.waiting_on_me
    assert by_id["mr:7:1"].priority == 1 and by_id["mr:7:2"].priority == 2
    assert by_id["mr:7:1"].person == "Ana Author"
    assert by_id["mr:7:1"].evidence == "opened 5d ago by Ana Author"
    assert by_id["mr:7:1"].project_hint == "grp/app"
    assert fake.calls[0]["reviewer_username"] == "me" and fake.calls[0]["get_all"] is False


async def test_own_mrs_slipping_causes(gl_cfg: Config, vault: VaultStore) -> None:
    mine = [
        mr(10, created_at=days_ago(6), head_pipeline={"status": "failed"}),
        mr(11, created_at=days_ago(6), blocking_discussions_resolved=False),
        mr(12, created_at=days_ago(6)),  # no approvals -> no review yet
        mr(13, created_at=days_ago(1)),  # too young
    ]
    res = await make_source(gl_cfg, vault, FakeGitlab(mine=mine)).collect(window())
    causes = {s.source_id: s.cause for s in res.signals}
    assert causes == {"mr:7:10": "failing pipeline", "mr:7:11": "unresolved discussions", "mr:7:12": "no review yet"}
    assert all(s.type == ItemType.slipping for s in res.signals)
    assert {s.title for s in res.signals} >= {"MR !10 open 6d: MR 10"}


async def test_approved_mr_is_not_slipping(gl_cfg: Config, vault: VaultStore) -> None:
    fake = FakeGitlab(mine=[mr(12, created_at=days_ago(6))], approved=["bob"])
    res = await make_source(gl_cfg, vault, fake).collect(window())
    assert res.signals == []


async def test_issue_past_due_and_stale_rules(gl_cfg: Config, vault: VaultStore) -> None:
    issues = [
        issue(1, due_date=(TODAY - timedelta(days=2)).isoformat(), updated_at=days_ago(0)),
        issue(2, updated_at=days_ago(8)),
        issue(3, updated_at=days_ago(2)),
        issue(4, due_date=(TODAY + timedelta(days=2)).isoformat(), updated_at=days_ago(1)),
    ]
    res = await make_source(gl_cfg, vault, FakeGitlab(issues=issues)).collect(window())
    by_id = {s.source_id: s for s in res.signals}
    assert set(by_id) == {"issue:7:1", "issue:7:2"}
    assert by_id["issue:7:1"].cause == "past due" and by_id["issue:7:1"].priority == 1
    assert by_id["issue:7:1"].due == TODAY - timedelta(days=2)
    assert by_id["issue:7:2"].cause == "untouched 8d" and by_id["issue:7:2"].priority == 2


async def test_project_filter_applies_to_all_lists(gl_cfg: Config, vault: VaultStore) -> None:
    gl_cfg.sources.gitlab.projects = ["grp/app"]
    fake = FakeGitlab(
        review=[mr(1), mr(2, project="other/repo")],
        mine=[mr(3, created_at=days_ago(9), head_pipeline={"status": "failed"}, project="other/repo")],
        issues=[issue(4, updated_at=days_ago(20)), issue(5, updated_at=days_ago(20), project="other/repo")],
    )
    res = await make_source(gl_cfg, vault, fake).collect(window())
    assert {s.source_id for s in res.signals} == {"mr:7:1", "issue:7:4"}
    assert any("configured project" in n for n in res.notes)


async def test_per_item_error_does_not_abort(gl_cfg: Config, vault: VaultStore) -> None:
    broken = obj(iid=99)  # missing project_id etc.
    res = await make_source(gl_cfg, vault, FakeGitlab(review=[broken, mr(1)])).collect(window())
    assert [s.source_id for s in res.signals] == ["mr:7:1"]


async def test_verify_paths(gl_cfg: Config, vault: VaultStore) -> None:
    item = Item(title="x", type=ItemType.waiting_on_me, source="gitlab", source_id="mr:7:1")

    merged = FakeGitlab(fetched=mr(1, state="merged"))
    assert (await make_source(gl_cfg, vault, merged).verify(item)).state == "resolved"

    missing = FakeGitlab(missing=True)
    res = await make_source(gl_cfg, vault, missing).verify(item)
    assert res.state == "resolved" and res.note == "not found"

    reassigned = FakeGitlab(fetched=mr(1, reviewers=[{"username": "bob"}]))
    res = await make_source(gl_cfg, vault, reassigned).verify(item)
    assert res.state == "resolved" and res.note == "no longer a reviewer"

    approved = FakeGitlab(fetched=mr(1), approved=["me"])
    res = await make_source(gl_cfg, vault, approved).verify(item)
    assert res.state == "resolved" and res.note == "you approved"

    still_open = FakeGitlab(fetched=mr(1))
    assert (await make_source(gl_cfg, vault, still_open).verify(item)).state == "open"

    bad = Item(title="x", source="gitlab", source_id="nonsense")
    assert (await make_source(gl_cfg, vault, still_open).verify(bad)).state == "unknown"


async def test_client_is_built_with_token(gl_cfg: Config, vault: VaultStore) -> None:
    with (
        patch("nbrain.sources.gitlab.get_secret", return_value="glpat-x"),
        patch("nbrain.sources.gitlab.gitlab.Gitlab") as ctor,
    ):
        ctor.return_value = MagicMock(user=obj(username="me"))
        status = await GitLabSource(gl_cfg, vault).healthcheck()
    ctor.assert_called_once_with("https://gitlab.com", private_token="glpat-x", timeout=30)
    assert status.ok
