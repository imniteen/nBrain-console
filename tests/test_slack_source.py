from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from nbrain.config.schema import Config
from nbrain.sources.base import Window
from nbrain.sources.slack import SlackSource
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore

ME = "U_ME"
NOW = time.time()


def window(**kw: Any) -> Window:
    base = dict(
        today=datetime.now(UTC).date(),
        email_lookback_days=7,
        commitment_lookback_days=30,
        awaiting_reply_hours=48,
        ticket_stale_days=5,
        review_age_days=3,
        tz="UTC",
    )
    base.update(kw)
    return Window(**base)


def hours_ago(h: float) -> str:
    return f"{NOW - h * 3600:.6f}"


def api_error(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


def match(text: str, ts: str, user: str, channel: str = "C1", name: str = "general") -> dict[str, Any]:
    return {
        "text": text,
        "ts": ts,
        "user": user,
        "channel": {"id": channel, "name": name},
        "permalink": f"https://slack.example/archives/{channel}/p{ts.replace('.', '')}",
    }


def msg(text: str, ts: str, user: str, **extra: Any) -> dict[str, Any]:
    return {"type": "message", "text": text, "ts": ts, "user": user, **extra}


@pytest.fixture
def slack_cfg(cfg: Config) -> Config:
    cfg.sources.slack.enabled = True
    cfg.sources.slack.user_id = ME
    return cfg


def make_source(cfg: Config, vault: VaultStore, client: MagicMock) -> SlackSource:
    src = SlackSource(cfg, vault)
    src._client = client
    return src


def basic_client() -> MagicMock:
    client = MagicMock()
    client.auth_test.return_value = {"ok": True, "user_id": ME, "user": "me", "team": "Acme"}
    client.users_info.side_effect = lambda user: {
        "ok": True,
        "user": {"id": user, "name": user.lower(), "real_name": "Bob Builder", "profile": {"display_name": "bob"}},
    }
    client.chat_getPermalink.side_effect = lambda channel, message_ts: {
        "ok": True,
        "permalink": f"https://slack.example/archives/{channel}/p{message_ts.replace('.', '')}",
    }
    return client


async def test_healthcheck_without_token(slack_cfg: Config, vault: VaultStore) -> None:
    with patch("nbrain.sources.slack.get_secret", return_value=None):
        status = await SlackSource(slack_cfg, vault).healthcheck()
    assert status.ok is False and "SLACK_TOKEN" in status.detail


async def test_healthcheck_reports_identity_and_scope_caveat(slack_cfg: Config, vault: VaultStore) -> None:
    slack_cfg.sources.slack.user_id = None
    src = make_source(slack_cfg, vault, basic_client())
    status = await src.healthcheck()
    assert status.ok and "U_ME" in status.detail and "search:read" in status.detail
    assert status.write_capable is True and src._me == ME


async def test_healthcheck_api_error_is_not_ok(slack_cfg: Config, vault: VaultStore) -> None:
    client = basic_client()
    client.auth_test.side_effect = api_error("invalid_auth")
    status = await make_source(slack_cfg, vault, client).healthcheck()
    assert status.ok is False and "invalid_auth" in status.detail


def search_client(mine: list[dict[str, Any]], mentions: list[dict[str, Any]]) -> MagicMock:
    client = basic_client()

    def search(query: str, count: int, page: int, sort: str) -> dict[str, Any]:
        matches = mine if query.startswith("from:") else mentions
        assert f"<@{ME}>" in query and "after:" in query
        return {"ok": True, "messages": {"matches": matches, "paging": {"pages": 1}}}

    client.search_messages.side_effect = search
    return client


async def test_own_messages_become_source_texts(slack_cfg: Config, vault: VaultStore) -> None:
    slack_cfg.sources.slack.max_messages = 1
    mine = [match("I'll send the deck Friday", hours_ago(30), ME), match("second", hours_ago(31), ME)]
    client = search_client(mine, [])
    res = await make_source(slack_cfg, vault, client).collect(window())
    assert len(res.texts) == 1
    t = res.texts[0]
    assert t.kind == "chat" and t.author_is_me and t.source_id == f"C1:{mine[0]['ts']}"
    assert t.participants == ["general"] and t.url == mine[0]["permalink"]
    assert t.observed_at == datetime.fromtimestamp(float(mine[0]["ts"]), tz=UTC)
    assert res.signals == []


async def test_mentions_only_when_old_enough_and_unanswered(slack_cfg: Config, vault: VaultStore) -> None:
    old_unanswered = match(f"<@{ME}> can you look at the deploy?", hours_ago(72), "U_BOB")
    old_answered = match(f"<@{ME}> ping", hours_ago(70), "U_BOB", channel="C2", name="dev")
    fresh = match(f"<@{ME}> quick one", hours_ago(3), "U_BOB")
    client = search_client([], [old_unanswered, old_answered, fresh])

    def replies(channel: str, ts: str, limit: int) -> dict[str, Any]:
        thread = [msg("orig", ts, "U_BOB")]
        if channel == "C2":
            thread.append(msg("done", hours_ago(60), ME, thread_ts=ts))
        return {"ok": True, "messages": thread}

    client.conversations_replies.side_effect = replies
    client.conversations_history.return_value = {"ok": True, "messages": []}

    res = await make_source(slack_cfg, vault, client).collect(window())
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.type == ItemType.waiting_on_me and sig.priority == 2
    assert sig.source_id == f"C1:{old_unanswered['ts']}"
    assert sig.title == f"Reply to bob in #general: {old_unanswered['text'][:60]}"
    assert sig.person == "bob" and sig.evidence == old_unanswered["text"][:200]
    assert sig.url == old_unanswered["permalink"]
    # the fresh mention was never checked against the API
    assert all(c.kwargs["ts"] != fresh["ts"] for c in client.conversations_replies.call_args_list)


async def test_channel_reply_after_mention_counts_as_answered(slack_cfg: Config, vault: VaultStore) -> None:
    mention = match(f"<@{ME}> thoughts?", hours_ago(72), "U_BOB")
    client = search_client([], [mention])
    client.conversations_replies.return_value = {"ok": True, "messages": [msg("orig", mention["ts"], "U_BOB")]}
    client.conversations_history.return_value = {"ok": True, "messages": [msg("here you go", hours_ago(50), ME)]}
    res = await make_source(slack_cfg, vault, client).collect(window())
    assert res.signals == []


async def test_search_missing_scope_falls_back_to_histories(slack_cfg: Config, vault: VaultStore) -> None:
    slack_cfg.sources.slack.channels = ["C1"]
    client = basic_client()
    client.search_messages.side_effect = api_error("missing_scope")
    client.conversations_list.return_value = {
        "ok": True,
        "channels": [{"id": "C1", "name": "general"}, {"id": "C9", "name": "skipped"}],
    }
    mention_ts = hours_ago(72)
    client.conversations_history.side_effect = lambda channel, oldest, limit: {
        "ok": True,
        "messages": [
            msg("I will write it up", hours_ago(10), ME),
            msg(f"<@{ME}> status?", mention_ts, "U_BOB"),
            msg("joined", hours_ago(5), "U_X", subtype="channel_join"),
        ]
        if float(oldest) < NOW - 72 * 3600 - 1
        else [],
    }
    client.conversations_replies.return_value = {"ok": True, "messages": [msg("orig", mention_ts, "U_BOB")]}

    res = await make_source(slack_cfg, vault, client).collect(window())
    assert [c.kwargs["channel"] for c in client.conversations_history.call_args_list][0] == "C1"
    assert all(c.kwargs["channel"] != "C9" for c in client.conversations_history.call_args_list)
    assert len(res.texts) == 1 and res.texts[0].text == "I will write it up"
    assert res.texts[0].url.endswith(f"/archives/C1/p{res.texts[0].source_id.split(':')[1].replace('.', '')}")
    assert len(res.signals) == 1 and res.signals[0].source_id == f"C1:{mention_ts}"
    assert any("search.messages unavailable" in n for n in res.notes)


async def test_other_search_errors_propagate(slack_cfg: Config, vault: VaultStore) -> None:
    client = basic_client()
    client.search_messages.side_effect = api_error("invalid_auth")
    with pytest.raises(SlackApiError):
        await make_source(slack_cfg, vault, client).collect(window())


async def test_verify_paths(slack_cfg: Config, vault: VaultStore) -> None:
    ts = hours_ago(72)
    item = Item(title="x", type=ItemType.waiting_on_me, source="slack", source_id=f"C1:{ts}")

    client = basic_client()
    client.conversations_replies.side_effect = api_error("thread_not_found")
    res = await make_source(slack_cfg, vault, client).verify(item)
    assert res.state == "resolved" and res.note == "message gone"

    client = basic_client()
    client.conversations_replies.return_value = {"ok": True, "messages": []}
    res = await make_source(slack_cfg, vault, client).verify(item)
    assert res.state == "resolved" and res.note == "message gone"

    client = basic_client()
    client.conversations_replies.return_value = {
        "ok": True,
        "messages": [msg("orig", ts, "U_BOB"), msg("on it", hours_ago(40), ME, thread_ts=ts)],
    }
    client.conversations_history.return_value = {"ok": True, "messages": []}
    res = await make_source(slack_cfg, vault, client).verify(item)
    assert res.state == "resolved" and res.note == "you replied"

    client = basic_client()
    client.conversations_replies.return_value = {"ok": True, "messages": [msg("orig", ts, "U_BOB")]}
    client.conversations_history.return_value = {"ok": True, "messages": [msg("unrelated", hours_ago(1), "U_X")]}
    assert (await make_source(slack_cfg, vault, client).verify(item)).state == "open"

    client = basic_client()
    client.conversations_replies.side_effect = api_error("not_in_channel")
    assert (await make_source(slack_cfg, vault, client).verify(item)).state == "unknown"

    bad = Item(title="x", source="slack", source_id="nonsense")
    assert (await make_source(slack_cfg, vault, basic_client()).verify(bad)).state == "unknown"
