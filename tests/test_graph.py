from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import networkx as nx
import pytest

from nbrain.vault.graph import GraphFilters, VaultGraph
from nbrain.vault.schema import Item, ItemStatus, ItemType, Meeting, Person, Project
from nbrain.vault.store import VaultStore

TODAY = date(2026, 9, 17)


def _item(id_: str, **kw) -> Item:
    base = dict(id=id_, title=id_.replace("-", " "), source="gmail", source_id=id_, first_seen=date(2026, 9, 1))
    base.update(kw)
    return Item(**base)


@pytest.fixture
def graph_vault(tmp_path: Path) -> VaultStore:
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()

    store.save(Person(id="Alice", title="Alice", tier=1, email="alice@example.com"))
    store.save(Person(id="Bob", title="Bob"))
    store.save(Person(id="Carol", title="Carol"))
    store.save(Person(id="Dave", title="Dave"))  # nothing links here: an orphan person
    store.save(
        Project(
            id="Infra Agent",
            title="Infra Agent",
            weight=2.0,
            body="Owner [[Alice]]. Design doc lives with [[Ghost]].",
        )
    )
    store.save(Meeting(id="Weekly Sync", title="Weekly Sync", cadence="weekly", attendees=["Alice", "Bob"]))

    # three open commitments to Alice inside Infra Agent, two of them from the Weekly Sync
    store.save(_item("i1-deck", promised_to="Alice", project="Infra Agent", meeting="Weekly Sync", first_seen=date(2026, 8, 1)))
    store.save(
        _item("i2-runbook", promised_to="Alice", project="Infra Agent", meeting="Weekly Sync", source="slack", first_seen=date(2026, 9, 1))
    )
    store.save(_item("i3-review", promised_to="Alice", project="Infra Agent", first_seen=date(2026, 9, 10)))
    # a delivered one from the same meeting
    store.save(
        _item(
            "i4-budget",
            promised_to="Bob",
            meeting="Weekly Sync",
            source="jira",
            status=ItemStatus.resolved,
            first_seen=date(2026, 7, 1),
            resolved_on=date(2026, 7, 15),
        )
    )
    # owed to Carol, who is in no meeting
    store.save(_item("i5-intro", promised_to="Carol", first_seen=date(2026, 9, 5)))
    # the orphan: no person, no project
    store.save(_item("i6-mystery", first_seen=date(2026, 6, 1)))
    # waiting on me from Bob
    store.save(_item("i7-approve", type=ItemType.waiting_on_me, promised_to="Bob", first_seen=date(2026, 9, 1)))
    # dismissed, still linked
    store.save(
        _item(
            "i8-old",
            promised_to="Alice",
            project="Infra Agent",
            status=ItemStatus.dismissed,
            first_seen=date(2026, 5, 1),
            dismissed_on=date(2026, 5, 20),
        )
    )
    return store


@pytest.fixture
def vg(graph_vault: VaultStore) -> VaultGraph:
    return VaultGraph(graph_vault)


def test_node_count_and_link_kinds(vg: VaultGraph, graph_vault: VaultStore):
    g = vg.build()
    notes = len(list(graph_vault.all_note_paths()))  # includes radar.md at the root
    sources = 3  # gmail, slack, jira
    phantoms = 1  # [[Ghost]]
    assert g.number_of_nodes() == notes + sources + phantoms

    kinds = {d["kind"] for _, _, d in g.edges(data=True)}
    assert {"promised_to", "project", "meeting", "attendee", "mention", "source"} <= kinds
    assert g.edges["i1-deck", "Alice"]["kind"] == "promised_to"
    assert g.edges["i1-deck", "Infra Agent"]["kind"] == "project"
    assert g.edges["i1-deck", "Weekly Sync"]["kind"] == "meeting"
    assert g.edges["Weekly Sync", "Bob"]["kind"] == "attendee"
    assert g.edges["Infra Agent", "Alice"]["kind"] == "mention"
    assert g.edges["i2-runbook", "source:slack"]["kind"] == "source"
    assert g.nodes["source:slack"]["type"] == "source"


def test_phantom_node_for_unresolved_link(vg: VaultGraph):
    g = vg.build()
    assert "Ghost" in g
    assert g.nodes["Ghost"]["type"] == "phantom"
    assert g.edges["Infra Agent", "Ghost"]["kind"] == "mention"
    # resolution is case-insensitive and does not create a phantom for a real note
    assert not [n for n, d in g.nodes(data=True) if d["type"] == "phantom" and n != "Ghost"]


def test_to_json_shape_and_brief_toggle(vg: VaultGraph):
    data = vg.to_json()
    assert set(data) == {"nodes", "links", "meta"}
    ids = {n["id"] for n in data["nodes"]}
    assert all({"source", "target", "kind"} <= set(link) for link in data["links"])
    assert all(link["source"] in ids and link["target"] in ids for link in data["links"])
    assert "radar" not in ids  # root/rendered notes hidden by default
    assert data["meta"]["date_min"] == "2026-05-01"
    assert data["meta"]["date_max"] == "2026-09-10"
    assert data["meta"]["by_type"]["item"] == 8

    with_briefs = vg.to_json(GraphFilters(include_briefs=True))
    assert "radar" in {n["id"] for n in with_briefs["nodes"]}


