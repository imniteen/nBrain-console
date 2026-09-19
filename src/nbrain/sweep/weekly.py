"""Friday review: what slipped and why, delivery rate, one process change, graph patterns."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from nbrain.brief.build import TEMPLATES
from nbrain.config.schema import Config
from nbrain.delivery.base import DeliveryResult, RenderedBrief, build_deliverers, deliver_all
from nbrain.llm.tasks import LLMTasks, WeeklyDraft
from nbrain.util import iso_week, today_in, unlink
from nbrain.vault.schema import Item, ItemStatus, ItemType, SweepLog
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)


@dataclass
class WeeklyReport:
    week: str
    start: date
    end: date
    markdown: str = ""
    path: Path | None = None
    deliveries: list[DeliveryResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    draft: WeeklyDraft | None = None


def _fallback(delivered: int, total: int, slipped: list[str], patterns: list[str]) -> WeeklyDraft:
    return WeeklyDraft(
        narrative=f"{len(slipped)} items slipped this week; commitments delivered {delivered}/{total}. The model was unavailable, so this is the numbers-only review.",
        slipped=slipped,
        delivery_comment=f"{delivered} of {total} tracked commitments were delivered.",
        process_improvement="Pick the single oldest slipping item and give it a due date you will keep.",
        patterns=patterns[:3],
    )


async def run_weekly(cfg: Config, store: VaultStore, *, today: date | None = None, skip_llm: bool = False, deliver: bool = True) -> WeeklyReport:
    today = today or today_in(cfg.user.timezone)
    start = today - timedelta(days=today.weekday())
    week = iso_week(today)
    rep = WeeklyReport(week=week, start=start, end=today)

    items = store.items()
    resolved = [i for i in items if i.status == ItemStatus.resolved and i.resolved_on and start <= i.resolved_on <= today]
    open_items = [i for i in items if i.status == ItemStatus.open]
    slipped_items: list[Item] = [i for i in open_items if i.type == ItemType.slipping] + [
        i for i in resolved if i.due and i.resolved_on and i.resolved_on > i.due
    ]
    slipped_lines = [
        f"{i.title} — {i.cause or ('late by ' + str((i.resolved_on - i.due).days) + 'd' if i.due and i.resolved_on else 'open ' + str(i.age_days(today) or 0) + 'd')}"
        for i in slipped_items[:10]
    ]
    since = today - timedelta(days=cfg.sweep.commitment_lookback_days)
    tracked = [i for i in items if i.type in (ItemType.commitment, ItemType.meeting_action) and i.status in (ItemStatus.open, ItemStatus.resolved) and (i.first_seen or today) >= since]
    delivered = sum(1 for i in tracked if i.status == ItemStatus.resolved)

    logs = [lg for lg in store.load_all(SweepLog) if lg.kind == "daily" and start.isoformat() <= lg.id <= today.isoformat()]
    split: dict[str, float] = {}
    for lg in logs:
        for k, v in (getattr(lg, "priority_split", {}) or {}).items():
            split[k] = split.get(k, 0.0) + float(v)
    if split:
        tot = sum(split.values())
        split = {k: round(v / tot, 3) for k, v in sorted(split.items(), key=lambda kv: -kv[1])}
    failed = sorted({s for lg in logs for s in lg.sources_failed})

    patterns: list[str] = []
    try:
        from nbrain.vault.graph import VaultGraph

        patterns = [p.text for p in VaultGraph(store).patterns(today)[:5]]
    except Exception as e:  # noqa: BLE001
        log.debug("graph patterns unavailable: %s", e)

    draft: WeeklyDraft | None = None
    if not skip_llm:
        try:
            tasks = LLMTasks(cfg, today=today)
            draft = await tasks.write_weekly(
                {
                    "week": week,
                    "resolved": [{"title": i.title, "person": unlink(i.promised_to), "project": unlink(i.project)} for i in resolved[:20]],
                    "slipped": slipped_lines,
                    "delivered": delivered,
                    "total": len(tracked),
                    "priority_split": split,
                    "open_count": len(open_items),
                    "patterns": patterns,
                    "sources_failed_this_week": failed,
                }
            )
        except Exception as e:  # noqa: BLE001
            rep.warnings.append(f"LLM weekly failed: {e}")
    if draft is None:
        draft = _fallback(delivered, len(tracked), slipped_lines, patterns)
    rep.draft = draft

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True, lstrip_blocks=True)
    rep.markdown = env.get_template("weekly.md.j2").render(
        d=draft, week=week, start=start, end=today, delivered=delivered, total=len(tracked), resolved=resolved,
        split=split, open_count=len(open_items), watch_count=sum(1 for i in items if i.status == ItemStatus.watch),
        sweeps=len(logs), not_checked=", ".join(failed),
    )
    rep.path = store.write_text(f"Reviews/{week}.md", rep.markdown)
    if deliver:
        subject = f"[nbrain] Weekly — {week}"
        rendered = RenderedBrief(today, subject, rep.markdown, None, rep.path, None, kind="weekly")
        rep.deliveries = await deliver_all(build_deliverers(cfg, store), rendered)
    return rep
