"""Locate, load and save config.yaml; resolve secrets from env, .env and the OS keyring."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from nbrain.config.schema import Config

POINTER_FILE = Path("~/.config/nbrain/vault_path").expanduser()
DEFAULT_VAULT = Path("~/nbrain-vault").expanduser()
KEYRING_SERVICE = "nbrain"


class ConfigError(RuntimeError):
    pass


def resolve_vault_path(explicit: str | Path | None = None) -> Path:
    """Order: explicit arg, NBRAIN_VAULT env, pointer file, default."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env := os.environ.get("NBRAIN_VAULT"):
        return Path(env).expanduser().resolve()
    if POINTER_FILE.exists():
        text = POINTER_FILE.read_text().strip()
        if text:
            return Path(text).expanduser().resolve()
    return DEFAULT_VAULT.resolve()


def write_pointer(vault: Path) -> None:
    POINTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    POINTER_FILE.write_text(str(vault))


def config_file(vault: Path) -> Path:
    return vault / "nbrain" / "config.yaml"


def load_config(vault: str | Path | None = None, *, must_exist: bool = True) -> Config:
    vault_path = resolve_vault_path(vault)
    path = config_file(vault_path)
    load_dotenv(vault_path / "nbrain" / ".env", override=False)
    if not path.exists():
        if must_exist:
            raise ConfigError(
                f"No config at {path}. Run `nbrain setup` or pass --vault / set NBRAIN_VAULT."
            )
        return Config(vault_path=vault_path)
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml is not valid YAML: {e}") from e
    raw["vault_path"] = str(vault_path)
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config.yaml failed validation:\n{e}") from e


class _QuotedTime(str):
    pass


def quote_times(data: Any) -> None:
    """Mark HH:MM values so the YAML dump quotes them (a bare 8:30 would load as 510)."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and _TIME_LIKE.match(v):
                h, _, rest = v.partition(":")
                data[k] = _QuotedTime(f"{int(h):02d}:{rest}")
            else:
                quote_times(v)
    elif isinstance(data, list):
        for v in data:
            quote_times(v)


yaml.SafeDumper.add_representer(
    _QuotedTime, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style="'")
)


def save_config(cfg: Config) -> Path:
    path = config_file(cfg.vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude={"vault_path"})
    quote_times(data)
    header = (
        "# nbrain configuration. Secrets never go here: reference env var names and put the\n"
        "# values in nbrain/.env (next to this file) or the OS keyring via `nbrain secret set`.\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def get_secret(env_name: str | None) -> str | None:
    """Env var first (includes .env), then keyring.

    Values are stripped: a pasted key almost always picks up a trailing space or CR, and the
    resulting auth failure ("User not found", "invalid api key") never names whitespace."""
    if not env_name:
        return None
    if val := os.environ.get(env_name):
        return val.strip() or None
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, env_name)
        return stored.strip() or None if stored else None
    except Exception:
        return None


def set_secret(env_name: str, value: str, *, use_keyring: bool, vault: Path | None = None) -> str:
    """Store a secret. Returns a human description of where it went."""
    value = value.strip()
    if not value:
        raise ConfigError(f"{env_name}: refusing to store an empty value")
    if "\n" in value or "\r" in value:
        raise ConfigError(
            f"{env_name}: the value contains a line break — it looks like more than one key was "
            "pasted. Copy a single key and try again."
        )
    if use_keyring:
        import keyring

        keyring.set_password(KEYRING_SERVICE, env_name, value)
        return f"keyring ({KEYRING_SERVICE}/{env_name})"
    if vault is None:
        raise ConfigError("vault path required to write .env")
    env_path = vault / "nbrain" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{env_name}=")]
    lines.append(f"{env_name}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    os.environ[env_name] = value
    return str(env_path)


def set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        if p.isdigit() and isinstance(cur, list):
            cur = cur[int(p)]
        else:
            cur = cur.setdefault(p, {})
    last = parts[-1]
    if last.isdigit() and isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def get_dotted(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for p in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(p)]
        else:
            cur = cur[p]
    return cur


_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def coerce_scalar(text: str) -> Any:
    """Parse a CLI value as YAML so `true`, `8`, `[a, b]` become typed values.

    Times like `8:30` stay strings (YAML 1.1 would read them as base-60 integers)."""
    if _TIME_LIKE.match(text.strip()):
        return text.strip()
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text
