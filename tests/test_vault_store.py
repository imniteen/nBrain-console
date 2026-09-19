from __future__ import annotations

from datetime import date, timedelta

import frontmatter

from nbrain.vault.schema import Item, ItemStatus, ItemType, Person, Project, Verified
from nbrain.vault.store import VaultStore, dump_note


def _item(**kw) -> Item:
    base = dict(
        title="Post action items in the AI-Infra chat",
        type=ItemType.commitment,
        source="google-chat",
        source_id="spaces/A/messages/1",
        url="https://chat.example/1",
        promised_to="Colleague One",
        project="Infra Agent",
        promised_on=date(2026, 9, 9),
        due=date(2026, 9, 17),
        evidence="I'll post the action items tomorrow.",
    )
    base.update(kw)
    return Item(**base)


def test_upsert_creates_then_updates(vault: VaultStore, today: date):
    item, outcome = vault.upsert_item(_item(), today)
    assert outcome == "created"
    assert item.id.startswith("20260917-post-action-items")
    assert item.first_seen == today
    assert item.promised_to == "[[Colleague One]]"

    again, outcome = vault.upsert_item(_item(title="Post the action items"), today + timedelta(days=1))
    assert outcome == "updated"
    assert again.id == item.id
    assert again.title == "Post the action items"
    assert again.first_seen == today
    assert len(vault.items()) == 1
    assert "Updated: title" in again.body


def test_frontmatter_is_obsidian_friendly(vault: VaultStore, today: date):
    item, _ = vault.upsert_item(_item(), today)
    path = vault.folder(Item) / f"{item.id}.md"
    text = path.read_text()
    assert text.startswith("---\n")
    assert 'promised_to: "[[Colleague One]]"' in text
    assert "due: 2026-09-17" in text  # unquoted date
    post = frontmatter.load(path)
    assert post.metadata["type"] == "commitment"
    assert post.metadata["tags"] == ["nbrain/item"]


def test_dismissed_items_are_skipped(vault: VaultStore, today: date):
    item, _ = vault.upsert_item(_item(), today)
    vault.dismiss_item(item, today, "handled in person")
    _, outcome = vault.upsert_item(_item(), today + timedelta(days=1))
    assert outcome == "skipped-dismissed"
    reloaded = vault.load(Item, item.id)
    assert reloaded.status == ItemStatus.dismissed
    assert reloaded.dismissed_reason == "handled in person"


def test_unconfirmed_streak_moves_to_watch(vault: VaultStore, today: date):
    item, _ = vault.upsert_item(_item(source="gitlab-email", source_id="mr-1"), today)
    assert vault.mark_unconfirmed(item, today, drop_after=3) == "unconfirmed"
    assert item.verified == Verified.unconfirmed
    assert vault.mark_unconfirmed(item, today, drop_after=3) == "unconfirmed"
    assert item.verified == Verified.stale
    assert vault.mark_unconfirmed(item, today, drop_after=3) == "watch"
    assert item.status == ItemStatus.watch
    assert vault.open_items() == []
    # re-seen at the source -> reopened
    back, outcome = vault.upsert_item(_item(source="gitlab-email", source_id="mr-1"), today)
    assert outcome == "reopened"
    assert back.unconfirmed_runs == 0


def test_resolve_and_snapshot(vault: VaultStore, today: date):
    item, _ = vault.upsert_item(_item(), today)
    snap = vault.snapshot_open()
    assert item.id in snap
    vault.resolve_item(item, today)
    assert vault.snapshot_open() == {}
    assert vault.load(Item, item.id).resolved_on == today


def test_people_and_project_attribution(vault: VaultStore):
    vault.save(Person(title="Ada Lovelace", email="ada@example.com", tier=1, aliases=["Ada"]))
    vault.save(Project(title="Infra Agent", keywords=["infra", "agent"], jira_keys=["INF"]))
    assert vault.person_by_email("ADA@example.com").title == "Ada Lovelace"
    assert vault.person_by_name("[[Ada]]").title == "Ada Lovelace"
    assert vault.project_for_text("INF-123 pipeline failing").title == "Infra Agent"
    assert vault.project_for_text("unrelated") is None


def test_unreadable_note_is_skipped(vault: VaultStore, today: date):
    vault.upsert_item(_item(), today)
    (vault.folder(Item) / "broken.md").write_text("---\n: : bad yaml\n---\nbody\n")
    assert len(vault.items()) == 1


def test_dump_note_multiline_body():
    text = dump_note({"title": "x", "url": "https://a.b/c?d=1"}, "line1\n\nline2")
    assert "url: https://a.b/c?d=1" in text
    assert text.endswith("line2\n")