def test_focus_depth(vg: VaultGraph):
    d1 = vg.to_json(GraphFilters(focus="Alice", depth=1))
    ids1 = {n["id"] for n in d1["nodes"]}
    assert ids1 == {"Alice", "i1-deck", "i2-runbook", "i3-review", "i8-old", "Weekly Sync", "Infra Agent"}

    d2 = vg.to_json(GraphFilters(focus="Alice", depth=2))
    ids2 = {n["id"] for n in d2["nodes"]}
    assert ids1 < ids2
    assert {"Bob", "i4-budget", "Ghost", "source:gmail"} <= ids2
    assert "i5-intro" not in ids2  # Alice -> i1 -> source:gmail -> i5 is three hops

    ids3 = {n["id"] for n in vg.to_json(GraphFilters(focus="Alice", depth=3))["nodes"]}
    assert ids2 < ids3
    assert "i5-intro" in ids3
    assert "Carol" not in ids3  # four hops; depth is capped at 3


def test_date_range_filter(vg: VaultGraph):
    july = vg.to_json(GraphFilters(date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)))
    items = {n["id"] for n in july["nodes"] if n["type"] == "item"}
    assert "i4-budget" in items  # lived 07-01..07-15
    assert "i6-mystery" in items  # open since June, still overlapping
    assert "i1-deck" not in items  # first seen in August
    assert "i8-old" not in items  # dismissed in May

    sept = vg.to_json(GraphFilters(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)))
    items = {n["id"] for n in sept["nodes"] if n["type"] == "item"}
    assert "i4-budget" not in items
    assert {"i1-deck", "i2-runbook", "i3-review", "i6-mystery"} <= items


def test_status_and_type_filters(vg: VaultGraph):
    data = vg.to_json(GraphFilters(statuses={"resolved"}))
    items = [n for n in data["nodes"] if n["type"] == "item"]
    assert [n["id"] for n in items] == ["i4-budget"]
    assert {"Bob", "Weekly Sync"} <= {n["id"] for n in data["nodes"]}

    only_people = vg.to_json(GraphFilters(types={"person"}))
    assert {n["type"] for n in only_people["nodes"]} == {"person"}


def test_include_orphans_toggle(vg: VaultGraph):
    ids = {n["id"] for n in vg.to_json(GraphFilters(include_orphans=True))["nodes"]}
    assert "Dave" in ids
    ids = {n["id"] for n in vg.to_json(GraphFilters(include_orphans=False))["nodes"]}
    assert "Dave" not in ids


def test_orphan_detection(vg: VaultGraph):
    m = vg.metrics(TODAY)
    assert m.orphans == ["i6-mystery"]
    assert m.components >= 1
    total = sum(len(c.members) for c in m.communities)
    assert total == 14  # 8 items + 4 people + 1 project + 1 meeting (Dave is his own community)
    biggest = m.communities[0]
    assert "Alice" in biggest.members
    assert biggest.open_items >= 3
    assert biggest.mean_open_age_days is not None
    assert biggest.delivery_rate is not None and 0 <= biggest.delivery_rate <= 1


def test_hub_detection(vg: VaultGraph):
    m = vg.metrics(TODAY)
    assert m.hubs[0]["id"] == "Alice"
    assert {h["type"] for h in m.hubs} <= {"item", "person", "project", "meeting"}
    assert m.betweenness and m.betweenness[0]["id"] in {"Alice", "Weekly Sync", "Infra Agent"}


def test_patterns_fire(vg: VaultGraph):
    pats = vg.patterns(TODAY)
    kinds = {p.kind for p in pats}
    assert {"convergence", "oldest_project", "orphans", "no_meeting", "meeting_delivery", "waiting_on_me"} <= kinds
    assert len(pats) <= 12
    assert [p.severity for p in pats] == sorted((p.severity for p in pats), reverse=True)
    g = vg.build()
    assert all(p.focus in g for p in pats)

    by_kind = {p.kind: p for p in pats}
    assert by_kind["convergence"].text == "3 open commitments converge on [[Alice]]"
    assert by_kind["convergence"].focus == "Alice"
    assert by_kind["oldest_project"].text.startswith("[[Infra Agent]] holds the oldest open items (mean 23d)")
    assert by_kind["orphans"].focus == "i6-mystery"
    assert by_kind["no_meeting"].text == "you owe [[Carol]] 1 items and have no meeting with them"
    assert by_kind["meeting_delivery"].text == "commitments made in [[Weekly Sync]] are delivered 1/3"
    assert by_kind["waiting_on_me"].text == "[[Bob]] has 1 items waiting on you, oldest 16d"


def test_export_writes_both_files(vg: VaultGraph, tmp_path: Path):
    pj = tmp_path / "out" / "graph.json"
    pg = tmp_path / "out" / "graph.graphml"
    vg.export(pj, pg)
    data = json.loads(pj.read_text())
    assert data["meta"]["node_count"] == vg.build().number_of_nodes()
    g2 = nx.read_graphml(pg)
    assert g2.number_of_nodes() == vg.build().number_of_nodes()
    assert g2.nodes["Alice"]["type"] == "person"
    assert "resolved" not in g2.nodes["Alice"]  # None attributes dropped


def test_cache_invalidates_when_a_note_changes(vg: VaultGraph, graph_vault: VaultStore):
    g1 = vg.build()
    assert vg.build() is g1
    assert not g1.has_edge("Infra Agent", "Bob")

    path = graph_vault.folder(Project) / "Infra Agent.md"
    path.write_text(path.read_text() + "\nAlso pairs with [[Bob]].\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))

    g2 = vg.build()
    assert g2 is not g1
    assert g2.has_edge("Infra Agent", "Bob")
    assert vg.build(force=True) is not g2
