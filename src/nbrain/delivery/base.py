"""Delivery channels. The vault files are always written by the store; channels are extras
the user toggles in config. A channel failure is reported, never fatal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from nbrain.config.schema import Config
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)


@dataclass
class RenderedBrief:
    day: date
    subject: str
    markdown: str
    html: str | None
    md_path: Path | None = None
    html_path: Path | None = None
    kind: str = "daily"


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""


class Deliverer(Protocol):
    name: str

    async def deliver(self, brief: RenderedBrief) -> DeliveryResult: ...


class FileDeliverer:
    name = "file"

    async def deliver(self, brief: RenderedBrief) -> DeliveryResult:
        parts = [str(p) for p in (brief.md_path, brief.html_path) if p]
        return DeliveryResult(self.name, bool(parts), ", ".join(parts) or "no files written")


def build_deliverers(cfg: Config, store: VaultStore) -> list[Deliverer]:
    out: list[Deliverer] = []
    if cfg.delivery.file.enabled:
        out.append(FileDeliverer())
    if cfg.delivery.gmail_draft.enabled or cfg.delivery.gmail_send.enabled:
        from nbrain.delivery.gmail import GmailDeliverer

        if cfg.delivery.gmail_draft.enabled:
            out.append(GmailDeliverer(cfg, mode="draft"))
        if cfg.delivery.gmail_send.enabled:
            out.append(GmailDeliverer(cfg, mode="send"))
    if cfg.delivery.slack_dm.enabled:
        from nbrain.delivery.slack_dm import SlackDMDeliverer

        out.append(SlackDMDeliverer(cfg))
    return out


async def deliver_all(deliverers: list[Deliverer], brief: RenderedBrief) -> list[DeliveryResult]:
    results: list[DeliveryResult] = []
    for d in deliverers:
        try:
            results.append(await d.deliver(brief))
        except Exception as e:  # noqa: BLE001
            log.warning("delivery via %s failed: %s", d.name, e)
            results.append(DeliveryResult(d.name, False, str(e)[:200]))
    return results
