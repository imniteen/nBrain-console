"""Relationship graph over the vault: notes are nodes, wikilinks are edges.

The graph is rebuilt lazily whenever a note file changes (keyed on the newest mtime and the
note count), so a long-running web process stays in step with human edits in Obsidian.
Unresolved wikilinks become ``phantom`` nodes, as Obsidian shows them, and every distinct item
``source`` becomes a synthetic ``source:<name>`` node so the graph shows where items come from.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

import frontmatter
import networkx as nx

from nbrain.util import find_wikilinks, unlink
from nbrain.vault.schema import Item, ItemStatus, ItemType, Meeting, Note, Person, Project
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

LinkKind = str  # promised_to|project|meeting|attendee|mention|surfaced_in|source|other
LINK_KINDS = ("promised_to", "project", "meeting", "attendee", "mention", "surfaced_in", "source", "other")

CORE_TYPES = frozenset({"item", "person", "project", "meeting"})
HIDEABLE_TYPES = frozenset({"brief", "review", "sweep", "note"})  # rendered / log notes

_FOLDER_TYPES: dict[str, str] = {
    "Items": "item",
    "People": "person",
    "Projects": "project",
    "Meetings": "meeting",
    "Briefs": "brief",
    "Reviews": "review",
    "Sweeps": "sweep",
}
_TYPED_MODELS: dict[str, type[Note]] = {
    "Items": Item,
    "People": Person,
    "Projects": Project,
    "Meetings": Meeting,
}
# frontmatter field -> link kind, per note type
_FM_LINK_FIELDS: dict[str, dict[str, LinkKind]] = {
    "item": {
        "promised_to": "promised_to",
        "project": "project",
        "meeting": "meeting",
        "surfaced_in": "surfaced_in",
    },
    "meeting": {"attendees": "attendee"},
}
_WS = re.compile(r"\s+")
_MD_NOISE = re.compile(r"[#>*_`]+")


# ---------------------------------------------------------------- data classes


@dataclass
class GraphNode:
    id: str
    label: str
    type: str  # item|person|project|meeting|brief|review|sweep|note|source|phantom
    folder: str
    status: str | None = None
    item_type: str | None = None
    date: str | None = None  # items: first_seen; briefs: the day
    resolved: str | None = None  # items: resolved_on or dismissed_on
    degree: int = 0
    preview: str = ""
    url: str | None = None
    tier: int | None = None
    weight: float | None = None


@dataclass
class GraphLink:
    source: str
    target: str
    kind: LinkKind


@dataclass
class GraphFilters:
    types: set[str] | None = None
    statuses: set[str] | None = None
    date_from: date | None = None
    date_to: date | None = None
    project: str | None = None
    person: str | None = None
    include_briefs: bool = False
    include_orphans: bool = True
    focus: str | None = None
    depth: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "types": sorted(self.types) if self.types else None,
            "statuses": sorted(self.statuses) if self.statuses else None,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "project": self.project,
            "person": self.person,
            "include_briefs": self.include_briefs,
            "include_orphans": self.include_orphans,
            "focus": self.focus,
            "depth": self.depth,
        }


@dataclass
class Community:
    id: int
    label: str
    members: list[str]
    open_items: int
    mean_open_age_days: float | None
    delivery_rate: float | None


@dataclass
class GraphMetrics:
    hubs: list[dict[str, Any]]
    betweenness: list[dict[str, Any]]
    communities: list[Community]
    orphans: list[str]
    components: int


@dataclass
class Pattern:
    text: str
    focus: str | None
    severity: int  # 1..3
    kind: str


# ---------------------------------------------------------------- helpers


def _preview(text: str, limit: int = 160) -> str:
    text = re.sub(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), text or "")
    text = _MD_NOISE.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _is_link(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("[[") and value.strip().endswith("]]")


def _link_targets(value: Any) -> list[str]:
    """Wikilink target names from a frontmatter value (str or list of str)."""
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for v in values:
        if _is_link(v):
            t = unlink(v)
            if t:
                out.append(t)
    return out


@dataclass
class _Parsed:
    node: GraphNode
    path: Path
    meta: dict[str, Any]
    body: str
    model: Note | None
    aliases: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- graph


class VaultGraph:
    """Build and query the relationship graph of a vault."""

    def __init__(self, store: VaultStore):
        self.store = store
        self._graph: nx.Graph | None = None
        self._stamp: tuple[int, int] | None = None
        self._items: dict[str, Item] = {}
        self._people: dict[str, Person] = {}
        self._projects: dict[str, Project] = {}
        self._meetings: dict[str, Meeting] = {}

    # ---------- build / cache ----------

    def _fingerprint(self) -> tuple[int, int]:
        paths = list(self.store.all_note_paths())
        newest = max((p.stat().st_mtime_ns for p in paths), default=0)
        return len(paths), newest

    def build(self, force: bool = False) -> nx.Graph:
        stamp = self._fingerprint()
        if not force and self._graph is not None and stamp == self._stamp:
            return self._graph
        log.debug("Rebuilding vault graph (%d notes)", stamp[0])
        self._graph = self._build()
        self._stamp = stamp
        return self._graph

    def _read(self, path: Path) -> _Parsed | None:
        try:
            post = frontmatter.load(path)
        except Exception as e:  # malformed YAML must not kill the graph
            log.warning("Graph: skipping unreadable note %s: %s", path, e)
            return None
        meta = dict(post.metadata)
        body = post.content or ""
        rel = path.relative_to(self.store.root)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        ntype = _FOLDER_TYPES.get(folder, "note")
        model: Note | None = None
        cls = _TYPED_MODELS.get(folder)
        if cls is not None:
            try:
                model = cls.from_frontmatter(meta, body, path.stem)
            except Exception as e:
                log.warning("Graph: invalid %s note %s: %s", folder, path, e)
        title = str(meta.get("title") or path.stem)
        node = GraphNode(id=path.stem, label=title, type=ntype, folder=folder)
        aliases = [str(a) for a in (meta.get("aliases") or []) if a]
        if isinstance(model, Item):
            node.status = model.status.value
            node.item_type = model.type.value
            node.date = _iso(model.first_seen)
            node.resolved = _iso(model.resolved_on or model.dismissed_on)
            node.url = model.url
            node.preview = _preview(model.evidence or body)
        elif isinstance(model, Person):
            node.tier = model.tier
            node.preview = _preview(" · ".join(x for x in (model.role, model.relationship, model.email) if x) or body)
        elif isinstance(model, Project):
            node.weight = model.weight
            node.status = model.status
            node.preview = _preview(model.milestone or body)
        elif isinstance(model, Meeting):
            node.preview = _preview(model.purpose or model.cadence or body)
        else:
            node.preview = _preview(body)
            if ntype == "brief" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
                node.date = path.stem
        return _Parsed(node=node, path=path, meta=meta, body=body, model=model, aliases=aliases)

    def _build(self) -> nx.Graph:
        g = nx.Graph()
        self._items, self._people, self._projects, self._meetings = {}, {}, {}, {}

        parsed: list[_Parsed] = []
        used_ids: set[str] = set()
        for path in sorted(self.store.all_note_paths(), key=lambda p: (p.parent != self.store.root, str(p))):
            p = self._read(path)
            if p is None:
                continue
            if p.node.id in used_ids:  # same stem in two folders: disambiguate the later one
                p.node.id = f"{p.node.folder}/{p.node.id}" if p.node.folder else f"{p.node.id}~{len(used_ids)}"
            used_ids.add(p.node.id)
            parsed.append(p)

        # resolver: stem, Folder/stem, aliases (all case-insensitive) -> node id
        index: dict[str, str] = {}
        for p in parsed:
            index.setdefault(p.path.stem.lower(), p.node.id)
            if p.node.folder:
                index.setdefault(f"{p.node.folder}/{p.path.stem}".lower(), p.node.id)
            for a in p.aliases:
                index.setdefault(a.lower(), p.node.id)
        for p in parsed:
            index.setdefault(p.node.label.lower(), p.node.id)

        for p in parsed:
            g.add_node(p.node.id, **asdict(p.node))
            if isinstance(p.model, Item):
                self._items[p.node.id] = p.model
            elif isinstance(p.model, Person):
                self._people[p.node.id] = p.model
            elif isinstance(p.model, Project):
                self._projects[p.node.id] = p.model
            elif isinstance(p.model, Meeting):
                self._meetings[p.node.id] = p.model

        phantoms: dict[str, str] = {}

        def resolve(name: str) -> str:
            key = name.strip().lower()
            if key in index:
                return index[key]
            short = key.rsplit("/", 1)[-1]
            if short in index:
                return index[short]
            if key not in phantoms:
                pid = name.strip()
                if pid in g:
                    pid = f"phantom:{pid}"
                phantoms[key] = pid
                g.add_node(
                    pid,
                    **asdict(GraphNode(id=pid, label=name.strip().rsplit("/", 1)[-1], type="phantom", folder="")),
                )
            return phantoms[key]

        def add_link(a: str, b: str, kind: LinkKind) -> None:
            if a == b or g.has_edge(a, b):
                return
            g.add_edge(a, b, kind=kind)

        for p in parsed:
            nid = p.node.id
            fields = _FM_LINK_FIELDS.get(p.node.type, {})
            for key, value in p.meta.items():
                targets = _link_targets(value)
                if not targets:
                    continue
                kind = fields.get(key, "other")
                for t in targets:
                    add_link(nid, resolve(t), kind)
            for t in find_wikilinks(p.body):
                add_link(nid, resolve(t), "mention")
            if isinstance(p.model, Item) and p.model.source:
                sid = f"source:{p.model.source}"
                if sid not in g:
                    g.add_node(
                        sid,
                        **asdict(GraphNode(id=sid, label=p.model.source, type="source", folder="")),
                    )
                add_link(nid, sid, "source")

        for n, deg in g.degree():
            g.nodes[n]["degree"] = deg
        return g

    # ---------- accessors ----------

    @property
    def graph(self) -> nx.Graph:
        return self.build()

    def nodes(self) -> list[GraphNode]:
        return [GraphNode(**self.graph.nodes[n]) for n in self.graph.nodes]

    def links(self) -> list[GraphLink]:
        return [GraphLink(a, b, d.get("kind", "other")) for a, b, d in self.graph.edges(data=True)]

    def _neighbors_of_type(self, g: nx.Graph, n: str, ntype: str) -> list[str]:
        return [m for m in g.neighbors(n) if g.nodes[m].get("type") == ntype]

    def _core(self, g: nx.Graph | None = None) -> nx.Graph:
        g = g or self.graph
        return g.subgraph([n for n, d in g.nodes(data=True) if d.get("type") in CORE_TYPES])

    # ---------- filtering / json ----------

    def _filter(self, filters: GraphFilters) -> nx.Graph:
        g = self.graph
        keep: list[str] = []
        for n, d in g.nodes(data=True):
            t = d.get("type")
            if not filters.include_briefs and t in HIDEABLE_TYPES:
                continue
            if filters.types and t not in filters.types:
                continue
            if t == "item":
                if filters.statuses and d.get("status") not in filters.statuses:
                    continue
                if (filters.date_from or filters.date_to) and not self._in_range(d, filters):
                    continue
            keep.append(n)
        sub = g.subgraph(keep)

        for center in (filters.project, filters.person):
            if center and center in sub:
                sub = nx.ego_graph(sub, center, radius=2)
        if filters.focus and filters.focus in sub:
            depth = max(1, min(3, filters.depth or 1))
            sub = nx.ego_graph(sub, filters.focus, radius=depth)
        elif filters.focus and filters.focus in g:
            # focus node exists but is hidden by another filter: show it anyway with its ego
            depth = max(1, min(3, filters.depth or 1))
            ego = nx.ego_graph(g, filters.focus, radius=depth)
            sub = g.subgraph(set(sub.nodes) & set(ego.nodes) | {filters.focus})
        if not filters.include_orphans:
            sub = sub.subgraph([n for n, deg in sub.degree() if deg > 0 or n == filters.focus])
        return sub

    @staticmethod
    def _in_range(d: dict[str, Any], f: GraphFilters) -> bool:
        start = date.fromisoformat(d["date"]) if d.get("date") else None
        end = date.fromisoformat(d["resolved"]) if d.get("resolved") else None
        if start is None:
            return True  # undated items are never excluded by a range
        if f.date_to and start > f.date_to:
            return False
        if f.date_from and end is not None and end < f.date_from:
            return False
        return True

    def to_json(self, filters: GraphFilters | None = None) -> dict[str, Any]:
        filters = filters or GraphFilters()
        full = self.graph
        sub = self._filter(filters)
        community_of = self._community_index()
        nodes: list[dict[str, Any]] = []
        for n in sub.nodes:
            d = dict(full.nodes[n])
            d["degree"] = sub.degree(n)
            d["total_degree"] = full.degree(n)
            d["community"] = community_of.get(n)
            nodes.append(d)
        nodes.sort(key=lambda d: (d["type"], d["id"]))
        links = [
            {"source": a, "target": b, "kind": d.get("kind", "other")}
            for a, b, d in sorted(sub.edges(data=True), key=lambda e: (e[0], e[1]))
        ]
        dates = [d["date"] for d in full.nodes.values() if d.get("type") == "item" and d.get("date")]
        dates += [d["resolved"] for d in full.nodes.values() if d.get("type") == "item" and d.get("resolved")]
        meta = {
            "node_count": len(nodes),
            "link_count": len(links),
            "total_nodes": full.number_of_nodes(),
            "total_links": full.number_of_edges(),
            "by_type": dict(sorted(Counter(d["type"] for d in nodes).items())),
            "by_status": dict(sorted(Counter(d["status"] for d in nodes if d["type"] == "item" and d.get("status")).items())),
            "by_kind": dict(sorted(Counter(link["kind"] for link in links).items())),
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
            "types": sorted({d["type"] for d in full.nodes.values()}),
            "statuses": [s.value for s in ItemStatus],
            "link_kinds": list(LINK_KINDS),
            "filters": filters.to_dict(),
        }
        return {"nodes": nodes, "links": links, "meta": meta}

    # ---------- metrics ----------

    def _communities(self) -> list[set[str]]:
        core = self._core()
        if core.number_of_nodes() == 0:
            return []
        comms = list(nx.algorithms.community.label_propagation_communities(core))
        comms.sort(key=lambda c: (-len(c), sorted(c)[0]))
        return [set(c) for c in comms]

    def _community_index(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i, c in enumerate(self._communities()):
            for n in c:
                out[n] = i
        return out

    def _top(self, scores: dict[str, float], k: int = 10) -> list[dict[str, Any]]:
        g = self.graph
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [
            {
                "id": n,
                "label": g.nodes[n].get("label", n),
                "type": g.nodes[n].get("type"),
                "degree": g.degree(n),
                "score": round(float(s), 4),
            }
            for n, s in ranked
            if s > 0
        ]

    def orphan_items(self) -> list[str]:
        """Open items with no project and no person link (frontmatter or body mention)."""
        g = self.graph
        out: list[str] = []
        for nid, it in self._items.items():
            if it.status != ItemStatus.open:
                continue
            if self._neighbors_of_type(g, nid, "project") or self._neighbors_of_type(g, nid, "person"):
                continue
            out.append(nid)
        out.sort(key=lambda n: (self._items[n].first_seen or date.max, n))
        return out

    def metrics(self, today: date | None = None) -> GraphMetrics:
        today = today or date.today()
        g = self.graph
        core = self._core(g)
        hubs = self._top(nx.degree_centrality(core)) if core.number_of_nodes() else []
        between = self._top(nx.betweenness_centrality(core)) if core.number_of_nodes() > 2 else []
        communities: list[Community] = []
        for i, members in enumerate(self._communities()):
            items = [self._items[m] for m in members if m in self._items]
            open_items = [it for it in items if it.status == ItemStatus.open]
            resolved = [it for it in items if it.status == ItemStatus.resolved]
            ages = [a for a in (it.age_days(today) for it in open_items) if a is not None]
            judged = len(open_items) + len(resolved)
            anchor = max(
                (m for m in members if g.nodes[m].get("type") != "item"),
                key=lambda m: (g.degree(m), m),
                default=None,
            )
            communities.append(
                Community(
                    id=i,
                    label=g.nodes[anchor]["label"] if anchor else f"community {i}",
                    members=sorted(members),
                    open_items=len(open_items),
                    mean_open_age_days=round(mean(ages), 1) if ages else None,
                    delivery_rate=round(len(resolved) / judged, 3) if judged else None,
                )
            )
        return GraphMetrics(
            hubs=hubs,
            betweenness=between,
            communities=communities,
            orphans=self.orphan_items(),
            components=nx.number_connected_components(core) if core.number_of_nodes() else 0,
        )

    # ---------- patterns ----------

    def _open_items_by(self, kind: LinkKind, pred: Any = None) -> dict[str, list[Item]]:
        """Open items grouped by the node they link to with edge kind ``kind``."""
        g = self.graph
        out: dict[str, list[Item]] = defaultdict(list)
        for nid, it in self._items.items():
            if it.status != ItemStatus.open or (pred and not pred(it)):
                continue
            for m in g.neighbors(nid):
                if g.edges[nid, m].get("kind") == kind:
                    out[m].append(it)
        return out

    def patterns(self, today: date) -> list[Pattern]:
        g = self.graph
        found: list[Pattern] = []

        def label(n: str) -> str:
            return g.nodes[n].get("label", n)

        def age(it: Item) -> int:
            return it.age_days(today) or 0

        owed_by_person = self._open_items_by("promised_to", lambda it: it.type != ItemType.waiting_on_me)
        waiting_by_person = self._open_items_by("promised_to", lambda it: it.type == ItemType.waiting_on_me)

        # (a) convergence
        for pid, items in sorted(owed_by_person.items()):
            if len(items) >= 3 and g.nodes[pid].get("type") in ("person", "phantom"):
                found.append(
                    Pattern(
                        text=f"{len(items)} open commitments converge on [[{label(pid)}]]",
                        focus=pid,
                        severity=3 if len(items) >= 5 else 2,
                        kind="convergence",
                    )
                )

        # (b) project holding the oldest open items
        by_project = self._open_items_by("project")
        by_project = {p: its for p, its in by_project.items() if g.nodes[p].get("type") == "project" and len(its) >= 2}
        if by_project:
            worst = max(by_project.items(), key=lambda kv: (mean(age(i) for i in kv[1]), kv[0]))
            pid, items = worst
            mean_age = round(mean(age(i) for i in items))
            slipping = sum(1 for i in items if i.type == ItemType.slipping or i.is_overdue(today))
            found.append(
                Pattern(
                    text=f"[[{label(pid)}]] holds the oldest open items (mean {mean_age}d) and {slipping} slipping",
                    focus=pid,
                    severity=3 if slipping >= 2 or mean_age >= 30 else 2 if mean_age >= 7 else 1,
                    kind="oldest_project",
                )
            )

        # (c) orphans
        orphans = self.orphan_items()
        if orphans:
            found.append(
                Pattern(
                    text=f"{len(orphans)} items have no project or person, likely mis-attributed",
                    focus=orphans[0],
                    severity=2 if len(orphans) >= 5 else 1,
                    kind="orphans",
                )
            )

        # (d) owed to a person we never meet
        attendees: set[str] = set()
        for mid in self._meetings:
            attendees.update(self._neighbors_of_type(g, mid, "person"))
        for pid, items in sorted(owed_by_person.items()):
            if g.nodes[pid].get("type") == "person" and pid not in attendees:
                found.append(
                    Pattern(
                        text=f"you owe [[{label(pid)}]] {len(items)} items and have no meeting with them",
                        focus=pid,
                        severity=2 if len(items) >= 2 else 1,
                        kind="no_meeting",
                    )
                )

        # (e) meeting delivery
        for mid in sorted(self._meetings):
            linked = [
                self._items[n]
                for n in g.neighbors(mid)
                if n in self._items and g.edges[mid, n].get("kind") == "meeting"
            ]
            judged = [i for i in linked if i.status in (ItemStatus.open, ItemStatus.resolved)]
            if len(linked) >= 3 and judged:
                delivered = sum(1 for i in judged if i.status == ItemStatus.resolved)
                rate = delivered / len(judged)
                found.append(
                    Pattern(
                        text=f"commitments made in [[{label(mid)}]] are delivered {delivered}/{len(judged)}",
                        focus=mid,
                        severity=3 if rate < 0.34 else 2 if rate < 0.67 else 1,
                        kind="meeting_delivery",
                    )
                )

        # (f) waiting on me, per person
        for pid, items in sorted(waiting_by_person.items()):
            oldest = max(age(i) for i in items)
            found.append(
                Pattern(
                    text=f"[[{label(pid)}]] has {len(items)} items waiting on you, oldest {oldest}d",
                    focus=pid,
                    severity=3 if oldest >= 7 or len(items) >= 3 else 2,
                    kind="waiting_on_me",
                )
            )

        found.sort(key=lambda p: (-p.severity, p.kind, p.text))
        return found[:12]

    # ---------- export ----------

    def export(self, path_json: Path, path_graphml: Path | None = None) -> None:
        path_json = Path(path_json)
        path_json.parent.mkdir(parents=True, exist_ok=True)
        path_json.write_text(json.dumps(self.to_json(GraphFilters(include_briefs=True)), indent=2, default=str))
        if path_graphml is None:
            return
        path_graphml = Path(path_graphml)
        path_graphml.parent.mkdir(parents=True, exist_ok=True)
        g = nx.Graph()
        for n, d in self.graph.nodes(data=True):
            g.add_node(n, **_graphml_attrs(d))
        for a, b, d in self.graph.edges(data=True):
            g.add_edge(a, b, **_graphml_attrs(d))
        nx.write_graphml(g, path_graphml)
        log.info("Exported graph: %s, %s", path_json, path_graphml)


def _graphml_attrs(d: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in d.items() if v is not None}


def pattern_dicts(patterns: Iterable[Pattern]) -> list[dict[str, Any]]:
    return [asdict(p) for p in patterns]
