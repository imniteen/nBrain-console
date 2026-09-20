"""The daily sweep. Ten steps, each survivable: a failing source becomes a line under
"Not checked", a failing model call degrades to a numbers-only brief, a failing channel is
reported. Only an unreachable vault stops the run."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from nbrain.brief.build import (
    BriefData,
    brief_input_for_llm,
    build_brief,
    fallback_draft,
    render_html,
    render_markdown,
)
from nbrain.config.schema import Config, Provider
from nbrain.delivery.base import DeliveryResult, RenderedBrief, build_deliverers, deliver_all
from nbrain.llm.tasks import BriefDraft, ExtractedCommitment, LLMTasks, ThreadClass
from nbrain.sources.base import CollectResult, Signal, Source, SourceStatus, Window
from nbrain.sweep.metrics import Metrics, compute_metrics
from nbrain.sweep.verify import VerifySummary, verify_open_items
from nbrain.util import now_in, today_in, wikilink
from nbrain.vault.render import render_memory, render_radar
from nbrain.vault.schema import ItemStatus, ItemType, SweepLog, Verified
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)


@dataclass
class SweepReport:
    today: date
    dry_run: bool
    source_status: dict[str, SourceStatus] = field(default_factory=dict)
    not_checked: list[str] = field(default_factory=list)
    blind_spots: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    per_source: dict[str, str] = field(default_factory=dict)
    people: Any = None  # PeopleUpdate from the relationship pass
    verify: VerifySummary | None = None
    collected: CollectResult = field(default_factory=CollectResult)
    created: int = 0
    updated: int = 0
    reopened: int = 0
    skipped_dismissed: int = 0
    metrics: Metrics | None = None
    draft: BriefDraft | None = None
    brief: BriefData | None = None
    markdown: str = ""
    html: str = ""
    md_path: Path | None = None
    html_path: Path | None = None
    deliveries: list[DeliveryResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    llm_calls: int = 0
    llm_tokens: tuple[int, int] = (0, 0)
    model: str = ""
    vault_root: Path | None = None


def build_native_sources(cfg: Config, store: VaultStore) -> list[Any]:
    sources: list[Any] = []
    if cfg.sources.google.enabled:
        from nbrain.sources.google import build_google_sources

        sources += build_google_sources(cfg, store)
    if cfg.sources.gitlab.enabled:
        from nbrain.sources.gitlab import GitLabSource

        sources.append(GitLabSource(cfg, store))
    if cfg.sources.jira.enabled:
        from nbrain.sources.jira import JiraSource

        sources.append(JiraSource(cfg, store))
    if cfg.sources.slack.enabled:
        from nbrain.sources.slack import SlackSource

        sources.append(SlackSource(cfg, store))
    return sources


class Sweep:
    def __init__(
        self,
        cfg: Config,
        store: VaultStore,
        *,
        today: date | None = None,
        dry_run: bool = False,
        only_sources: list[str] | None = None,
        skip_llm: bool = False,
        deliver: bool = True,
    ):
        self.cfg = cfg
        self.real_store = store
        self.today = today or today_in(cfg.user.timezone)
        self.dry_run = dry_run
        self.only_sources = only_sources
        self.skip_llm = skip_llm
        self.deliver = deliver and not dry_run
        self.report = SweepReport(today=self.today, dry_run=dry_run)
        self.store = store
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    # ---------- helpers ----------

    def _window(self) -> Window:
        s = self.cfg.sweep
        return Window(
            today=self.today,
            email_lookback_days=s.email_lookback_days,
            commitment_lookback_days=s.commitment_lookback_days,
            awaiting_reply_hours=s.awaiting_reply_hours,
            ticket_stale_days=s.ticket_stale_days,
            review_age_days=s.review_age_days,
            tz=self.cfg.user.timezone,
        )

    def _shadow_vault(self) -> VaultStore:
        """Dry runs write to a throwaway copy so the real ledger is untouched."""
        self._tmp = tempfile.TemporaryDirectory(prefix="nbrain-dry-")
        dst = Path(self._tmp.name) / "vault"
        shutil.copytree(
            self.real_store.root,
            dst,
            ignore=shutil.ignore_patterns(".obsidian", "*.lock", ".env", "*.json"),
        )
        # dry runs still need Google token/credentials for reads
        for name in ("credentials.json", "google-token.json"):
            src = self.real_store.root / "nbrain" / name
            if src.exists():
                shutil.copyfile(src, dst / "nbrain" / name)
        return VaultStore(dst)

    def _check_bedrock(self) -> None:
        cfgs = [self.cfg.llm.for_task(t) for t in ("default", "extract", "write", "mcp")]  # type: ignore[arg-type]
        for lcfg in {c.provider: c for c in cfgs}.values():
            if lcfg.provider == Provider.bedrock:
                from nbrain.llm.aws_creds import ensure_credentials

                state = ensure_credentials(lcfg)
                if not state.ok:
                    raise RuntimeError(f"AWS credentials unusable: {state.detail}")
                if state.minutes_left is not None and state.minutes_left < 120:
                    self.report.warnings.append(f"AWS credentials expire in {state.minutes_left:.0f} min")

    # ---------- steps ----------

    async def _healthcheck(self, sources: list[Any]) -> list[Any]:
        async def one(src: Any) -> tuple[Any, SourceStatus]:
            try:
                st = await asyncio.wait_for(src.healthcheck(), 60)
            except Exception as e:  # noqa: BLE001
                st = SourceStatus(src.name, False, f"healthcheck failed: {e}")
            return src, st

        results = await asyncio.gather(*(one(s) for s in sources))
        ok: list[Any] = []
        for src, st in results:
            self.report.source_status[src.name] = st
            if st.ok:
                ok.append(src)
            else:
                self.report.not_checked.append(f"{src.name} ({st.detail[:80]})")
        return ok

    async def _collect(self, sources: list[Any], window: Window) -> CollectResult:
        timeout = self.cfg.sweep.per_source_timeout_seconds

        async def one(src: Any) -> tuple[str, CollectResult | Exception]:
            try:
                return src.name, await asyncio.wait_for(src.collect(window), timeout)
            except Exception as e:  # noqa: BLE001
                return src.name, e

        merged = CollectResult()
        for name, res in await asyncio.gather(*(one(s) for s in sources)):
            if isinstance(res, Exception):
                log.warning("collect %s failed: %s", name, res)
                self.report.not_checked.append(f"{name} ({type(res).__name__}: {str(res)[:80]})")
                self.report.per_source[name] = f"FAILED: {type(res).__name__}"
                continue
            self.report.per_source[name] = (
                f"{len(res.signals)} signals, {len(res.texts)} texts, {len(res.events)} events, "
                f"{len(res.interactions)} interactions"
                + (" — nothing found" if not (res.signals or res.texts or res.events) else "")
            )
            merged.extend(res)
        return merged

    def _signals_from_classification(self, classes: list[ThreadClass], texts_by_id: dict[str, Any]) -> list[Signal]:
        out: list[Signal] = []
        for c in classes:
            t = texts_by_id.get(c.thread_id)
            if t is None:
                continue
            if c.category == "needs_my_reply":
                out.append(
                    Signal(
                        type=ItemType.waiting_on_me,
                        title=f"Reply to {t.author or 'sender'}: {t.title}"[:120],
                        source=t.source,
                        source_id=t.source_id,
                        url=t.url,
                        person=t.author,
                        person_email=t.author_email,
                        observed_at=t.observed_at,
                        evidence=c.ask or t.text[:200],
                        priority=c.urgency,
                    )
                )
            elif c.category == "waiting_on_them":
                out.append(
                    Signal(
                        type=ItemType.waiting_on_them,
                        title=f"Waiting on reply: {t.title}"[:120],
                        source=t.source,
                        source_id=t.source_id,
                        url=t.url,
                        person=t.author if not t.author_is_me else (t.participants[0] if t.participants else None),
                        observed_at=t.observed_at,
                        evidence=c.ask or t.text[:200],
                        priority=3,
                    )
                )
            elif c.category == "phishing_shaped":
                out.append(
                    Signal(
                        type=ItemType.watch,
                        title=f"Phishing-shaped: {t.title}"[:120],
                        source=t.source,
                        source_id=t.source_id,
                        url=t.url,
                        person=t.author,
                        evidence=c.summary or t.text[:160],
                        priority=3,
                        confidence=0.5,
                    )
                )
        return out

    def _signals_from_extraction(self, found: list[ExtractedCommitment], texts_by_id: dict[str, Any]) -> list[Signal]:
        out: list[Signal] = []
        for c in found:
            t = texts_by_id.get(c.text_id)
            if t is None:
                continue
            if c.type in ("commitment", "meeting-action") and not c.owner_is_me:
                continue  # someone else's promise; not ours to track
            if c.title.upper().startswith("SUSPICIOUS:"):
                itype, conf = ItemType.watch, 0.4
            else:
                itype, conf = ItemType(c.type), c.confidence
            out.append(
                Signal(
                    type=itype,
                    title=c.title[:120],
                    source=t.source,
                    source_id=f"{t.source_id}#{abs(hash(c.title)) % 10_000}",
                    url=t.url,
                    person=c.promised_to if itype != ItemType.waiting_on_me else (c.owner_name or t.author),
                    due=c.due,
                    promised_on=c.promised_on or (t.observed_at.date() if t.observed_at else None),
                    observed_at=t.observed_at,
                    evidence=c.evidence,
                    priority=1 if getattr(t, "unopened_by_me", False) else 2,
                    confidence=conf,
                    project_hint=c.project_hint,
                    meeting=t.meeting,
                )
            )
        return out

    def _merge(self, signals: list[Signal]) -> None:
        index = self.store.index_items()
        people = {p.title.lower(): p for p in self.store.people()}
        for sig in signals:
            item = sig.to_item()
            if sig.confidence < 0.6 and item.type != ItemType.watch:
                item.status = ItemStatus.watch
                item.type = ItemType.watch if item.type == ItemType.commitment else item.type
            # people: link to a known Person note when we can, otherwise keep the name as-is
            person = self.store.person_by_email(sig.person_email) if sig.person_email else None
            if person is None and sig.person:
                person = people.get(sig.person.lower()) or self.store.person_by_name(sig.person)
            if person is not None:
                item.promised_to = wikilink(person.id)
                if person.tier == 1 and item.priority > 1:
                    item.priority = 1
            if not item.project:
                proj = self.store.project_for_text(sig.project_hint, sig.title, sig.evidence, sig.url)
                if proj is not None:
                    item.project = wikilink(proj.id)
            item.verified = Verified.live
            _, outcome = self.store.upsert_item(item, self.today, index=index)
            if outcome == "created":
                self.report.created += 1
            elif outcome == "updated":
                self.report.updated += 1
            elif outcome == "reopened":
                self.report.reopened += 1
            else:
                self.report.skipped_dismissed += 1

    # ---------- run ----------

    async def run(self) -> SweepReport:
        started = time.monotonic()
        if not self.real_store.exists():
            raise RuntimeError(f"Vault not found or not initialised at {self.real_store.root}. Run `nbrain setup`.")
        self.store = self._shadow_vault() if self.dry_run else self.real_store
        self.report.vault_root = self.store.root
        try:
            return await self._run_inner(started)
        finally:
            if self._tmp is not None:
                self._tmp.cleanup()

    async def _run_inner(self, started: float) -> SweepReport:
        rep = self.report
        tasks = LLMTasks(self.cfg, today=self.today)
        if not self.skip_llm:
            try:
                self._check_bedrock()
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"LLM unavailable: {e}; falling back to numbers-only brief")
                self.skip_llm = True

        # 1-2. sources + health
        sources: list[Any] = build_native_sources(self.cfg, self.store)
        from nbrain.mcp.registry import MCPRegistry
        from nbrain.mcp.router import MCPRouter
        from nbrain.sources.mcp_generic import build_mcp_sources, native_role_map

        registry = MCPRegistry(self.cfg)
        if not self.skip_llm:
            sources += build_mcp_sources(self.cfg, registry, tasks)
        elif self.cfg.enabled_mcp_servers():
            rep.not_checked.append("MCP servers (need the model, which is unavailable)")
        if self.only_sources:
            sources = [s for s in sources if s.name in self.only_sources or s.name.split(":")[0] in self.only_sources]
        router = MCPRouter(self.cfg, native_role_map(sources))
        rep.coverage_gaps = router.coverage().gaps()
        rep.blind_spots += rep.coverage_gaps
        ok_sources = await self._healthcheck(sources)
        by_name: dict[str, Source] = {s.name: s for s in ok_sources}
        window = self._window()

        # 3. snapshot + verify
        prior = self.store.snapshot_open()
        rep.verify = await verify_open_items(
            self.store, by_name, self.today, drop_after=self.cfg.sweep.unconfirmed_drop_after_runs
        )

        # 4. collect
        rep.collected = await self._collect(ok_sources, window)
        rep.not_checked += rep.collected.notes
        for k, v in rep.collected.findings.items():
            rep.blind_spots.append(f"{k}: {v}")

        # 4b. relationships: who you actually deal with, and through which channel
        try:
            from nbrain.vault.people import update_people

            rep.people = update_people(self.store, self.cfg, rep.collected.interactions, self.today)
            log.info("people pass: %s", rep.people.counts())
        except Exception as e:  # noqa: BLE001 - never let this block the brief
            rep.warnings.append(f"people pass failed: {e}")

        # 5. extract with the model
        signals = list(rep.collected.signals)
        texts_by_id = {t.source_id: t for t in rep.collected.texts}
        if not self.skip_llm and rep.collected.texts:
            email_texts = [t for t in rep.collected.texts if t.kind == "email"]
            other_texts = [t for t in rep.collected.texts if t.kind != "email"]
            try:
                if email_texts:
                    payload = [
                        {
                            "id": t.source_id,
                            "subject": t.title,
                            "from": t.author,
                            "from_email": t.author_email,
                            "participants": t.participants,
                            "last_message_from_me": t.author_is_me,
                            "observed_at": t.observed_at.isoformat() if t.observed_at else None,
                            "snippet": t.text[:600],
                        }
                        for t in email_texts
                    ]
                    classes = await tasks.classify_threads(payload)
                    signals += self._signals_from_classification(classes, texts_by_id)
                if other_texts:
                    found = await tasks.extract_commitments(other_texts)
                    signals += self._signals_from_extraction(found, texts_by_id)
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"LLM extraction failed: {e}")
        elif rep.collected.texts:
            rep.not_checked.append(f"{len(rep.collected.texts)} texts not read for commitments (model unavailable)")

        # 6. merge
        self._merge(signals)

        # 7. metrics
        rep.metrics = compute_metrics(self.store, self.cfg, self.today, prior)

        # 8. write with the model
        retractions = rep.verify.retractions if rep.verify else []
        if not self.skip_llm:
            try:
                rep.draft = await tasks.write_brief(
                    brief_input_for_llm(rep.metrics, rep.collected.events, self.today, self.cfg.user.timezone, rep.not_checked, retractions)
                )
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"LLM brief failed: {e}; numbers-only brief")
        if rep.draft is None:
            rep.draft = fallback_draft(rep.metrics, self.today, self.cfg.user.first_name)

        subject = self.cfg.delivery.gmail_draft.subject.format(date=self.today.isoformat())
        rep.brief = build_brief(
            today=self.today,
            first_name=self.cfg.user.first_name,
            role_track=self.cfg.role_track,
            tz=self.cfg.user.timezone,
            metrics=rep.metrics,
            draft=rep.draft,
            events=rep.collected.events,
            not_checked=rep.not_checked,
            gaps=rep.coverage_gaps,
            extras=list(self.cfg.sweep.extras),
            retractions=retractions,
            subject=subject,
        )
        rep.markdown = render_markdown(rep.brief)
        rep.html = render_html(rep.brief) if self.cfg.delivery.file.html or self.cfg.delivery.gmail_draft.enabled or self.cfg.delivery.gmail_send.enabled else ""
        rep.warnings += rep.brief.warnings

        # 9. render vault files
        rep.md_path, rep.html_path = self.store.write_brief(self.today, rep.markdown, rep.html or None)
        self.store.write_text("radar.md", render_radar(self.store, self.cfg, self.today, rep.not_checked, rep.blind_spots))
        status_lines = {n: ("ok — " + s.detail if s.ok else "FAILED — " + s.detail) for n, s in rep.source_status.items()}
        self.store.write_text("memory.md", render_memory(self.store, self.cfg, self.today, status_lines))
        self._link_items_to_brief()
        rep.llm_calls = tasks.usage.calls
        rep.llm_tokens = (tasks.usage.input_tokens, tasks.usage.output_tokens)
        rep.model = ", ".join(sorted(tasks.usage.models)) or "none"
        rep.duration_seconds = time.monotonic() - started
        self._write_sweep_log(rep)

        # 10. deliver
        if self.deliver:
            rendered = RenderedBrief(self.today, subject, rep.markdown, rep.html or None, rep.md_path, rep.html_path)
            rep.deliveries = await deliver_all(build_deliverers(self.cfg, self.store), rendered)
        return rep

    def _link_items_to_brief(self) -> None:
        if not self.report.brief:
            return
        brief_link = f"[[Briefs/{self.today.isoformat()}]]"
        shown = self.report.brief.due_today + self.report.brief.waiting + self.report.brief.slipping + self.report.brief.commitments + self.report.brief.bottleneck
        for item in shown:
            if brief_link not in item.surfaced_in:
                item.surfaced_in.append(brief_link)
                self.store.save(item)

    def _write_sweep_log(self, rep: SweepReport) -> None:
        m = rep.metrics
        logn = SweepLog(
            title=f"Sweep {self.today.isoformat()}",
            kind="daily",
            run_at=now_in(self.cfg.user.timezone).isoformat(timespec="seconds"),
            duration_seconds=round(rep.duration_seconds, 1),
            sources_ok=[n for n, s in rep.source_status.items() if s.ok],
            sources_failed=[n for n, s in rep.source_status.items() if not s.ok],
            items_new=rep.created,
            items_resolved=len(rep.verify.resolved) if rep.verify else 0,
            items_open=m.open_count if m else 0,
            llm_calls=rep.llm_calls,
            llm_input_tokens=rep.llm_tokens[0],
            llm_output_tokens=rep.llm_tokens[1],
            model=rep.model,
            dry_run=self.dry_run,
        )
        logn.id = self.today.isoformat()
        logn.priority_split = m.priority_split if m else {}
        logn.imbalance = bool(m and m.imbalance_runs > 0)
        if rep.people is not None:
            logn.people = rep.people.counts()
        body = ["## Collected per source"] + [f"- {k}: {v}" for k, v in sorted(rep.per_source.items())]
        body += ["", "## Not checked"] + ([f"- {n}" for n in rep.not_checked] or ["- none"])
        body += ["", "## Warnings"] + ([f"- {w}" for w in rep.warnings] or ["- none"])
        if rep.verify:
            body += ["", "## Verification", f"- {rep.verify.counts()}"]
        logn.body = "\n".join(body) + "\n"
        self.store.save_sweep_log(logn)
