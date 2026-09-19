from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nbrain.brief import caps
from nbrain.config.schema import Config, RoleTrack
from nbrain.sources.base import (
    BaseSource,
    CalendarEvent,
    CollectResult,
    Signal,
    SourceStatus,
    VerifyResult,
    Window,
)
from nbrain.sweep import pipeline
from nbrain.vault.schema import Item, ItemStatus, ItemType, Person, Project, SweepLog
from nbrain.vault.store import VaultStore


class FakeSource(BaseSource):
    name = "fake-tickets"
    roles = {"tickets"}
    verifiable = True

    def __init__(self, signals: list[Signal], events: list[CalendarEvent] | None = None, resolved_ids: set[str] | None = None):
        self._signals = signals
        self._events = events or []
        self._resolved = resolved_ids or set()

    async def healthcheck(self) -> SourceStatus:
        return SourceStatus(self.name, True, "fake")

    async def collect(self, window: Window) -> CollectResult:
        return CollectResult(signals=list(self._signals), events=list(self._events), findings={"fake": "no due dates"})

    async def verify(self, item: Item) -> VerifyResult:
        if item.source_id in self._resolved:
            return VerifyResult(state="resolved", note="closed")
        return VerifyResult(state="open")


class BrokenSource(BaseSource):
    name = "broken"
    roles = {"code"}

    async def healthcheck(self) -> SourceStatus:
        raise RuntimeError("boom")


def _sig(i: int, **kw) -> Signal:
    base = dict(
        type=ItemType.slipping,
        title=f"Ticket INF-{i} stale",
        source="fake-tickets",
        source_id=f"INF-{i}",
        url=f"https://jira.example/browse/INF-{i}",
        cause="untouched 9d",
        project_hint="INF infra",
        due=date(2026, 9, 17) - timedelta(days=i % 3),
        priority=1,
    )
    base.update(kw)
    return Signal(**base)


@pytest.fixture
def populated(vault: VaultStore, cfg: Config):
    vault.save(Project(title="Infra Agent", keywords=["infra"], jira_keys=["INF"], weight=1.0))
    vault.save(Project(title="Dashboard", keywords=["dashboard"], weight=1.0))
    vault.save(Person(title="Ada Lovelace", email="ada@example.com", tier=1))
    cfg.role_track = RoleTrack.lead
    return vault


async def _run(cfg, store, sources, today, **kw):
    monkey = kw.pop("monkeypatch")
    monkey.setattr(pipeline, "build_native_sources", lambda c, s: sources)
    sweep = pipeline.Sweep(cfg, store, today=today, skip_llm=True, deliver=False, **kw)
    return await sweep.run()


