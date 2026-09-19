"""The assist drawer: re-read the source, draft from what is there now, cache on a fingerprint."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from nbrain.config.loader import save_config
from nbrain.config.schema import Config
from nbrain.llm.tasks import ItemAssist
from nbrain.sources.base import ContextMessage, ItemContext
from nbrain.vault.schema import Item, ItemType
from nbrain.vault.store import VaultStore


class FakeSource:
    """Stands in for gmail. `bump()` simulates the thread moving on."""

    name = "gmail"
    roles = {"email"}
    verifiable = True

    def __init__(self) -> None:
        self.n = 1
        self.fetches = 0
        self.available = True

    def bump(self) -> None:
        self.n += 1

    async def healthcheck(self):  # pragma: no cover - not used here
        raise NotImplementedError

    async def collect(self, window):  # pragma: no cover - not used here
        raise NotImplementedError

    async def verify(self, item):  # pragma: no cover - not used here
        raise NotImplementedError

    async def fetch_context(self, item: Item) -> ItemContext:
        self.fetches += 1
        if not self.available:
            return ItemContext.unavailable("gmail token expired")
        return ItemContext(
            kind="thread",
            title="Re: Schema Files",
            url="https://mail.example/t/1",
            status=f"awaiting your reply · {self.n} messages",
            participants=["Mohit", "me"],
            messages=[ContextMessage(author="Mohit", text=f"message {i}") for i in range(self.n)],
            awaiting_me=True,
            fingerprint=f"t1:{self.n}",
        )


@pytest.fixture
def assist_app(tmp_path, monkeypatch):
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    cfg = Config(vault_path=store.root)
    cfg.user.name, cfg.user.first_name, cfg.user.email = "Niteen", "Niteen", "n@example.com"
    cfg.user.timezone = "Europe/London"
    save_config(cfg)

    item = Item(
        title="Reply to Mohit: Re: Schema Files",
        type=ItemType.waiting_on_me,
        source="gmail",
        source_id="t1",
        url="https://mail.example/t/1",
    )
    item.id = "20260917-reply-to-mohit"
    item.first_seen = date(2026, 9, 17)
    store.save(item)

    src = FakeSource()
    calls = {"n": 0}

    async def fake_assist(self, item_payload, context_payload):
        calls["n"] += 1
        return ItemAssist(
            situation="Mohit is waiting.",
            draft=f"Hi Mohit,\n\n[CONFIRM: date] (v{calls['n']})",
            next_step="Send the script.",
        )

    monkeypatch.setattr("nbrain.llm.tasks.LLMTasks.assist", fake_assist)

    from nbrain.web import app as appmod

    monkeypatch.setattr(appmod, "create_app", appmod.create_app)  # keep the symbol honest
    monkeypatch.setattr("nbrain.sweep.pipeline.build_native_sources", lambda c, s: [src])

    client = TestClient(appmod.create_app(store.root))
    return client, store, src, calls, item.id


def test_generates_then_serves_from_cache(assist_app):
    client, store, src, calls, item_id = assist_app

    r1 = client.get(f"/api/items/{item_id}/assist")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["cached"] is False
    assert d1["assist"]["draft"].endswith("(v1)")
    assert d1["context"]["available"] is True and d1["context"]["fingerprint"] == "t1:1"
    assert calls["n"] == 1 and src.fetches == 1

    r2 = client.get(f"/api/items/{item_id}/assist")
    d2 = r2.json()
    assert d2["cached"] is True
    assert d2["assist"]["draft"].endswith("(v1)"), "unchanged thread must reuse the draft"
    assert calls["n"] == 1, "no second model call for an unchanged thread"
    assert src.fetches == 2, "the source is re-read every time, even on a cache hit"


def test_cache_is_invalidated_when_the_thread_moves(assist_app):
    client, store, src, calls, item_id = assist_app
    client.get(f"/api/items/{item_id}/assist")
    src.bump()  # somebody replied

    d = client.get(f"/api/items/{item_id}/assist").json()
    assert d["cached"] is False
    assert d["assist"]["draft"].endswith("(v2)"), "a moved thread must be redrafted"
    assert d["context"]["fingerprint"] == "t1:2"
    assert calls["n"] == 2


def test_refresh_forces_a_redraft(assist_app):
    client, store, src, calls, item_id = assist_app
    client.get(f"/api/items/{item_id}/assist")
    d = client.get(f"/api/items/{item_id}/assist?refresh=1").json()
    assert d["cached"] is False and calls["n"] == 2


def test_unavailable_context_is_reported_not_faked(assist_app):
    client, store, src, calls, item_id = assist_app
    src.available = False
    d = client.get(f"/api/items/{item_id}/assist").json()
    assert d["context"]["available"] is False
    assert "expired" in d["context"]["note"]
    assert d["assist"] is not None  # still drafts, but from the item alone


def test_unknown_item_is_404(assist_app):
    client, *_ = assist_app
    assert client.get("/api/items/nope/assist").status_code == 404


def test_cache_lives_outside_the_notes(assist_app):
    """Generated text must not end up inside the user's markdown notes."""
    client, store, src, calls, item_id = assist_app
    client.get(f"/api/items/{item_id}/assist")

    note = (store.folder(Item) / f"{item_id}.md").read_text()
    assert "CONFIRM" not in note and "Mohit is waiting" not in note
    assert store.assist_path(item_id).exists()
    assert store.assist_path(item_id).is_relative_to(store.root / "nbrain")


def test_item_links_open_the_drawer(assist_app):
    client, store, src, calls, item_id = assist_app
    html = client.get("/ledger?status=all").text
    assert f'data-item="{item_id}"' in html
    assert 'id="drawer"' in html and "/api/items/" in html
