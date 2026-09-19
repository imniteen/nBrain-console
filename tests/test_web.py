from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nbrain.config.loader import config_file, load_config, save_config
from nbrain.config.schema import Config
from nbrain.vault.schema import Item, ItemStatus, ItemType, Person, Project
from nbrain.vault.store import VaultStore
from nbrain.web.app import create_app

BRIEF = """# Daily brief — Thursday 17 September 2026

| Due today | Waiting on you | Oldest | Delivered |
|---|---|---|---|
| 1 | 1 | 4d | 0/2 |

> [!important] Today's one thing
> Send the Q3 numbers to [[Alice Smith]].

## Due today
- **Send Q3 numbers** · [[20260913-send-q3-numbers]]
"""


@pytest.fixture
def web_vault(tmp_path: Path) -> VaultStore:
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    cfg = Config(vault_path=store.root)
    cfg.user.name = "Test User"
    cfg.user.email = "test@example.com"
    cfg.user.timezone = "Europe/London"
    save_config(cfg)

    store.save(Person(title="Alice Smith", id="Alice Smith", email="alice@example.com", tier=1))
    store.save(Project(title="Atlas", id="Atlas", keywords=["atlas"], weight=1.0))
    store.save(
        Item(
            id="20260913-send-q3-numbers",
            title="Send Q3 numbers",
            type=ItemType.commitment,
            source="gmail",
            source_id="t1",
            promised_to="Alice Smith",
            project="Atlas",
            due=date(2026, 9, 17),
            first_seen=date(2026, 9, 13),
            evidence="I'll send the Q3 numbers by Thursday.",
            url="https://mail.example.com/t1",
            body="## History\n- 2026-09-13 — First seen via gmail.\n",
        )
    )
    store.save(
        Item(
            id="20260915-review-mr-42",
            title="Review MR !42",
            type=ItemType.waiting_on_me,
            source="gitlab",
            source_id="mr42",
            promised_to="Alice Smith",
            first_seen=date(2026, 9, 15),
        )
    )
    store.write_text("Briefs/2026-09-17.md", BRIEF)
    store.write_text("Briefs/latest.md", BRIEF)
    store.write_text("Briefs/2026-09-17.html", "<html><body><h1>brief</h1></body></html>")
    return store


@pytest.fixture
def client(web_vault: VaultStore) -> TestClient:
    return TestClient(create_app(web_vault.root))


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"ok": True}


