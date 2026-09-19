"""Deterministic numbers for the brief. Computed from frontmatter, never by the model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from nbrain.config.schema import Config
from nbrain.util import unlink
from nbrain.vault.schema import Item, ItemStatus, ItemType
from nbrain.vault.store import VaultStore


@dataclass
class Changed:
    new: list[Item] = field(default_factory=list)
    moved: list[tuple[Item, str]] = field(default_factory=list)
    cleared: list[Item] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.new or self.moved or self.cleared)


@dataclass
class Metrics:
    due_today: list[Item]
    overdue: list[Item]
    waiting_on_me: list[Item]
    waiting_on_them: list[Item]
    slipping: list[Item]
    commitments_open: list[Item]
    watch: list[Item]
    oldest_days: int | None
    delivered: int
    total_commitments: int
    priority_split: dict[str, float]
    imbalance: bool
    imbalance_runs: int
    changed: Changed
    open_count: int

    @property
    def delivery_score(self) -> str:
        return f"{self.delivered}/{self.total_commitments}" if self.total_commitments else "–"

    @property
    def urgent(self) -> list[Item]:
        seen: set[str] = set()
        out: list[Item] = []
        for i in self.overdue + self.due_today:
            if i.id not in seen:
                seen.add(i.id)
                out.append(i)
        return out


def _sort_key(today: date):  # type: ignore[no-untyped-def]
    return lambda i: (i.priority, i.due or date.max, i.first_seen or today)


def priority_split(store: VaultStore, today: date) -> dict[str, float]:
    """Share of today's activity per project, from items seen or changed today."""
    counts: dict[str, int] = {}
    for it in store.items(ItemStatus.open, ItemStatus.resolved, ItemStatus.watch):
        touched = it.last_seen == today or it.first_seen == today or it.resolved_on == today
        if not touched:
            continue
        name = unlink(it.project)
        if name:
            counts[name] = counts.get(name, 0) + 1
    total = sum(counts.values())
    if not total:
        return {}
    return {k: round(v / total, 3) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])}


def detect_imbalance(split: dict[str, float], store: VaultStore, ratio: float) -> bool:
    """True when equally weighted projects are split worse than ratio (e.g. 70/30)."""
    projects = {p.title: p.weight for p in store.projects()}
    weighted = [(name, projects.get(name, 1.0)) for name in split]
    if len(weighted) < 2 and len(projects) >= 2:
        # only one project got any attention while another equal-weight project exists
        others = [n for n in projects if n not in split]
        if others and split and max(split.values()) >= ratio:
            return True
    if len(split) < 2:
        return False
    top = max(split.values())
    weights = {w for _, w in weighted}
    return len(weights) == 1 and top >= ratio


def previous_imbalance_runs(store: VaultStore, before: date) -> int:
    from nbrain.vault.schema import SweepLog

    logs = sorted(store.load_all(SweepLog), key=lambda s: s.id, reverse=True)
    n = 0
    for lg in logs:
        if lg.kind != "daily" or lg.id >= before.isoformat():
            continue
        if getattr(lg, "imbalance", False):
            n += 1
        else:
            break
    return n


def changed_since(prior: dict[str, dict[str, Any]], store: VaultStore, today: date) -> Changed:
    ch = Changed()
    now = {i.id: i for i in store.items()}
    for iid, it in now.items():
        before = prior.get(iid)
        if before is None:
            if it.first_seen == today and it.status in (ItemStatus.open, ItemStatus.watch):
                ch.new.append(it)
            continue
        if it.status == ItemStatus.resolved and before["status"] in ("open", "watch"):
            ch.cleared.append(it)
        elif it.status in (ItemStatus.open, ItemStatus.watch):
            reasons = []
            if it.status.value != before["status"]:
                reasons.append(f"{before['status']} → {it.status.value}")
            if it.verified.value != before["verified"]:
                reasons.append(f"now {it.verified.value}")
            if it.due != before.get("due"):
                reasons.append("due date changed")
            if it.title != before.get("title"):
                reasons.append("retitled")
            if reasons:
                ch.moved.append((it, ", ".join(reasons)))
    return ch


def compute_metrics(store: VaultStore, cfg: Config, today: date, prior: dict[str, dict[str, Any]]) -> Metrics:
    items = store.items()
    open_items = [i for i in items if i.status == ItemStatus.open]
    key = _sort_key(today)

    due_today = sorted((i for i in open_items if i.is_due_today(today)), key=key)
    overdue = sorted((i for i in open_items if i.is_overdue(today)), key=key)
    waiting_on_me = sorted((i for i in open_items if i.type == ItemType.waiting_on_me), key=key)
    waiting_on_them = sorted((i for i in open_items if i.type == ItemType.waiting_on_them), key=key)
    slipping = sorted((i for i in open_items if i.type == ItemType.slipping), key=key)
    commitments_open = sorted(
        (i for i in open_items if i.type in (ItemType.commitment, ItemType.meeting_action)), key=key
    )
    watch = [i for i in items if i.status == ItemStatus.watch]

    ages = [a for a in (i.age_days(today) for i in open_items) if a is not None]
    oldest = max(ages) if ages else None

    since = today.toordinal() - cfg.sweep.commitment_lookback_days
    tracked = [
        i
        for i in items
        if i.type in (ItemType.commitment, ItemType.meeting_action)
        and i.status in (ItemStatus.open, ItemStatus.resolved)
        and (i.first_seen or today).toordinal() >= since
    ]
    delivered = sum(1 for i in tracked if i.status == ItemStatus.resolved)

    split = priority_split(store, today)
    imbalance_now = detect_imbalance(split, store, cfg.sweep.priority_imbalance_ratio)
    runs = previous_imbalance_runs(store, today) + 1 if imbalance_now else 0

    return Metrics(
        due_today=due_today,
        overdue=overdue,
        waiting_on_me=waiting_on_me,
        waiting_on_them=waiting_on_them,
        slipping=slipping,
        commitments_open=commitments_open,
        watch=watch,
        oldest_days=oldest,
        delivered=delivered,
        total_commitments=len(tracked),
        priority_split=split,
        imbalance=imbalance_now and runs >= 2,
        imbalance_runs=runs,
        changed=changed_since(prior, store, today),
        open_count=len(open_items),
    )
