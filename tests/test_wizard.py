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
