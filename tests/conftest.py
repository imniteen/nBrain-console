from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nbrain.config.schema import Config
from nbrain.vault.store import VaultStore


@pytest.fixture
def vault(tmp_path: Path) -> VaultStore:
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    return store


@pytest.fixture
def cfg(vault: VaultStore) -> Config:
    c = Config(vault_path=vault.root)
    c.user.name = "Test User"
    c.user.email = "test@example.com"
    c.user.timezone = "Europe/London"
    return c


@pytest.fixture
def today() -> date:
    return date(2026, 9, 17)
