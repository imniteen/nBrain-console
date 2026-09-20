"""Run one connector's consent flow in the background so the page stays responsive.

A Connect can take minutes: the browser opens, the user signs in through SSO, an admin
approval may be waiting. Holding an HTTP request open for that would time out in the
browser, so the POST starts a thread and returns immediately; the page polls for the result."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict
from typing import Any

from nbrain.config.loader import load_config
from nbrain.mcp import catalog, connect

log = logging.getLogger(__name__)


class ConnectRunner:
    """One connect at a time. `start` is non-blocking; poll `status()`."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self.current: str | None = None
        self.result: dict[str, Any] | None = None
        self.auth_url: str | None = None  # shown as a fallback link if the browser stays shut
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, vault, key: str, *, action: str = "connect") -> bool:  # noqa: ANN001
        with self._guard:
            if self.running:
                return False
            self.current, self.result, self.auth_url = key, None, None
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(vault, key, action), name=f"connect-{key}", daemon=True
            )
            self._thread.start()
            return True

    def _run(self, vault, key: str, action: str) -> None:  # noqa: ANN001
        try:
            # Reload rather than share the request's config: the flow writes headers_env back.
            cfg = load_config(vault)
            server = connect.server_for(cfg, key)
            if action == "disconnect":
                asyncio.run(connect.disconnect(cfg, server))
                status = connect.status(cfg, server)
            else:
                status = asyncio.run(
                    connect.connect(cfg, server, on_url=self._remember, cancel=self._cancel)
                )
            self.result = {"key": key, "action": action, "ok": True, "status": asdict(status)}
        except Exception as err:  # noqa: BLE001 - every failure belongs on the page, not in a 500
            log.warning("connector %s failed to %s: %s", key, action, err)
            self.result = {"key": key, "action": action, "ok": False, "error": str(err)}
        finally:
            self.current, self.auth_url = None, None

    def cancel(self) -> bool:
        """Stop waiting on the provider. Nothing is written, so a retry starts clean."""
        if not self.running:
            return False
        self._cancel.set()
        return True

    def _remember(self, url: str) -> None:
        self.auth_url = url

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "current": self.current,
            "auth_url": self.auth_url,
            "result": self.result,
        }


def rows(cfg) -> list[dict[str, Any]]:  # noqa: ANN001
    """One row per catalogue connector, configured or not, for the Sources page."""
    out: list[dict[str, Any]] = []
    for conn in catalog.CONNECTORS.values():
        server = catalog.find(cfg, conn.key)
        status = connect.status(cfg, server) if server else connect.Status(name=conn.key, label=conn.label, style=conn.auth)
        out.append(
            {
                "conn": conn,
                "server": server,
                "status": status,
                "configured": server is not None,
                "enabled": bool(server and server.enabled),
                "domain": _domain(server.url) if server and server.url else "",
            }
        )
    return out


def _domain(url: str) -> str:
    return url.split("://", 1)[-1].split("/")[0]
