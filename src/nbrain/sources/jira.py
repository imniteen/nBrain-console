"""Jira source: my past-due / stale tickets and tickets I reported that others are sitting on.

Read-only: only GET requests against the REST search and issue endpoints. Works with Jira Cloud
(basic auth: email + API token, REST v3) and Data Center (bearer token when no email is set)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx
from dateutil import parser as dtparser

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

FIELDS = "summary,status,assignee,reporter,duedate,updated,priority,project,issuetype"
CONTEXT_FIELDS = (
    "summary,status,assignee,reporter,priority,duedate,updated,description,issuetype,created"
)
SEARCH_PATHS = ("/rest/api/3/search/jql", "/rest/api/3/search", "/rest/api/2/search")
NO_DUE_DATES = "no due dates on any open issue; staleness is the only proxy"
MAX_COMMENTS = 10
CONTEXT_CHARS = 2000
_BLANK_RUN = re.compile(r"\n{3,}")


class JiraAuthError(RuntimeError):
    pass


def _rich_text(value: Any) -> str:
    """Jira v2 descriptions/comments are plain strings; v3 returns Atlassian Document Format."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_rich_text(v) for v in value)
    if isinstance(value, dict):
        node = str(value.get("type") or "")
        if node == "hardBreak":
            return "\n"
        inner = str(value.get("text") or "") + _rich_text(value.get("content"))
        if node in ("paragraph", "heading", "listItem", "blockquote", "codeBlock", "rule"):
            return inner + "\n"
        if node == "mention":
            return str((value.get("attrs") or {}).get("text") or inner)
        return inner
    return ""


def _flatten(value: Any, limit: int = CONTEXT_CHARS) -> str:
    return _BLANK_RUN.sub("\n\n", _rich_text(value)).strip()[:limit]


