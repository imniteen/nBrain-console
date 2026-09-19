from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from nbrain.config.schema import Config
from nbrain.scheduler.daemon import SweepLock, needs_catch_up
from nbrain.sweep.metrics import detect_imbalance
from nbrain.vault.schema import Project
from nbrain.vault.store import VaultStore


def test_needs_catch_up(vault: VaultStore, cfg: Config):
    tz = ZoneInfo(cfg.user.timezone)
    cfg.sweep.daily_time = "08:15"
    wed = datetime(2026, 9, 16, 9, 0, tzinfo=tz)  # Wednesday, 45 min after due
    assert needs_catch_up(cfg, wed)
    (vault.root / "Briefs" / "2026-09-16.md").write_text("done")
    assert not needs_catch_up(cfg, wed)
    assert not needs_catch_up(cfg, datetime(2026, 9, 17, 7, 0, tzinfo=tz))  # before due
    assert not needs_catch_up(cfg, datetime(2026, 9, 17, 20, 0, tzinfo=tz))  # past grace
    assert not needs_catch_up(cfg, datetime(2026, 9, 19, 9, 0, tzinfo=tz))  # Saturday


def test_sweep_lock(vault: VaultStore):
    lock = SweepLock(vault.root)
    assert lock.acquire()
    assert not SweepLock(vault.root).acquire()
    lock.release()
    assert SweepLock(vault.root).acquire()


def test_imbalance_only_for_equal_weights(vault: VaultStore):
    vault.save(Project(title="A", weight=1.0))
    vault.save(Project(title="B", weight=1.0))
    assert detect_imbalance({"A": 0.8, "B": 0.2}, vault, 0.7)
    assert not detect_imbalance({"A": 0.6, "B": 0.4}, vault, 0.7)
    assert detect_imbalance({"A": 1.0}, vault, 0.7)  # B starved entirely
    vault.save(Project(title="B", weight=2.0))
    assert not detect_imbalance({"A": 0.8, "B": 0.2}, vault, 0.7)  # weights differ: not an equal-weight alarm


def test_metrics_date_helpers():
    assert date(2026, 9, 17).weekday() == 3
