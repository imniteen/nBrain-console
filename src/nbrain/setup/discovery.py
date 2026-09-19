"""Discovery: infer aggressively so the wizard can ask sparingly. Every probe is optional and
failure-tolerant; results carry the evidence they came from."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from nbrain.config.schema import Config
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

AUTOMATION_RE = re.compile(r"no-?reply|notification|noreply|do-?not-?reply|mailer|alerts?@|bot@|jira|gitlab|github|calendar-notification|drive-shares", re.I)
MINE_RE = re.compile(r"gitlab|github|jira|pipeline|build|alert|monitor|pagerduty|sentry|grafana|datadog|ci@", re.I)


@dataclass
class PersonCandidate:
    name: str
    email: str | None
    evidence: list[str] = field(default_factory=list)
    score: int = 0
    one_to_one: bool = False
    external: bool = False


@dataclass
class MeetingCandidate:
    title: str
    cadence: str
    organiser_is_me: bool
    attendees: list[str]
    has_agenda: bool
    time: str


@dataclass
class DiscoveryResult:
    people: list[PersonCandidate] = field(default_factory=list)
    meetings: list[MeetingCandidate] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    noise_mine: list[str] = field(default_factory=list)
    noise_suppress: list[str] = field(default_factory=list)
    organised_meetings: int = 0
    reports_hint: int = 0
    notes: list[str] = field(default_factory=list)
    name_variants: list[str] = field(default_factory=list)

    def suggested_track(self) -> str:
        if self.reports_hint >= 2:
            return "manager"
        if self.organised_meetings >= 3:
            return "lead"
        return "ic"


def _domain(email: str | None) -> str:
    return (email or "").rsplit("@", 1)[-1].lower()


async def _google(cfg: Config, res: DiscoveryResult) -> None:
    from nbrain.sources.google.auth import GoogleAuth

    auth = GoogleAuth(cfg)
    me = cfg.user.email.lower()
    my_domain = _domain(me)
    people: dict[str, PersonCandidate] = {}

    def person(name: str | None, email: str | None, why: str, pts: int = 1) -> None:
        key = (email or name or "").lower()
        if not key or key == me:
            return
        p = people.setdefault(key, PersonCandidate(name=name or email or "?", email=email))
        if name and (p.name == p.email or not p.name):
            p.name = name
        p.score += pts
        if why not in p.evidence:
            p.evidence.append(why)
        p.external = bool(email) and _domain(email) != my_domain

    # calendar: 14 days back and forward, recurring events
    if cfg.sources.google.calendar:
        try:
            cal = auth.service("calendar", "v3")
            now = datetime.now(UTC)
            events = await asyncio.to_thread(
                lambda: cal.events().list(calendarId="primary", timeMin=(now - timedelta(days=14)).isoformat(), timeMax=(now + timedelta(days=14)).isoformat(), singleEvents=True, orderBy="startTime", maxResults=250).execute()
            )
            recurring: dict[str, list[dict[str, Any]]] = {}
            for ev in events.get("items", []):
                if ev.get("recurringEventId"):
                    recurring.setdefault(ev["recurringEventId"], []).append(ev)
                for a in ev.get("attendees", []) or []:
                    if a.get("self"):
                        continue
                    person(a.get("displayName"), a.get("email"), f"meeting: {ev.get('summary', '?')}")
            for evs in recurring.values():
                ev = evs[0]
                attendees = [a for a in ev.get("attendees", []) or [] if not a.get("self") and not a.get("resource")]
                organiser_me = (ev.get("organizer", {}).get("email", "").lower() == me)
                n = len(evs)
                cadence = "daily" if n >= 8 else "weekly" if n >= 3 else "biweekly"
                start = ev.get("start", {}).get("dateTime", "")
                t = start[11:16] if len(start) >= 16 else ""
                res.meetings.append(MeetingCandidate(ev.get("summary", "?"), cadence, organiser_me, [a.get("displayName") or a.get("email", "") for a in attendees], len(ev.get("description", "") or "") > 20, t))
                if organiser_me and attendees:
                    res.organised_meetings += 1
                if len(attendees) == 1:
                    a = attendees[0]
                    person(a.get("displayName"), a.get("email"), f"recurring 1:1 '{ev.get('summary')}'", 5)
                    people[(a.get("email") or a.get("displayName") or "").lower()].one_to_one = True
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"calendar discovery failed: {e}")

    # gmail: top human senders + automation over 30 days
    if cfg.sources.google.gmail:
        try:
            gm = auth.service("gmail", "v1")
            msgs = await asyncio.to_thread(lambda: gm.users().messages().list(userId="me", q="newer_than:30d -category:promotions", maxResults=200).execute())
            senders: Counter[str] = Counter()
            names: dict[str, str] = {}
            for m in msgs.get("messages", [])[:200]:
                try:
                    full = await asyncio.to_thread(lambda mid=m["id"]: gm.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["From"]).execute())
                except Exception:  # noqa: BLE001
                    continue
                frm = next((h["value"] for h in full.get("payload", {}).get("headers", []) if h["name"].lower() == "from"), "")
                mm = re.match(r"\s*\"?([^\"<]*)\"?\s*<?([^>]+@[^>]+)>?", frm)
                if not mm:
                    continue
                name, email = mm.group(1).strip(), mm.group(2).strip().lower()
                senders[email] += 1
                names[email] = name or email
            for email, n in senders.most_common(60):
                if AUTOMATION_RE.search(email) or AUTOMATION_RE.search(names[email]):
                    (res.noise_mine if MINE_RE.search(email + names[email]) else res.noise_suppress).append(email)
                elif email != me:
                    person(names[email], email, f"{n} emails in 30d", min(n, 5))
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"gmail discovery failed: {e}")

    res.people += sorted(people.values(), key=lambda p: -p.score)


async def _jira(cfg: Config, res: DiscoveryResult) -> None:
    import httpx

    from nbrain.config.loader import get_secret

    j = cfg.sources.jira
    token = get_secret(j.token_env)
    if not token:
        res.notes.append("jira: no token")
        return
    auth = (j.email, token) if j.email else None
    headers = {} if j.email else {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(base_url=j.url, auth=auth, headers=headers, timeout=30) as c:
            r = await c.get("/rest/api/3/search/jql", params={"jql": "assignee = currentUser() ORDER BY updated DESC", "maxResults": 50, "fields": "project"})
            if r.status_code in (404, 410):
                r = await c.get("/rest/api/2/search", params={"jql": "assignee = currentUser() ORDER BY updated DESC", "maxResults": 50, "fields": "project"})
            r.raise_for_status()
            keys = Counter(i["fields"]["project"]["key"] for i in r.json().get("issues", []))
            res.projects += [f"{k} (Jira, {n} issues)" for k, n in keys.most_common(5)]
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"jira discovery failed: {e}")


async def _gitlab(cfg: Config, res: DiscoveryResult) -> None:
    from nbrain.config.loader import get_secret

    g = cfg.sources.gitlab
    token = get_secret(g.token_env)
    if not token:
        res.notes.append("gitlab: no token")
        return
    try:
        import gitlab

        gl = gitlab.Gitlab(g.url, private_token=token)
        await asyncio.to_thread(gl.auth)
        me = gl.user.username if gl.user else g.username
        mrs = await asyncio.to_thread(lambda: gl.mergerequests.list(author_username=me, scope="all", per_page=50, get_all=False))
        paths = Counter(mr.references["full"].split("!")[0] for mr in mrs if getattr(mr, "references", None))
        res.projects += [f"{p} (GitLab, {n} MRs)" for p, n in paths.most_common(5)]
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"gitlab discovery failed: {e}")


async def discover(cfg: Config, store: VaultStore) -> DiscoveryResult:
    res = DiscoveryResult()
    probes = []
    if cfg.sources.google.enabled:
        probes.append(_google(cfg, res))
    if cfg.sources.jira.enabled:
        probes.append(_jira(cfg, res))
    if cfg.sources.gitlab.enabled:
        probes.append(_gitlab(cfg, res))
    if not probes:
        res.notes.append("no sources enabled; nothing to discover")
        return res
    await asyncio.gather(*probes, return_exceptions=True)
    # dedupe noise
    res.noise_mine = sorted(set(res.noise_mine))
    res.noise_suppress = sorted(set(res.noise_suppress) - set(res.noise_mine))
    return res
