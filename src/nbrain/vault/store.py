"""Read and write the Obsidian vault. The vault is the source of truth; nothing is cached
elsewhere. All logic keys off frontmatter, and human edits to Items/People/Projects are honoured."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

import frontmatter
import yaml

from nbrain.util import safe_filename, slugify, unlink, wikilink
from nbrain.vault.schema import (
    Item,
    ItemStatus,
    Meeting,
    Note,
    Person,
    Project,
    SweepLog,
    Verified,
)

log = logging.getLogger(__name__)
_TIME = __import__("re").compile(r"\d{1,2}:\d{2}(:\d{2})?")
N = TypeVar("N", bound=Note)

FOLDERS = ("Items", "People", "Projects", "Meetings", "Briefs", "Reviews", "Sweeps", "nbrain")

RADAR_HEADER = (
    "> [!info] Rendered file\n"
    "> `radar.md` is regenerated on every sweep from the notes in `Items/`. Edit the item\n"
    "> notes (or use `nbrain dismiss` / `nbrain resolve`), not this file.\n\n"
)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:  # noqa: D401
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    tag = "tag:yaml.org,2002:str"
    if "\n" in data:
        return dumper.represent_scalar(tag, data, style="|")
    if data.startswith("[["):  # Obsidian requires quoted wikilinks in properties
        return dumper.represent_scalar(tag, data, style='"')
    if _TIME.fullmatch(data):  # keep HH:MM a string, never sexagesimal
        return dumper.represent_scalar(tag, data, style="'")
    return dumper.represent_scalar(tag, data)


_NoAliasDumper.add_representer(str, _represent_str)


def dump_note(meta: dict[str, Any], body: str) -> str:
    fm = yaml.dump(meta, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n\n{body.strip()}\n" if body.strip() else f"---\n{fm}---\n"


class VaultStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    # ---------- layout ----------

    def exists(self) -> bool:
        return (self.root / "nbrain").is_dir()

    def ensure_layout(self) -> None:
        for f in FOLDERS:
            (self.root / f).mkdir(parents=True, exist_ok=True)
        obsidian = self.root / ".obsidian"
        if not obsidian.exists():
            obsidian.mkdir()
            (obsidian / "app.json").write_text('{"useMarkdownLinks": false, "newLinkFormat": "shortest"}\n')
        radar = self.root / "radar.md"
        if not radar.exists():
            radar.write_text(RADAR_HEADER + "# Radar\n\nNo sweep has run yet.\n")
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text("nbrain/.env\nnbrain/*.json\nnbrain/*.lock\n.obsidian/workspace*.json\n")

    def folder(self, cls: type[Note]) -> Path:
        return self.root / cls.FOLDER

    # ---------- generic note IO ----------

    def _read(self, cls: type[N], path: Path) -> N | None:
        try:
            post = frontmatter.load(path)
        except Exception as e:  # malformed YAML should not kill a sweep
            log.warning("Skipping unreadable note %s: %s", path, e)
            return None
        try:
            return cls.from_frontmatter(dict(post.metadata), post.content, path.stem)
        except Exception as e:
            log.warning("Skipping invalid note %s: %s", path, e)
            return None

    def load_all(self, cls: type[N]) -> list[N]:
        folder = self.folder(cls)
        if not folder.exists():
            return []
        out: list[N] = []
        for p in sorted(folder.glob("*.md")):
            n = self._read(cls, p)
            if n is not None:
                out.append(n)
        return out

    def load(self, cls: type[N], note_id: str) -> N | None:
        p = self.folder(cls) / f"{note_id}.md"
        return self._read(cls, p) if p.exists() else None

    def save(self, note: Note) -> Path:
        folder = self.folder(type(note))
        folder.mkdir(parents=True, exist_ok=True)
        if not note.id:
            note.id = safe_filename(note.title)
        path = folder / f"{note.id}.md"
        path.write_text(dump_note(note.frontmatter(), note.body))
        return path

    def delete(self, note: Note) -> None:
        p = self.folder(type(note)) / f"{note.id}.md"
        if p.exists():
            p.unlink()

    def write_text(self, rel_path: str, text: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def read_text(self, rel_path: str) -> str | None:
        p = self.root / rel_path
        return p.read_text() if p.exists() else None

    # ---------- items ----------

    def items(self, *statuses: ItemStatus) -> list[Item]:
        all_items = self.load_all(Item)
        if not statuses:
            return all_items
        return [i for i in all_items if i.status in statuses]

    def open_items(self) -> list[Item]:
        return self.items(ItemStatus.open)

    def find_item(self, source: str, source_id: str) -> Item | None:
        for it in self.load_all(Item):
            if it.source == source and it.source_id == source_id:
                return it
        return None

    def _new_item_id(self, item: Item, today: date) -> str:
        base = f"{today.strftime('%Y%m%d')}-{slugify(item.title)}"
        candidate, n = base, 2
        while (self.folder(Item) / f"{candidate}.md").exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def upsert_item(self, incoming: Item, today: date, *, index: dict[tuple[str, str], Item] | None = None) -> tuple[Item, str]:
        """Merge a freshly collected item into the vault.

        Returns (item, outcome) where outcome is one of: created, updated, reopened, skipped-dismissed.
        """
        existing = (index or {}).get(incoming.dedupe_key) or self.find_item(incoming.source, incoming.source_id)
        if existing is None:
            incoming.first_seen = incoming.first_seen or today
            incoming.last_seen = today
            incoming.last_verified = today
            incoming.verified = Verified.live if incoming.verified == Verified.live else incoming.verified
            incoming.id = self._new_item_id(incoming, today)
            incoming.body = _initial_body(incoming, today)
            self.save(incoming)
            if index is not None:
                index[incoming.dedupe_key] = incoming
            return incoming, "created"

        if existing.status == ItemStatus.dismissed:
            return existing, "skipped-dismissed"

        outcome = "updated"
        if existing.status in (ItemStatus.resolved, ItemStatus.watch):
            existing.status = ItemStatus.open
            existing.resolved_on = None
            existing.unconfirmed_runs = 0
            outcome = "reopened"
            _append_history(existing, today, "Re-seen at the source; reopened.")

        changed: list[str] = []
        for field in ("title", "url", "due", "promised_to", "project", "meeting", "cause", "evidence", "priority", "type"):
            new = getattr(incoming, field)
            if new not in (None, "", []) and new != getattr(existing, field):
                changed.append(field)
                setattr(existing, field, new)
        existing.last_seen = today
        existing.last_verified = today
        existing.verified = Verified.live
        existing.unconfirmed_runs = 0
        if changed:
            _append_history(existing, today, "Updated: " + ", ".join(changed) + ".")
        self.save(existing)
        if index is not None:
            index[existing.dedupe_key] = existing
        return existing, outcome

    def index_items(self) -> dict[tuple[str, str], Item]:
        return {i.dedupe_key: i for i in self.load_all(Item)}

    def resolve_item(self, item: Item, today: date, note: str = "Verified resolved at the source.") -> None:
        item.status = ItemStatus.resolved
        item.resolved_on = today
        item.verified = Verified.live
        item.last_verified = today
        _append_history(item, today, note)
        self.save(item)

    def dismiss_item(self, item: Item, today: date, reason: str) -> None:
        item.status = ItemStatus.dismissed
        item.dismissed_on = today
        item.dismissed_reason = reason
        _append_history(item, today, f"Dismissed: {reason}")
        self.save(item)

    def mark_unconfirmed(self, item: Item, today: date, drop_after: int) -> str:
        """Rule 7: an unconfirmed item leaves the brief after N runs. Returns 'unconfirmed' or 'watch'."""
        item.unconfirmed_runs += 1
        item.last_verified = today
        if item.unconfirmed_runs >= drop_after:
            item.verified = Verified.stale
            item.status = ItemStatus.watch
            _append_history(item, today, f"Unconfirmed for {item.unconfirmed_runs} runs; moved to watch list.")
            self.save(item)
            return "watch"
        item.verified = Verified.unconfirmed if item.unconfirmed_runs < 2 else Verified.stale
        self.save(item)
        return "unconfirmed"

    def mark_live(self, item: Item, today: date) -> None:
        item.verified = Verified.live
        item.unconfirmed_runs = 0
        item.last_verified = today
        self.save(item)

    def snapshot_open(self) -> dict[str, dict[str, Any]]:
        """Cheap state capture used for the changed-since-yesterday diff."""
        return {
            i.id: {
                "status": i.status.value,
                "verified": i.verified.value,
                "due": i.due,
                "title": i.title,
                "type": i.type.value,
                "unconfirmed_runs": i.unconfirmed_runs,
                "last_seen": i.last_seen,
            }
            for i in self.load_all(Item)
            if i.status in (ItemStatus.open, ItemStatus.watch)
        }

    # ---------- people / projects / meetings ----------

    def people(self) -> list[Person]:
        return self.load_all(Person)

    def projects(self) -> list[Project]:
        return self.load_all(Project)

    def meetings(self) -> list[Meeting]:
        return self.load_all(Meeting)

    def person_by_email(self, email: str | None) -> Person | None:
        if not email:
            return None
        e = email.lower()
        for p in self.people():
            if (p.email or "").lower() == e:
                return p
        return None

    def person_by_name(self, name: str | None) -> Person | None:
        if not name:
            return None
        n = unlink(name) or ""
        n = n.lower()
        for p in self.people():
            if p.title.lower() == n or n in [a.lower() for a in p.aliases]:
                return p
        return None

    def project_for_text(self, *texts: str | None) -> Project | None:
        """Attribute a signal to a project by keyword / key / repo match."""
        blob = " ".join(t for t in texts if t).lower()
        if not blob:
            return None
        best: tuple[int, Project] | None = None
        for pr in self.projects():
            score = 0
            for kw in pr.keywords + pr.jira_keys + pr.repos + pr.chat_spaces + pr.slack_channels + [pr.title]:
                if kw and kw.lower() in blob:
                    score += len(kw)
            if score and (best is None or score > best[0]):
                best = (score, pr)
        return best[1] if best else None

    def ensure_person(self, name: str, email: str | None = None, **kw: Any) -> Person:
        p = self.person_by_email(email) or self.person_by_name(name)
        if p:
            return p
        p = Person(title=safe_filename(name), email=email, **kw)
        p.id = p.title
        self.save(p)
        return p

    # ---------- assist cache ----------
    # Generated drafts are derived data, not notes. They live in a sidecar under nbrain/ so an
    # item note stays something the user wrote and Obsidian shows nothing machine-made.

    def assist_path(self, item_id: str) -> Path:
        return self.root / "nbrain" / "assist" / f"{safe_filename(item_id)}.json"

    def read_assist(self, item_id: str) -> dict[str, Any] | None:
        p = self.assist_path(item_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("assist cache unreadable for %s: %s", item_id, e)
            return None
        return data if isinstance(data, dict) else None

    def write_assist(self, item_id: str, data: dict[str, Any]) -> Path:
        p = self.assist_path(item_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1, default=str))
        return p

    def clear_assist(self, item_id: str) -> None:
        self.assist_path(item_id).unlink(missing_ok=True)

    # ---------- sweep log / briefs ----------

    def save_sweep_log(self, logn: SweepLog) -> Path:
        return self.save(logn)

    def write_brief(self, day: date, markdown: str, html: str | None) -> tuple[Path, Path | None]:
        md_path = self.write_text(f"Briefs/{day.isoformat()}.md", markdown)
        shutil.copyfile(md_path, self.root / "Briefs" / "latest.md")
        html_path = None
        if html is not None:
            html_path = self.write_text(f"Briefs/{day.isoformat()}.html", html)
            shutil.copyfile(html_path, self.root / "Briefs" / "latest.html")
        return md_path, html_path

    def previous_brief_path(self, before: date) -> Path | None:
        briefs = sorted((self.root / "Briefs").glob("????-??-??.md"))
        briefs = [b for b in briefs if b.stem < before.isoformat()]
        return briefs[-1] if briefs else None

    # ---------- misc ----------

    def all_note_paths(self) -> Iterable[Path]:
        for p in self.root.rglob("*.md"):
            if ".obsidian" in p.parts or "nbrain" in p.parts[:1]:
                continue
            yield p


def _append_history(item: Item, today: date, line: str) -> None:
    body = item.body.rstrip()
    if "## History" not in body:
        body += "\n\n## History\n"
    body += f"\n- {today.isoformat()} — {line}"
    item.body = body.strip() + "\n"


def _initial_body(item: Item, today: date) -> str:
    parts: list[str] = []
    if item.evidence:
        parts.append("> " + item.evidence.strip().replace("\n", "\n> "))
    links = [x for x in (item.promised_to, item.project, item.meeting) if x]
    if links:
        parts.append("Related: " + ", ".join(links))
    if item.url:
        parts.append(f"Source: {item.url}")
    parts.append("## History")
    parts.append(f"- {today.isoformat()} — First seen via {item.source}.")
    return "\n\n".join(parts) + "\n"


def link_to(note: Note) -> str:
    return wikilink(note.id) or ""
