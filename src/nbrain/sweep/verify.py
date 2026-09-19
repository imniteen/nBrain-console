"""Rule 7: never carry an item forward unverified. Re-check every open item at its source."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

from nbrain.sources.base import Source
from nbrain.vault.schema import Item, ItemStatus
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)


@dataclass
class VerifySummary:
    live: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retractions: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("live", "resolved", "unconfirmed", "watch", "errors")}


async def verify_open_items(
    store: VaultStore,
    sources: dict[str, Source],
    today: date,
    *,
    drop_after: int,
    timeout: float = 30.0,
    concurrency: int = 5,
) -> VerifySummary:
    summary = VerifySummary()
    sem = asyncio.Semaphore(concurrency)
    items = [i for i in store.items(ItemStatus.open, ItemStatus.watch)]

    async def one(item: Item) -> None:
        src = sources.get(item.source)
        if src is None or not getattr(src, "verifiable", False):
            outcome = store.mark_unconfirmed(item, today, drop_after)
            (summary.watch if outcome == "watch" else summary.unconfirmed).append(item.id)
            return
        async with sem:
            try:
                res = await asyncio.wait_for(src.verify(item), timeout)
            except Exception as e:  # noqa: BLE001
                log.warning("verify %s via %s failed: %s", item.id, item.source, e)
                summary.errors.append(f"{item.id}: {e}")
                outcome = store.mark_unconfirmed(item, today, drop_after)
                (summary.watch if outcome == "watch" else summary.unconfirmed).append(item.id)
                return
        if res.state == "resolved":
            store.resolve_item(item, today, res.note or "Verified resolved at the source.")
            summary.resolved.append(item.id)
            if item.status == ItemStatus.watch or (item.first_seen == today):
                summary.retractions.append(f"{item.title}: {res.note or 'already resolved'}")
        elif res.state == "open":
            if item.status == ItemStatus.watch:
                item.status = ItemStatus.open  # source says it is real again
            store.mark_live(item, today)
            summary.live.append(item.id)
        else:
            outcome = store.mark_unconfirmed(item, today, drop_after)
            (summary.watch if outcome == "watch" else summary.unconfirmed).append(item.id)

    await asyncio.gather(*(one(i) for i in items))
    return summary
