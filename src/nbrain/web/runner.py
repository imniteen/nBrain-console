"""Run a daily or weekly sweep in a background thread, guarded by the daemon's SweepLock,
and keep the last 200 log lines in memory so the UI can show progress."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nbrain.config.loader import load_config
from nbrain.scheduler.daemon import SweepLock
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

Kind = Literal["daily", "weekly"]
LOG_LINES = 200


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted log lines."""

    def __init__(self, maxlen: int = LOG_LINES):
        super().__init__(level=logging.INFO)
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self.lines.append(line)

    def tail(self, n: int = LOG_LINES) -> list[str]:
        with self._lock:
            return list(self.lines)[-n:]


class SweepRunner:
    """One sweep at a time per vault. `start` is non-blocking; poll `status()`."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.handler = RingBufferHandler()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self.last_result: dict[str, Any] | None = None
        self.current: dict[str, Any] | None = None
        nb = logging.getLogger("nbrain")
        nb.addHandler(self.handler)
        if nb.getEffectiveLevel() > logging.INFO:
            nb.setLevel(logging.INFO)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: Kind, *, dry_run: bool = False, skip_llm: bool = False) -> bool:
        """Returns False when another sweep (here or in the daemon) holds the lock."""
        with self._guard:
            if self.running:
                return False
            lock = SweepLock(self.vault)
            if not lock.acquire():
                return False
            self.current = {
                "kind": kind,
                "dry_run": dry_run,
                "skip_llm": skip_llm,
                "started": datetime.now().isoformat(timespec="seconds"),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(kind, dry_run, skip_llm, lock),
                name=f"nbrain-web-{kind}",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, kind: Kind, dry_run: bool, skip_llm: bool, lock: SweepLock) -> None:
        t0 = time.monotonic()
        result: dict[str, Any] = dict(self.current or {})
        log.info("%s sweep starting (dry_run=%s, skip_llm=%s)", kind, dry_run, skip_llm)
        try:
            cfg = load_config(self.vault)
            store = VaultStore(cfg.vault)
            if kind == "weekly":
                from nbrain.sweep.weekly import run_weekly

                rep = asyncio.run(run_weekly(cfg, store, skip_llm=skip_llm, deliver=not dry_run))
                log.info("weekly review %s written to %s; warnings=%s", rep.week, rep.path, rep.warnings)
                result.update(
                    ok=True,
                    week=rep.week,
                    path=str(rep.path) if rep.path else None,
                    warnings=list(rep.warnings),
                    deliveries=[{"channel": d.channel, "ok": d.ok} for d in rep.deliveries],
                )
            else:
                from nbrain.sweep.pipeline import Sweep

                rep = asyncio.run(Sweep(cfg, store, dry_run=dry_run, skip_llm=skip_llm).run())
                log.info(
                    "daily sweep done in %.0fs: %d created, %d updated, %d open; not checked=%s; warnings=%s; deliveries=%s",
                    rep.duration_seconds,
                    rep.created,
                    rep.updated,
                    rep.metrics.open_count if rep.metrics else 0,
                    rep.not_checked,
                    rep.warnings,
                    [(d.channel, d.ok) for d in rep.deliveries],
                )
                result.update(
                    ok=True,
                    created=rep.created,
                    updated=rep.updated,
                    not_checked=list(rep.not_checked),
                    warnings=list(rep.warnings),
                    deliveries=[{"channel": d.channel, "ok": d.ok} for d in rep.deliveries],
                    open_items=rep.metrics.open_count if rep.metrics else None,
                    md_path=str(rep.md_path) if rep.md_path else None,
                )
        except Exception as e:  # noqa: BLE001 - surface in the UI, never crash the server
            log.exception("%s sweep failed", kind)
            result.update(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            lock.release()
            result["duration_seconds"] = round(time.monotonic() - t0, 1)
            result["finished"] = datetime.now().isoformat(timespec="seconds")
            self.last_result = result
            self.current = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "current": self.current,
            "last_result": self.last_result,
            "log_tail": self.handler.tail(),
        }
