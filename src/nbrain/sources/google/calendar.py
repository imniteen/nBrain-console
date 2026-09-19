"""Google Calendar source: today's and tomorrow's meetings, plus agenda nags for my own meetings."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from nbrain.config.schema import Config
from nbrain.sources.base import (
    BaseSource,
    CalendarEvent,
    CollectResult,
    ContextMessage,
    ItemContext,
    Signal,
    SourceStatus,
    VerifyResult,
    Window,
)
from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, http_status

# Shared with the other Google sub-sources so every Google failure is phrased the same way.
from nbrain.sources.google.gmail import why_unavailable
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

MAX_EVENTS = 100
MIN_AGENDA_CHARS = 20
CONTEXT_CHARS = 2000
CONTEXT_ATTENDEES = 15


def _parse_dt(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


class CalendarSource(BaseSource):
    name = "google-calendar"
    roles = {"calendar"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore, auth: GoogleAuth):
        self.cfg = cfg
        self.store = store
        self.auth = auth

    @property
    def _me(self) -> str:
        return self.cfg.user.email.lower()

    @property
    def _my_domain(self) -> str:
        return self._me.rsplit("@", 1)[-1] if "@" in self._me else ""

    async def healthcheck(self) -> SourceStatus:
        st = self.auth.status()
        return SourceStatus(self.name, st.ok, st.detail, st.checked_at, st.write_capable)

    # ---------- collect ----------

    def _list_events(self, svc: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        token: str | None = None
        while len(events) < MAX_EVENTS:
            resp = (
                svc.events()
                .list(
                    calendarId="primary",
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=min(100, MAX_EVENTS - len(events)),
                    pageToken=token,
                )
                .execute()
            )
            events += resp.get("items") or []
            token = resp.get("nextPageToken")
            if not token:
                break
        return events[:MAX_EVENTS]

    def _to_event(self, ev: dict[str, Any], tz: ZoneInfo) -> CalendarEvent | None:
        if ev.get("status") == "cancelled":
            return None
        start_raw = (ev.get("start") or {}).get("dateTime")
        end_raw = (ev.get("end") or {}).get("dateTime")
        if not start_raw or not end_raw:
            return None  # all-day event
        attendees_raw = [a for a in ev.get("attendees") or [] if not a.get("resource")]
        my_response: str | None = None
        for a in attendees_raw:
            if a.get("self") or str(a.get("email", "")).lower() == self._me:
                my_response = a.get("responseStatus")
        if my_response == "declined":
            return None
        organizer = ev.get("organizer") or {}
        org_email = str(organizer.get("email", "")).lower()
        organiser_is_me = bool(organizer.get("self")) or org_email == self._me
        names = [str(a.get("displayName") or a.get("email") or "") for a in attendees_raw]
        emails = [str(a.get("email", "")).lower() for a in attendees_raw if a.get("email")]
        declined = [
            str(a.get("displayName") or a.get("email") or "")
            for a in attendees_raw
            if a.get("responseStatus") == "declined"
        ]
        others = [e for e in emails if e != self._me]
        external = any(
            self._my_domain and e.rsplit("@", 1)[-1] != self._my_domain for e in others
        )
        description = str(ev.get("description") or "").strip()
        return CalendarEvent(
            source=self.name,
            source_id=str(ev.get("id", "")),
            title=str(ev.get("summary") or "(no title)"),
            start=_parse_dt(start_raw, tz),
            end=_parse_dt(end_raw, tz),
            url=ev.get("htmlLink"),
            organiser_is_me=organiser_is_me,
            organiser=organizer.get("displayName") or org_email or None,
            attendees=[n for n in names if n],
            attendee_emails=emails,
            declined=declined,
            has_agenda=len(description) > MIN_AGENDA_CHARS,
            external=external,
            my_response=my_response,
            is_focus_block=organiser_is_me and not others,
        )

    async def collect(self, window: Window) -> CollectResult:
        tz = ZoneInfo(window.tz or self.cfg.user.timezone or "UTC")
        start = datetime.combine(window.today, time.min, tzinfo=tz)
        end = start + timedelta(days=2)
        svc = self.auth.service("calendar", "v3")
        raw_events = await asyncio.to_thread(self._list_events, svc, start, end)
        result = CollectResult()
        for raw in raw_events:
            try:
                ev = self._to_event(raw, tz)
            except (ValueError, TypeError) as e:
                log.warning("google-calendar: skipping malformed event %s: %s", raw.get("id"), e)
                continue
            if ev is None:
                continue
            result.events.append(ev)
            if (
                ev.start.date() == window.today
                and ev.organiser_is_me
                and not ev.has_agenda
                and len(ev.attendees) > 1
            ):
                result.signals.append(
                    Signal(
                        type=ItemType.waiting_on_me,
                        title=f"Add an agenda: {ev.title}",
                        source=self.name,
                        source_id=ev.source_id,
                        url=ev.url,
                        due=window.today,
                        observed_at=ev.start,
                        priority=2,
                        meeting=ev.title,
                        cause="you organise this meeting today and it has no description",
                    )
                )
        return result

    # ---------- verify ----------

    def _get_event(self, svc: Any, event_id: str) -> dict[str, Any]:
        return svc.events().get(calendarId="primary", eventId=event_id).execute()

    async def verify(self, item: Item) -> VerifyResult:
        svc = self.auth.service("calendar", "v3")
        try:
            ev = await asyncio.to_thread(self._get_event, svc, item.source_id)
        except HttpError as e:
            if http_status(e) in (404, 410):
                return VerifyResult(state="resolved", note="event gone", url=item.url)
            log.warning("google-calendar: verify failed for %s: %s", item.source_id, e)
            return VerifyResult(state="unknown", note=f"calendar error {http_status(e)}")
        if ev.get("status") == "cancelled":
            return VerifyResult(state="resolved", note="event cancelled", url=ev.get("htmlLink"))
        description = str(ev.get("description") or "").strip()
        if item.type == ItemType.waiting_on_me and len(description) > MIN_AGENDA_CHARS:
            return VerifyResult(state="resolved", note="agenda added", url=ev.get("htmlLink"))
        return VerifyResult(state="open", url=ev.get("htmlLink") or item.url)

    # ---------- live context ----------

    async def fetch_context(self, item: Item) -> ItemContext:
        """An event has no conversation, so this is a 'document': who is coming and what is on it."""
        tz = ZoneInfo(self.cfg.user.timezone or "UTC")
        try:
            svc = self.auth.service("calendar", "v3")
            ev = await asyncio.to_thread(self._get_event, svc, item.source_id)
        except HttpError as e:
            return ItemContext.unavailable(why_unavailable(e, "Google Calendar"))
        except GoogleAuthError as e:
            return ItemContext.unavailable(f"Google is not authorised: {e}")
        except OSError as e:
            return ItemContext.unavailable(f"could not reach Google Calendar: {e}")
        if ev.get("status") == "cancelled":
            return ItemContext.unavailable("that calendar event has been cancelled")
        start_raw = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        if not start_raw:
            return ItemContext.unavailable("that calendar event has no start time to read")

        attendees = [a for a in ev.get("attendees") or [] if not a.get("resource")]
        by_status: dict[str, int] = {}
        participants: list[str] = []
        for a in attendees[:CONTEXT_ATTENDEES]:
            reply = str(a.get("responseStatus") or "needsAction")
            by_status[reply] = by_status.get(reply, 0) + 1
            participants.append(f"{a.get('displayName') or a.get('email') or '?'} ({reply})")
        organizer = ev.get("organizer") or {}
        organiser_is_me = bool(organizer.get("self")) or str(organizer.get("email", "")).lower() == self._me
        agenda = str(ev.get("description") or "").strip()
        mine = next((a for a in attendees if a.get("self") or str(a.get("email", "")).lower() == self._me), {})
        title = str(ev.get("summary") or "(no title)")
        when = _parse_dt(start_raw, tz).strftime("%a %d %b %H:%M") if "T" in start_raw else start_raw
        return ItemContext(
            kind="document",
            title=title,
            url=ev.get("htmlLink") or item.url,
            status=f"{when} · {len(attendees)} attendees · {'agenda attached' if len(agenda) > MIN_AGENDA_CHARS else 'no agenda'}",
            participants=participants,
            messages=(
                [ContextMessage(author=organizer.get("displayName") or organizer.get("email"), text=agenda[:CONTEXT_CHARS], kind="note")]
                if agenda
                else []
            ),
            facts={
                "when": when,
                "organiser": "you" if organiser_is_me else str(organizer.get("displayName") or organizer.get("email") or "unknown"),
                "attendees": str(len(attendees)),
                "responses": ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())) or "none recorded",
                "your response": str(mine.get("responseStatus") or "not invited"),
                "agenda": f"{len(agenda)} chars" if agenda else "none",
            },
            awaiting_me=organiser_is_me and len(agenda) <= MIN_AGENDA_CHARS,
            fingerprint=f"{ev.get('updated') or ev.get('etag') or ''}:{len(attendees)}:{len(agenda)}",
        )
