"""GitLab source: MRs awaiting my review, my MRs that are slipping, my stale or overdue issues.

Read-only: only ``list``/``get`` calls are made. Every rule here is deterministic, so this source
emits Signals only (no SourceTexts for the LLM)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any

import gitlab
from gitlab.exceptions import GitlabError, GitlabGetError

from nbrain.config.loader import get_secret
from nbrain.config.schema import Config
from nbrain.sources.base import (
    BaseSource,
    CollectResult,
    ContextMessage,
    ItemContext,
    Signal,
    SourceStatus,
    VerifyResult,
    Window,
)
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

PAGE = 50
MAX_NOTES = 10
NOTE_SCAN = 50  # notes read before the newest-first slice, so one loud MR cannot stall the UI
CONTEXT_CHARS = 2000


class GitLabAuthError(RuntimeError):
    pass


def _attrs(obj: Any) -> dict[str, Any]:
    """python-gitlab RESTObjects expose ``.attributes``; plain dicts pass through."""
    if isinstance(obj, dict):
        return obj
    return dict(getattr(obj, "attributes", None) or vars(obj))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_days(value: str | None, today: date) -> int:
    dt = _dt(value)
    return (today - dt.date()).days if dt else 0


def _age_words(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 0)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _names(entries: Any) -> list[str]:
    return [
        str(e.get("name") or e.get("username"))
        for e in entries or []
        if isinstance(e, dict) and (e.get("name") or e.get("username"))
    ]


def _project_path(a: dict[str, Any]) -> str | None:
    """`references.full` looks like ``group/proj!12`` (MR) or ``group/proj#7`` (issue)."""
    full = (a.get("references") or {}).get("full") or ""
    for sep in ("!", "#"):
        if sep in full:
            return full.split(sep, 1)[0]
    return None


