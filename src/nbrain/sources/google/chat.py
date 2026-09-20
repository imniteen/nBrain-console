"""Google Chat source: what I said (commitments) and what was said to me (replies I owe).

Identifying "me": the Chat API has no `self` marker on messages. We resolve my user id from the
first message whose `sender.email` equals cfg.user.email (Workspace populates `email` for
same-domain human senders). If no such message is seen, we fall back to a HUMAN sender whose
displayName equals cfg.user.name — which mis-attributes if a colleague shares your name.
Read-only: only spaces().list and spaces().messages().list/get are called."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from nbrain.config.schema import Config
from nbrain.sources.base import (
    BaseSource,
    CollectResult,
    ContextMessage,
    Interaction,
    ItemContext,
    Signal,
    SourceStatus,
    SourceText,
    VerifyResult,
    Window,
)
from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, http_status

# _age_words/why_unavailable live in gmail.py so the four Google sub-sources phrase Google
# failures and ages identically; neither touches Gmail state.
from nbrain.sources.google.gmail import _age_words, why_unavailable
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

MAX_SPACES = 30
PER_SPACE_PAGE = 25
CONTEXT_MESSAGES = 15
CONTEXT_CHARS = 2000
CONTEXT_LOOKBACK_DAYS = 14


@dataclass
class _Msg:
    name: str  # spaces/AAA/messages/BBB
    space: str  # spaces/AAA
    space_title: str
    space_type: str  # SPACE | GROUP_CHAT | DIRECT_MESSAGE
    thread: str
    sender_id: str
    sender_name: str
    sender_email: str
    sender_human: bool
    created: datetime
    text: str
    mentioned_ids: list[str]

    @property
    def url(self) -> str:
        parts = self.name.split("/")
        if len(parts) == 4:
            return f"https://chat.google.com/room/{parts[1]}/{parts[3]}"
        return f"https://chat.google.com/room/{self.space.split('/')[-1]}"


def _parse_time(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(tz)


def _to_msg(raw: dict[str, Any], space: dict[str, Any], tz: ZoneInfo) -> _Msg | None:
    if not raw.get("name") or not raw.get("createTime"):
        return None
    sender = raw.get("sender") or {}
    mentions = [
        str(((a.get("userMention") or {}).get("user") or {}).get("name", ""))
        for a in raw.get("annotations") or []
        if a.get("type") == "USER_MENTION"
    ]
    return _Msg(
        name=str(raw["name"]),
        space=str(space.get("name", "")),
        space_title=str(space.get("displayName") or space.get("name", "")),
        space_type=str(space.get("spaceType") or space.get("type") or ""),
        thread=str((raw.get("thread") or {}).get("name") or ""),
        sender_id=str(sender.get("name", "")),
        sender_name=str(sender.get("displayName") or ""),
        sender_email=str(sender.get("email") or "").lower(),
        sender_human=str(sender.get("type", "HUMAN")).upper() == "HUMAN",
        created=_parse_time(str(raw["createTime"]), tz),
        text=str(raw.get("text") or raw.get("formattedText") or "").strip(),
        mentioned_ids=[m for m in mentions if m],
    )


class ChatSource(BaseSource):
    name = "google-chat"
    roles = {"chat"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore, auth: GoogleAuth):
        self.cfg = cfg
        self.store = store
        self.auth = auth
        self._my_user_id: str | None = None

    async def healthcheck(self) -> SourceStatus:
        st = self.auth.status()
        return SourceStatus(self.name, st.ok, st.detail, st.checked_at, st.write_capable)

    # ---------- identity ----------

    def _is_me(self, m: _Msg) -> bool:
        me_email = self.cfg.user.email.lower()
        if m.sender_email and me_email:
            return m.sender_email == me_email
        if self._my_user_id and m.sender_id:
            return m.sender_id == self._my_user_id
        name = self.cfg.user.name.strip().lower()
        return bool(name) and m.sender_human and m.sender_name.strip().lower() == name

    def _resolve_me(self, msgs: list[_Msg]) -> None:
        me_email = self.cfg.user.email.lower()
        for m in msgs:
            if me_email and m.sender_email == me_email and m.sender_id:
                self._my_user_id = m.sender_id
                return
        name = self.cfg.user.name.strip().lower()
        for m in msgs:
            if name and m.sender_human and m.sender_name.strip().lower() == name and m.sender_id:
                self._my_user_id = m.sender_id
                log.info("google-chat: identified self by display name only (limitation)")
                return

    def _mentions_me(self, m: _Msg) -> bool:
        if self._my_user_id and self._my_user_id in m.mentioned_ids:
            return True
        first = (self.cfg.user.first_name or self.cfg.user.name.split(" ")[0]).strip().lower()
        return bool(first) and f"@{first}" in m.text.lower()

    # ---------- API ----------

    def _list_spaces(self, svc: Any) -> list[dict[str, Any]]:
        spaces: list[dict[str, Any]] = []
        token: str | None = None
        while len(spaces) < MAX_SPACES:
            resp = svc.spaces().list(pageSize=100, pageToken=token).execute()
            spaces += resp.get("spaces") or []
            token = resp.get("nextPageToken")
            if not token:
                break
        return spaces[:MAX_SPACES]

    def _list_messages(self, svc: Any, space: str, since_iso: str, limit: int) -> list[dict[str, Any]]:
        resp = (
            svc.spaces()
            .messages()
            .list(
                parent=space,
                filter=f'createTime > "{since_iso}"',
                orderBy="createTime desc",
                pageSize=min(PER_SPACE_PAGE, limit),
            )
            .execute()
        )
        return list(resp.get("messages") or [])[:limit]

    # ---------- collect ----------

    async def collect(self, window: Window) -> CollectResult:
        tz = ZoneInfo(window.tz or self.cfg.user.timezone or "UTC")
        now = datetime.now(tz)
        since = now - timedelta(days=window.commitment_lookback_days)
        since_iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        cap = self.cfg.sources.google.max_chat_messages
        svc = self.auth.service("chat", "v1")
        spaces = await asyncio.to_thread(self._list_spaces, svc)
        result = CollectResult()

        msgs: list[_Msg] = []
        failures = 0
        for space in spaces:
            remaining = cap - len(msgs)
            if remaining <= 0:
                result.notes.append(f"google-chat: scan capped at {cap} messages")
                break
            try:
                raws = await asyncio.to_thread(
                    self._list_messages, svc, str(space["name"]), since_iso, remaining
                )
            except HttpError as e:
                failures += 1
                log.warning("google-chat: could not list %s: %s", space.get("name"), e)
                continue
            msgs += [m for m in (_to_msg(r, space, tz) for r in raws) if m is not None]
        if failures:
            result.notes.append(f"google-chat: {failures} space(s) could not be read")

        self._resolve_me(msgs)
        result.interactions += self._interactions(msgs)
        for m in msgs:
            if self._is_me(m):
                if m.text:
                    result.texts.append(self._text(m, author_is_me=True))
                continue
            if not m.sender_human or not m.text:
                continue
            direct = m.space_type == "DIRECT_MESSAGE"
            if not (self._mentions_me(m) or direct):
                continue
            if self._replied_after(m, msgs):
                continue
            result.texts.append(self._text(m, author_is_me=False))
            old = (now - m.created).total_seconds() / 3600 >= window.awaiting_reply_hours
            if old and (self._mentions_me(m) or (direct and "?" in m.text)):
                result.signals.append(self._waiting_signal(m))
        return result

    # ---------- interactions ----------

    def _interactions(self, msgs: list[_Msg]) -> list[Interaction]:
        """One per human per space, from the messages this sweep already read."""
        by_space: dict[str, list[_Msg]] = {}
        for m in msgs:
            if m.space:
                by_space.setdefault(m.space, []).append(m)
        out: list[Interaction] = []
        for space, in_space in by_space.items():
            try:
                out += self._space_interactions(space, in_space)
            except Exception as e:  # noqa: BLE001 - relationship data never blocks collection
                log.warning("google-chat: no interactions for %s: %s", space, e)
        return out

    def _space_interactions(self, space: str, msgs: list[_Msg]) -> list[Interaction]:
        latest = max(msgs, key=lambda m: m.created)
        speakers: dict[str, _Msg] = {}
        for m in msgs:
            if not m.sender_human or self._is_me(m):
                continue
            who = m.sender_email or m.sender_id or m.sender_name
            if who:
                speakers.setdefault(who, m)
        return [
            Interaction(
                person_email=m.sender_email or None,
                person_name=m.sender_name or None,
                channel="chat",
                at=latest.created,
                ref=space,
                group=latest.space_type != "DIRECT_MESSAGE",
                with_me=True,
                inbound=not self._is_me(latest),
                subject=latest.space_title or space,
            )
            for m in speakers.values()
        ]

    def _replied_after(self, m: _Msg, msgs: list[_Msg]) -> bool:
        for other in msgs:
            if other.space != m.space or other.created <= m.created or not self._is_me(other):
                continue
            if not m.thread or not other.thread or other.thread == m.thread:
                return True
        return False

    def _text(self, m: _Msg, *, author_is_me: bool) -> SourceText:
        person = self.store.person_by_email(m.sender_email) if m.sender_email else None
        return SourceText(
            source=self.name,
            source_id=m.name,
            url=m.url,
            kind="chat",
            title=m.space_title,
            author=self.cfg.user.name if author_is_me else (person.title if person else m.sender_name),
            author_email=self.cfg.user.email if author_is_me else (m.sender_email or None),
            author_is_me=author_is_me,
            participants=[m.space_title],
            observed_at=m.created,
            text=m.text,
        )

    def _waiting_signal(self, m: _Msg) -> Signal:
        person = self.store.person_by_email(m.sender_email) if m.sender_email else None
        who = person.title if person else (m.sender_name or "someone")
        where = m.space_title if m.space_type != "DIRECT_MESSAGE" else "DM"
        snippet = m.text if len(m.text) <= 80 else m.text[:77] + "..."
        return Signal(
            type=ItemType.waiting_on_me,
            title=f"Reply to {who} in {where}: {snippet}",
            source=self.name,
            source_id=m.name,
            url=m.url,
            person=who if (person or m.sender_name) else None,
            person_email=m.sender_email or None,
            observed_at=m.created,
            priority=2,
            evidence=m.text,
        )

    # ---------- verify ----------

    async def verify(self, item: Item) -> VerifyResult:
        tz = ZoneInfo(self.cfg.user.timezone or "UTC")
        svc = self.auth.service("chat", "v1")
        try:
            raw = await asyncio.to_thread(
                lambda: svc.spaces().messages().get(name=item.source_id).execute()
            )
        except HttpError as e:
            if http_status(e) == 404:
                return VerifyResult(state="resolved", note="message gone", url=item.url)
            log.warning("google-chat: verify failed for %s: %s", item.source_id, e)
            return VerifyResult(state="unknown", note=f"chat error {http_status(e)}")
        space_name = "/".join(item.source_id.split("/")[:2])
        space = raw.get("space") or {"name": space_name}
        target = _to_msg(raw, space, tz)
        if target is None:
            return VerifyResult(state="unknown", note="message unreadable")
        since_iso = target.created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            raws = await asyncio.to_thread(
                self._list_messages, svc, space_name, since_iso, PER_SPACE_PAGE
            )
        except HttpError as e:
            return VerifyResult(state="unknown", note=f"chat error {http_status(e)}")
        later = [m for m in (_to_msg(r, space, tz) for r in raws) if m is not None]
        if not self._my_user_id:
            self._resolve_me(later + [target])
        if self._replied_after(target, later):
            return VerifyResult(state="resolved", note="you replied since", url=target.url)
        return VerifyResult(state="open", url=target.url)

    # ---------- live context ----------

    def _get_space(self, svc: Any, name: str) -> dict[str, Any]:
        return svc.spaces().get(name=name).execute()

    async def _surrounding(self, svc: Any, target: _Msg, space: dict[str, Any], tz: ZoneInfo) -> list[_Msg]:
        """The target plus what was said around it in the space, oldest first."""
        since = target.created - timedelta(days=CONTEXT_LOOKBACK_DAYS)
        since_iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            raws = await asyncio.to_thread(
                self._list_messages, svc, target.space, since_iso, CONTEXT_MESSAGES
            )
        except HttpError as e:
            log.warning("google-chat: could not list %s for context: %s", target.space, e)
            raws = []
        msgs = {m.name: m for m in (_to_msg(r, space, tz) for r in raws) if m is not None}
        msgs[target.name] = target
        return sorted(msgs.values(), key=lambda m: m.created)[-CONTEXT_MESSAGES:]

    async def fetch_context(self, item: Item) -> ItemContext:
        tz = ZoneInfo(self.cfg.user.timezone or "UTC")
        space_name = "/".join(item.source_id.split("/")[:2])
        try:
            svc = self.auth.service("chat", "v1")
            raw = await asyncio.to_thread(
                lambda: svc.spaces().messages().get(name=item.source_id).execute()
            )
        except HttpError as e:
            return ItemContext.unavailable(why_unavailable(e, "Google Chat"))
        except GoogleAuthError as e:
            return ItemContext.unavailable(f"Google is not authorised: {e}")
        except OSError as e:
            return ItemContext.unavailable(f"could not reach Google Chat: {e}")
        space = raw.get("space") or {}
        if not space.get("displayName"):
            try:
                space = await asyncio.to_thread(self._get_space, svc, space_name)
            except (HttpError, OSError) as e:
                log.info("google-chat: space %s not readable (%s)", space_name, e)
                space = space or {"name": space_name}
        target = _to_msg(raw, space, tz)
        if target is None:
            return ItemContext.unavailable("that Google Chat message could not be read")

        msgs = await self._surrounding(svc, target, space, tz)
        self._resolve_me(msgs)
        messages = [
            ContextMessage(
                author=self.cfg.user.name if self._is_me(m) else (m.sender_name or None),
                author_email=(self.cfg.user.email if self._is_me(m) else m.sender_email) or None,
                is_me=self._is_me(m),
                at=m.created,
                text=m.text[:CONTEXT_CHARS],
                kind="message",
            )
            for m in msgs
        ]
        last = msgs[-1]
        where = target.space_title or space_name
        age = _age_words((datetime.now(tz) - last.created).total_seconds())
        return ItemContext(
            kind="thread",
            title=f"{where}: {target.text[:60]}" if target.text else where,
            url=item.url or target.url,
            status=f"{'awaiting your reply' if not self._is_me(last) else 'you replied last'} · {len(msgs)} messages · last {age}",
            participants=sorted({m.sender_name for m in msgs if m.sender_name}),
            messages=messages,
            facts={
                "space": where,
                "messages": str(len(msgs)),
                "space type": target.space_type or "unknown",
            },
            awaiting_me=not self._is_me(last),
            fingerprint=f"{last.name}:{len(msgs)}",
        )
