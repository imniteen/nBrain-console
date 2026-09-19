"""Gmail source: metadata-only thread scan for replies I owe and replies I am waiting for.

The sweep only uses `users().threads().list/get(format="metadata")`. Bodies are fetched in one
place and one place only — `fetch_context`, when the user opens an item and asks for help with
it. Nothing here is ever modified: there is no send, draft, label or trash call in this module."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from nbrain.config.schema import Config
from nbrain.sources.base import (
    BaseSource,
    CollectResult,
    ContextMessage,
    ItemContext,
    Signal,
    SourceStatus,
    SourceText,
    VerifyResult,
    Window,
)
from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, http_status
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date"]
MAX_TEXTS = 25
THREAD_URL = "https://mail.google.com/mail/u/0/#inbox/{tid}"
CONTEXT_MESSAGES = 8  # newest-biased cap: one huge thread must not stall the drawer
CONTEXT_BODY_CHARS = 2000

_TAGS = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</tr>")
_SCRIPTY = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_QUOTE_HEADER = re.compile(r"^\s*(On .{5,120}\bwrote:|-{2,}\s*Original Message|_{5,})", re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass
class _Thread:
    id: str
    subject: str
    snippet: str
    last_at: datetime
    last_from_name: str
    last_from_email: str
    inbound: bool
    participants: list[str] = field(default_factory=list)
    recipient_emails: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return THREAD_URL.format(tid=self.id)

    def age_hours(self, now: datetime) -> float:
        return (now - self.last_at).total_seconds() / 3600


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        name = str(h.get("name", "")).lower()
        if name and name not in out:
            out[name] = str(h.get("value", ""))
    return out


def _addresses(value: str) -> list[tuple[str, str]]:
    """'A <a@x>, b@y' -> [('A', 'a@x'), ('b', 'b@y')] with lower-cased emails."""
    out: list[tuple[str, str]] = []
    for name, email in getaddresses([value or ""]):
        email = email.strip().lower()
        if not email:
            continue
        out.append((name.strip().strip('"') or email.split("@")[0], email))
    return out


def _message_time(msg: dict[str, Any], headers: dict[str, str], tz: ZoneInfo) -> datetime:
    raw = msg.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).astimezone(tz)
        except (TypeError, ValueError):
            pass
    if headers.get("date"):
        try:
            dt = parsedate_to_datetime(headers["date"])
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(tz)
        except (TypeError, ValueError):
            pass
    return datetime.now(tz)


def _matches(email: str, patterns: list[str]) -> bool:
    e = email.lower()
    return any(p and p.lower() in e for p in patterns)


def _age_words(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 0)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def why_unavailable(err: BaseException, api: str) -> str:
    """Plain-words reason a live read failed, for ItemContext.note."""
    status = http_status(err)
    if status in (404, 410):
        return f"that {api} item no longer exists"
    if status == 403:
        return f"{api} refused the read (403) - the Google token is missing the read-only scope for it"
    if status == 401:
        return "the Google token has expired or been revoked; run `nbrain auth google`"
    if status is not None:
        return f"{api} returned HTTP {status}"
    return f"could not reach {api}: {type(err).__name__}: {err}"


def _decode(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", "replace")


def _strip_html(html: str) -> str:
    text = _BLOCK_END.sub("\n", _SCRIPTY.sub(" ", html))
    return _BLANK_RUN.sub("\n\n", unescape(_TAGS.sub("", text))).strip()


def _drop_quotes(text: str) -> str:
    """Trim the quoted reply chain so the model reads this message, not the whole history."""
    kept: list[str] = []
    for line in text.split("\n"):
        if _QUOTE_HEADER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    trimmed = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    return trimmed or text.strip()


def message_body(payload: dict[str, Any]) -> str:
    """text/plain from anywhere in the MIME tree, else text/html with the tags stripped."""
    plain: list[str] = []
    html: list[str] = []
    queue: list[dict[str, Any]] = [payload]
    while queue:
        part = queue.pop(0)
        mime = str(part.get("mimeType") or "")
        data = str((part.get("body") or {}).get("data") or "")
        if data and mime.startswith("text/plain"):
            plain.append(_decode(data))
        elif data and mime.startswith("text/html"):
            html.append(_decode(data))
        queue += [p for p in part.get("parts") or [] if isinstance(p, dict)]
    body = "\n".join(p for p in plain if p).strip()
    if not body:
        body = _strip_html("\n".join(html))
    return _drop_quotes(body)


def summarise_thread(thread: dict[str, Any], me: str, tz: ZoneInfo) -> _Thread | None:
    """Reduce a metadata-format thread to the few facts the rules need."""
    messages = thread.get("messages") or []
    if not messages:
        return None
    me = me.lower()
    last = messages[-1]
    lh = _headers(last)
    froms = _addresses(lh.get("from", ""))
    from_name, from_email = froms[0] if froms else ("", "")
    seen: dict[str, str] = {}
    for m in messages:
        h = _headers(m)
        for key in ("from", "to", "cc"):
            for name, email in _addresses(h.get(key, "")):
                seen.setdefault(email, name)
    subject = lh.get("subject") or _headers(messages[0]).get("subject") or "(no subject)"
    return _Thread(
        id=str(thread.get("id", "")),
        subject=subject,
        snippet=str(last.get("snippet") or thread.get("snippet") or "").strip(),
        last_at=_message_time(last, lh, tz),
        last_from_name=from_name,
        last_from_email=from_email,
        inbound=from_email != me,
        participants=[f"{n} <{e}>" if n != e.split("@")[0] else e for e, n in seen.items()],
        recipient_emails=[e for _, e in _addresses(lh.get("to", "")) if e != me],
    )


class GmailSource(BaseSource):
    name = "gmail"
    roles = {"email"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore, auth: GoogleAuth):
        self.cfg = cfg
        self.store = store
        self.auth = auth

    @property
    def _me(self) -> str:
        return self.cfg.user.email.lower()

    async def healthcheck(self) -> SourceStatus:
        st = self.auth.status()
        return SourceStatus(self.name, st.ok, st.detail, st.checked_at, st.write_capable)

    # ---------- collect ----------

    def _list_thread_ids(self, svc: Any, query: str, limit: int) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        while len(ids) < limit:
            req = svc.users().threads().list(
                userId="me", q=query, maxResults=min(100, limit - len(ids)), pageToken=token
            )
            resp = req.execute()
            ids += [str(t["id"]) for t in resp.get("threads") or [] if t.get("id")]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def _get_thread(self, svc: Any, tid: str) -> dict[str, Any]:
        return (
            svc.users()
            .threads()
            .get(userId="me", id=tid, format="metadata", metadataHeaders=METADATA_HEADERS)
            .execute()
        )

    async def collect(self, window: Window) -> CollectResult:
        tz = ZoneInfo(window.tz or self.cfg.user.timezone or "UTC")
        now = datetime.now(tz)
        g = self.cfg.sources.google
        svc = self.auth.service("gmail", "v1")
        query = f"newer_than:{window.email_lookback_days}d -category:promotions"
        thread_ids = await asyncio.to_thread(self._list_thread_ids, svc, query, g.max_threads)
        result = CollectResult()
        if len(thread_ids) >= g.max_threads:
            result.notes.append(f"gmail: scan capped at {g.max_threads} threads")

        automation = 0
        failures = 0
        for tid in thread_ids:
            try:
                raw = await asyncio.to_thread(self._get_thread, svc, tid)
            except HttpError as e:
                failures += 1
                log.warning("gmail: could not fetch thread %s: %s", tid, e)
                continue
            th = summarise_thread(raw, self._me, tz)
            if th is None:
                continue
            if _matches(th.last_from_email, self.cfg.noise.suppress):
                continue
            if _matches(th.last_from_email, self.cfg.noise.phishing):
                result.signals.append(self._phishing_signal(th))
                continue
            if _matches(th.last_from_email, self.cfg.noise.mine):
                automation += 1
                continue
            self._classify(th, now, window, result)

        if automation:
            result.findings["gmail"] = (
                f"{automation} automated thread(s) from noise.mine senders seen; not classified"
            )
        if failures:
            result.notes.append(f"gmail: {failures} thread(s) could not be fetched")
        return result

    def _phishing_signal(self, th: _Thread) -> Signal:
        return Signal(
            type=ItemType.watch,
            title=f"Phishing-shaped: {th.subject}",
            source=self.name,
            source_id=th.id,
            url=th.url,
            person=th.last_from_name or None,
            person_email=th.last_from_email or None,
            observed_at=th.last_at,
            evidence=th.snippet or None,
            priority=3,
        )

    def _classify(self, th: _Thread, now: datetime, window: Window, result: CollectResult) -> None:
        old = th.age_hours(now) >= window.awaiting_reply_hours
        if th.inbound:
            person = self.store.person_by_email(th.last_from_email)
            if old and person is not None and person.tier in (1, 2):
                result.signals.append(
                    Signal(
                        type=ItemType.waiting_on_me,
                        title=f"Reply to {person.title}: {th.subject}",
                        source=self.name,
                        source_id=th.id,
                        url=th.url,
                        person=person.title,
                        person_email=th.last_from_email,
                        observed_at=th.last_at,
                        priority=1 if person.tier == 1 else 2,
                        evidence=th.snippet or None,
                    )
                )
                return
            if len(result.texts) < MAX_TEXTS:
                result.texts.append(
                    SourceText(
                        source=self.name,
                        source_id=th.id,
                        url=th.url,
                        kind="email",
                        title=th.subject,
                        author=person.title if person else th.last_from_name,
                        author_email=th.last_from_email,
                        author_is_me=False,
                        participants=th.participants,
                        observed_at=th.last_at,
                        text=f"{th.subject}\n{th.snippet}",
                    )
                )
            return
        if old and "?" in th.snippet:
            other_email = th.recipient_emails[0] if th.recipient_emails else None
            other = self.store.person_by_email(other_email)
            other_name = other.title if other else (other_email or "them")
            result.signals.append(
                Signal(
                    type=ItemType.waiting_on_them,
                    title=f"Waiting on {other_name}: {th.subject}",
                    source=self.name,
                    source_id=th.id,
                    url=th.url,
                    person=other_name if other_email else None,
                    person_email=other_email,
                    observed_at=th.last_at,
                    priority=3,
                    evidence=th.snippet or None,
                )
            )

    # ---------- verify ----------

    async def verify(self, item: Item) -> VerifyResult:
        svc = self.auth.service("gmail", "v1")
        try:
            raw = await asyncio.to_thread(self._get_thread, svc, item.source_id)
        except HttpError as e:
            if http_status(e) == 404:
                return VerifyResult(state="resolved", note="thread gone", url=item.url)
            log.warning("gmail: verify failed for %s: %s", item.source_id, e)
            return VerifyResult(state="unknown", note=f"gmail error {http_status(e)}")
        th = summarise_thread(raw, self._me, ZoneInfo(self.cfg.user.timezone or "UTC"))
        if th is None:
            return VerifyResult(state="resolved", note="thread empty", url=item.url)
        if not th.inbound:
            return VerifyResult(state="resolved", note="you replied", url=th.url)
        return VerifyResult(state="open", url=th.url)

    # ---------- live context ----------

    def _get_full_thread(self, svc: Any, tid: str) -> dict[str, Any]:
        """The only place Gmail message bodies are read, and still a pure GET."""
        return svc.users().threads().get(userId="me", id=tid, format="full").execute()

    def _context_message(self, msg: dict[str, Any], tz: ZoneInfo) -> ContextMessage:
        headers = _headers(msg)
        froms = _addresses(headers.get("from", ""))
        name, email = froms[0] if froms else ("", "")
        body = message_body(msg.get("payload") or {}) or str(msg.get("snippet") or "")
        return ContextMessage(
            author=name or email or None,
            author_email=email or None,
            is_me=bool(email) and email == self._me,
            at=_message_time(msg, headers, tz),
            text=body[:CONTEXT_BODY_CHARS],
            kind="message",
        )

    async def fetch_context(self, item: Item) -> ItemContext:
        tz = ZoneInfo(self.cfg.user.timezone or "UTC")
        try:
            svc = self.auth.service("gmail", "v1")
            raw = await asyncio.to_thread(self._get_full_thread, svc, item.source_id)
        except HttpError as e:
            return ItemContext.unavailable(why_unavailable(e, "Gmail"))
        except GoogleAuthError as e:
            return ItemContext.unavailable(f"Google is not authorised: {e}")
        except OSError as e:  # network / DNS / TLS
            return ItemContext.unavailable(f"could not reach Gmail: {e}")
        raw_messages = raw.get("messages") or []
        if not raw_messages:
            return ItemContext.unavailable("that Gmail thread has no readable messages")

        participants: dict[str, str] = {}
        for m in raw_messages:
            h = _headers(m)
            for key in ("from", "to", "cc"):
                for name, email in _addresses(h.get(key, "")):
                    participants.setdefault(email, name)
        messages = [self._context_message(m, tz) for m in raw_messages[-CONTEXT_MESSAGES:]]
        subject = (
            _headers(raw_messages[0]).get("subject")
            or _headers(raw_messages[-1]).get("subject")
            or "(no subject)"
        )
        now = datetime.now(tz)
        last = messages[-1]
        facts = {"subject": subject, "messages": str(len(raw_messages))}
        inbound = next((m for m in reversed(messages) if not m.is_me), None)
        if inbound is not None and inbound.at is not None:
            facts["last inbound"] = f"{inbound.author or 'them'}, {_age_words((now - inbound.at).total_seconds())}"
        if len(raw_messages) > CONTEXT_MESSAGES:
            facts["shown"] = f"last {CONTEXT_MESSAGES} of {len(raw_messages)} messages"
        stance = "awaiting your reply" if not last.is_me else "you replied last"
        age = _age_words((now - last.at).total_seconds()) if last.at else "unknown age"
        return ItemContext(
            kind="thread",
            title=subject,
            url=item.url or THREAD_URL.format(tid=str(raw.get("id") or item.source_id)),
            status=f"{stance} · {len(raw_messages)} messages · last {age}",
            participants=[
                f"{n} <{e}>" if n and n != e.split("@")[0] else e for e, n in participants.items()
            ],
            messages=messages,
            facts=facts,
            awaiting_me=not last.is_me,
            fingerprint=f"{raw_messages[-1].get('id') or ''}:{len(raw_messages)}",
        )
