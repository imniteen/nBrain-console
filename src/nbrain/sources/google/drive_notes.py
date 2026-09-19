"""Meeting-notes source: recent Gemini / meeting-notes Google Docs, flattened for the LLM.

Emits SourceTexts only — action items in prose are the model's job. Read-only: Drive
files().list/get and Docs documents().get."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from nbrain.config.schema import Config
from nbrain.sources.base import (
    BaseSource,
    CollectResult,
    ContextMessage,
    ItemContext,
    SourceStatus,
    SourceText,
    VerifyResult,
    Window,
)
from nbrain.sources.google.auth import GoogleAuth, GoogleAuthError, http_status

# Shared with the other Google sub-sources so every Google failure is phrased the same way.
from nbrain.sources.google.gmail import _age_words, why_unavailable
from nbrain.vault.schema import Item
from nbrain.vault.store import VaultStore

log = logging.getLogger(__name__)

DOC_MIME = "application/vnd.google-apps.document"
FILE_FIELDS = "files(id,name,modifiedTime,webViewLink,viewedByMeTime,owners)"
CONTEXT_FILE_FIELDS = "id,name,modifiedTime,webViewLink,viewedByMeTime,owners,trashed"
MAX_TEXT_CHARS = 8000
CONTEXT_CHARS = 2000
LOOKBACK_DAYS = 2
_ACTION_HEADING = re.compile(r"next\s*steps|action\s*items|to-?dos?\b", re.IGNORECASE)
_TITLE_SUFFIX = re.compile(
    r"\s*[-–—:|]?\s*(notes\s+by\s+gemini|meeting\s+notes|gemini\s+notes)\s*$", re.IGNORECASE
)
_TITLE_DATE = re.compile(r"\s*[-–—]\s*\d{4}[/-]\d{2}[/-]\d{2}.*$")


def _paragraph_text(par: dict[str, Any]) -> str:
    parts: list[str] = []
    for el in par.get("elements") or []:
        run = el.get("textRun")
        if run and run.get("content"):
            parts.append(str(run["content"]))
        elif el.get("person"):
            parts.append(str((el["person"].get("personProperties") or {}).get("name", "")))
    text = "".join(parts).replace("\v", "\n").rstrip("\n")
    style = str((par.get("paragraphStyle") or {}).get("namedStyleType") or "")
    if style.startswith("HEADING") or style == "TITLE":
        level = 1 if style == "TITLE" else int(style.rsplit("_", 1)[-1] or 2)
        return f"{'#' * min(level, 6)} {text.strip()}"
    if par.get("bullet"):
        return f"- {text.strip()}"
    return text


def _elements_text(content: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for el in content:
        if "paragraph" in el:
            lines.append(_paragraph_text(el["paragraph"]))
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                cells = []
                for cell in row.get("tableCells") or []:
                    cells.append(" ".join(_elements_text(cell.get("content") or [])).strip())
                lines.append(" | ".join(c for c in cells if c))
        elif "tableOfContents" in el:
            continue
    return lines


def _doc_text(doc: dict[str, Any]) -> str:
    """Flatten a Docs API document to plain text; headings become '# ...', bullets '- ...'."""
    body = (doc.get("body") or {}).get("content") or []
    lines = [ln.rstrip() for ln in _elements_text(body)]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _action_section(text: str) -> str | None:
    """Text under the first heading that looks like next steps / action items, if any."""
    lines = text.split("\n")
    start: int | None = None
    for i, ln in enumerate(lines):
        if ln.startswith("#") and _ACTION_HEADING.search(ln):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("#"):
            end = j
            break
    section = "\n".join(lines[start:end]).strip()
    return section or None


def _meeting_name(name: str) -> str:
    cleaned = _TITLE_SUFFIX.sub("", name).strip()
    cleaned = _TITLE_DATE.sub("", cleaned).strip(" -–—:|")
    return cleaned or name


def _parse_time(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(tz)


class DriveNotesSource(BaseSource):
    name = "meeting-notes"
    roles = {"notes"}
    verifiable = True

    def __init__(self, cfg: Config, store: VaultStore, auth: GoogleAuth):
        self.cfg = cfg
        self.store = store
        self.auth = auth

    async def healthcheck(self) -> SourceStatus:
        st = self.auth.status()
        return SourceStatus(self.name, st.ok, st.detail, st.checked_at, st.write_capable)

    # ---------- API ----------

    def _list_files(self, drive: Any, since: datetime) -> list[dict[str, Any]]:
        g = self.cfg.sources.google
        since_iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        q = f"({g.notes_query}) and mimeType='{DOC_MIME}' and modifiedTime > '{since_iso}'"
        resp = (
            drive.files()
            .list(q=q, fields=FILE_FIELDS, orderBy="modifiedTime desc", pageSize=g.max_notes_docs)
            .execute()
        )
        return list(resp.get("files") or [])[: g.max_notes_docs]

    def _get_doc(self, docs: Any, file_id: str) -> dict[str, Any]:
        return docs.documents().get(documentId=file_id).execute()

    # ---------- collect ----------

    async def collect(self, window: Window) -> CollectResult:
        tz = ZoneInfo(window.tz or self.cfg.user.timezone or "UTC")
        since = datetime.now(tz) - timedelta(days=LOOKBACK_DAYS)
        drive = self.auth.service("drive", "v3")
        docs = self.auth.service("docs", "v1")
        files = await asyncio.to_thread(self._list_files, drive, since)
        result = CollectResult()
        failures = 0
        for f in files:
            file_id = str(f.get("id", ""))
            if not file_id:
                continue
            try:
                doc = await asyncio.to_thread(self._get_doc, docs, file_id)
            except HttpError as e:
                failures += 1
                log.warning("meeting-notes: could not read doc %s: %s", file_id, e)
                continue
            text = _doc_text(doc)
            if not text:
                continue
            section = _action_section(text)
            name = str(f.get("name") or doc.get("title") or "Meeting notes")
            result.texts.append(
                SourceText(
                    source=self.name,
                    source_id=file_id,
                    url=f.get("webViewLink"),
                    kind="notes",
                    title=name,
                    author=None,
                    author_is_me=False,
                    participants=[
                        str(o.get("displayName") or o.get("emailAddress") or "")
                        for o in f.get("owners") or []
                    ],
                    observed_at=_parse_time(f.get("modifiedTime"), tz),
                    text=section or text[:MAX_TEXT_CHARS],
                    meeting=_meeting_name(name),
                    unopened_by_me=f.get("viewedByMeTime") is None,
                )
            )
        if failures:
            result.notes.append(f"meeting-notes: {failures} doc(s) could not be read")
        return result

    # ---------- verify ----------

    async def verify(self, item: Item) -> VerifyResult:
        drive = self.auth.service("drive", "v3")
        try:
            f = await asyncio.to_thread(
                lambda: drive.files()
                .get(fileId=item.source_id, fields="id,trashed,webViewLink")
                .execute()
            )
        except HttpError as e:
            if http_status(e) == 404:
                return VerifyResult(state="unknown", note="notes doc no longer exists", url=item.url)
            return VerifyResult(state="unknown", note=f"drive error {http_status(e)}")
        return VerifyResult(
            state="unknown",
            note="notes docs cannot confirm completion",
            url=f.get("webViewLink") or item.url,
        )

    # ---------- live context ----------

    def _get_file(self, drive: Any, file_id: str) -> dict[str, Any]:
        return drive.files().get(fileId=file_id, fields=CONTEXT_FILE_FIELDS).execute()

    async def fetch_context(self, item: Item) -> ItemContext:
        tz = ZoneInfo(self.cfg.user.timezone or "UTC")
        try:
            drive = self.auth.service("drive", "v3")
            docs = self.auth.service("docs", "v1")
            f = await asyncio.to_thread(self._get_file, drive, item.source_id)
        except HttpError as e:
            return ItemContext.unavailable(why_unavailable(e, "Google Drive"))
        except GoogleAuthError as e:
            return ItemContext.unavailable(f"Google is not authorised: {e}")
        except OSError as e:
            return ItemContext.unavailable(f"could not reach Google Drive: {e}")
        if f.get("trashed"):
            return ItemContext.unavailable("that notes doc is in the trash")
        try:
            doc = await asyncio.to_thread(self._get_doc, docs, item.source_id)
        except HttpError as e:
            return ItemContext.unavailable(why_unavailable(e, "Google Docs"))
        except OSError as e:
            return ItemContext.unavailable(f"could not reach Google Docs: {e}")

        text = _doc_text(doc)
        if not text:
            return ItemContext.unavailable("that notes doc is empty")
        section = _action_section(text)
        title = str(f.get("name") or doc.get("title") or "Meeting notes")
        modified = _parse_time(f.get("modifiedTime"), tz)
        viewed = f.get("viewedByMeTime")
        owners = [str(o.get("displayName") or o.get("emailAddress") or "") for o in f.get("owners") or []]
        body = (section or text)[:CONTEXT_CHARS]
        age = _age_words((datetime.now(tz) - modified).total_seconds()) if modified else "unknown"
        return ItemContext(
            kind="document",
            title=title,
            url=f.get("webViewLink") or item.url,
            status=f"modified {age} · {'next steps found' if section else 'no next-steps section'}",
            participants=[o for o in owners if o],
            messages=[
                ContextMessage(
                    author=owners[0] if owners else None,
                    at=modified,
                    text=body,
                    kind="note",
                )
            ],
            facts={
                "modified": modified.strftime("%Y-%m-%d %H:%M") if modified else "unknown",
                "opened by you": "no, never" if viewed is None else f"yes, {viewed}",
                "action items": "next-steps section" if section else "none found; whole doc shown",
                "meeting": _meeting_name(title),
            },
            awaiting_me=viewed is None,
            fingerprint=f"{f.get('modifiedTime') or ''}:{len(text)}",
        )
