"""Slack source: my own recent messages (for the LLM to find commitments) and mentions of me
that I have not answered.

Read-only: search, conversations.*, users.info and chat.getPermalink only. Nothing is posted."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from nbrain.config.loader import get_secret
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
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

MAX_CHANNELS = 40
MAX_MENTIONS = 50
CONTEXT_MESSAGES = 15
CONTEXT_CHARS = 2000
SEARCH_UNAVAILABLE = {"missing_scope", "not_allowed_token_type", "method_not_supported_for_channel_type"}
GONE = {"thread_not_found", "message_not_found", "channel_not_found"}
NO_ACCESS = {"missing_scope", "not_in_channel", "invalid_auth", "token_revoked", "account_inactive"}
SCOPES_DETAIL = (
    "check token scopes: needs channels:history, groups:history, im:history, mpim:history, "
    "users:read, search:read only (nbrain cannot verify scopes; it never posts)"
)


class SlackAuthError(RuntimeError):
    pass


def _ts_dt(ts: str | float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)


def _day_ts(day: date) -> float:
    return datetime.combine(day, time.min, tzinfo=UTC).timestamp()


def _data(resp: Any) -> dict[str, Any]:
    return resp.data if hasattr(resp, "data") else dict(resp)


def _is_app(m: dict[str, Any]) -> bool:
    """Bots and apps post messages but are not people to have a relationship with."""
    return bool(m.get("bot_id") or m.get("app_id") or m.get("subtype"))


@dataclass
class _Chan:
    """What one channel contributed to this sweep: who spoke and how recently."""

    name: str
    at: float = 0.0
    last_is_me: bool = False
    users: set[str] = field(default_factory=set)

    def saw(self, ts: float, uid: str, *, is_me: bool) -> None:
        if ts >= self.at:
            self.at, self.last_is_me = ts, is_me
        if not is_me:
            self.users.add(uid)


class SlackSource(BaseSource):
    name = "slack"
    roles = {"chat"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore):
        self.cfg = cfg
        self.store = store
        self._client: WebClient | None = None
        self._me: str | None = cfg.sources.slack.user_id or None
        self._users: dict[str, str] = {}
        self._user_emails: dict[str, str] = {}  # filled from the same users.info call as the name
        self._channels: dict[str, str] = {}
        self._dms: set[str] = set()  # im/mpim ids seen while listing conversations
        self._permalinks: dict[str, str] = {}

    # ---------- client ----------

    def _web(self) -> WebClient:
        if self._client is None:
            scfg = self.cfg.sources.slack
            token = get_secret(scfg.token_env)
            if not token:
                raise SlackAuthError(
                    f"no Slack token: set {scfg.token_env} (env, nbrain/.env or `nbrain secret set`)"
                )
            self._client = WebClient(token=token, timeout=30)
        return self._client

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        fn = getattr(self._web(), method)
        return _data(await asyncio.to_thread(fn, **kwargs))

    async def _whoami(self) -> str:
        if self._me:
            return self._me
        auth = await self._call("auth_test")
        self._me = str(auth["user_id"])
        return self._me

    async def _user_name(self, uid: str | None) -> str:
        if not uid:
            return "someone"
        if uid not in self._users:
            try:
                u = (await self._call("users_info", user=uid)).get("user") or {}
                profile = u.get("profile") or {}
                self._users[uid] = (
                    profile.get("display_name") or u.get("real_name") or u.get("name") or uid
                )
                email = str(profile.get("email") or "").strip().lower()
                if email:  # only when the token may read it; never guessed from the handle
                    self._user_emails[uid] = email
            except SlackApiError as e:
                log.warning("slack: users.info failed for %s: %s", uid, e)
                self._users[uid] = uid
        return self._users[uid]

    async def _channel_name(self, channel: str) -> str:
        if channel not in self._channels:
            try:
                info = (await self._call("conversations_info", channel=channel)).get("channel") or {}
                self._channels[channel] = str(info.get("name") or info.get("user") or channel)
            except SlackApiError as e:
                log.info("slack: conversations.info failed for %s: %s", channel, e.response.get("error"))
                self._channels[channel] = channel
        return self._channels[channel]

    async def _permalink(self, channel: str, ts: str) -> str | None:
        key = f"{channel}:{ts}"
        if key not in self._permalinks:
            try:
                resp = await self._call("chat_getPermalink", channel=channel, message_ts=ts)
                self._permalinks[key] = resp.get("permalink") or ""
            except SlackApiError as e:
                log.warning("slack: permalink failed for %s: %s", key, e)
                self._permalinks[key] = ""
        return self._permalinks[key] or None

    # ---------- protocol ----------

    async def healthcheck(self) -> SourceStatus:
        now = datetime.now(UTC)
        try:
            auth = await self._call("auth_test")
            self._me = self.cfg.sources.slack.user_id or str(auth["user_id"])
        except SlackAuthError as e:
            return SourceStatus(self.name, False, detail=str(e), checked_at=now)
        except SlackApiError as e:
            return SourceStatus(self.name, False, detail=f"auth.test: {e.response.get('error')}", checked_at=now)
        except Exception as e:  # noqa: BLE001 - healthcheck must never raise
            return SourceStatus(self.name, False, detail=f"{type(e).__name__}: {e}", checked_at=now)
        detail = f"authenticated as {auth.get('user')} ({self._me}) in {auth.get('team')}; {SCOPES_DETAIL}"
        return SourceStatus(self.name, True, detail=detail, checked_at=now, write_capable=True)

    async def collect(self, window: Window) -> CollectResult:
        me = await self._whoami()
        result = CollectResult()
        cap = self.cfg.sources.slack.max_messages
        mention_since = _day_ts(window.email_since)
        try:
            mine = await self._search(f"from:<@{me}> after:{window.commitment_since.isoformat()}", cap)
            mentions = await self._search(f"<@{me}> -from:<@{me}> after:{window.email_since.isoformat()}", MAX_MENTIONS)
        except SlackApiError as e:
            if e.response.get("error") not in SEARCH_UNAVAILABLE:
                raise
            log.info("slack: search unavailable (%s); scanning channel histories", e.response.get("error"))
            result.notes.append("slack: search.messages unavailable; scanned up to 40 channels only")
            mine, mentions = await self._scan_histories(me, _day_ts(window.commitment_since), mention_since, cap)

        for m in mine[:cap]:
            if not m.get("text"):
                continue
            channel = m["channel"]
            result.texts.append(
                SourceText(
                    source=self.name,
                    source_id=f"{channel}:{m['ts']}",
                    url=m.get("permalink") or await self._permalink(channel, m["ts"]),
                    kind="chat",
                    author=self.cfg.user.name or me,
                    author_is_me=True,
                    participants=[m.get("channel_name") or channel],
                    observed_at=_ts_dt(m["ts"]),
                    text=m["text"],
                )
            )

        result.interactions += await self._interactions([*mine[:cap], *mentions[:MAX_MENTIONS]], me)

        cutoff = datetime.now(UTC).timestamp() - window.awaiting_reply_hours * 3600
        for m in mentions[:MAX_MENTIONS]:
            try:
                sig = await self._mention_signal(m, me, cutoff, mention_since)
                if sig:
                    result.signals.append(sig)
            except SlackApiError as e:  # per-item: log and continue
                log.warning("slack: skipping mention %s: %s", m.get("ts"), e.response.get("error"))
        return result

    async def verify(self, item: Item) -> VerifyResult:
        try:
            channel, ts = item.source_id.split(":", 1)
        except ValueError:
            return VerifyResult(state="unknown", note=f"unrecognised source_id {item.source_id!r}")
        me = await self._whoami()
        try:
            state = await self._reply_state(channel, ts, me)
        except SlackApiError as e:
            return VerifyResult(state="unknown", note=f"slack: {e.response.get('error')}")
        if state == "gone":
            return VerifyResult(state="resolved", note="message gone")
        if state == "replied":
            return VerifyResult(state="resolved", note="you replied")
        return VerifyResult(state="open")

    # ---------- live context ----------

    async def _context_window(self, channel: str, ts: str) -> tuple[list[dict[str, Any]], bool]:
        """The thread when the message has one, otherwise the channel around it. Oldest first."""
        replies = (
            await self._call("conversations_replies", channel=channel, ts=ts, limit=CONTEXT_MESSAGES)
        ).get("messages") or []
        if len(replies) > 1:
            return replies[-CONTEXT_MESSAGES:], True
        before = (
            await self._call(
                "conversations_history", channel=channel, latest=ts, inclusive=True, limit=CONTEXT_MESSAGES
            )
        ).get("messages") or []
        after = (
            await self._call("conversations_history", channel=channel, oldest=ts, limit=CONTEXT_MESSAGES)
        ).get("messages") or []
        merged = {str(m["ts"]): m for m in [*replies, *before, *after] if m.get("ts")}
        ordered = sorted(merged.values(), key=lambda m: float(m["ts"]))
        return ordered[-CONTEXT_MESSAGES:], False

    async def fetch_context(self, item: Item) -> ItemContext:
        try:
            channel, ts = item.source_id.split(":", 1)
        except ValueError:
            return ItemContext.unavailable(f"unrecognised Slack source_id {item.source_id!r}")
        try:
            me = await self._whoami()
            raw, threaded = await self._context_window(channel, ts)
            name = await self._channel_name(channel)
        except SlackAuthError as e:
            return ItemContext.unavailable(str(e))
        except SlackApiError as e:
            err = str(e.response.get("error") or "unknown error")
            if err in GONE:
                return ItemContext.unavailable("that Slack message or channel is gone")
            if err in NO_ACCESS:
                return ItemContext.unavailable(f"Slack refused the read ({err}); check the token scopes")
            return ItemContext.unavailable(f"Slack error: {err}")
        except OSError as e:
            return ItemContext.unavailable(f"could not reach Slack: {e}")
        usable = [m for m in raw if m.get("text") and not m.get("subtype")]
        if not any(str(m.get("ts")) == ts for m in raw) and not usable:
            return ItemContext.unavailable("that Slack message is no longer readable")

        messages = [
            ContextMessage(
                author=await self._user_name(m.get("user")),
                is_me=m.get("user") == me,
                at=_ts_dt(m["ts"]),
                text=str(m.get("text") or "")[:CONTEXT_CHARS],
                kind="message",
            )
            for m in usable
        ]
        if not messages:
            return ItemContext.unavailable("that Slack conversation has no readable messages")
        last = messages[-1]
        return ItemContext(
            kind="thread",
            title=f"#{name}: {messages[0].text[:60]}",
            url=item.url or await self._permalink(channel, ts),
            status=f"{'awaiting your reply' if not last.is_me else 'you replied last'} · {len(messages)} messages in {'a thread' if threaded else 'the channel'}",
            participants=sorted({m.author for m in messages if m.author}),
            messages=messages,
            facts={
                "channel": f"#{name}",
                "messages": str(len(messages)),
                "shape": "thread" if threaded else "channel around the message",
            },
            awaiting_me=not last.is_me,
            fingerprint=f"{usable[-1]['ts']}:{len(usable)}",
        )

    # ---------- interactions ----------

    def _is_dm(self, channel: str) -> bool:
        return channel in self._dms or channel.startswith("D")

    def _tally(self, channels: dict[str, _Chan], m: dict[str, Any], me: str) -> None:
        uid, cid = m.get("user"), m.get("channel")
        if not uid or not cid or _is_app(m):
            return
        name = str(m.get("channel_name") or self._channels.get(cid) or cid)
        channels.setdefault(cid, _Chan(name=name)).saw(
            float(m.get("ts") or 0), str(uid), is_me=uid == me
        )

    async def _interactions(self, messages: list[dict[str, Any]], me: str) -> list[Interaction]:
        """One per human per channel, from the messages this sweep already read."""
        channels: dict[str, _Chan] = {}
        for m in messages:
            try:
                self._tally(channels, m, me)
            except Exception as e:  # noqa: BLE001 - a malformed message costs only itself
                log.warning("slack: skipping interaction for %s: %s", m.get("ts"), e)
        out: list[Interaction] = []
        for cid, chan in channels.items():
            for uid in sorted(chan.users):
                try:
                    who = await self._user_name(uid)  # populates the email cache too, one call
                    out.append(
                        Interaction(
                            person_email=self._user_emails.get(uid),
                            person_name=who,
                            channel="slack",
                            at=_ts_dt(chan.at),
                            ref=cid,
                            group=not self._is_dm(cid),
                            with_me=True,
                            inbound=not chan.last_is_me,
                            subject=chan.name,
                        )
                    )
                except Exception as e:  # noqa: BLE001 - relationship data never blocks collection
                    log.warning("slack: no interaction for %s in %s: %s", uid, cid, e)
        return out

    # ---------- fetching ----------

    async def _search(self, query: str, cap: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < cap:
            resp = await self._call("search_messages", query=query, count=100, page=page, sort="timestamp")
            block = resp.get("messages") or {}
            for m in block.get("matches") or []:
                ch = m.get("channel") or {}
                out.append({**m, "channel": ch.get("id"), "channel_name": ch.get("name")})
            paging = block.get("paging") or {}
            if page >= int(paging.get("pages") or 1):
                break
            page += 1
        return [m for m in out if m.get("channel")]

    async def _scan_histories(
        self, me: str, mine_since: float, mention_since: float, cap: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        listing = await self._call(
            "conversations_list", types="im,mpim,public_channel,private_channel", limit=200, exclude_archived=True
        )
        channels = listing.get("channels") or []
        wanted = set(self.cfg.sources.slack.channels)
        if wanted:
            channels = [c for c in channels if c["id"] in wanted]
        mine: list[dict[str, Any]] = []
        mentions: list[dict[str, Any]] = []
        for ch in channels[:MAX_CHANNELS]:
            name = ch.get("name") or ch.get("id")
            self._channels[ch["id"]] = name
            if ch.get("is_im") or ch.get("is_mpim"):
                self._dms.add(ch["id"])
            try:
                hist = await self._call(
                    "conversations_history", channel=ch["id"], oldest=str(min(mine_since, mention_since)), limit=200
                )
            except SlackApiError as e:
                log.warning("slack: history failed for %s: %s", name, e.response.get("error"))
                continue
            for m in hist.get("messages") or []:
                if m.get("subtype") or not m.get("text"):
                    continue
                entry = {**m, "channel": ch["id"], "channel_name": name}
                if m.get("user") == me and float(m["ts"]) >= mine_since and len(mine) < cap:
                    mine.append(entry)
                elif m.get("user") != me and f"<@{me}>" in m["text"]:
                    mentions.append(entry)
        return mine, mentions

    async def _mention_signal(
        self, m: dict[str, Any], me: str, cutoff: float, since: float
    ) -> Signal | None:
        ts = m["ts"]
        text = m.get("text") or ""
        if m.get("user") == me or f"<@{me}>" not in text:
            return None
        if float(ts) < since or float(ts) > cutoff:
            return None  # too old for the window, or too fresh to count as awaiting reply
        channel = m["channel"]
        if await self._reply_state(channel, ts, me, thread_ts=m.get("thread_ts")) != "open":
            return None
        who = await self._user_name(m.get("user"))
        chan_name = m.get("channel_name") or self._channels.get(channel) or channel
        return Signal(
            type=ItemType.waiting_on_me,
            title=f"Reply to {who} in #{chan_name}: {text[:60]}",
            source=self.name,
            source_id=f"{channel}:{ts}",
            url=m.get("permalink") or await self._permalink(channel, ts),
            person=who,
            observed_at=_ts_dt(ts),
            evidence=text[:200],
            priority=2,
        )

    async def _reply_state(self, channel: str, ts: str, me: str, thread_ts: str | None = None) -> str:
        """'gone' | 'replied' | 'open' for a message, considering its thread and the channel after it."""
        try:
            replies = (await self._call("conversations_replies", channel=channel, ts=ts, limit=50)).get("messages") or []
        except SlackApiError as e:
            if e.response.get("error") in GONE:
                return "gone"
            raise
        if not any(r.get("ts") == ts for r in replies):
            return "gone"
        root = thread_ts or next((r.get("thread_ts") for r in replies if r.get("ts") == ts), None)
        if root and root != ts:
            replies += (await self._call("conversations_replies", channel=channel, ts=root, limit=50)).get("messages") or []
        later = (await self._call("conversations_history", channel=channel, oldest=ts, limit=50)).get("messages") or []
        for msg in [*replies, *later]:
            if msg.get("user") == me and float(msg.get("ts", 0)) > float(ts):
                return "replied"
        return "open"
