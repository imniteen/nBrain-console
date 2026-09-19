"""Google Workspace sources (Gmail, Calendar, Chat, Drive meeting notes). Read-only."""

from __future__ import annotations

from nbrain.config.schema import Config
from nbrain.sources.base import BaseSource
from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, required_scopes
from nbrain.sources.google.calendar import CalendarSource
from nbrain.sources.google.chat import ChatSource
from nbrain.sources.google.drive_notes import DriveNotesSource
from nbrain.sources.google.gmail import GmailSource
from nbrain.vault.store import VaultStore

__all__ = [
    "CalendarSource",
    "ChatSource",
    "DriveNotesSource",
    "GmailSource",
    "GoogleAuth",
    "GoogleAuthError",
    "build_google_sources",
    "required_scopes",
]


def build_google_sources(cfg: Config, store: VaultStore) -> list[BaseSource]:
    """Enabled Google sub-sources sharing one GoogleAuth; empty when Google is disabled."""
    g = cfg.sources.google
    if not g.enabled:
        return []
    auth = GoogleAuth(cfg)
    sources: list[BaseSource] = []
    if g.gmail:
        sources.append(GmailSource(cfg, store, auth))
    if g.calendar:
        sources.append(CalendarSource(cfg, store, auth))
    if g.chat:
        sources.append(ChatSource(cfg, store, auth))
    if g.drive_notes:
        sources.append(DriveNotesSource(cfg, store, auth))
    return sources