class GitLabSource(BaseSource):
    name = "gitlab"
    roles = {"code"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore):
        self.cfg = cfg
        self.store = store
        self._gl: gitlab.Gitlab | None = None
        self._me: str | None = cfg.sources.gitlab.username or None

    # ---------- client ----------

    def _client(self) -> gitlab.Gitlab:
        if self._gl is None:
            gcfg = self.cfg.sources.gitlab
            token = get_secret(gcfg.token_env)
            if not token:
                raise GitLabAuthError(
                    f"no GitLab token: set {gcfg.token_env} (env, nbrain/.env or `nbrain secret set`)"
                )
            self._gl = gitlab.Gitlab(gcfg.url, private_token=token, timeout=30)
        return self._gl

    async def _username(self) -> str:
        if self._me:
            return self._me
        gl = self._client()
        await asyncio.to_thread(gl.auth)
        user = gl.user
        if user is None:
            raise GitLabAuthError("GitLab token did not resolve to a user")
        self._me = str(_attrs(user).get("username"))
        return self._me

    def _projects_filter(self) -> set[str]:
        return set(self.cfg.sources.gitlab.projects)

    def _in_scope(self, a: dict[str, Any]) -> bool:
        wanted = self._projects_filter()
        if not wanted:
            return True
        path = _project_path(a)
        return path in wanted if path else False

    # ---------- protocol ----------

    async def healthcheck(self) -> SourceStatus:
        now = datetime.now(UTC)
        try:
            me = await self._username()
        except (GitLabAuthError, GitlabError, OSError) as e:
            return SourceStatus(self.name, False, detail=str(e), checked_at=now)
        except Exception as e:  # noqa: BLE001 - healthcheck must never raise
            return SourceStatus(self.name, False, detail=f"{type(e).__name__}: {e}", checked_at=now)
        detail = (
            f"authenticated as {me}; token scope unknown (GitLab does not expose it) - "
            "create a read_api token so the token cannot write"
        )
        return SourceStatus(self.name, True, detail=detail, checked_at=now, write_capable=True)

    async def collect(self, window: Window) -> CollectResult:
        me = await self._username()
        gl = self._client()
        result = CollectResult()

        review = await asyncio.to_thread(
            gl.mergerequests.list,
            reviewer_username=me,
            state="opened",
            scope="all",
            per_page=PAGE,
            get_all=False,
        )
        for mr in list(review)[:PAGE]:
            try:
                sig = self._review_signal(_attrs(mr), window)
                if sig:
                    result.signals.append(sig)
            except Exception as e:  # noqa: BLE001 - per-item errors must not abort the sweep
                log.warning("gitlab: skipping review MR: %s", e)

        mine = await asyncio.to_thread(
            gl.mergerequests.list,
            author_username=me,
            state="opened",
            scope="all",
            per_page=PAGE,
            get_all=False,
        )
        for mr in list(mine)[:PAGE]:
            try:
                sig = await self._slipping_mr_signal(_attrs(mr), window)
                if sig:
                    result.signals.append(sig)
            except Exception as e:  # noqa: BLE001
                log.warning("gitlab: skipping own MR: %s", e)

        issues = await asyncio.to_thread(
            gl.issues.list,
            assignee_username=me,
            state="opened",
            scope="all",
            per_page=PAGE,
            get_all=False,
        )
        for issue in list(issues)[:PAGE]:
            try:
                sig = self._issue_signal(_attrs(issue), window)
                if sig:
                    result.signals.append(sig)
            except Exception as e:  # noqa: BLE001
                log.warning("gitlab: skipping issue: %s", e)

        if self._projects_filter():
            result.notes.append(
                f"gitlab: limited to {len(self._projects_filter())} configured project(s)"
            )
        return result

    async def verify(self, item: Item) -> VerifyResult:
        try:
            kind, pid, iid = item.source_id.split(":", 2)
        except ValueError:
            return VerifyResult(state="unknown", note=f"unrecognised source_id {item.source_id!r}")
        gl = self._client()
        project = gl.projects.get(pid, lazy=True)
        manager = project.mergerequests if kind == "mr" else project.issues
        try:
            obj = await asyncio.to_thread(manager.get, iid)
        except GitlabGetError as e:
            if e.response_code == 404:
                return VerifyResult(state="resolved", note="not found")
            return VerifyResult(state="unknown", note=str(e))
        a = _attrs(obj)
        state = a.get("state")
        url = a.get("web_url")
        if state in ("merged", "closed"):
            return VerifyResult(state="resolved", note=state, url=url)
        if kind == "mr" and item.type == ItemType.waiting_on_me:
            me = await self._username()
            reviewers = {r.get("username") for r in a.get("reviewers") or []}
            if reviewers and me not in reviewers:
                return VerifyResult(state="resolved", note="no longer a reviewer", url=url)
            approved_by = await self._approved_by(obj)
            if approved_by is not None and me in approved_by:
                return VerifyResult(state="resolved", note="you approved", url=url)
        return VerifyResult(state="open", note=state, url=url)

    # ---------- live context ----------

    async def _notes(self, obj: Any) -> list[dict[str, Any]]:
        """Human notes on an MR or issue, newest-first cap applied, oldest-first out."""
        try:
            raw = await asyncio.to_thread(
                obj.notes.list, per_page=NOTE_SCAN, get_all=False, sort="desc", order_by="created_at"
            )
        except Exception as e:  # noqa: BLE001 - notes are best effort; the rest of the context still helps
            log.info("gitlab: notes unavailable: %s", e)
            return []
        return [_attrs(n) for n in list(raw)[:NOTE_SCAN]]

    def _note_messages(self, notes: list[dict[str, Any]], me: str) -> list[ContextMessage]:
        human = [n for n in notes if not n.get("system")]
        human.sort(key=lambda n: str(n.get("created_at") or ""))
        return [
            ContextMessage(
                author=(n.get("author") or {}).get("name") or (n.get("author") or {}).get("username"),
                is_me=(n.get("author") or {}).get("username") == me,
                at=_dt(n.get("created_at")),
                text=str(n.get("body") or "")[:CONTEXT_CHARS],
                kind="comment",
            )
            for n in human[-MAX_NOTES:]
        ]

    async def fetch_context(self, item: Item) -> ItemContext:
        try:
            kind, pid, iid = item.source_id.split(":", 2)
        except ValueError:
            return ItemContext.unavailable(f"unrecognised GitLab source_id {item.source_id!r}")
        try:
            me = await self._username()
            gl = self._client()
            project = gl.projects.get(pid, lazy=True)
            manager = project.mergerequests if kind == "mr" else project.issues
            obj = await asyncio.to_thread(manager.get, iid)
        except GitlabGetError as e:
            if e.response_code == 404:
                return ItemContext.unavailable(
                    f"that GitLab {'merge request' if kind == 'mr' else 'issue'} no longer exists"
                )
            if e.response_code in (401, 403):
                return ItemContext.unavailable(
                    f"GitLab refused the read (HTTP {e.response_code}): the token lacks read_api on this project"
                )
            return ItemContext.unavailable(f"GitLab error: {e}")
        except GitLabAuthError as e:
            return ItemContext.unavailable(str(e))
        except (GitlabError, OSError) as e:
            return ItemContext.unavailable(f"could not reach GitLab: {type(e).__name__}: {e}")

        a = _attrs(obj)
        notes = await self._notes(obj)
        messages = self._note_messages(notes, me)
        description = str(a.get("description") or "").strip()
        if description:
            author = (a.get("author") or {}).get("name") or (a.get("author") or {}).get("username")
            messages.insert(
                0,
                ContextMessage(
                    author=author,
                    is_me=(a.get("author") or {}).get("username") == me,
                    at=_dt(a.get("created_at")),
                    text=description[:CONTEXT_CHARS],
                    kind="note",
                ),
            )
        if kind == "mr":
            return await self._mr_context(item, a, obj, notes, messages, me)
        return self._issue_context(item, a, notes, messages, me)

    async def _mr_context(
        self,
        item: Item,
        a: dict[str, Any],
        obj: Any,
        notes: list[dict[str, Any]],
        messages: list[ContextMessage],
        me: str,
    ) -> ItemContext:
        pipeline = str((a.get("head_pipeline") or {}).get("status") or "")
        approved = await self._approved_by(obj)
        unresolved = sum(1 for n in notes if n.get("resolvable") and not n.get("resolved"))
        draft = bool(a.get("draft") or a.get("work_in_progress"))
        reviewers = {r.get("username") for r in a.get("reviewers") or []}
        parts = [str(a.get("state") or "unknown")]
        if draft:
            parts.append("draft")
        if pipeline:
            parts.append(f"pipeline {pipeline}")
        parts.append(f"{len(approved)} approvals" if approved is not None else "approvals unreadable")
        if unresolved:
            parts.append(f"{unresolved} unresolved threads")
        elif a.get("blocking_discussions_resolved") is False:
            parts.append("unresolved discussions")
        updated = _dt(a.get("updated_at"))
        return ItemContext(
            kind="review",
            title=f"MR !{a.get('iid')}: {a.get('title') or ''}".strip(),
            url=a.get("web_url") or item.url,
            status=" · ".join(parts),
            participants=sorted(
                {*_names([a.get("author")]), *_names(a.get("assignees")), *_names(a.get("reviewers"))}
            ),
            messages=messages,
            facts={
                "source branch": str(a.get("source_branch") or "unknown"),
                "target branch": str(a.get("target_branch") or "unknown"),
                "author": ", ".join(_names([a.get("author")])) or "unknown",
                "assignees": ", ".join(_names(a.get("assignees"))) or "none",
                "reviewers": ", ".join(_names(a.get("reviewers"))) or "none",
                "changes": str(a.get("changes_count") or "unknown"),
                "updated": _age_words((datetime.now(UTC) - updated).total_seconds()) if updated else "unknown",
            },
            awaiting_me=(me in reviewers and (approved is None or me not in approved))
            if reviewers
            else (bool(messages) and not messages[-1].is_me),
            fingerprint=f"{a.get('updated_at') or ''}:{len(notes)}:{pipeline}",
        )

    def _issue_context(
        self,
        item: Item,
        a: dict[str, Any],
        notes: list[dict[str, Any]],
        messages: list[ContextMessage],
        me: str,
    ) -> ItemContext:
        assignees = _names(a.get("assignees")) or _names([a.get("assignee")])
        updated = _dt(a.get("updated_at"))
        parts = [str(a.get("state") or "unknown")]
        parts.append(f"assigned to {', '.join(assignees)}" if assignees else "unassigned")
        if updated:
            parts.append(f"updated {_age_words((datetime.now(UTC) - updated).total_seconds())}")
        usernames = {
            u.get("username") for u in [*(a.get("assignees") or []), a.get("assignee")] if isinstance(u, dict)
        }
        return ItemContext(
            kind="review",
            title=f"Issue #{a.get('iid')}: {a.get('title') or ''}".strip(),
            url=a.get("web_url") or item.url,
            status=" · ".join(parts),
            participants=sorted({*_names([a.get("author")]), *assignees}),
            messages=messages,
            facts={
                "author": ", ".join(_names([a.get("author")])) or "unknown",
                "assignees": ", ".join(assignees) or "none",
                "labels": ", ".join(str(label) for label in a.get("labels") or []) or "none",
                "due date": str(a.get("due_date") or "none"),
                "comments": str(sum(1 for n in notes if not n.get("system"))),
            },
            awaiting_me=me in usernames if usernames else (bool(messages) and not messages[-1].is_me),
            fingerprint=f"{a.get('updated_at') or ''}:{len(notes)}:{a.get('state') or ''}",
        )

    # ---------- rules ----------

    def _review_signal(self, a: dict[str, Any], window: Window) -> Signal | None:
        if not self._in_scope(a):
            return None
        author = (a.get("author") or {}).get("name") or (a.get("author") or {}).get("username")
        age = _age_days(a.get("created_at"), window.today)
        return Signal(
            type=ItemType.waiting_on_me,
            title=f"Review MR !{a['iid']}: {a.get('title', '')}",
            source=self.name,
            source_id=f"mr:{a['project_id']}:{a['iid']}",
            url=a.get("web_url"),
            person=author,
            observed_at=_dt(a.get("created_at")),
            priority=1 if age > window.review_age_days else 2,
            project_hint=_project_path(a),
            evidence=f"opened {age}d ago by {author}",
        )

    async def _slipping_mr_signal(self, a: dict[str, Any], window: Window) -> Signal | None:
        if not self._in_scope(a):
            return None
        age = _age_days(a.get("created_at"), window.today)
        if age <= window.review_age_days:
            return None
        cause: str | None = None
        if (a.get("head_pipeline") or {}).get("status") == "failed":
            cause = "failing pipeline"
        elif a.get("blocking_discussions_resolved") is False:
            cause = "unresolved discussions"
        else:
            approved_by = await self._approved_by(a)
            if approved_by is not None and not approved_by:
                cause = "no review yet"
            elif approved_by is None and not a.get("upvotes"):
                cause = "no review yet"
        if cause is None:
            return None
        return Signal(
            type=ItemType.slipping,
            title=f"MR !{a['iid']} open {age}d: {a.get('title', '')}",
            source=self.name,
            source_id=f"mr:{a['project_id']}:{a['iid']}",
            url=a.get("web_url"),
            observed_at=_dt(a.get("created_at")),
            priority=1 if cause == "failing pipeline" else 2,
            project_hint=_project_path(a),
            cause=cause,
            evidence=f"opened {age}d ago; {cause}",
        )

    def _issue_signal(self, a: dict[str, Any], window: Window) -> Signal | None:
        if not self._in_scope(a):
            return None
        due = date.fromisoformat(a["due_date"]) if a.get("due_date") else None
        untouched = _age_days(a.get("updated_at"), window.today)
        if due and due < window.today:
            cause, priority = "past due", 1
        elif untouched > window.ticket_stale_days:
            cause, priority = f"untouched {untouched}d", 2
        else:
            return None
        return Signal(
            type=ItemType.slipping,
            title=f"Issue #{a['iid']}: {a.get('title', '')}",
            source=self.name,
            source_id=f"issue:{a['project_id']}:{a['iid']}",
            url=a.get("web_url"),
            due=due,
            observed_at=_dt(a.get("updated_at")),
            priority=priority,
            project_hint=_project_path(a),
            cause=cause,
            evidence=f"due {due}" if due else f"last updated {untouched}d ago",
        )

    async def _approved_by(self, mr: Any) -> set[str] | None:
        """Usernames who approved, or None when approvals cannot be read (per-item, best effort)."""
        a = _attrs(mr)
        try:
            gl = self._client()
            lazy = gl.projects.get(a["project_id"], lazy=True).mergerequests.get(a["iid"], lazy=True)
            approvals = await asyncio.to_thread(lazy.approvals.get)
        except Exception as e:  # noqa: BLE001
            log.debug("gitlab: approvals unavailable for !%s: %s", a.get("iid"), e)
            return None
        return {
            (entry.get("user") or {}).get("username")
            for entry in _attrs(approvals).get("approved_by") or []
        }
