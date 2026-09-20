"""Durable storage for MCP OAuth tokens.

Without this, `fastmcp`'s OAuth keeps its tokens in memory: every process start reopens a
browser, which makes a scheduled sweep impossible. Tokens live in one 0600 JSON file beside
`.env` in the vault — the same place the Google token already sits — so the daemon can read
them unattended and `nbrain disconnect` can delete them.

The file is a secret. It is never rendered in the web UI, never logged, and only ever
summarised as "connected / expires in N" by `connect.py`."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILENAME = "oauth.json"


def token_file(vault: Path) -> Path:
    return vault / "nbrain" / FILENAME


class FileTokenStore:
    """An `AsyncKeyValue` (py-key-value) backed by a single JSON file.

    Deliberately not async under the hood: the file holds a handful of small records, and a
    real async store would drag in a dependency to save microseconds on a once-a-day flow."""

    def __init__(self, path: Path):
        self.path = path

    # ----- file -----
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            # A corrupt token file must not break the sweep; the worst case is re-authorising.
            log.warning("ignoring unreadable %s: %s", self.path, err)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash cannot leave a half-written token file behind.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".oauth-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _slot(collection: str | None, key: str) -> str:
        return f"{collection or 'default'}\x1f{key}"

    def _live(self, entry: Any) -> tuple[dict[str, Any] | None, float | None]:
        """The stored value and its remaining ttl, or (None, None) once it has expired."""
        if not isinstance(entry, dict) or "value" not in entry:
            return None, None
        expires = entry.get("expires_at")
        if expires is None:
            return entry["value"], None
        remaining = float(expires) - time.time()
        if remaining <= 0:
            return None, None
        return entry["value"], remaining

    # ----- AsyncKeyValue -----
    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        return self._live(self._read().get(self._slot(collection, key)))[0]

    async def ttl(self, key: str, *, collection: str | None = None) -> tuple[dict[str, Any] | None, float | None]:
        return self._live(self._read().get(self._slot(collection, key)))

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: Any | None = None,
    ) -> None:
        data = self._read()
        entry: dict[str, Any] = {"value": dict(value)}
        if ttl is not None:
            entry["expires_at"] = time.time() + float(ttl)
        data[self._slot(collection, key)] = entry
        self._write(data)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        data = self._read()
        if data.pop(self._slot(collection, key), None) is None:
            return False
        self._write(data)
        return True

    async def get_many(self, keys: list[str], *, collection: str | None = None) -> list[dict[str, Any] | None]:
        data = self._read()
        return [self._live(data.get(self._slot(collection, k)))[0] for k in keys]

    async def ttl_many(
        self, keys: list[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        data = self._read()
        return [self._live(data.get(self._slot(collection, k))) for k in keys]

    async def put_many(
        self,
        keys: list[str],
        values: list[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: Any | None = None,
    ) -> None:
        data = self._read()
        for key, value in zip(keys, values, strict=True):
            entry: dict[str, Any] = {"value": dict(value)}
            if ttl is not None:
                entry["expires_at"] = time.time() + float(ttl)
            data[self._slot(collection, key)] = entry
        self._write(data)

    async def delete_many(self, keys: list[str], *, collection: str | None = None) -> int:
        data = self._read()
        gone = sum(data.pop(self._slot(collection, k), None) is not None for k in keys)
        if gone:
            self._write(data)
        return gone

    # ----- nbrain -----
    # fastmcp keys every record as "<server url>/<what>" across three collections.
    TOKENS = ("mcp-oauth-token", "tokens")
    CLIENT = ("mcp-oauth-client-info", "client_info")
    EXPIRY = ("mcp-oauth-token-expiry", "token_expiry")

    def _record(self, server_url: str, what: tuple[str, str]) -> dict[str, Any] | None:
        collection, suffix = what
        slot = self._slot(collection, f"{server_url.rstrip('/')}/{suffix}")
        return self._live(self._read().get(slot))[0]

    def peek(self, collection: str, key: str) -> dict[str, Any] | None:
        """One record by collection and key, for callers outside fastmcp's own layout."""
        return self._live(self._read().get(self._slot(collection, key)))[0]

    def holds(self, server_url: str) -> bool:
        """True when a usable token is stored for this server."""
        return self._record(server_url, self.TOKENS) is not None

    def expires_at(self, server_url: str) -> float | None:
        """Unix time the access token goes stale, if the server said. Refresh may extend it."""
        rec = self._record(server_url, self.EXPIRY)
        value = rec.get("expires_at") if rec else None
        return float(value) if isinstance(value, int | float) else None

    def forget(self, server_url: str) -> bool:
        """Drop every record for one server, so the next connect starts clean."""
        data = self._read()
        base = server_url.rstrip("/")
        keep = {slot: v for slot, v in data.items() if not _matches_server(slot, base)}
        if len(keep) == len(data):
            return False
        self._write(keep)
        return True


def _matches_server(slot: str, base: str) -> bool:
    _, _, key = slot.partition("\x1f")
    return key == base or key.startswith(f"{base}/")