async def test_sweep_end_to_end_without_model(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    tz = ZoneInfo(cfg.user.timezone)
    ev_today = CalendarEvent(
        source="cal", source_id="e1", title="Infra sync", start=datetime(2026, 9, 17, 10, tzinfo=tz),
        end=datetime(2026, 9, 17, 11, tzinfo=tz), organiser_is_me=True, attendees=["Me", "Ada Lovelace"], has_agenda=False,
    )
    ev_tomorrow = CalendarEvent(
        source="cal", source_id="e2", title="Client review", start=datetime(2026, 9, 18, 14, tzinfo=tz),
        end=datetime(2026, 9, 18, 15, tzinfo=tz), external=True, attendees=["Me", "x@client.com"],
    )
    signals = [_sig(i) for i in range(1, 8)] + [
        Signal(type=ItemType.waiting_on_me, title="Reply to Ada: design doc", source="fake-tickets", source_id="w1",
               person="Ada Lovelace", person_email="ada@example.com", observed_at=datetime.now(UTC), priority=2),
        Signal(type=ItemType.commitment, title="Send the dashboard numbers", source="fake-tickets", source_id="c1",
               person="Ada Lovelace", promised_on=today - timedelta(days=8), due=today, project_hint="dashboard", confidence=0.9),
        Signal(type=ItemType.commitment, title="Maybe look into caching", source="fake-tickets", source_id="c2", confidence=0.3),
    ]
    src = FakeSource(signals, [ev_today, ev_tomorrow])
    rep = await _run(cfg, populated, [src, BrokenSource()], today, monkeypatch=monkeypatch)

    assert rep.created == 10
    assert any("broken" in n for n in rep.not_checked)
    assert any("Not connected:" in b and "email" in b for b in rep.coverage_gaps)
    assert "email" in rep.markdown  # the brief itself names the gap (Rule 6)
    assert "fake: no due dates" in rep.blind_spots

    items = {i.source_id: i for i in populated.items()}
    assert items["INF-1"].project == "[[Infra Agent]]"
    assert items["c1"].project == "[[Dashboard]]"
    assert items["w1"].promised_to == "[[Ada Lovelace]]" and items["w1"].priority == 1  # tier-1 bump
    assert items["c2"].status == ItemStatus.watch  # low confidence

    m = rep.metrics
    assert len(m.urgent) >= 3
    assert m.delivery_score == "0/1"
    assert rep.brief.due_overflow > 0 and rep.brief.due_today == []  # planning problem line
    assert rep.brief.bottleneck  # lead track
    assert caps.word_count(rep.markdown) <= caps.TOTAL_WORDS
    assert "Not checked:" in rep.markdown and "broken" in rep.markdown
    assert "Infra sync" in rep.markdown and "no agenda" in rep.markdown
    assert "Tomorrow" in rep.markdown
    assert len(rep.html.encode()) < caps.HTML_MAX_BYTES
    _assert_email_safe(rep.html)

    assert (populated.root / "Briefs" / f"{today}.md").exists()
    assert (populated.root / "Briefs" / "latest.md").exists()
    radar = (populated.root / "radar.md").read_text()
    assert "Ticket INF-1 stale" in radar and "fake: no due dates" in radar
    logs = populated.load_all(SweepLog)
    assert len(logs) == 1 and logs[0].items_new == 10 and logs[0].priority_split
    assert rep.md_path and "[[Briefs/2026-09-17]]" in populated.load(Item, items["INF-1"].id).surfaced_in


async def test_second_run_verifies_resolves_and_diffs(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    day1 = today
    day2 = today + timedelta(days=1)
    src = FakeSource([_sig(1), _sig(2)])
    await _run(cfg, populated, [src], day1, monkeypatch=monkeypatch)
    # day 2: INF-1 closed at the source and no longer collected; INF-3 is new
    src2 = FakeSource([_sig(2), _sig(3)], resolved_ids={"INF-1"})
    rep = await _run(cfg, populated, [src2], day2, monkeypatch=monkeypatch)
    items = {i.source_id: i for i in populated.items()}
    assert items["INF-1"].status == ItemStatus.resolved
    assert rep.verify.resolved and rep.metrics.changed.cleared[0].source_id == "INF-1"
    assert [i.source_id for i in rep.metrics.changed.new] == ["INF-3"]
    assert "Cleared: 1" in rep.markdown


async def test_unconfirmed_when_source_missing(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    src = FakeSource([_sig(1)])
    await _run(cfg, populated, [src], today, monkeypatch=monkeypatch)
    for n in range(1, 4):
        rep = await _run(cfg, populated, [], today + timedelta(days=n), monkeypatch=monkeypatch)
    item = populated.items()[0]
    assert item.status == ItemStatus.watch and item.unconfirmed_runs == 3
    assert rep.verify.watch == [item.id]


async def test_dry_run_leaves_vault_untouched(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    before = sorted(p.name for p in populated.root.rglob("*.md"))
    rep = await _run(cfg, populated, [FakeSource([_sig(1)])], today, monkeypatch=monkeypatch, dry_run=True)
    after = sorted(p.name for p in populated.root.rglob("*.md"))
    assert before == after
    assert rep.created == 1 and "INF-1" in rep.markdown


async def test_brief_never_claims_coverage_it_lacks(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    """No sources at all must not render as 'all enabled sources answered'."""
    rep = await _run(cfg, populated, [], today, monkeypatch=monkeypatch)
    assert "No sources are connected" in rep.markdown
    assert "All enabled sources answered" not in rep.markdown


async def test_every_list_item_is_on_its_own_line(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    """Jinja's trim_blocks eats the newline after a block tag, which silently ran the numbered
    actions and the slipping/meeting rows together into one paragraph."""
    from nbrain.llm.tasks import Action, BriefDraft

    tz = ZoneInfo(cfg.user.timezone)
    ev = CalendarEvent(
        source="cal", source_id="e1", title="Infra sync", start=datetime(2026, 9, 17, 10, tzinfo=tz),
        end=datetime(2026, 9, 17, 11, tzinfo=tz), organiser_is_me=True, attendees=["Me", "Ada"], has_agenda=False,
    )
    draft = BriefDraft(
        one_thing="Do the thing.",
        assessment="Short.",
        actions=[Action(text=f"Action number {n}", effort_hint="ten minutes") for n in range(1, 4)],
    )
    monkeypatch.setattr(pipeline.Sweep, "_check_bedrock", lambda self: None)
    monkeypatch.setattr(pipeline, "build_native_sources", lambda c, s: [FakeSource([_sig(1), _sig(2)], [ev])])

    async def fake_write_brief(self, payload):
        return draft

    monkeypatch.setattr("nbrain.llm.tasks.LLMTasks.write_brief", fake_write_brief)
    monkeypatch.setattr("nbrain.llm.tasks.LLMTasks.extract_commitments", lambda self, t, **k: _none())
    monkeypatch.setattr("nbrain.llm.tasks.LLMTasks.classify_threads", lambda self, t, **k: _none())
    sweep = pipeline.Sweep(cfg, populated, today=today, deliver=False)
    rep = await sweep.run()

    md = rep.markdown
    for n in (1, 2, 3):
        assert f"\n{n}. Action number {n} — _ten minutes_\n" in md, f"action {n} not on its own line"
    # slipping rows and the meeting line must not merge with what follows either
    for line in md.splitlines():
        assert line.count("· ✅ live") <= 1, f"two rows merged onto one line: {line[:120]}"
        assert not (line.startswith("- ") and line.count(" — ") > 1 and "Infra sync" in line)


async def _none():
    return []


def _assert_email_safe(html: str) -> None:
    """Gmail is not a browser. These are the constraints from brief-format.md, not preferences."""
    import re

    assert "<style" not in html, "Gmail strips <style> blocks; styles must be inline"
    assert "<script" not in html and "javascript:" not in html
    assert "<img" not in html, "no external images"
    assert "@import" not in html and "var(--" not in html, "no CSS variables in email"
    assert "display:flex" not in html and "display:grid" not in html, "tables only"
    # every element with a CSS background must also carry the bgcolor attribute, because Gmail
    # drops the CSS one and the tinted blocks would render as coloured text on white
    for tag in re.findall(r"<(?:td|th|table)\b[^>]*>", html):
        if re.search(r"style=\"[^\"]*background:#", tag):
            assert "bgcolor=" in tag, f"background without bgcolor attribute: {tag[:120]}"


async def test_email_brief_is_gmail_safe_and_matches_portal_palette(populated: VaultStore, cfg: Config, today: date, monkeypatch):
    """The email uses the portal's palette as literal hex, under email constraints."""
    from nbrain.brief.build import PALETTE

    rep = await _run(cfg, populated, [FakeSource([_sig(1)])], today, monkeypatch=monkeypatch)
    html = rep.html
    _assert_email_safe(html)
    assert PALETTE["accent"] == "#4F46E5" and PALETTE["page_bg"] == "#F6F7F9"
    assert 'bgcolor="#F6F7F9"' in html and 'bgcolor="#FFFFFF"' in html  # page + card
    assert PALETTE["accent"] in html  # the one-thing callout and links
    assert html.count("640px") >= 2  # fixed, centred card width
