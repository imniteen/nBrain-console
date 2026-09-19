"""In-process scheduler: daily and weekly sweeps as cron jobs in the user's timezone, a lock
file so runs never overlap, catch-up for a missed run after sleep, and the optional web UI."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from nbrain.config.loader import load_config
from nbrain.config.schema import Config
from nbrain.util import hhmm
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class SweepLock:
    def __init__(self, vault: Path):
        self.path = vault / "nbrain" / "sweep.lock"

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = datetime.now().timestamp() - self.path.stat().st_mtime
            except FileNotFoundError:
                return self.acquire()
            if age > 3 * 3600:  # stale lock from a dead run
                self.path.unlink(missing_ok=True)
                return self.acquire()
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


def run_daily_job(vault: Path) -> None:
    """Reload config every run so settings changes apply without a restart."""
    cfg = load_config(vault)
    store = VaultStore(cfg.vault)
    lock = SweepLock(cfg.vault)
    if not lock.acquire():
        log.warning("Another sweep is running; skipping this tick")
        return
    try:
        from nbrain.sweep.pipeline import Sweep

        rep = asyncio.run(Sweep(cfg, store).run())
        log.info(
            "Daily sweep done in %.0fs: %d new, %d open, deliveries=%s, warnings=%s",
            rep.duration_seconds,
            rep.created,
            rep.metrics.open_count if rep.metrics else 0,
            [(d.channel, d.ok) for d in rep.deliveries],
            rep.warnings,
        )
    except Exception:
        log.exception("Daily sweep failed")
    finally:
        lock.release()


def run_weekly_job(vault: Path) -> None:
    cfg = load_config(vault)
    store = VaultStore(cfg.vault)
    lock = SweepLock(cfg.vault)
    if not lock.acquire():
        log.warning("Another sweep is running; skipping weekly tick")
        return
    try:
        from nbrain.sweep.weekly import run_weekly

        rep = asyncio.run(run_weekly(cfg, store))
        log.info("Weekly review written to %s", rep.path)
    except Exception:
        log.exception("Weekly review failed")
    finally:
        lock.release()


def needs_catch_up(cfg: Config, now: datetime) -> bool:
    """True when today's run was due earlier, within the grace window, and no brief exists yet."""
    if cfg.sweep.weekdays_only and now.weekday() >= 5:
        return False
    h, m = hhmm(cfg.sweep.daily_time)
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < due:
        return False
    if (now - due).total_seconds() > cfg.sweep.missed_run_grace_hours * 3600:
        return False
    return not (cfg.vault / "Briefs" / f"{now.date().isoformat()}.md").exists()


def start_web_thread(cfg: Config) -> threading.Thread | None:
    if not cfg.web.enabled:
        return None
    try:
        from nbrain.web.app import run_server_in_thread
    except ImportError as e:  # web UI optional
        log.warning("Web UI unavailable: %s", e)
        return None
    return run_server_in_thread(cfg)


def run_daemon(vault: Path, *, with_web: bool = True) -> None:
    cfg = load_config(vault)
    tz = ZoneInfo(cfg.user.timezone)
    sched = BlockingScheduler(timezone=tz)
    h, m = hhmm(cfg.sweep.daily_time)
    dow = "mon-fri" if cfg.sweep.weekdays_only else "*"
    sched.add_job(run_daily_job, CronTrigger(day_of_week=dow, hour=h, minute=m, timezone=tz), args=[cfg.vault], id="daily", misfire_grace_time=cfg.sweep.missed_run_grace_hours * 3600, coalesce=True)
    if cfg.sweep.weekly.enabled:
        wh, wm = hhmm(cfg.sweep.weekly.time)
        sched.add_job(run_weekly_job, CronTrigger(day_of_week=cfg.sweep.weekly.day, hour=wh, minute=wm, timezone=tz), args=[cfg.vault], id="weekly", misfire_grace_time=6 * 3600, coalesce=True)
    log.info("nbrain daemon: daily %s (%s), weekly %s %s, tz %s", cfg.sweep.daily_time, dow, cfg.sweep.weekly.day, cfg.sweep.weekly.time, cfg.user.timezone)
    if with_web:
        start_web_thread(cfg)
    if needs_catch_up(cfg, datetime.now(tz)):
        log.info("Missed today's run; catching up now")
        sched.add_job(run_daily_job, args=[cfg.vault], id="catch-up")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("daemon stopped")


# ---------- launchd ----------

LABEL = "ai.nbrain.daemon"


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def nbrain_executable() -> str:
    candidate = Path(sys.executable).parent / "nbrain"
    return str(candidate) if candidate.exists() else "nbrain"


def write_launchd_plist(cfg: Config) -> Path:
    logs = cfg.nbrain_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{nbrain_executable()}</string><string>daemon</string><string>--vault</string><string>{cfg.vault}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")}</string>
    <key>HOME</key><string>{Path.home()}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logs / "daemon.log"}</string>
  <key>StandardErrorPath</key><string>{logs / "daemon.err.log"}</string>
</dict></plist>
"""
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist)
    return path


def launchctl(action: str) -> tuple[bool, str]:
    uid = os.getuid()
    path = plist_path()
    if action == "load":
        cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(path)]
    elif action == "unload":
        cmd = ["launchctl", "bootout", f"gui/{uid}/{LABEL}"]
    else:
        cmd = ["launchctl", "print", f"gui/{uid}/{LABEL}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out[:400]
