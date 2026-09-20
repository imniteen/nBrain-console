"""Who is a real person, and how much do I actually work with them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nbrain.config.schema import Config
from nbrain.sources.base import Interaction
from nbrain.vault.people import (
    build_people_graph,
    classify,
    looks_automated,
    people_patterns,
    update_people,
)
from nbrain.vault.schema import Item, ItemType, Person, PersonKind, Project
from nbrain.vault.store import VaultStore

TODAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[VaultStore, Config]:
    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    cfg = Config(vault_path=store.root)
    cfg.user.name, cfg.user.first_name = "Niteen", "Niteen"
    cfg.user.email = "niteen.b@crebrl.ai"
    cfg.noise.phishing = ["notion"]
    return store, cfg


def ix(email=None, name=None, channel="email", ref="r1", group=False, inbound=None, at=NOW, with_me=True):
    return Interaction(
        person_email=email, person_name=name, channel=channel, ref=ref,
        group=group, inbound=inbound, at=at, with_me=with_me,
    )


# ---------- classification ----------


def test_robots_are_recognised_however_human_the_name():
    assert looks_automated("no-reply@gitlab.com", "GitLab")
    assert looks_automated("notifications@slack.com", "Slack")
    assert looks_automated("billing@mail.notion.so", "Catherine at Notion")
    assert looks_automated("bounces+123@sendgrid.net", None)
    assert not looks_automated("mohit@crebrl.ai", "Mohit Bhonde")
    assert not looks_automated("ada.lovelace@client.com", "Ada Lovelace")


def test_a_meeting_is_the_strongest_evidence_of_a_human(setup):
    _, cfg = setup
    kind, why = classify(cfg, email="mohit@crebrl.ai", name="Mohit", interactions=[ix(channel="meeting")])
    assert kind is PersonKind.colleague and "meeting" in why


def test_two_way_correspondence_proves_someone_is_answering(setup):
    _, cfg = setup
    kind, why = classify(cfg, email="linus@client.com", name="Linus",
                         interactions=[ix(inbound=True, ref="a"), ix(inbound=False, ref="b")])
    assert kind is PersonKind.external and "written to each other" in why


def test_one_sighting_stays_unknown_rather_than_guessing(setup):
    _, cfg = setup
    kind, why = classify(cfg, email="someone@elsewhere.com", name="Someone", interactions=[ix(inbound=True)])
    assert kind is PersonKind.unknown and "not enough" in why


def test_phishing_is_never_promoted_to_a_contact(setup):
    _, cfg = setup
    # even with a meeting-shaped interaction, the phishing rule wins
    kind, _ = classify(cfg, email="billing@mail.notion.so", name="Catherine at Notion",
                       interactions=[ix(channel="meeting"), ix(inbound=True, ref="b")])
    assert kind is PersonKind.suspicious


def test_noise_list_marks_automation(setup):
    _, cfg = setup
    cfg.noise.mine = ["jenkins"]
    kind, why = classify(cfg, email="ci@jenkins.example", name="Jenkins", interactions=[ix()])
    assert kind is PersonKind.automation and "noise list" in why


# ---------- counting ----------


def test_counts_are_per_channel_and_idempotent(setup):
    store, cfg = setup
    batch = [
        ix("mohit@crebrl.ai", "Mohit Bhonde", "email", "t1", inbound=True),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "email", "t2", inbound=False),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "meeting", "e1"),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "chat", "s1"),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "slack", "c1", group=True),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "ticket", "INF-1"),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "review", "mr:1:2"),
    ]
    update_people(store, cfg, batch, TODAY)
    p = store.person_by_email("mohit@crebrl.ai")
    assert (p.emails, p.meetings, p.chats, p.group_chats, p.tickets, p.reviews) == (2, 1, 1, 1, 1, 1)
    assert p.total_interactions == 7
    assert p.channel_mix()["email"] == 2
    assert p.one_to_one is True and p.tier == 1  # a 1:1 meeting means never let them wait

    update_people(store, cfg, batch, TODAY)  # same sweep data tomorrow
    assert store.person_by_email("mohit@crebrl.ai").total_interactions == 7, "re-reads must not inflate"


def test_machinery_never_becomes_a_note(setup):
    store, cfg = setup
    res = update_people(store, cfg, [
        ix("no-reply@gitlab.com", "GitLab", "email", "t1", inbound=True),
        ix("billing@mail.notion.so", "Catherine at Notion", "email", "t2", inbound=True),
    ], TODAY)
    assert store.people() == []
    assert len(res.skipped_automation) == 2


def test_being_named_in_content_is_not_a_relationship(setup):
    store, cfg = setup
    update_people(store, cfg, [
        ix("ada@crebrl.ai", "Ada", "email", "t1", inbound=True, with_me=False),
        ix("ada@crebrl.ai", "Ada", "email", "t2", inbound=True, with_me=False),
    ], TODAY)
    p = store.person_by_email("ada@crebrl.ai")
    assert p is not None and p.total_interactions == 0, "mentions must not count as working together"


def test_external_flag_follows_the_domain(setup):
    store, cfg = setup
    update_people(store, cfg, [
        ix("mohit@crebrl.ai", "Mohit", "email", "a", inbound=True),
        ix("mohit@crebrl.ai", "Mohit", "email", "b", inbound=False),
        ix("linus@client.com", "Linus", "email", "c", inbound=True),
        ix("linus@client.com", "Linus", "email", "d", inbound=False),
    ], TODAY)
    assert store.person_by_email("mohit@crebrl.ai").external is False
    assert store.person_by_email("linus@client.com").external is True


def test_a_hand_set_classification_is_respected(setup):
    store, cfg = setup
    p = Person(title="Ada Lovelace", id="Ada Lovelace", email="ada@crebrl.ai", kind=PersonKind.colleague)
    p.kind_evidence = None  # set by hand, not by us
    store.save(p)
    update_people(store, cfg, [ix("ada@crebrl.ai", "Ada Lovelace", "email", "t1", inbound=True)], TODAY)
    assert store.person_by_email("ada@crebrl.ai").kind is PersonKind.colleague


# ---------- the graph ----------


def _populated(store: VaultStore, cfg: Config) -> None:
    update_people(store, cfg, [
        ix("mohit@crebrl.ai", "Mohit Bhonde", "email", "t1", inbound=True),
        ix("mohit@crebrl.ai", "Mohit Bhonde", "meeting", "e1"),
        ix("linus@client.com", "Linus", "email", "t2", inbound=True),
        ix("linus@client.com", "Linus", "email", "t3", inbound=False),
        ix("no-reply@gitlab.com", "GitLab", "email", "t4", inbound=True),
    ], TODAY)
    store.save(Project(title="Atlas", id="Atlas"))
    item = Item(title="Reply to Mohit", type=ItemType.waiting_on_me, source="gmail", source_id="t1",
                promised_to="Mohit Bhonde", project="Atlas", first_seen=TODAY)
    item.id = "20260920-reply-to-mohit"
    store.save(item)


def test_graph_is_about_people_not_sources(setup):
    store, cfg = setup
    _populated(store, cfg)
    g = build_people_graph(store, cfg, TODAY)
    types = {n["type"] for n in g["nodes"]}
    assert "source" not in types, "source nodes were the thing the user did not want"
    assert "me" in types and "person" in types
    me = next(n for n in g["nodes"] if n["type"] == "me")
    assert me["label"] == "Niteen"

    mohit = next(n for n in g["nodes"] if n["label"] == "Mohit Bhonde")
    assert mohit["weight"] == 2 and mohit["kind"] == "colleague"
    assert mohit["open_items"] == 1
    assert "email" in mohit["mix"] and "meetings" in mohit["mix"]

    works = [ln for ln in g["links"] if ln["kind"] == "works_with"]
    assert {ln["source"] for ln in works} == {"__me__"}, "every person hangs off you"
    assert any("email" in str(ln["label"]) for ln in works)


def test_graph_hides_machinery_but_says_how_much(setup):
    store, cfg = setup
    _populated(store, cfg)
    g = build_people_graph(store, cfg, TODAY)
    assert not any(n["label"] == "GitLab" for n in g["nodes"])
    assert g["meta"]["people"] == 2


def test_graph_can_be_narrowed_to_established_relationships(setup):
    store, cfg = setup
    _populated(store, cfg)
    # somebody who has mailed once: real enough to record, too thin to clutter the graph
    update_people(store, cfg, [ix("passerby@elsewhere.com", "Passerby", "email", "t9", inbound=True)], TODAY)

    wide = build_people_graph(store, cfg, TODAY, min_interactions=1)
    narrow = build_people_graph(store, cfg, TODAY, min_interactions=2)
    assert wide["meta"]["people"] == 3
    assert narrow["meta"]["people"] == 2
    assert not any(n["label"] == "Passerby" for n in narrow["nodes"])

    # and they can be dropped by confidence instead of volume
    no_unknown = build_people_graph(store, cfg, TODAY, include_unknown=False)
    assert not any(n.get("kind") == "unknown" for n in no_unknown["nodes"])


def test_patterns_talk_about_relationships(setup):
    store, cfg = setup
    _populated(store, cfg)
    quiet = store.person_by_email("mohit@crebrl.ai")
    quiet.tier = 1
    quiet.last_interaction = TODAY - timedelta(days=30)
    store.save(quiet)

    texts = " ".join(p["text"] for p in people_patterns(store, cfg, TODAY))
    assert "Mohit Bhonde" in texts
    assert "No contact" in texts or "busiest" in texts
    assert "source" not in texts.lower()


# ---------- not confusing yourself with a colleague ----------


def test_you_are_never_your_own_contact(setup):
    store, cfg = setup
    cfg.user.name_variants = ["N. Badgujar"]
    res = update_people(store, cfg, [
        ix("niteen.b@crebrl.ai", "Niteen", "email", "t1", inbound=False),
        ix(None, "Niteen", "chat", "s1"),
        ix(None, "N. Badgujar", "chat", "s2"),
    ], TODAY)
    assert store.people() == [], "your own messages must not create a contact"
    assert len(set(res.skipped_self)) >= 1


def test_a_fuller_version_of_your_name_is_flagged_not_counted(setup):
    """Chat shows 'Niteen Badgujar' while config says 'Niteen', with no email to link them."""
    store, cfg = setup
    res = update_people(store, cfg, [
        ix(None, "Niteen Badgujar", "chat", "s1"),
        ix(None, "Niteen Badgujar", "chat", "s2"),
    ], TODAY)
    p = store.person_by_name("Niteen Badgujar")
    assert p is not None, "it is recorded, so you can see and correct it"
    assert p.total_interactions == 0, "but never counted as someone you work with"
    assert p.kind is PersonKind.unknown
    assert "may be you" in (p.kind_evidence or "")
    assert res.possible_self == ["Niteen Badgujar"]


def test_a_declared_collision_is_treated_as_a_real_colleague(setup):
    store, cfg = setup
    cfg.user.name_collisions = ["Niteen Badgujar"]
    update_people(store, cfg, [
        ix(None, "Niteen Badgujar", "chat", "s1"),
        ix(None, "Niteen Badgujar", "meeting", "e1"),
    ], TODAY)
    p = store.person_by_name("Niteen Badgujar")
    assert p.total_interactions == 2, "you told us they are a different person"


def test_brand_and_role_names_are_automation(setup):
    """'Notion Team' and 'OpenRouter Team' reached the graph as people before this."""
    store, cfg = setup
    cfg.noise.phishing = []
    for name, email in [("Notion Team", "hi@makenotion.com"), ("OpenRouter Team", "team@openrouter.ai"),
                        ("Catherine at Notion", "billing@mail.notion.so"), ("The Slack Team", None)]:
        kind, _ = classify(cfg, email=email, name=name, interactions=[ix()])
        assert kind is PersonKind.automation, name
    assert store.people() == []


def test_one_person_arriving_under_two_identities_is_merged(setup):
    """Email gives an address, chat gives only a name. Splitting them halves the relationship
    and lets the thinner half's evidence win."""
    store, cfg = setup
    update_people(store, cfg, [
        ix("bharat@crebrl.ai", "Bharat Agrawal", "email", "t1", inbound=True),
        ix("bharat@crebrl.ai", "Bharat Agrawal", "email", "t2", inbound=False),
        ix(None, "Bharat Agrawal", "chat", "s1"),
    ], TODAY)
    people = store.people()
    assert len(people) == 1, "one human, one note"
    p = people[0]
    assert p.total_interactions == 3 and p.emails == 2 and p.chats == 1
    assert p.kind is PersonKind.colleague
    assert "seen once" not in (p.kind_evidence or ""), "evidence must reflect the merged record"


def test_a_known_person_absorbs_later_name_only_sightings(setup):
    store, cfg = setup
    update_people(store, cfg, [
        ix("ada@crebrl.ai", "Ada Lovelace", "email", "t1", inbound=True),
        ix("ada@crebrl.ai", "Ada Lovelace", "email", "t2", inbound=False),
    ], TODAY)
    update_people(store, cfg, [ix(None, "Ada Lovelace", "chat", "s9")], TODAY)  # a later sweep
    assert len(store.people()) == 1
    assert store.person_by_email("ada@crebrl.ai").chats == 1


def test_only_one_person_is_the_busiest(setup):
    store, cfg = setup
    for name, n in [("Ada", 6), ("Bob", 4), ("Cara", 2)]:
        update_people(store, cfg, [
            ix(f"{name.lower()}@crebrl.ai", name, "email", f"{name}{i}", inbound=bool(i % 2))
            for i in range(n)
        ], TODAY)
    texts = [p["text"] for p in people_patterns(store, cfg, TODAY) if p["kind"] == "top_contact"]
    assert sum("busiest" in t for t in texts) == 1, "only one contact can be the busiest"
    assert "Ada" in texts[0]
