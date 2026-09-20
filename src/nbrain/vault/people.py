"""Turn raw interactions into people you actually work with.

Two jobs, deliberately separate:

1. **Classify.** An inbox is mostly not people. Build robots, ticket notifications, marketing and
   phishing all arrive looking like correspondents. Guessing wrong in the friendly direction is
   worse than guessing unknown, because a phishing sender promoted to "colleague" then earns a
   place in the graph and the brief.

2. **Count.** How much you work with someone is the sum of threads, meetings, chats, tickets and
   reviews — not a feeling. Counting is deterministic and the evidence is recorded, so a wrong
   number is checkable rather than mysterious.

Every classification carries its reason in `kind_evidence`, and a hand-edited `kind` is never
overwritten, because the person reading the graph knows things the heuristics cannot.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from nbrain.config.schema import Config
from nbrain.sources.base import Interaction
from nbrain.util import safe_filename
from nbrain.vault.schema import Person, PersonKind
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

# Local parts that are machinery wearing a person's clothes.
_ROBOT_LOCAL = re.compile(
    r"^(no-?reply|donot-?reply|do-?not-?reply|notifications?|alerts?|mailer|postmaster|bounces?|"
    r"support|help|info|admin|billing|invoices?|receipts?|news(letter)?|marketing|updates?|"
    r"team|hello|contact|jira|confluence|gitlab|github|bot|ci|build|monitor|security)([+.-]|$)",
    re.I,
)
_ROBOT_DOMAIN = re.compile(r"(^|\.)(mailgun|sendgrid|amazonses|mailchimp|sparkpost|postmark|intercom|zendesk)\.", re.I)
# A display name that is a brand or a role, not a human: "Catherine at Notion", "The Slack Team".
_BRAND_NAME = re.compile(r"\b(at|from|via)\s+[A-Z]\w+|^(the\s+)?\w+\s+team$|support|notifications?", re.I)

MIN_FOR_CONFIDENT = 2  # below this, "unknown" is the honest answer


@dataclass
class PeopleUpdate:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_automation: list[str] = field(default_factory=list)
    skipped_self: list[str] = field(default_factory=list)
    possible_self: list[str] = field(default_factory=list)
    reclassified: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "automation": len(self.skipped_automation),
            "self": len(set(self.skipped_self)),
            "possible_self": len(self.possible_self),
        }


def _domain(email: str | None) -> str:
    return (email or "").rsplit("@", 1)[-1].lower() if email and "@" in email else ""


def looks_automated(email: str | None, name: str | None) -> bool:
    """Machinery, however human the display name looks.

    Either signal is enough on its own. An earlier version required both and let
    "Notion Team" and "OpenRouter Team" through as people."""
    if email:
        local = email.split("@", 1)[0]
        if _ROBOT_LOCAL.match(local) or _ROBOT_DOMAIN.search(_domain(email)):
            return True
    return bool(name and _BRAND_NAME.search(name))


def classify(
    cfg: Config,
    *,
    email: str | None,
    name: str | None,
    interactions: list[Interaction],
) -> tuple[PersonKind, str]:
    """Decide what this address is, and say why in one line."""
    my_domain = _domain(cfg.user.email)
    dom = _domain(email)
    blob = f"{email or ''} {name or ''}".lower()

    for pattern in cfg.noise.phishing:
        if pattern.lower() in blob:
            return PersonKind.suspicious, f"matches a sender you flagged as phishing ({pattern})"

    for pattern in cfg.noise.suppress + cfg.noise.mine:
        if pattern.lower() in blob:
            return PersonKind.automation, f"on your noise list ({pattern})"

    if looks_automated(email, name):
        return PersonKind.automation, "address or display name looks like a system, not a person"

    # Behaviour beats appearance: a meeting or a two-way conversation is strong evidence of a human.
    met = [i for i in interactions if i.channel == "meeting" and i.with_me]
    two_way = {i.inbound for i in interactions if i.with_me and i.inbound is not None}
    channels = {i.channel for i in interactions if i.with_me}

    if met:
        where = "an internal" if dom and dom == my_domain else "a"
        return (
            PersonKind.colleague if dom == my_domain else PersonKind.external,
            f"in {where} meeting with you ({len(met)} in the window)",
        )
    if len(two_way) == 2:
        kind = PersonKind.colleague if dom == my_domain else PersonKind.external
        return kind, "you have written to each other, so a human is answering"
    if len(channels) >= 2:
        kind = PersonKind.colleague if dom == my_domain else PersonKind.external
        return kind, f"you deal with them on {len(channels)} channels ({', '.join(sorted(channels))})"
    if dom and dom == my_domain and len(interactions) >= MIN_FOR_CONFIDENT:
        return PersonKind.colleague, f"on your own domain, seen {len(interactions)} times"
    if len(interactions) >= MIN_FOR_CONFIDENT:
        return PersonKind.external, f"outside {my_domain or 'your domain'}, seen {len(interactions)} times"
    return PersonKind.unknown, "seen once; not enough to tell a person from a system"


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def is_self(cfg: Config, email: str | None, name: str | None) -> bool:
    """Am I this person? Each source has its own is-me check; this is the net beneath them."""
    if email and cfg.user.email and email.lower() == cfg.user.email.lower():
        return True
    mine = {_norm(cfg.user.name), _norm(cfg.user.first_name), *(_norm(v) for v in cfg.user.name_variants)}
    mine.discard("")
    return _norm(name) in mine


def maybe_self(cfg: Config, email: str | None, name: str | None) -> bool:
    """A display name that starts with my first name and carries no address to disprove it.

    Chat and Slack often give a fuller name than the one in config, so "Niteen" in settings and
    "Niteen Badgujar" in chat are the same person with no email to link them. Counting that as a
    colleague would quietly attribute my own activity to someone else, so it is recorded but not
    counted, with the reason attached."""
    if email or not name:
        return False
    first = _norm(cfg.user.first_name) or _norm(cfg.user.name).split(" ")[0]
    if not first:
        return False
    if _norm(name) in {_norm(c) for c in cfg.user.name_collisions}:
        return False  # the user has told us this is somebody else
    return _norm(name).split(" ")[0] == first


def _match(store: VaultStore, email: str | None, name: str | None) -> Person | None:
    return store.person_by_email(email) or (store.person_by_name(name) if name else None)


def update_people(
    store: VaultStore,
    cfg: Config,
    interactions: list[Interaction],
    today: date,
) -> PeopleUpdate:
    """Fold this sweep's interactions into the People notes.

    Idempotent: each interaction carries a stable key and a person only counts it once, so
    re-reading the same thread tomorrow does not inflate the relationship."""
    result = PeopleUpdate()
    if not interactions:
        return result

    # One human can arrive under two identities in a single sweep: email threads carry an address,
    # chat often carries only a display name. Grouping naively splits them, which both halves the
    # counts and lets the smaller half's evidence win. Resolve names to addresses first.
    name_to_email: dict[str, str] = {}
    for i in interactions:
        if i.person_email and i.person_name:
            name_to_email.setdefault(_norm(i.person_name), i.person_email.lower())
    for person in store.people():
        if person.email:
            name_to_email.setdefault(_norm(person.title), person.email.lower())
            for alias in person.aliases:
                name_to_email.setdefault(_norm(alias), person.email.lower())

    grouped: dict[str, list[Interaction]] = defaultdict(list)
    display: dict[str, str] = {}
    for i in interactions:
        if is_self(cfg, i.person_email, i.person_name):
            result.skipped_self.append(i.person_name or i.person_email or "?")
            continue
        ident = (i.person_email or "").lower() or name_to_email.get(_norm(i.person_name), "") or _norm(i.person_name)
        if not ident:
            continue
        grouped[ident].append(i)
        if i.person_name and ident not in display:
            display[ident] = i.person_name
    grouped_named = {(ident, display.get(ident, ident)): group for ident, group in grouped.items()}

    for (_, shown_as), group in grouped_named.items():
        email = next((i.person_email for i in group if i.person_email), None)
        name = next((i.person_name for i in group if i.person_name), None) or shown_as
        kind, why = classify(cfg, email=email, name=name, interactions=group)
        unsure_self = maybe_self(cfg, email, name)
        if unsure_self:
            kind, why = (
                PersonKind.unknown,
                f"shares your first name and has no address, so this may be you — "
                f"set user.name to your full name, or list '{name}' under name collisions",
            )
            result.possible_self.append(name or "?")

        person = _match(store, email, name)
        if kind in (PersonKind.automation, PersonKind.suspicious) and person is None:
            # Do not create a note for machinery; it would clutter the vault and the graph.
            result.skipped_automation.append(name or email or "?")
            continue

        if person is None:
            person = Person(title=safe_filename(name or email or "unknown"), email=email)
            person.id = person.title
            person.kind, person.kind_evidence = kind, why
            result.created.append(person.id)
        else:
            if person.email is None and email:
                person.email = email
            # A hand-set classification wins: the user knows things the heuristics do not.
            if person.kind == PersonKind.unknown or person.kind_evidence:
                if person.kind != kind:
                    result.reclassified.append(f"{person.id}: {person.kind.value} → {kind.value}")
                person.kind, person.kind_evidence = kind, why
            if person.id not in result.created:
                result.updated.append(person.id)

        seen = set(person.interaction_refs)
        for i in group:
            key = i.key()
            if key in seen:
                continue
            seen.add(key)
            if unsure_self or not i.with_me:
                continue  # merely named in something I read; not a relationship
            if i.channel == "email":
                person.emails += 1
            elif i.channel == "meeting":
                person.meetings += 1
                if not i.group:
                    person.one_to_one = True
            elif i.channel in ("chat", "slack"):
                if i.group:
                    person.group_chats += 1
                else:
                    person.chats += 1
            elif i.channel == "ticket":
                person.tickets += 1
            elif i.channel == "review":
                person.reviews += 1
            when = i.at.date() if i.at else today
            person.first_interaction = min(person.first_interaction or when, when)
            person.last_interaction = max(person.last_interaction or when, when)

        # keep the dedupe list bounded; oldest keys matter least
        person.interaction_refs = sorted(seen)[-400:]
        person.external = bool(person.email) and _domain(person.email) != _domain(cfg.user.email)
        _retier(person)
        store.save(person)

    return result


def _retier(person: Person) -> None:
    """Tier from behaviour, but never override a tier the user set by hand.

    Tier 1 is "never let this wait". A standing one-to-one is the clearest signal of that;
    sheer volume is the next best."""
    if person.relationship in ("manager", "report"):
        person.tier = 1
        return
    if person.one_to_one or person.total_interactions >= 15:
        person.tier = min(person.tier, 1)
    elif person.total_interactions >= 4:
        person.tier = min(person.tier, 2)


def top_people(store: VaultStore, limit: int = 12) -> list[Person]:
    real = [p for p in store.people() if p.is_real_person() and p.total_interactions]
    return sorted(real, key=lambda p: -p.total_interactions)[:limit]


# --------------------------------------------------------------------------------------
# The people graph: you at the centre, the humans around you, and what actually connects
# you to each. Deliberately not the vault graph — no source nodes, no brief nodes.
# --------------------------------------------------------------------------------------

_KIND_ORDER = {
    PersonKind.colleague: 0,
    PersonKind.external: 1,
    PersonKind.unknown: 2,
    PersonKind.automation: 3,
    PersonKind.suspicious: 4,
}


def _edge_label(person: Person) -> str:
    mix = person.channel_mix()
    return ", ".join(f"{n} {k}" for k, n in list(mix.items())[:3]) or "no activity yet"


def build_people_graph(
    store: VaultStore,
    cfg: Config,
    today: date,
    *,
    include_unknown: bool = True,
    include_automation: bool = False,
    min_interactions: int = 1,
) -> dict[str, object]:
    """Nodes and links for the people view, in the shape the graph page already renders."""
    me = cfg.user.first_name or cfg.user.name or "You"
    nodes: list[dict[str, object]] = [
        {
            "id": "__me__",
            "label": me,
            "type": "me",
            "degree": 0,
            "preview": cfg.user.email or "",
            "weight": 0,
        }
    ]
    links: list[dict[str, object]] = []

    people = store.people()
    shown: list[Person] = []
    for p in people:
        if p.kind == PersonKind.automation and not include_automation:
            continue
        if p.kind == PersonKind.suspicious:
            continue  # never a contact
        if p.kind == PersonKind.unknown and not include_unknown:
            continue
        if p.total_interactions < min_interactions:
            continue
        shown.append(p)

    items = [i for i in store.items() if i.status.value in ("open", "watch")]
    by_person: dict[str, list] = defaultdict(list)
    for it in items:
        if it.person:
            by_person[it.person.lower()].append(it)

    for p in sorted(shown, key=lambda x: (_KIND_ORDER[x.kind], -x.total_interactions)):
        mine = by_person.get(p.title.lower(), [])
        last = p.last_interaction
        quiet = (today - last).days if last else None
        nodes.append(
            {
                "id": p.id,
                "label": p.title,
                "type": "person",
                "kind": p.kind.value,
                "tier": p.tier,
                "weight": p.total_interactions,
                "degree": max(1, p.total_interactions),
                "open_items": len(mine),
                "one_to_one": p.one_to_one,
                "quiet_days": quiet,
                "date": last.isoformat() if last else None,
                "mix": p.channel_mix(),
                "preview": f"{_edge_label(p)}{f' · {len(mine)} open with you' if mine else ''}",
                "url": None,
            }
        )
        links.append(
            {
                "source": "__me__",
                "target": p.id,
                "kind": "works_with",
                "weight": p.total_interactions,
                "label": _edge_label(p),
            }
        )
        # what the relationship is actually about
        for it in mine[:6]:
            nodes.append(
                {
                    "id": it.id,
                    "label": it.title,
                    "type": "item",
                    "status": it.status.value,
                    "item_type": it.type.value,
                    "degree": 1,
                    "date": (it.due or it.first_seen).isoformat() if (it.due or it.first_seen) else None,
                    "preview": it.evidence or "",
                    "url": it.url,
                }
            )
            links.append({"source": p.id, "target": it.id, "kind": "owes", "weight": 1, "label": it.type.value})
        for proj in {it.project_name for it in mine if it.project_name}:
            links.append({"source": p.id, "target": proj, "kind": "project", "weight": 1, "label": "project"})

    seen = {n["id"] for n in nodes}
    for proj in store.projects():
        if any(link["target"] == proj.title for link in links):
            if proj.title not in seen:
                nodes.append({"id": proj.title, "label": proj.title, "type": "project", "degree": 1, "weight": proj.weight, "preview": proj.status or ""})
                seen.add(proj.title)

    links = [ln for ln in links if ln["source"] in seen and ln["target"] in seen]
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "mode": "people",
            "node_count": len(nodes),
            "link_count": len(links),
            "people": len(shown),
            "hidden_automation": sum(1 for p in people if p.kind == PersonKind.automation),
            "hidden_suspicious": sum(1 for p in people if p.kind == PersonKind.suspicious),
            "by_kind": {k.value: sum(1 for p in shown if p.kind == k) for k in PersonKind},
        },
    }


def people_patterns(store: VaultStore, cfg: Config, today: date) -> list[dict[str, object]]:
    """Findings about relationships, not about note links."""
    out: list[dict[str, object]] = []
    people = [p for p in store.people() if p.is_real_person()]
    items = [i for i in store.items() if i.status.value == "open"]
    owed: dict[str, int] = defaultdict(int)
    for it in items:
        if it.person:
            owed[it.person] += 1

    ranked = [p for p in sorted(people, key=lambda x: -x.total_interactions) if p.channel_mix()]
    for rank, p in enumerate(ranked[:3]):
        lead = "Your busiest contact is" if rank == 0 else "You also work closely with"
        out.append({
            "text": f"{lead} [[{p.title}]]: {_edge_label(p)}",
            "focus": p.id, "severity": 1, "kind": "top_contact",
        })

    for name, n in sorted(owed.items(), key=lambda kv: -kv[1]):
        if n >= 2:
            out.append({"text": f"You owe [[{name}]] {n} things", "focus": name, "severity": 3, "kind": "owes"})

    for p in people:
        if p.tier == 1 and p.last_interaction and (today - p.last_interaction).days >= 14:
            out.append({
                "text": f"No contact with [[{p.title}]] for {(today - p.last_interaction).days} days, and they are tier 1",
                "focus": p.id, "severity": 2, "kind": "gone_quiet",
            })

    for p in people:
        if p.meetings and not (p.emails or p.chats or p.group_chats):
            out.append({
                "text": f"[[{p.title}]] only ever appears in meetings, never in writing",
                "focus": p.id, "severity": 1, "kind": "meeting_only",
            })

    return sorted(out, key=lambda d: -int(d["severity"]))[:12]
