"""OAuth for the Google Workspace sources.

Read-only by construction: every source scope is a `.readonly` scope. The only write-capable
scopes (`gmail.compose`, `gmail.send`) are requested solely when the corresponding delivery
channel is enabled in config, and delivery is implemented elsewhere. Nothing in this package
ever calls a mutating Google API."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nbrain.config.schema import Config
from nbrain.sources.base import SourceStatus

log = logging.getLogger(__name__)

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
CHAT_MESSAGES_READONLY = "https://www.googleapis.com/auth/chat.messages.readonly"
CHAT_SPACES_READONLY = "https://www.googleapis.com/auth/chat.spaces.readonly"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DOCS_READONLY = "https://www.googleapis.com/auth/documents.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"

WRITE_SCOPES = frozenset({GMAIL_COMPOSE, GMAIL_SEND})


class GoogleAuthError(RuntimeError):
    """Raised when no usable Google token exists and we may not prompt for one."""


def required_scopes(cfg: Config) -> list[str]:
    """Scopes for the enabled sub-sources. Read-only unless a Gmail delivery channel is on."""
    g = cfg.sources.google
    scopes: list[str] = []
    if g.gmail:
        scopes.append(GMAIL_READONLY)
    if g.calendar:
        scopes.append(CALENDAR_READONLY)
    if g.chat:
        scopes += [CHAT_MESSAGES_READONLY, CHAT_SPACES_READONLY]
    if g.drive_notes:
        scopes += [DRIVE_READONLY, DOCS_READONLY]
    # Deliberate: write scopes only when the user has explicitly enabled a delivery channel.
    if cfg.delivery.gmail_draft.enabled:
        scopes.append(GMAIL_COMPOSE)
    if cfg.delivery.gmail_send.enabled:
        scopes.append(GMAIL_SEND)
    return scopes


def http_status(err: BaseException) -> int | None:
    """Status code of a googleapiclient HttpError, or None for anything else."""
    resp = getattr(err, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(err, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class GoogleAuth:
    """Holds one set of user credentials shared by all Google sub-sources."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.credentials_path: Path = cfg.nbrain_dir / cfg.sources.google.credentials_file
        self.token_path: Path = cfg.nbrain_dir / cfg.sources.google.token_file
        self.scopes: list[str] = required_scopes(cfg)
        self._creds: Any = None
        self._services: dict[tuple[str, str], Any] = {}

    # ---------- token file ----------

    def _stored_token(self) -> dict[str, Any] | None:
        if not self.token_path.exists():
            return None
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError) as e:
            log.warning("Unreadable Google token at %s: %s", self.token_path, e)
            return None

    def _stored_scopes(self) -> set[str]:
        data = self._stored_token() or {}
        raw = data.get("scopes") or []
        if isinstance(raw, str):
            raw = raw.split()
        return {str(s) for s in raw}

    def _save_token(self, creds: Any) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    # ---------- credentials ----------

    def credentials(self, interactive: bool = False) -> Any:
        """Return valid google.oauth2 credentials, refreshing or (if allowed) re-authorising."""
        if self._creds is not None and getattr(self._creds, "valid", False):
            return self._creds

        if not self.scopes:
            # Google rejects a scope-less authorisation URL with a bare
            # "Error 400: invalid_request — Missing required parameter: scope", which names
            # neither nbrain nor the setting at fault. Fail here instead, with the fix.
            raise GoogleAuthError(
                "Google is enabled but every sub-source is switched off, so there are no scopes "
                "to request. Turn at least one on, for example:\n"
                "  nbrain config set sources.google.gmail true\n"
                "  nbrain config set sources.google.calendar true\n"
                "  nbrain config set sources.google.chat true\n"
                "  nbrain config set sources.google.drive_notes true"
            )

        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds: Any = None
        stored = self._stored_token()
        if stored is not None:
            missing = set(self.scopes) - self._stored_scopes()
            if missing:
                log.info("Google token lacks scopes %s; re-authorisation needed", sorted(missing))
            else:
                try:
                    creds = Credentials.from_authorized_user_info(stored, self.scopes)
                except ValueError as e:
                    log.warning("Google token unusable (%s); re-authorisation needed", e)

        if creds is not None and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
            except RefreshError as e:
                log.warning("Google token refresh failed: %s", e)
                creds = None

        if creds is not None and creds.valid:
            self._creds = creds
            return creds

        if not interactive:
            raise GoogleAuthError(
                "No valid Google token for the required scopes. Run `nbrain auth google` "
                "to authorise (read-only scopes unless Gmail delivery is enabled)."
            )
        if not self.credentials_path.exists():
            raise GoogleAuthError(
                f"OAuth client file not found at {self.credentials_path}. Download an OAuth "
                "'Desktop app' client from Google Cloud Console and save it there."
            )

        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), self.scopes)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        self._creds = creds
        self._services.clear()
        return creds

    # ---------- API clients ----------

    def service(self, api: str, version: str) -> Any:
        """A cached discovery client, e.g. service('gmail', 'v1')."""
        key = (api, version)
        svc = self._services.get(key)
        if svc is None:
            from googleapiclient.discovery import build

            svc = build(api, version, credentials=self.credentials(), cache_discovery=False)
            self._services[key] = svc
        return svc

    # ---------- status ----------

    def status(self) -> SourceStatus:
        stored = self._stored_token()
        now = datetime.now(UTC)
        if stored is None:
            return SourceStatus(
                name="google",
                ok=False,
                detail=f"no token at {self.token_path}; run `nbrain auth google`",
                checked_at=now,
            )
        have = self._stored_scopes()
        missing = sorted(set(self.scopes) - have)
        write_capable = bool(have & WRITE_SCOPES)
        if missing:
            detail = "token missing scopes: " + ", ".join(s.rsplit("/", 1)[-1] for s in missing)
            ok = False
        else:
            detail = "token scopes: " + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in have))
            ok = True
        if write_capable:
            detail += " (write-capable: gmail compose/send granted for delivery)"
        return SourceStatus(
            name="google", ok=ok, detail=detail, checked_at=now, write_capable=write_capable
        )
