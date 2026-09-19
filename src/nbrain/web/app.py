"""FastAPI app for the local nbrain UI. Reads the vault, edits ledger items and config.yaml,
starts sweeps in a background thread. Loopback only; nothing here talks to an external system
except the explicit source health checks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from nbrain.config.loader import (
    ConfigError,
    config_file,
    get_secret,
    load_config,
    resolve_vault_path,
    save_config,
    set_dotted,
    set_secret,
)
from nbrain.config.schema import (
    Config,
    LLMConfig,
    MCPServerConfig,
    OutputMode,
    Provider,
    RoleTrack,
)
from nbrain.llm.tasks import LLMTasks
from nbrain.sources.base import ItemContext
from nbrain.sweep.metrics import compute_metrics
from nbrain.util import humanize_age, today_in, unlink
from nbrain.vault.schema import Item, ItemStatus, ItemType, Meeting, Person, Project, SweepLog
from nbrain.vault.store import VaultStore
from nbrain.web.md import render_markdown, render_value
from nbrain.web.runner import SweepRunner

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE = re.compile(r"^[\w.\- ]+$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NOTE_FOLDERS: tuple[tuple[str, type[Any]], ...] = (
    ("Items", Item),
    ("People", Person),
    ("Projects", Project),
    ("Meetings", Meeting),
)

# ---------------------------------------------------------------- settings form spec

TEXT_FIELDS = (
    "user.name", "user.first_name", "user.email", "user.timezone",
    "user.working_hours.start", "user.working_hours.end", "role_track",
    "sources.google.credentials_file", "sources.google.token_file", "sources.google.notes_query",
    "sources.google.max_threads", "sources.google.max_chat_messages", "sources.google.max_notes_docs",
    "sources.gitlab.url", "sources.gitlab.token_env",
    "sources.slack.token_env", "sources.slack.max_messages",
    "sources.jira.url", "sources.jira.email", "sources.jira.token_env",
    "sources.jira.jql_assigned", "sources.jira.jql_reported", "sources.jira.max_issues",
    "delivery.gmail_draft.subject", "delivery.gmail_send.subject",
    "sweep.daily_time", "sweep.weekly.day", "sweep.weekly.time",
    "sweep.email_lookback_days", "sweep.commitment_lookback_days", "sweep.awaiting_reply_hours",
    "sweep.ticket_stale_days", "sweep.review_age_days", "sweep.urgent_max_lines",
    "sweep.brief_word_cap", "sweep.unconfirmed_drop_after_runs", "sweep.priority_imbalance_ratio",
    "sweep.per_source_timeout_seconds", "sweep.missed_run_grace_hours",
    "web.host", "web.port",
)
OPTIONAL_TEXT_FIELDS = (
    "user.manager", "sources.gitlab.username", "sources.slack.user_id",
    "delivery.slack_dm.channel", "mcp_config_file",
)
BOOL_FIELDS = (
    "sources.google.enabled", "sources.google.gmail", "sources.google.calendar",
    "sources.google.chat", "sources.google.drive_notes",
    "sources.gitlab.enabled", "sources.slack.enabled", "sources.jira.enabled",
    "delivery.file.enabled", "delivery.file.html", "delivery.gmail_draft.enabled",
    "delivery.gmail_send.enabled", "delivery.slack_dm.enabled",
    "sweep.weekdays_only", "sweep.weekly.enabled", "web.enabled",
)
LIST_FIELDS = (
    "user.name_variants", "user.name_collisions", "sources.gitlab.projects",
    "sources.slack.channels", "noise.mine", "noise.suppress", "noise.phishing",
)
EXTRAS = ("priority_balance", "delivery_score", "changed_since_yesterday", "tomorrow_preview")

# Settings are split into pages so no single screen carries every knob. Each page declares the
# boolean fields it owns, because an unticked box only means False when its page was on screen —
# otherwise saving one page would silently clear checkboxes the user could not even see.
SETTINGS_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("profile", "Profile", "Who you are, and how the brief addresses you"),
    ("model", "Model", "Which model runs each part of a sweep"),
    ("sources", "Sources", "What nbrain reads, natively and over MCP"),
    ("sweep", "Sweep", "When it runs, what counts as late, what is noise"),
    ("delivery", "Delivery", "Where the finished brief is sent"),
    ("secrets", "Secrets", "Tokens and API keys, stored outside config.yaml"),
    ("advanced", "Advanced", "The local web server"),
)
SECTION_BOOLS: dict[str, tuple[str, ...]] = {
    "profile": (),
    "model": tuple(f"llm.{t}.enabled" for t in ("extract", "write", "mcp"))
    + tuple(f"llm.{t}.anthropic_fallbacks" for t in ("default", "extract", "write", "mcp")),
    "sources": (
        "sources.google.enabled", "sources.google.gmail", "sources.google.calendar",
        "sources.google.chat", "sources.google.drive_notes",
        "sources.gitlab.enabled", "sources.slack.enabled", "sources.jira.enabled",
    ),
    "sweep": ("sweep.weekdays_only", "sweep.weekly.enabled", "sweep.extras"),
    "delivery": (
        "delivery.file.enabled", "delivery.file.html", "delivery.gmail_draft.enabled",
        "delivery.gmail_send.enabled", "delivery.slack_dm.enabled",
    ),
    "secrets": (),
    "advanced": ("web.enabled",),
}


def section_bools(section: str) -> str:
    """The hidden `_bools` value a settings page posts, declaring what it owns."""
    return ",".join(SECTION_BOOLS.get(section, ()))
LLM_TASKS = ("default", "extract", "write", "mcp")
LLM_TEXT = ("provider", "model", "output_mode", "max_tokens", "timeout_seconds")
LLM_OPTIONAL = ("api_key_env", "base_url", "region", "profile", "refresh_command", "temperature")
LLM_BOOL = ("anthropic_fallbacks",)


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", raw or "") if p.strip()]


def _apply_llm(data: dict[str, Any], form: Any, task: str, full_form: bool, declared: set[str] | None = None) -> None:
    declared = declared or set()
    prefix = f"llm.{task}."
    section = data.setdefault("llm", {})
    if task != "default":
        enabled_key = f"{prefix}enabled"
        if full_form or enabled_key in form or enabled_key in declared:
            if not form.get(enabled_key):
                section[task] = None
                return
        elif not any(k.startswith(prefix) for k in form):
            return
        if section.get(task) is None:
            section[task] = LLMConfig().model_dump(mode="json")
    target = section.setdefault(task, LLMConfig().model_dump(mode="json"))
    for f in LLM_TEXT:
        if (v := form.get(prefix + f)) is not None:
            target[f] = v.strip()
    for f in LLM_OPTIONAL:
        if (v := form.get(prefix + f)) is not None:
            target[f] = v.strip() or None
    for f in LLM_BOOL:
        if full_form or (prefix + f) in form or (prefix + f) in declared:
            target[f] = bool(form.get(prefix + f))


def apply_settings_form(cfg: Config, form: Any) -> tuple[dict[str, Any], str | None]:
    """Overlay form fields on the current config dump. Returns (data, mcp_json_error)."""
    data = cfg.model_dump(mode="json")
    full_form = form.get("_form") == "settings"
    # a settings page posts the boolean keys it owns; absent ones there mean "unticked"
    declared = {k for k in str(form.get("_bools") or "").split(",") if k}
    for name in TEXT_FIELDS:
        if (v := form.get(name)) is not None:
            set_dotted(data, name, v.strip())
    for name in OPTIONAL_TEXT_FIELDS:
        if (v := form.get(name)) is not None:
            set_dotted(data, name, v.strip() or None)
    for name in BOOL_FIELDS:
        if full_form or name in form or name in declared:
            set_dotted(data, name, bool(form.get(name)))
    for name in LIST_FIELDS:
        if (v := form.get(name)) is not None:
            set_dotted(data, name, _split_list(v))
    if full_form or "sweep.extras" in form or "sweep.extras" in declared:
        chosen = [e for e in form.getlist("sweep.extras") if e in EXTRAS]
        set_dotted(data, "sweep.extras", chosen)
    for task in LLM_TASKS:
        _apply_llm(data, form, task, full_form, declared)
    mcp_error: str | None = None
    if (raw := form.get("mcp_servers_json")) is not None:
        raw = raw.strip()
        try:
            parsed = json.loads(raw) if raw else []
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON list of server objects")
            data["mcp_servers"] = [
                MCPServerConfig.model_validate(s).model_dump(mode="json") for s in parsed
            ]
        except (ValueError, ValidationError) as e:
            mcp_error = f"mcp_servers: {e}"
    data["vault_path"] = str(cfg.vault)
    return data, mcp_error


# ---------------------------------------------------------------- app


def create_app(vault: Path | None = None) -> FastAPI:
    vault_path = resolve_vault_path(vault)
    cfg = load_config(vault_path)
    app = FastAPI(title="nbrain", docs_url=None, redoc_url=None)
    app.state.vault = vault_path
    app.state.cfg = cfg
    app.state.store = VaultStore(cfg.vault)
    app.state.runner = SweepRunner(cfg.vault)
    app.state.graph = None
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["md"] = render_markdown
    templates.env.filters["fmval"] = render_value
    templates.env.filters["age"] = humanize_age

    def _weekday(iso: str) -> str:
        try:
            return date.fromisoformat(str(iso)).strftime("%A")
        except ValueError:
            return ""

    templates.env.filters["weekday"] = _weekday
    templates.env.globals["item_types"] = [t.value for t in ItemType]
    templates.env.globals["item_statuses"] = [s.value for s in ItemStatus]
    templates.env.globals["providers"] = [p.value for p in Provider]
    templates.env.globals["output_modes"] = [m.value for m in OutputMode]
    templates.env.globals["role_tracks"] = [r.value for r in RoleTrack]
    templates.env.globals["extras_all"] = EXTRAS
    templates.env.globals["llm_tasks"] = LLM_TASKS

    # ---------- helpers ----------

    def current_cfg() -> Config:
        try:
            cfg2 = load_config(app.state.vault)
        except ConfigError as e:
            log.warning("config reload failed, keeping previous: %s", e)
            return app.state.cfg  # type: ignore[no-any-return]
        app.state.cfg = cfg2
        return cfg2

    def store_for(cfg: Config) -> VaultStore:
        st: VaultStore = app.state.store
        if st.root != cfg.vault:
            st = VaultStore(cfg.vault)
            app.state.store = st
        return st

    def item_ids(store: VaultStore) -> set[str]:
        """Ids of tracked items, so links to them open the assist drawer instead of navigating."""
        try:
            return {i.id for i in store.items()}
        except Exception:  # noqa: BLE001 - a broken note must not break a page
            return set()

    def today_for(cfg: Config) -> date:
        try:
            return today_in(cfg.user.timezone)
        except Exception:  # noqa: BLE001 - bad tz in config must not break pages
            return date.today()

    def _asset_version() -> str:
        """Cache-buster for /static. Without it the browser keeps a stale app.css after an
        upgrade and the UI renders with the previous design."""
        try:
            return str(int((STATIC / "app.css").stat().st_mtime))
        except OSError:
            return "0"

    def page(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> HTMLResponse:
        cfg = ctx.get("cfg") or app.state.cfg
        ctx.setdefault("cfg", cfg)
        ctx.setdefault("runner_running", app.state.runner.running)
        ctx.setdefault("active", request.url.path.split("/")[1] or "dashboard")
        ctx.setdefault("asset_v", _asset_version())
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, status: int, message: str) -> HTMLResponse:
        return page(request, "error.html", {"title": f"{status}", "message": message}, status)

    def wants_json(request: Request) -> bool:
        return "application/json" in request.headers.get("accept", "")

    def latest_sweep(store: VaultStore) -> SweepLog | None:
        logs = store.load_all(SweepLog)
        if not logs:
            return None
        return max(logs, key=lambda s: (s.run_at or "", s.id))

    def list_docs(store: VaultStore, folder: str, pattern: str) -> list[str]:
        d = store.root / folder
        if not d.exists():
            return []
        return sorted((p.stem for p in d.glob(pattern)), reverse=True)

    def find_note(store: VaultStore, note_id: str) -> tuple[str, Path] | None:
        if "/" in note_id or "\\" in note_id or note_id in ("", ".", ".."):
            return None
        for folder, _cls in NOTE_FOLDERS:
            base = (store.root / folder).resolve()
            p = (base / f"{note_id}.md").resolve()
            if p.parent == base and p.exists():
                return folder, p
        low = note_id.lower()
        for folder, _cls in NOTE_FOLDERS:
            base = store.root / folder
            if not base.exists():
                continue
            for p in base.glob("*.md"):
                if p.stem.lower() == low:
                    return folder, p
        return None

    def secret_status(cfg: Config) -> list[dict[str, Any]]:
        names: dict[str, set[str]] = {}

        def add(env: str | None, who: str) -> None:
            if env:
                names.setdefault(env, set()).add(who)

        for task in LLM_TASKS:
            section = getattr(cfg.llm, task)
            if section is not None:
                add(section.default_api_key_env(), f"llm.{task}")
        add(cfg.sources.gitlab.token_env, "gitlab")
        add(cfg.sources.slack.token_env, "slack")
        add(cfg.sources.jira.token_env, "jira")
        for s in cfg.mcp_servers:
            for env in s.headers_env.values():
                add(env, f"mcp:{s.name}")
        return [
            {"env": env, "set": get_secret(env) is not None, "used_by": ", ".join(sorted(who))}
            for env, who in sorted(names.items())
        ]

    def settings_page(
        request: Request, cfg: Config, data: dict[str, Any], *, error: str | None = None,
        message: str | None = None, mcp_json: str | None = None, status: int = 200,
        section: str = "profile",
    ) -> HTMLResponse:
        if mcp_json is None:
            mcp_json = json.dumps(data.get("mcp_servers", []), indent=2)
        return page(
            request,
            "settings.html",
            {
                "title": "Settings",
                "section": section,
                "sections": SETTINGS_SECTIONS,
                "section_bools": section_bools(section),
                "cfg": cfg,
                "c": data,
                "error": error,
                "message": message,
                "mcp_json": mcp_json,
                "secrets": secret_status(cfg),
                "config_path": str(config_file(cfg.vault)),
            },
            status,
        )

    # ---------- dashboard ----------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        today = today_for(cfg)
        brief_md = store.read_text("Briefs/latest.md")
        dates = list_docs(store, "Briefs", "????-??-??.md")
        try:
            metrics = compute_metrics(store, cfg, today, {})
        except Exception as e:  # noqa: BLE001
            log.warning("metrics failed: %s", e)
            metrics = None
        last = latest_sweep(store)
        status = app.state.runner.status()
        warnings: list[str] = []
        if last:
            warnings += [f"source failed: {n}" for n in last.sources_failed]
        if status["last_result"]:
            warnings += status["last_result"].get("warnings") or []
            if status["last_result"].get("error"):
                warnings.append(status["last_result"]["error"])
        return page(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "cfg": cfg,
                "today": today,
                "brief_html": render_markdown(brief_md, item_ids(store)) if brief_md else None,
                "latest_date": dates[0] if dates else None,
                "metrics": metrics,
                "last_sweep": last,
                "run_status": status,
                "warnings": warnings,
                "started": request.query_params.get("started"),
            },
        )

    # ---------- briefs / reviews / radar / memory ----------

    @app.get("/briefs", response_class=HTMLResponse)
    def briefs(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        dates = list_docs(store, "Briefs", "????-??-??.md")
        rows = [{"date": d, "html": (store.root / "Briefs" / f"{d}.html").exists()} for d in dates]
        return page(request, "briefs.html", {"title": "Briefs", "cfg": cfg, "rows": rows})

    @app.get("/briefs/{day}.html")
    def brief_html(request: Request, day: str) -> Response:
        cfg = current_cfg()
        store = store_for(cfg)
        if not _DATE_RE.match(day):
            return error_page(request, 404, "Not a brief date.")
        html = store.read_text(f"Briefs/{day}.html")
        if html is None:
            return error_page(request, 404, f"No HTML brief for {day}.")
        return HTMLResponse(html)

    @app.get("/briefs/{day}", response_class=HTMLResponse)
    def brief(request: Request, day: str) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        if not _DATE_RE.match(day):
            return error_page(request, 404, "Not a brief date.")
        text = store.read_text(f"Briefs/{day}.md")
        if text is None:
            return error_page(request, 404, f"No brief for {day}.")
        return page(
            request,
            "document.html",
            {
                "title": f"Brief {day}",
                "cfg": cfg,
                "body_html": render_markdown(text, item_ids(store)),
                "back": ("/briefs", "All briefs"),
                "raw_link": f"/briefs/{day}.html" if (store.root / "Briefs" / f"{day}.html").exists() else None,
                "raw_label": "Email (HTML) version",
            },
        )

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        return page(
            request, "reviews.html",
            {"title": "Weekly reviews", "cfg": cfg, "weeks": list_docs(store, "Reviews", "*.md")},
        )

    @app.get("/reviews/{week}", response_class=HTMLResponse)
    def review(request: Request, week: str) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        if not _SLUG_RE.match(week) or ".." in week:
            return error_page(request, 404, "Not a review id.")
        text = store.read_text(f"Reviews/{week}.md")
        if text is None:
            return error_page(request, 404, f"No review {week}.")
        return page(
            request, "document.html",
            {"title": f"Review {week}", "cfg": cfg, "body_html": render_markdown(text, item_ids(store)), "back": ("/reviews", "All reviews")},
        )

    @app.get("/radar", response_class=HTMLResponse)
    def radar(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        text = store.read_text("radar.md") or "_No radar yet. Run a sweep._"
        return page(request, "document.html", {"title": "Radar", "cfg": cfg, "body_html": render_markdown(text, item_ids(store))})

    @app.get("/memory", response_class=HTMLResponse)
    def memory(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        text = store_for(cfg).read_text("memory.md") or "_No memory.md yet. Run a sweep._"
        return page(request, "document.html", {"title": "Memory", "cfg": cfg, "body_html": render_markdown(text)})

    # ---------- ledger ----------

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger(
        request: Request, status: str = "open", type: str = "", q: str = "", focus: str = "",  # noqa: A002
    ) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        today = today_for(cfg)
        items = store.items()
        if status and status != "all":
            items = [i for i in items if i.status.value == status]
        if type:
            items = [i for i in items if i.type.value == type]
        if q:
            needle = q.lower()
            items = [
                i for i in items
                if needle in " ".join(
                    x for x in (i.title, i.evidence, i.person, i.project_name, i.source, i.id) if x
                ).lower()
            ]
        if focus and not any(i.id == focus for i in items):
            extra = store.load(Item, focus)
            if extra:
                items.insert(0, extra)
        items.sort(key=lambda i: (i.priority, i.due or date.max, i.first_seen or today, i.id))
        return page(
            request,
            "ledger.html",
            {
                "title": "Ledger",
                "cfg": cfg,
                "items": items,
                "today": today,
                "status": status,
                "type": type,
                "q": q,
                "focus": focus,
                "counts": {s.value: len(store.items(s)) for s in ItemStatus},
            },
        )

    @app.post("/items/{item_id}/dismiss")
    def dismiss(request: Request, item_id: str, reason: str = Form("")) -> Response:
        cfg = current_cfg()
        store = store_for(cfg)
        item = store.load(Item, item_id) if _SLUG_RE.match(item_id) else None
        if item is None:
            return error_page(request, 404, f"No item {item_id}.")
        if not reason.strip():
            return error_page(request, 400, "A reason is required to dismiss an item.")
        store.dismiss_item(item, today_for(cfg), reason.strip())
        log.info("dismissed %s: %s", item_id, reason.strip())
        return RedirectResponse(f"/ledger?status=dismissed&focus={item_id}", status_code=303)

    @app.post("/items/{item_id}/resolve")
    def resolve(request: Request, item_id: str) -> Response:
        cfg = current_cfg()
        store = store_for(cfg)
        item = store.load(Item, item_id) if _SLUG_RE.match(item_id) else None
        if item is None:
            return error_page(request, 404, f"No item {item_id}.")
        store.resolve_item(item, today_for(cfg), "Marked resolved in the web UI.")
        log.info("resolved %s", item_id)
        return RedirectResponse(f"/ledger?status=resolved&focus={item_id}", status_code=303)

    # ---------- notes ----------

    @app.get("/note/{note_id}", response_class=HTMLResponse)
    def note(request: Request, note_id: str) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        found = find_note(store, note_id)
        if found is None:
            return error_page(request, 404, f"No note called {note_id!r} in Items, People, Projects or Meetings.")
        folder, path = found
        try:
            post = frontmatter.load(path)
            meta, body = dict(post.metadata), post.content
        except Exception as e:  # noqa: BLE001
            meta, body = {"error": f"frontmatter unreadable: {e}"}, path.read_text()
        title = str(meta.get("title") or path.stem)
        return page(
            request,
            "note.html",
            {
                "title": title,
                "cfg": cfg,
                "folder": folder,
                "note_id": path.stem,
                "meta": meta,
                "body_html": render_markdown(body),
                "is_item": folder == "Items",
            },
        )

    # ---------- sources ----------

    def sources_ctx(cfg: Config) -> dict[str, Any]:
        s = cfg.sources
        native = [
            {"name": "google", "enabled": s.google.enabled,
             "detail": ", ".join(k for k in ("gmail", "calendar", "chat", "drive_notes") if getattr(s.google, k))},
            {"name": "gitlab", "enabled": s.gitlab.enabled, "detail": s.gitlab.url},
            {"name": "slack", "enabled": s.slack.enabled, "detail": s.slack.token_env},
            {"name": "jira", "enabled": s.jira.enabled, "detail": s.jira.url},
        ]
        from nbrain.llm.factory import describe_model

        llms = [
            {"task": t, "desc": describe_model(getattr(cfg.llm, t))}
            for t in LLM_TASKS if getattr(cfg.llm, t) is not None
        ]
        return {"native": native, "mcp": cfg.mcp_servers, "llms": llms}

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        return page(request, "sources.html", {"title": "Sources", "cfg": cfg, **sources_ctx(cfg), "results": None})

    @app.post("/sources/check", response_class=HTMLResponse)
    async def sources_check(request: Request) -> HTMLResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        results: list[dict[str, Any]] = []

        async def check_native() -> None:
            try:
                from nbrain.sweep.pipeline import build_native_sources

                srcs = await asyncio.to_thread(build_native_sources, cfg, store)
            except Exception as e:  # noqa: BLE001
                results.append({"name": "native sources", "ok": False, "detail": f"could not build: {e}", "write_capable": False})
                return
            for src in srcs:
                name = getattr(src, "name", type(src).__name__)
                try:
                    st = await asyncio.wait_for(src.healthcheck(), 30)
                    results.append({"name": name, "ok": st.ok, "detail": st.detail, "write_capable": st.write_capable})
                except TimeoutError:
                    results.append({"name": name, "ok": False, "detail": "healthcheck timed out (30s)", "write_capable": False})
                except Exception as e:  # noqa: BLE001
                    results.append({"name": name, "ok": False, "detail": f"healthcheck failed: {e}", "write_capable": False})

        async def check_mcp() -> None:
            if not cfg.enabled_mcp_servers():
                return
            try:
                from nbrain.mcp.registry import MCPRegistry

                reg = MCPRegistry(cfg)
            except Exception as e:  # noqa: BLE001
                results.append({"name": "mcp", "ok": False, "detail": f"registry unavailable: {e}", "write_capable": False})
                return
            for name in reg.names():
                server = reg.servers[name]
                try:
                    tools = await asyncio.wait_for(reg.list_tools(name), 30)
                    allowed = sum(1 for t in tools if t["allowed"])
                    results.append({
                        "name": f"mcp:{name}", "ok": True,
                        "detail": f"{len(tools)} tools, {allowed} allowed by the read-only gate",
                        "write_capable": server.allow_write,
                    })
                except TimeoutError:
                    results.append({"name": f"mcp:{name}", "ok": False, "detail": "timed out (30s)", "write_capable": server.allow_write})
                except Exception as e:  # noqa: BLE001
                    results.append({"name": f"mcp:{name}", "ok": False, "detail": f"{type(e).__name__}: {e}", "write_capable": server.allow_write})

        async def check_llm() -> None:
            seen: set[str] = set()
            for task in LLM_TASKS:
                lcfg = getattr(cfg.llm, task)
                if lcfg is None:
                    continue
                key = f"{lcfg.provider}:{lcfg.model}:{lcfg.profile}:{lcfg.region}:{lcfg.api_key_env}"
                if key in seen:
                    continue
                seen.add(key)
                label = f"llm:{task}"
                try:
                    if lcfg.provider == Provider.bedrock:
                        from nbrain.llm.aws_creds import check_credentials

                        state = await asyncio.wait_for(asyncio.to_thread(check_credentials, lcfg), 30)
                        results.append({"name": label, "ok": state.ok, "detail": state.detail, "write_capable": False})
                    else:
                        env = lcfg.default_api_key_env()
                        has = env is None or get_secret(env) is not None
                        detail = "no key needed" if env is None else (f"{env} is set" if has else f"{env} is not set")
                        results.append({"name": label, "ok": has, "detail": detail, "write_capable": False})
                except Exception as e:  # noqa: BLE001
                    results.append({"name": label, "ok": False, "detail": f"{type(e).__name__}: {e}", "write_capable": False})

        await check_llm()
        await check_native()
        await check_mcp()
        return page(request, "sources.html", {"title": "Sources", "cfg": cfg, **sources_ctx(cfg), "results": results})

    # ---------- settings ----------

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        return settings_section(request, "profile")

    @app.get("/settings/{section}", response_class=HTMLResponse)
    def settings_section(request: Request, section: str) -> HTMLResponse:
        if section not in {s for s, _, _ in SETTINGS_SECTIONS}:
            return error_page(request, 404, f"No settings page called {section!r}.")
        cfg = current_cfg()
        return settings_page(
            request, cfg, cfg.model_dump(mode="json"),
            message=request.query_params.get("msg"), section=section,
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(request: Request) -> Response:
        cfg = current_cfg()
        form = await request.form()
        data, mcp_error = apply_settings_form(cfg, form)
        raw_mcp = form.get("mcp_servers_json")
        section = str(form.get("_section") or "profile")
        if section not in {s for s, _, _ in SETTINGS_SECTIONS}:
            section = "profile"
        if mcp_error:
            return settings_page(request, cfg, data, error=mcp_error, mcp_json=str(raw_mcp or ""), status=400, section=section)
        try:
            new_cfg = Config.model_validate(data)
        except ValidationError as e:
            msgs = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
            return settings_page(request, cfg, data, error=msgs, mcp_json=str(raw_mcp) if raw_mcp is not None else None, status=400, section=section)
        save_config(new_cfg)
        app.state.cfg = new_cfg
        log.info("settings saved to %s", config_file(new_cfg.vault))
        return RedirectResponse(f"/settings/{section}?msg=Saved.", status_code=303)

    @app.post("/settings/secret")
    def settings_secret(
        request: Request, env_name: str = Form(""), value: str = Form(""), store: str = Form("dotenv"),
    ) -> Response:
        cfg = current_cfg()
        env_name = env_name.strip()
        if not _ENV_RE.match(env_name):
            return settings_page(request, cfg, cfg.model_dump(mode="json"), error="Secret name must look like AN_ENV_VAR.", status=400, section="secrets")
        if not value:
            return settings_page(request, cfg, cfg.model_dump(mode="json"), error="Secret value is empty.", status=400, section="secrets")
        try:
            where = set_secret(env_name, value, use_keyring=(store == "keyring"), vault=cfg.vault)
        except Exception as e:  # noqa: BLE001 - keyring backends fail in odd ways
            return settings_page(request, cfg, cfg.model_dump(mode="json"), error=f"Could not store secret: {e}", status=500, section="secrets")
        log.info("secret %s stored in %s", env_name, where)
        return RedirectResponse(f"/settings/secrets?msg=Stored+{env_name}+in+{'keyring' if store == 'keyring' else '.env'}", status_code=303)

    # ---------- run ----------

    def start_run(request: Request, kind: str, dry_run: bool, skip_llm: bool) -> Response:
        runner: SweepRunner = app.state.runner
        ok = runner.start(kind, dry_run=dry_run, skip_llm=skip_llm)  # type: ignore[arg-type]
        if not ok:
            if wants_json(request):
                return JSONResponse({"error": "a sweep is already running"}, status_code=409)
            return error_page(request, 409, "A sweep is already running (lock held). Try again when it finishes.")
        if wants_json(request):
            return JSONResponse({"started": True, "kind": kind})
        return RedirectResponse("/?started=1", status_code=303)

    @app.post("/run")
    def run_daily(request: Request, dry_run: str = Form(""), skip_llm: str = Form("")) -> Response:
        return start_run(request, "daily", bool(dry_run), bool(skip_llm))

    @app.post("/run/weekly")
    def run_weekly_route(request: Request, dry_run: str = Form(""), skip_llm: str = Form("")) -> Response:
        return start_run(request, "weekly", bool(dry_run), bool(skip_llm))

    @app.get("/run/status")
    def run_status() -> JSONResponse:
        return JSONResponse(app.state.runner.status())

    # ---------- graph ----------

    @app.get("/graph", response_class=HTMLResponse)
    def graph(request: Request) -> HTMLResponse:
        # goes through page() so the graph gets the same shell context as every other page
        # (cfg for the sidebar, active nav item, sweep status, stylesheet cache-buster)
        return page(
            request,
            "graph.html",
            {"title": "Graph", "graph_json_url": "/api/graph.json", "patterns_url": "/api/graph/patterns"},
        )

    def vault_graph(store: VaultStore) -> Any:
        from nbrain.vault.graph import VaultGraph

        vg = app.state.graph
        if vg is None or vg.store.root != store.root:
            vg = VaultGraph(store)
            app.state.graph = vg
        return vg

    @app.get("/api/graph.json")
    def api_graph(request: Request) -> JSONResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        try:
            from nbrain.vault.graph import GraphFilters
        except ImportError:
            return JSONResponse({"error": "graph module unavailable"}, status_code=503)
        qp = request.query_params

        def flag(name: str, default: bool) -> bool:
            v = qp.get(name)
            if v is None or v == "":
                return default
            return v.lower() not in ("0", "false", "no", "off")

        def csv(name: str) -> set[str] | None:
            vals = {x.strip() for v in qp.getlist(name) for x in v.split(",") if x.strip()}
            return vals or None

        def day(name: str) -> date | None:
            v = qp.get(name)
            try:
                return date.fromisoformat(v) if v else None
            except ValueError:
                return None

        try:
            depth = int(qp.get("depth") or 1)
        except ValueError:
            depth = 1
        filters = GraphFilters(
            types=csv("types"),
            statuses=csv("statuses"),
            date_from=day("date_from"),
            date_to=day("date_to"),
            project=qp.get("project") or None,
            person=qp.get("person") or None,
            include_briefs=flag("include_briefs", False),
            include_orphans=flag("include_orphans", True),
            focus=qp.get("focus") or None,
            depth=depth,
        )
        try:
            return JSONResponse(vault_graph(store).to_json(filters))
        except Exception as e:  # noqa: BLE001
            log.exception("graph build failed")
            return JSONResponse({"error": f"graph failed: {e}"}, status_code=500)

    # ---------- item assist: live context + a draft, generated on demand ----------

    def _source_for(cfg: Config, store: VaultStore, source_name: str) -> Any:
        from nbrain.sweep.pipeline import build_native_sources

        for src in build_native_sources(cfg, store):
            if src.name == source_name:
                return src
        return None

    def _assist_item_payload(item: Item, today: date) -> dict[str, Any]:
        return {
            "title": item.title,
            "type": item.type.value,
            "status": item.status.value,
            "source": item.source,
            "person": unlink(item.promised_to),
            "project": unlink(item.project),
            "due": item.due.isoformat() if item.due else None,
            "promised_on": item.promised_on.isoformat() if item.promised_on else None,
            "age_days": item.age_days(today),
            "evidence": item.evidence,
            "cause": item.cause,
            "url": item.url,
        }

    @app.get("/api/items/{item_id}/assist")
    async def api_assist(item_id: str, refresh: bool = False) -> JSONResponse:
        """Re-read the item at its source, then draft help for it.

        The source is always re-read, even on a cache hit, because that is what tells us whether
        the cached draft is still about the same situation."""
        cfg = current_cfg()
        store = store_for(cfg)
        item = store.load(Item, item_id)
        if item is None:
            return JSONResponse({"error": f"no item {item_id}"}, status_code=404)

        src = _source_for(cfg, store, item.source)
        if src is None:
            ctx = ItemContext.unavailable(
                f"{item.source} is not connected right now, so its live state cannot be read."
            )
        else:
            try:
                ctx = await asyncio.wait_for(src.fetch_context(item), 60)
            except TimeoutError:
                ctx = ItemContext.unavailable(f"{item.source} did not answer within 60 seconds.")
            except Exception as e:  # noqa: BLE001 - a dead source must not break the drawer
                log.warning("fetch_context failed for %s: %s", item_id, e)
                ctx = ItemContext.unavailable(f"{item.source} could not be read: {e}")

        cached = store.read_assist(item_id)
        fresh = (
            cached is not None
            and not refresh
            and ctx.available
            and cached.get("fingerprint") == ctx.fingerprint
            and ctx.fingerprint != ""
        )
        payload: dict[str, Any] = {
            "item_id": item_id,
            "title": item.title,
            "url": item.url or ctx.url,
            "type": item.type.value,
            "source": item.source,
            "context": ctx.model_dump(mode="json"),
        }
        if fresh:
            payload |= {"assist": cached["assist"], "cached": True, "generated_at": cached.get("generated_at")}
            return JSONResponse(payload)

        try:
            tasks = LLMTasks(cfg, today=today_for(cfg))
            assist = await tasks.assist(_assist_item_payload(item, today_for(cfg)), payload["context"])
        except Exception as e:  # noqa: BLE001
            log.warning("assist generation failed for %s: %s", item_id, e)
            return JSONResponse(payload | {"error": f"could not draft: {e}", "assist": None}, status_code=200)

        generated_at = datetime.now(UTC).isoformat(timespec="seconds")
        store.write_assist(
            item_id,
            {"fingerprint": ctx.fingerprint, "generated_at": generated_at, "assist": assist.model_dump(mode="json")},
        )
        return JSONResponse(payload | {"assist": assist.model_dump(mode="json"), "cached": False, "generated_at": generated_at})

    @app.get("/api/graph/patterns")
    def api_patterns() -> JSONResponse:
        cfg = current_cfg()
        store = store_for(cfg)
        try:
            from nbrain.vault.graph import pattern_dicts
        except ImportError:
            return JSONResponse({"error": "graph module unavailable"}, status_code=503)
        try:
            pats = vault_graph(store).patterns(today_for(cfg))
            return JSONResponse({"patterns": pattern_dicts(pats)})
        except Exception as e:  # noqa: BLE001
            log.exception("graph patterns failed")
            return JSONResponse({"error": f"patterns failed: {e}"}, status_code=500)

    # ---------- misc ----------

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return app


# ---------------------------------------------------------------- servers


def _loopback_host(host: str) -> str:
    if host in ("127.0.0.1", "localhost", "::1"):
        return host
    log.warning("web.host %r is not loopback; binding to 127.0.0.1 instead", host)
    return "127.0.0.1"


def serve(cfg: Config) -> None:
    """Blocking: run the UI in the foreground."""
    uvicorn.run(create_app(cfg.vault), host=_loopback_host(cfg.web.host), port=cfg.web.port, log_level="info")


def run_server_in_thread(cfg: Config) -> threading.Thread:
    """Start uvicorn in a daemon thread (no signal handlers) and return the started thread."""
    config = uvicorn.Config(
        create_app(cfg.vault), host=_loopback_host(cfg.web.host), port=cfg.web.port, log_level="warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]  # older uvicorn
    thread = threading.Thread(target=server.run, name="nbrain-web", daemon=True)
    thread.server = server  # type: ignore[attr-defined]  # lets the caller set should_exit
    thread.start()
    log.info("web UI on http://%s:%d", config.host, config.port)
    return thread
