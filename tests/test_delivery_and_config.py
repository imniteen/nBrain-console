from __future__ import annotations

import base64
from datetime import date
from email import message_from_bytes

import pytest

from nbrain.config.loader import coerce_scalar, get_dotted, load_config, save_config, set_dotted
from nbrain.config.schema import Config, MCPServerConfig
from nbrain.delivery.base import RenderedBrief
from nbrain.delivery.gmail import GmailDeliverer
from nbrain.delivery.slack_dm import md_to_mrkdwn
from nbrain.vault.store import VaultStore


def test_gmail_message_addresses_only_me(cfg: Config):
    cfg.delivery.gmail_draft.enabled = True
    d = GmailDeliverer(cfg, mode="draft")
    raw = d._message(RenderedBrief(date(2026, 9, 17), "[nbrain] Daily — 2026-09-17", "# hi", "<b>hi</b>"))["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "test@example.com" and msg["From"] == "test@example.com"
    assert msg["Cc"] is None and msg["Bcc"] is None
    parts = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in parts and "text/html" in parts


def test_gmail_refuses_empty_recipient(cfg: Config):
    cfg.user.email = ""
    with pytest.raises(RuntimeError):
        GmailDeliverer(cfg, mode="send")._message(RenderedBrief(date(2026, 9, 17), "s", "m", None))


def test_md_to_mrkdwn():
    md = "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n**bold** and [link](https://x.y) and [[note]]\n> [!important] One thing\n---\n"
    out = md_to_mrkdwn(md)
    assert "*Title*" in out and "*bold*" in out and "<https://x.y|link>" in out and "note" in out
    assert "|---|" not in out and "[!important]" not in out


def test_config_round_trip_and_dotted(vault: VaultStore, cfg: Config):
    cfg.sweep.daily_time = "07:45"
    cfg.mcp_servers.append(MCPServerConfig(name="x", transport="http", url="https://mcp.example/mcp", roles=["tickets"]))
    path = save_config(cfg)
    text = path.read_text()
    assert "api_key" not in text.lower().replace("api_key_env", "")  # secrets never land in config.yaml
    loaded = load_config(vault.root)
    assert loaded.sweep.daily_time == "07:45"
    assert loaded.mcp_servers[0].url == "https://mcp.example/mcp"
    assert loaded.vault == vault.root
    data = loaded.model_dump(mode="json")
    set_dotted(data, "sweep.weekly.day", coerce_scalar("thu"))
    set_dotted(data, "mcp_servers.0.roles", coerce_scalar("[code, tickets]"))
    new = Config.model_validate(data)
    assert new.sweep.weekly.day == "thu" and new.mcp_servers[0].roles == ["code", "tickets"]
    assert get_dotted(data, "sweep.weekly.day") == "thu"
    assert coerce_scalar("true") is True and coerce_scalar("8") == 8 and coerce_scalar("08:30") == "08:30" and coerce_scalar("8:30") == "8:30"


def test_times_survive_yaml_round_trip(vault: VaultStore, cfg: Config):
    """A bare 8:30 in YAML 1.1 is the number 510; times must stay strings both ways."""
    cfg.sweep.daily_time = "8:30"
    cfg.sweep.weekly.time = "16:00"
    cfg.user.working_hours.start = "9:00"
    path = save_config(cfg)
    text = path.read_text()
    assert "daily_time: '08:30'" in text and "start: '09:00'" in text
    loaded = load_config(vault.root)
    assert loaded.sweep.daily_time == "08:30" and loaded.user.working_hours.start == "09:00"
    # a hand-edited bare time that YAML already turned into an int is recovered
    path.write_text(text.replace("daily_time: '08:30'", "daily_time: 8:30"))
    assert load_config(vault.root).sweep.daily_time == "08:30"


def test_secret_round_trip_via_dotenv(vault: VaultStore, cfg: Config, monkeypatch):
    """The path `nbrain secret NAME` uses: write to nbrain/.env, resolve through get_secret."""
    from nbrain.config.loader import get_secret, set_secret

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    save_config(cfg)
    where = set_secret("OPENROUTER_API_KEY", "sk-or-test", use_keyring=False, vault=vault.root)
    env_path = vault.root / "nbrain" / ".env"
    assert str(env_path) == where
    assert oct(env_path.stat().st_mode)[-3:] == "600"  # not world readable
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-test"
    # replacing a value must not leave the old one behind
    set_secret("OPENROUTER_API_KEY", "sk-or-second", use_keyring=False, vault=vault.root)
    assert env_path.read_text().count("OPENROUTER_API_KEY=") == 1
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-second"
    # the secret never reaches config.yaml
    assert "sk-or-" not in (vault.root / "nbrain" / "config.yaml").read_text()
    assert get_secret("NOT_SET_ANYWHERE") is None


def test_secret_whitespace_is_stripped(vault: VaultStore, cfg: Config, monkeypatch):
    """A pasted key usually carries a trailing space or CR; the provider then answers 401
    without ever mentioning whitespace, so strip on the way in and on the way out."""
    from nbrain.config.loader import ConfigError, get_secret, set_secret

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    save_config(cfg)
    set_secret("OPENROUTER_API_KEY", "  sk-or-v1-abc123 \t", use_keyring=False, vault=vault.root)
    stored = (vault.root / "nbrain" / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-v1-abc123\n" in stored
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-v1-abc123"

    # a whitespace-padded value already sitting in the environment is cleaned on read
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fromenv\r")
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-v1-fromenv"

    with pytest.raises(ConfigError):
        set_secret("OPENROUTER_API_KEY", "   ", use_keyring=False, vault=vault.root)
    with pytest.raises(ConfigError):  # two keys pasted at once
        set_secret("OPENROUTER_API_KEY", "sk-or-v1-one\nsk-or-v1-two", use_keyring=False, vault=vault.root)


def test_portal_only_links_do_not_leak_into_deliveries():
    """`[[id|details]]` opens the assist drawer in the portal; elsewhere it is noise."""
    from nbrain.brief.build import to_plain_text

    md = "1. Reply to Mohit [[20260917-reply-to-mohit|details]]\n2. Chase the build\n"
    plain = to_plain_text(md)
    assert "[[" not in plain and "20260917" not in plain
    assert "Reply to Mohit" in plain and "Chase the build" in plain

    mrkdwn = md_to_mrkdwn(md)
    assert "[[" not in mrkdwn and "20260917" not in mrkdwn


def test_gmail_plain_part_is_clean(cfg: Config):
    import base64
    from email import message_from_bytes

    cfg.delivery.gmail_draft.enabled = True
    d = GmailDeliverer(cfg, mode="draft")
    md = "# Brief\n\n1. Reply to Mohit [[20260917-reply|details]]\n"
    raw = d._message(RenderedBrief(date(2026, 9, 17), "s", md, "<b>x</b>"))["raw"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    plain = next(p for p in msg.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    assert "[[" not in plain and "20260917" not in plain