def _age_words(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 0)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = dtparser.parse(value)
        except (ValueError, OverflowError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _user_key(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    return user.get("accountId") or user.get("key") or user.get("name") or user.get("emailAddress")


class JiraSource(BaseSource):
    name = "jira"
    roles = {"tickets"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore):
        self.cfg = cfg
        self.store = store
        self._api = "/rest/api/3"  # switched to v2 if only the v2 search endpoint answers

    # ---------- client ----------

    def _client(self) -> httpx.AsyncClient:
        jcfg = self.cfg.sources.jira
        if not jcfg.url:
            raise JiraAuthError("sources.jira.url is not set")
        token = get_secret(jcfg.token_env)
        if not token:
            raise JiraAuthError(
                f"no Jira token: set {jcfg.token_env} (env, nbrain/.env or `nbrain secret set`)"
            )
        if jcfg.email:
            return httpx.AsyncClient(base_url=jcfg.url, auth=(jcfg.email, token), timeout=30)
        return httpx.AsyncClient(
            base_url=jcfg.url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )

    async def _search(self, client: httpx.AsyncClient, jql: str, limit: int) -> list[dict[str, Any]]:
        params = {"jql": jql, "maxResults": limit, "fields": FIELDS}
        last: httpx.Response | None = None
        for path in SEARCH_PATHS:
            resp = await client.get(path, params=params)
            last = resp
            if resp.status_code in (404, 410):
                continue
            resp.raise_for_status()
            self._api = "/rest/api/2" if path.startswith("/rest/api/2") else "/rest/api/3"
            return list(resp.json().get("issues") or [])
        raise JiraAuthError(f"no Jira search endpoint answered (last status {last.status_code if last else '?'})")

    # ---------- protocol ----------

    async def healthcheck(self) -> SourceStatus:
        now = datetime.now(UTC)
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._api}/myself")
                if resp.status_code == 404 and self._api.endswith("/3"):
                    resp = await client.get("/rest/api/2/myself")
                resp.raise_for_status()
                who = resp.json().get("displayName") or resp.json().get("name") or "?"
        except JiraAuthError as e:
            return SourceStatus(self.name, False, detail=str(e), checked_at=now)
        except httpx.HTTPStatusError as e:
            return SourceStatus(
                self.name, False, detail=f"HTTP {e.response.status_code} from {e.request.url.path}", checked_at=now
            )
        except Exception as e:  # noqa: BLE001 - healthcheck must never raise
            return SourceStatus(self.name, False, detail=f"{type(e).__name__}: {e}", checked_at=now)
        detail = f"authenticated as {who}; Jira tokens carry the account's full permissions, nbrain only reads"
        return SourceStatus(self.name, True, detail=detail, checked_at=now, write_capable=True)

    async def collect(self, window: Window) -> CollectResult:
        jcfg = self.cfg.sources.jira
        result = CollectResult()
        async with self._client() as client:
            assigned = await self._search(client, jcfg.jql_assigned, jcfg.max_issues)
            reported = await self._search(client, jcfg.jql_reported, jcfg.max_issues)

        seen: set[str] = set()
        for issue in assigned:
            try:
                sig = self._assigned_signal(issue, window)
            except Exception as e:  # noqa: BLE001 - per-item errors must not abort the sweep
                log.warning("jira: skipping %s: %s", issue.get("key"), e)
                continue
            if sig:
                seen.add(sig.source_id)
                result.signals.append(sig)
        if assigned and not any((i.get("fields") or {}).get("duedate") for i in assigned):
            result.findings["jira"] = NO_DUE_DATES

        for issue in reported:
            if issue.get("key") in seen:
                continue
            try:
                sig = self._reported_signal(issue, window)
            except Exception as e:  # noqa: BLE001
                log.warning("jira: skipping %s: %s", issue.get("key"), e)
                continue
            if sig:
                result.signals.append(sig)
        return result

    async def verify(self, item: Item) -> VerifyResult:
        key = item.source_id
        async with self._client() as client:
            resp = await client.get(f"{self._api}/issue/{key}", params={"fields": "status,assignee,updated"})
        if resp.status_code == 404:
            return VerifyResult(state="resolved", note="issue gone")
        if resp.status_code >= 400:
            return VerifyResult(state="unknown", note=f"HTTP {resp.status_code}")
        fields = resp.json().get("fields") or {}
        url = self._url(key)
        category = ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
        if category == "done":
            return VerifyResult(state="resolved", note=(fields.get("status") or {}).get("name"), url=url)
        updated = _parse_dt(fields.get("updated"))
        note = None
        if updated and item.last_seen and updated.date() > item.last_seen:
            note = "updated since last run"
        return VerifyResult(state="open", note=note, url=url)

    # ---------- live context ----------

    def _is_me(self, user: dict[str, Any] | None) -> bool:
        if not user:
            return False
        email = str(user.get("emailAddress") or "").lower()
        me_email = (self.cfg.user.email or "").lower()
        if email and me_email:
            return email == me_email
        name = str(user.get("displayName") or user.get("name") or "").strip().lower()
        return bool(name) and name == (self.cfg.user.name or "").strip().lower()

    async def _comments(self, client: httpx.AsyncClient, key: str) -> tuple[list[dict[str, Any]], int]:
        """The most recent comments, oldest-first, plus the total the issue has."""
        resp = await client.get(
            f"{self._api}/issue/{key}/comment",
            params={"maxResults": MAX_COMMENTS, "orderBy": "-created"},
        )
        if resp.status_code >= 400:
            log.info("jira: comments unreadable for %s (HTTP %s)", key, resp.status_code)
            return [], 0
        body = resp.json()
        comments = list(body.get("comments") or [])
        total = int(body.get("total") or len(comments))
        newest = sorted(comments, key=lambda c: str(c.get("created") or ""))[-MAX_COMMENTS:]
        return newest, total

    async def fetch_context(self, item: Item) -> ItemContext:
        key = item.source_id
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._api}/issue/{key}", params={"fields": CONTEXT_FIELDS})
                if resp.status_code == 404:
                    return ItemContext.unavailable(f"Jira issue {key} no longer exists")
                if resp.status_code in (401, 403):
                    return ItemContext.unavailable(
                        f"Jira refused the read of {key} (HTTP {resp.status_code}): the token lacks permission or has expired"
                    )
                if resp.status_code >= 400:
                    return ItemContext.unavailable(f"Jira returned HTTP {resp.status_code} for {key}")
                fields = resp.json().get("fields") or {}
                comments, total = await self._comments(client, key)
        except JiraAuthError as e:
            return ItemContext.unavailable(str(e))
        except httpx.HTTPError as e:
            return ItemContext.unavailable(f"could not reach Jira: {type(e).__name__}: {e}")

        reporter = fields.get("reporter")
        messages: list[ContextMessage] = []
        description = _flatten(fields.get("description"))
        if description:
            messages.append(
                ContextMessage(
                    author=(reporter or {}).get("displayName"),
                    author_email=(reporter or {}).get("emailAddress"),
                    is_me=self._is_me(reporter),
                    at=_parse_dt(fields.get("created")),
                    text=description,
                    kind="note",
                )
            )
        for c in comments:
            author = c.get("author") or {}
            messages.append(
                ContextMessage(
                    author=author.get("displayName") or author.get("name"),
                    author_email=author.get("emailAddress"),
                    is_me=self._is_me(author),
                    at=_parse_dt(c.get("created")),
                    text=_flatten(c.get("body")),
                    kind="comment",
                )
            )
        return self._context(item, key, fields, messages, total)

    def _context(
        self,
        item: Item,
        key: str,
        fields: dict[str, Any],
        messages: list[ContextMessage],
        comment_total: int,
    ) -> ItemContext:
        assignee, reporter = fields.get("assignee"), fields.get("reporter")
        state = str((fields.get("status") or {}).get("name") or "unknown")
        updated = _parse_dt(fields.get("updated"))
        parts = [state]
        parts.append(
            f"assignee {assignee.get('displayName') or assignee.get('name')}" if assignee else "unassigned"
        )
        if updated:
            parts.append(f"updated {_age_words((datetime.now(UTC) - updated).total_seconds())}")
        comments = [m for m in messages if m.kind == "comment"]
        assignee_is_me = self._is_me(assignee)
        return ItemContext(
            kind="ticket",
            title=f"{key}: {fields.get('summary') or ''}".strip(),
            url=self._url(key),
            status=" · ".join(parts),
            participants=[
                str(u.get("displayName") or u.get("name"))
                for u in (reporter, assignee)
                if u and (u.get("displayName") or u.get("name"))
            ],
            messages=messages,
            facts={
                "key": key,
                "type": str((fields.get("issuetype") or {}).get("name") or "unknown"),
                "priority": str((fields.get("priority") or {}).get("name") or "none"),
                "due date": str(fields.get("duedate") or "none"),
                "reporter": str((reporter or {}).get("displayName") or (reporter or {}).get("name") or "unknown"),
                "comments": str(comment_total),
            },
            awaiting_me=assignee_is_me if assignee else (bool(comments) and not comments[-1].is_me),
            fingerprint=f"{fields.get('updated') or ''}:{comment_total}",
        )

    # ---------- rules ----------

    def _url(self, key: str) -> str:
        return f"{self.cfg.sources.jira.url.rstrip('/')}/browse/{key}"

    def _base(self, issue: dict[str, Any], window: Window) -> dict[str, Any]:
        f = issue.get("fields") or {}
        updated = _parse_dt(f.get("updated"))
        return {
            "key": issue["key"],
            "summary": f.get("summary") or "",
            "project": (f.get("project") or {}).get("key") or issue["key"].split("-")[0],
            "due": date.fromisoformat(f["duedate"]) if f.get("duedate") else None,
            "updated": updated,
            "untouched": (window.today - updated.date()).days if updated else 0,
            "assignee": f.get("assignee"),
            "reporter": f.get("reporter"),
        }

    def _assigned_signal(self, issue: dict[str, Any], window: Window) -> Signal | None:
        b = self._base(issue, window)
        if b["due"] and b["due"] < window.today:
            cause, priority = f"past due since {b['due'].isoformat()}", 1
        elif b["untouched"] > window.ticket_stale_days:
            cause, priority = f"untouched {b['untouched']}d", 2
        else:
            return None
        return Signal(
            type=ItemType.slipping,
            title=f"{b['key']}: {b['summary']}",
            source=self.name,
            source_id=b["key"],
            url=self._url(b["key"]),
            project_hint=f"{b['project']} {b['summary']}",
            due=b["due"],
            observed_at=b["updated"],
            cause=cause,
            evidence=f"status {((issue.get('fields') or {}).get('status') or {}).get('name', '?')}",
            priority=priority,
        )

    def _reported_signal(self, issue: dict[str, Any], window: Window) -> Signal | None:
        b = self._base(issue, window)
        assignee = b["assignee"]
        if not assignee or _user_key(assignee) == _user_key(b["reporter"]):
            return None
        if b["untouched"] <= window.ticket_stale_days:
            return None
        person = assignee.get("displayName") or assignee.get("name")
        return Signal(
            type=ItemType.waiting_on_them,
            title=f"{b['key']}: {b['summary']}",
            source=self.name,
            source_id=b["key"],
            url=self._url(b["key"]),
            person=person,
            person_email=assignee.get("emailAddress"),
            project_hint=f"{b['project']} {b['summary']}",
            due=b["due"],
            observed_at=b["updated"],
            cause=f"untouched {b['untouched']}d",
            evidence=f"assigned to {person}, reported by you",
            priority=3,
        )
