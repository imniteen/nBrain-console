"""Re-running setup must preserve what is already configured.

The wizard is interactive, so these drive it by stubbing `questionary` at the module boundary:
`_ask` is what every prompt goes through, so a scripted queue of answers is enough to exercise
the real control flow without a terminal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nbrain.config.loader import save_config
from nbrain.config.schema import Config
from nbrain.setup import wizard
from nbrain.vault.schema import Person, Project
from nbrain.vault.store import VaultStore


@pytest.fixture
def configured(tmp_path: Path) -> tuple[Config, VaultStore]:
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    cfg = Config(vault_path=store.root)
    cfg.user.name, cfg.user.first_name, cfg.user.email = "Niteen", "Niteen", "n@example.com"
    cfg.user.timezone = "Asia/Kolkata"
    cfg.sources.google.enabled = True
    cfg.sources.slack.enabled = True
    cfg.sources.gitlab.enabled = False
    cfg.sources.jira.enabled = False
    cfg.delivery.gmail_draft.enabled = True
    cfg.sweep.daily_time = "10:15"
    save_config(cfg)
    return cfg, store


def test_fresh_setup_runs_every_step(tmp_path: Path):
    store = VaultStore(tmp_path / "v")
    store.ensure_layout()
    empty = Config(vault_path=store.root)
    assert wizard._is_configured(empty) is False
    assert wizard._choose_steps(empty, store) == {k for k, _ in wizard.SETUP_STEPS}


def test_rerun_offers_a_choice_and_honours_it(configured, monkeypatch):
    cfg, store = configured
    assert wizard._is_configured(cfg) is True

    asked: dict[str, object] = {}

    def fake_ask(q):
        asked["choices"] = q
        return ["sources"]

    monkeypatch.setattr(wizard, "_ask", fake_ask)
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    chosen = wizard._choose_steps(cfg, store)
    assert chosen == {"sources"}, "only the selected step runs"
    # every step is offered, and none is pre-ticked except the suggestion below
    offered = [c.value for c in asked["choices"]["choices"]]
    assert offered == [k for k, _ in wizard.SETUP_STEPS]


def test_empty_people_preselects_discovery(configured, monkeypatch):
    cfg, store = configured
    captured: dict[str, object] = {}
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    monkeypatch.setattr(wizard, "_ask", lambda q: captured.setdefault("q", q) and [])
    wizard._choose_steps(cfg, store)
    pre = {c.value for c in captured["q"]["choices"] if c.checked}
    assert pre == {"discovery"}, "an empty vault should nudge towards discovery"

    store.save(Person(title="Ada", id="Ada", tier=1))
    store.save(Project(title="Atlas", id="Atlas"))
    captured.clear()
    wizard._choose_steps(cfg, store)
    pre = {c.value for c in captured["q"]["choices"] if c.checked}
    assert pre == set(), "nothing is pre-ticked once the vault has content"


def test_selecting_nothing_changes_nothing(configured, monkeypatch):
    cfg, store = configured
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    monkeypatch.setattr(wizard, "_ask", lambda q: [])
    assert wizard._choose_steps(cfg, store) == set()


def test_source_checkboxes_reflect_current_state(configured, monkeypatch):
    """The complaint that started this: a re-run showed everything unticked."""
    cfg, store = configured
    captured: dict[str, object] = {}

    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    monkeypatch.setattr(wizard, "_ask", lambda q: captured.setdefault("q", q) and [])
    monkeypatch.setattr(wizard, "_secret_step", lambda *a, **k: True)
    wizard._sources_step(cfg, store.root)

    checked = {c.value for c in captured["q"]["choices"] if c.checked}
    assert checked == {"google", "slack"}, "already-enabled sources must arrive ticked"
    labels = " ".join(str(c.title) for c in captured["q"]["choices"])
    assert "nothing yet" in labels or "authorised" in labels  # status is shown inline


def test_unticking_disables_and_keeps_settings(configured, monkeypatch):
    cfg, store = configured
    cfg.sources.slack.user_id = "U123"
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: dict(k, kind="checkbox"))
    monkeypatch.setattr(wizard.Q, "path", lambda msg, **k: {"kind": "path"})
    monkeypatch.setattr(wizard.Q, "confirm", lambda msg, **k: {"kind": "confirm"})

    def answer(q):
        kind = q.get("kind") if isinstance(q, dict) else None
        if kind == "checkbox":
            return ["google"]  # slack unticked
        if kind == "path":
            return "/nonexistent/credentials.json"
        return False

    monkeypatch.setattr(wizard, "_ask", answer)
    monkeypatch.setattr(wizard, "_secret_step", lambda *a, **k: True)
    wizard._sources_step(cfg, store.root)

    assert cfg.sources.slack.enabled is False
    assert cfg.sources.slack.user_id == "U123", "settings survive being switched off"
    assert cfg.sources.google.enabled is True


def test_existing_google_client_is_not_re_requested(configured, monkeypatch):
    cfg, store = configured
    creds = store.root / "nbrain" / cfg.sources.google.credentials_file
    creds.write_text('{"installed": {}}')
    prompts: list[str] = []

    def fake_ask(q):
        prompts.append(str(q))
        if isinstance(q, dict) and "choices" in q:
            return ["google"]
        return False  # "Replace it?" -> no

    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    monkeypatch.setattr(wizard.Q, "confirm", lambda msg, **k: f"confirm:{msg}")
    monkeypatch.setattr(wizard.Q, "path", lambda msg, **k: pytest.fail("must not ask for a path again"))
    monkeypatch.setattr(wizard, "_ask", fake_ask)
    wizard._sources_step(cfg, store.root)

    assert any("Replace" in p for p in prompts), "it should offer to replace, not demand a path"
    assert creds.read_text() == '{"installed": {}}', "the existing client is untouched"


def _stub_prompts(monkeypatch, *, picked, domain="be.glean.com"):
    """Drive the sources step: a checkbox answer, a domain, and a no to connecting now."""
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: dict(k, kind="checkbox"))
    monkeypatch.setattr(wizard.Q, "text", lambda msg, **k: {"kind": "text", "msg": msg})
    monkeypatch.setattr(wizard.Q, "path", lambda msg, **k: {"kind": "path"})
    monkeypatch.setattr(wizard.Q, "confirm", lambda msg, **k: {"kind": "confirm"})
    monkeypatch.setattr(wizard, "_secret_step", lambda *a, **k: True)

    def answer(q):
        kind = q.get("kind") if isinstance(q, dict) else None
        if kind == "checkbox":
            return list(picked)
        if kind == "text":
            return domain if "domain" in str(q.get("msg", "")) else ""
        if kind == "path":
            return "/nonexistent.json"
        return False  # including "connect now?" — these tests stop short of the browser

    monkeypatch.setattr(wizard, "_ask", answer)


def test_glean_is_offered_as_a_standard_source(configured, monkeypatch):
    cfg, store = configured
    captured: dict[str, object] = {}
    monkeypatch.setattr(wizard.Q, "checkbox", lambda *a, **k: k)
    monkeypatch.setattr(wizard, "_ask", lambda q: captured.setdefault("q", q) and [])
    monkeypatch.setattr(wizard, "_secret_step", lambda *a, **k: True)
    wizard._sources_step(cfg, store.root)

    offered = [c.value for c in captured["q"]["choices"]]
    assert "mcp:glean" in offered, "Glean should sit alongside the native sources"
    assert "mcp:slack" in offered, "so should Slack's own MCP server"
    label = next(str(c.title) for c in captured["q"]["choices"] if c.value == "mcp:glean")
    assert "Glean" in label and "knowledge" in label.lower()


def test_picking_glean_writes_an_oauth_entry_with_no_token(configured, monkeypatch):
    """The whole point: a connector must not ask the user to find an API token."""
    cfg, store = configured
    _stub_prompts(monkeypatch, picked=["google", "mcp:glean"], domain="acme-be.glean.com")
    wizard._sources_step(cfg, store.root)

    srv = wizard._find_mcp(cfg, "glean")
    assert srv is not None
    assert srv.enabled is True
    assert srv.transport == "http"
    assert srv.url == "https://acme-be.glean.com/mcp/default"
    assert srv.auth == "oauth", "Glean registers nbrain itself, so fastmcp holds the token"
    assert srv.headers_env == {}, "an OAuth connector must not also send a stale bearer"
    assert srv.roles == ["knowledge"]
    assert srv.instructions  # a sensible default so the collector is not aimless


def test_glean_domain_is_normalised(configured, monkeypatch):
    cfg, store = configured
    _stub_prompts(monkeypatch, picked=["mcp:glean"], domain="https://acme-be.glean.com/")
    wizard._sources_step(cfg, store.root)
    assert wizard._find_mcp(cfg, "glean").url == "https://acme-be.glean.com/mcp/default"


def test_declining_to_connect_now_still_leaves_it_set_up(configured, monkeypatch):
    """Answering no to the browser prompt must not throw the configuration away."""
    cfg, store = configured
    _stub_prompts(monkeypatch, picked=["mcp:glean"])
    wizard._sources_step(cfg, store.root)
    srv = wizard._find_mcp(cfg, "glean")
    assert srv is not None and srv.enabled is True and srv.url


def test_unticking_glean_switches_it_off_without_losing_it(configured, monkeypatch):
    cfg, store = configured
    _stub_prompts(monkeypatch, picked=["mcp:glean"], domain="acme-be.glean.com")
    wizard._sources_step(cfg, store.root)
    assert wizard._find_mcp(cfg, "glean").enabled is True

    _stub_prompts(monkeypatch, picked=[])  # glean unticked
    wizard._sources_step(cfg, store.root)
    srv = wizard._find_mcp(cfg, "glean")
    assert srv.enabled is False
    assert srv.url == "https://acme-be.glean.com/mcp/default", "settings survive being switched off"
    assert len([s for s in cfg.mcp_servers if s.name == "glean"]) == 1, "no duplicate entry"


def test_rerun_does_not_duplicate_glean(configured, monkeypatch):
    cfg, store = configured
    for _ in range(3):
        _stub_prompts(monkeypatch, picked=["mcp:glean"], domain="acme-be.glean.com")
        wizard._sources_step(cfg, store.root)
    assert len([s for s in cfg.mcp_servers if s.name == "glean"]) == 1