def test_dashboard_shows_brief_and_metrics(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Send the Q3 numbers to" in r.text
    assert 'href="/note/Alice%20Smith"' in r.text  # wikilink rewritten
    assert "open items" in r.text
    assert 'action="/run"' in r.text  # the sweep trigger is present (label may change)
    assert 'class="sidebar"' in r.text and 'href="/ledger"' in r.text  # app shell renders


def test_briefs_pages(client: TestClient) -> None:
    assert "2026-09-17" in client.get("/briefs").text
    r = client.get("/briefs/2026-09-17")
    assert r.status_code == 200 and "Due today" in r.text and "/briefs/2026-09-17.html" in r.text
    raw = client.get("/briefs/2026-09-17.html")
    assert raw.status_code == 200 and raw.text.startswith("<html>")
    assert client.get("/briefs/2020-01-01").status_code == 404
    assert client.get("/briefs/not-a-date").status_code == 404


def test_reviews_radar_memory(client: TestClient, web_vault: VaultStore) -> None:
    web_vault.write_text("Reviews/2026-W38.md", "# Week 38\n\nDelivered 3/4.")
    assert "2026-W38" in client.get("/reviews").text
    assert "Delivered 3/4" in client.get("/reviews/2026-W38").text
    assert client.get("/reviews/nope").status_code == 404
    assert client.get("/radar").status_code == 200
    r = client.get("/memory")
    assert r.status_code == 200 and "No memory.md" in r.text


def test_ledger_lists_filters_and_focus(client: TestClient) -> None:
    r = client.get("/ledger")
    assert r.status_code == 200
    assert "Send Q3 numbers" in r.text and "Review MR !42" in r.text
    r = client.get("/ledger", params={"type": "waiting-on-me"})
    assert "Review MR !42" in r.text and "Send Q3 numbers" not in r.text
    r = client.get("/ledger", params={"q": "q3"})
    assert "Send Q3 numbers" in r.text and "Review MR !42" not in r.text
    r = client.get("/ledger", params={"focus": "20260915-review-mr-42"})
    assert 'id="item-20260915-review-mr-42" class="focus' in r.text


def test_dismiss_requires_reason_and_persists(client: TestClient, web_vault: VaultStore) -> None:
    r = client.post("/items/20260913-send-q3-numbers/dismiss", data={"reason": ""})
    assert r.status_code == 400
    assert web_vault.load(Item, "20260913-send-q3-numbers").status == ItemStatus.open

    r = client.post("/items/20260913-send-q3-numbers/dismiss", data={"reason": "not mine"})
    assert r.status_code == 200  # followed the redirect to the ledger
    assert "status=dismissed" in str(r.url)
    item = web_vault.load(Item, "20260913-send-q3-numbers")
    assert item.status == ItemStatus.dismissed
    assert item.dismissed_reason == "not mine"
    assert "Dismissed: not mine" in item.body
    assert "Send Q3 numbers" in client.get("/ledger", params={"status": "dismissed"}).text
    assert "Send Q3 numbers" not in client.get("/ledger", params={"status": "open"}).text
    assert client.post("/items/does-not-exist/dismiss", data={"reason": "x"}).status_code == 404


def test_resolve_persists(client: TestClient, web_vault: VaultStore) -> None:
    r = client.post("/items/20260915-review-mr-42/resolve")
    assert r.status_code == 200
    item = web_vault.load(Item, "20260915-review-mr-42")
    assert item.status == ItemStatus.resolved
    assert item.resolved_on is not None
    assert "Review MR !42" in client.get("/ledger", params={"status": "resolved"}).text


def test_note_renders_any_folder(client: TestClient) -> None:
    r = client.get("/note/20260913-send-q3-numbers")
    assert r.status_code == 200
    assert "promised_to" in r.text and 'href="/note/Alice%20Smith"' in r.text
    assert "First seen via gmail" in r.text
    assert "Alice Smith" in client.get("/note/Alice Smith").text
    assert "Atlas" in client.get("/note/Atlas").text
    assert client.get("/note/missing").status_code == 404
    assert client.get("/note/..%2Fnbrain%2Fconfig").status_code == 404


def test_settings_get_and_post_persist(client: TestClient, web_vault: VaultStore) -> None:
    r = client.get("/settings")  # defaults to the Profile page
    assert r.status_code == 200
    assert 'name="user.name"' in r.text and 'name="_section" value="profile"' in r.text
    assert 'name="sweep.daily_time"' not in r.text  # lives on its own page now

    r = client.get("/settings/sweep")
    assert r.status_code == 200
    assert 'name="sweep.daily_time"' in r.text and 'value="08:15"' in r.text

    r = client.get("/settings/secrets")
    assert r.status_code == 200
    assert "ANTHROPIC_API_KEY" in r.text  # secret status shown, value never

    r = client.post("/settings", data={"sweep.daily_time": "07:30", "user.name_variants": "Nit, Niteen B"})
    assert r.status_code == 200 and "Saved" in r.text
    saved = yaml.safe_load(config_file(web_vault.root).read_text())
    assert saved["sweep"]["daily_time"] == "07:30"
    assert saved["user"]["name_variants"] == ["Nit", "Niteen B"]
    assert saved["user"]["name"] == "Test User"  # untouched fields survive a partial post
    assert "vault_path" not in saved
    assert load_config(web_vault.root).sweep.daily_time == "07:30"


def test_settings_full_form_checkboxes_and_llm_override(client: TestClient, web_vault: VaultStore) -> None:
    form = {
        "_form": "settings",
        "user.name": "Test User",
        "sweep.daily_time": "09:00",
        "sweep.extras": ["delivery_score"],
        "sources.gitlab.enabled": "1",
        "llm.extract.enabled": "1",
        "llm.extract.provider": "ollama",
        "llm.extract.model": "qwen3",
        "llm.extract.base_url": "http://localhost:11434/v1",
        "mcp_servers_json": '[{"name": "notes", "transport": "http", "url": "http://localhost:9000/mcp"}]',
    }
    r = client.post("/settings", data=form)
    assert r.status_code == 200, r.text
    cfg = load_config(web_vault.root)
    assert cfg.sweep.extras == ["delivery_score"]
    assert cfg.sources.gitlab.enabled is True
    assert cfg.sweep.weekdays_only is False  # unchecked box in a full form
    assert cfg.llm.extract is not None and cfg.llm.extract.model == "qwen3"
    assert cfg.llm.write is None
    assert cfg.mcp_servers[0].name == "notes" and cfg.mcp_servers[0].allow_write is False


def test_settings_invalid_rerenders_and_does_not_persist(client: TestClient, web_vault: VaultStore) -> None:
    r = client.post("/settings", data={"_section": "sweep", "sweep.email_lookback_days": "lots"})
    assert r.status_code == 400
    assert "Not saved" in r.text and "email_lookback_days" in r.text
    assert 'value="lots"' in r.text  # the attempted value is kept in the form
    assert load_config(web_vault.root).sweep.email_lookback_days == 7

    r = client.post("/settings", data={"_section": "sources", "mcp_servers_json": "not json"})
    assert r.status_code == 400 and "mcp_servers" in r.text
    assert load_config(web_vault.root).mcp_servers == []

    r = client.post("/settings", data={"role_track": "cto"})
    assert r.status_code == 400
    assert load_config(web_vault.root).role_track.value == "ic"


def test_secret_form_writes_dotenv(client: TestClient, web_vault: VaultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NBRAIN_TEST_SECRET", raising=False)
    r = client.post("/settings/secret", data={"env_name": "NBRAIN_TEST_SECRET", "value": "s3cret", "store": "dotenv"})
    assert r.status_code == 200
    env = (web_vault.root / "nbrain" / ".env").read_text()
    assert "NBRAIN_TEST_SECRET=s3cret" in env
    assert "s3cret" not in client.get("/settings").text
    assert client.post("/settings/secret", data={"env_name": "bad name", "value": "x"}).status_code == 400


def test_sources_page(client: TestClient) -> None:
    r = client.get("/sources")
    assert r.status_code == 200 and "gitlab" in r.text and "anthropic:" in r.text
    # nothing enabled -> the check runs and reports the model key status only, no network
    r = client.post("/sources/check")
    assert r.status_code == 200 and "Check results" in r.text and "llm:default" in r.text


def test_run_status_and_graph(client: TestClient) -> None:
    s = client.get("/run/status").json()
    assert s["running"] is False and s["last_result"] is None and isinstance(s["log_tail"], list)
    assert client.get("/graph").status_code == 200
    r = client.get("/api/graph.json", params={"focus": "Alice Smith", "depth": 2})
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert "nodes" in body and any(n["id"] == "Alice Smith" for n in body["nodes"])
    r = client.get("/api/graph/patterns")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert isinstance(r.json()["patterns"], list)


def test_app_shell_and_cache_busting(client):
    """Every page renders the shell, and the stylesheet carries a version so an upgraded
    nbrain never shows the previous design from browser cache."""
    import re

    for path in ("/", "/ledger", "/briefs", "/reviews", "/sources", "/settings", "/radar", "/memory"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'class="sidebar"' in r.text, path
        assert 'class="topbar"' in r.text, path
        assert re.search(r'/static/app\.css\?v=\d+', r.text), path
        # exactly one shell heading, in the topbar (a rendered document may carry its own h1)
        head = r.text.split('<main class="content">')[0]
        assert head.count("<h1>") == 1, path


def test_active_nav_item_is_marked(client):
    r = client.get("/ledger")
    assert 'href="/ledger" class="active" aria-current="page"' in r.text
    assert 'href="/briefs" class="active"' not in r.text


def test_settings_pages_split_and_are_reachable(client: TestClient) -> None:
    pages = {
        "profile": 'name="user.timezone"',
        "model": 'name="llm.default.provider"',
        "sources": 'name="sources.google.enabled"',
        "sweep": 'name="sweep.daily_time"',
        "delivery": 'name="delivery.gmail_draft.enabled"',
        "secrets": "Store secret",
        "advanced": 'name="web.port"',
    }
    for key, marker in pages.items():
        r = client.get(f"/settings/{key}")
        assert r.status_code == 200, key
        assert marker in r.text, key
        assert f'href="/settings/{key}" class="active"' in r.text, key
        # each page carries only its own fields
        others = [m for k, m in pages.items() if k != key and m.startswith("name=")]
        assert not [m for m in others if m in r.text], f"{key} leaks fields from another page"
    assert client.get("/settings/nonsense").status_code == 404


def test_saving_one_page_does_not_clear_another(client: TestClient, web_vault: VaultStore) -> None:
    """The bug this guards: an unticked box only means False when its page was on screen."""
    from nbrain.web.app import section_bools

    # turn things on across two different pages
    client.post("/settings", data={
        "_section": "sources", "_bools": section_bools("sources"),
        "sources.google.enabled": "1", "sources.jira.enabled": "1",
    })
    client.post("/settings", data={
        "_section": "delivery", "_bools": section_bools("delivery"),
        "delivery.file.enabled": "1", "delivery.gmail_draft.enabled": "1",
    })
    cfg = load_config(web_vault.root)
    assert cfg.sources.google.enabled and cfg.sources.jira.enabled
    assert cfg.delivery.gmail_draft.enabled

    # now save the Profile page, which owns no checkboxes at all
    client.post("/settings", data={
        "_section": "profile", "_bools": section_bools("profile"), "user.name": "Renamed",
    })
    cfg = load_config(web_vault.root)
    assert cfg.user.name == "Renamed"
    assert cfg.sources.google.enabled and cfg.sources.jira.enabled, "sources were cleared"
    assert cfg.delivery.gmail_draft.enabled, "delivery was cleared"

    # unticking on the page that owns the box still works
    client.post("/settings", data={
        "_section": "delivery", "_bools": section_bools("delivery"), "delivery.file.enabled": "1",
    })
    cfg = load_config(web_vault.root)
    assert cfg.delivery.gmail_draft.enabled is False, "unticking on its own page must apply"
    assert cfg.sources.google.enabled, "sources still untouched"


def test_collapsed_panels_keep_their_values(client: TestClient, web_vault: VaultStore) -> None:
    """A switched-off source collapses in the UI. Its fields are hidden, not removed, so the
    browser still submits them and the settings survive being turned off and on again."""
    from nbrain.web.app import section_bools

    client.post("/settings", data={
        "_section": "sources", "_bools": section_bools("sources"),
        "sources.gitlab.enabled": "1",
        "sources.gitlab.url": "https://gitlab.example.com",
        "sources.gitlab.username": "niteen",
        "sources.gitlab.projects": "grp/one, grp/two",
    })
    assert load_config(web_vault.root).sources.gitlab.url == "https://gitlab.example.com"

    # switch GitLab off; the hidden inputs still post their current values
    client.post("/settings", data={
        "_section": "sources", "_bools": section_bools("sources"),
        "sources.gitlab.url": "https://gitlab.example.com",
        "sources.gitlab.username": "niteen",
        "sources.gitlab.projects": "grp/one, grp/two",
    })
    cfg = load_config(web_vault.root)
    assert cfg.sources.gitlab.enabled is False
    assert cfg.sources.gitlab.url == "https://gitlab.example.com", "settings lost when switched off"
    assert cfg.sources.gitlab.projects == ["grp/one", "grp/two"]


def test_sources_page_renders_a_panel_per_source(client: TestClient) -> None:
    html = client.get("/settings/sources").text
    for name in ("Google Workspace", "GitLab", "Slack", "Jira"):
        assert name in html, name
    assert html.count('class="panel"') >= 4
    assert 'class="switch"' in html and 'class="panel-off"' in html
    assert "details class=\"adv\"" in html or 'class="adv"' in html
