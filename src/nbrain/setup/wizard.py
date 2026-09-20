"""`nbrain setup`: vault → model → sources → MCP → delivery → schedule → discovery → role track →
the five gap questions → write files → dry run → offer the service. Every network step is
optional and failure-tolerant."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.table import Table

from nbrain.auth.loopback import cert_advice, redirect_uri
from nbrain.config.loader import get_secret, load_config, save_config, set_secret, write_pointer
from nbrain.config.schema import (
    Config,
    LLMConfig,
    MCPServerConfig,
    OutputMode,
    Provider,
    RoleTrack,
)
from nbrain.mcp import catalog
from nbrain.mcp.catalog import CONNECTORS
from nbrain.mcp.connect import connect as connector_connect
from nbrain.mcp.connect import status as connector_status
from nbrain.util import safe_filename
from nbrain.vault.render import render_memory
from nbrain.vault.schema import Meeting, Person, Project
from nbrain.vault.store import VaultStore

console = Console()
Q = questionary
STYLE = questionary.Style([("qmark", "fg:#185FA5 bold"), ("question", "bold"), ("answer", "fg:#0F6E56"), ("pointer", "fg:#185FA5")])

DEFAULT_MODELS = {
    Provider.anthropic: "claude-opus-5",
    Provider.openai: "gpt-5",
    Provider.openrouter: "openai/gpt-5.6-luna",
    Provider.ollama: "qwen3:14b",
    Provider.openai_compatible: "local-model",
    Provider.bedrock: "us.anthropic.claude-opus-5",
}


def _ask(q: Any) -> Any:
    ans = q.ask()
    if ans is None:  # Ctrl-C
        console.print("\n[yellow]Setup cancelled. Nothing else was changed.[/yellow]")
        raise SystemExit(130)
    return ans


def _in_git_checkout(path: Path) -> bool:
    for p in [path, *path.parents]:
        if (p / ".git").exists():
            return True
    return False


def _local_tz() -> str:
    try:
        link = os.readlink("/etc/localtime")
        return link.split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return os.environ.get("TZ") or "UTC"


def _secret_step(env_name: str, vault: Path, *, prompt: str, required: bool = False) -> bool:
    """Returns True when the secret is available afterwards."""
    if get_secret(env_name):
        console.print(f"  {env_name} already set.")
        if not _ask(Q.confirm(f"Replace {env_name}?", default=False, style=STYLE)):
            return True
    for attempt in range(3):
        val = _ask(Q.password(prompt, style=STYLE))
        if val:
            break
        if not required:
            return False
        if attempt < 2:
            console.print(f"  [yellow]{env_name} is required for this provider. Paste the key, or press Ctrl-C to start over.[/yellow]")
    else:
        console.print(f"  [yellow]Continuing without {env_name}. Set it later with `nbrain secret {env_name}`.[/yellow]")
        return False
    where = _ask(Q.select("Store it in", choices=["nbrain/.env (file, chmod 600)", "macOS Keychain"], style=STYLE))
    console.print("  stored in " + set_secret(env_name, val, use_keyring=where.startswith("macOS"), vault=vault))
    return True


def _llm_step(label: str, vault: Path, default: LLMConfig | None = None) -> LLMConfig:
    provider = Provider(_ask(Q.select(f"{label}: provider", choices=[p.value for p in Provider], default=(default.provider.value if default else "anthropic"), style=STYLE)))
    cfg = LLMConfig(provider=provider, model=DEFAULT_MODELS[provider])
    cfg.model = _ask(Q.text("Model id", default=cfg.model, style=STYLE))
    if provider in (Provider.anthropic, Provider.openai, Provider.openrouter, Provider.openai_compatible):
        env = cfg.default_api_key_env() or "NBRAIN_LLM_API_KEY"
        cfg.api_key_env = _ask(Q.text("Env var holding the API key", default=env, style=STYLE))
        if provider == Provider.anthropic and not get_secret(cfg.api_key_env):
            console.print("  (leave the key empty to use ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile)")
        needs_key = provider in (Provider.openai, Provider.openrouter)
        _secret_step(
            cfg.api_key_env,
            vault,
            prompt=f"{provider.value} API key" + ("" if needs_key else " (blank to skip)"),
            required=needs_key,
        )
    if provider in (Provider.ollama, Provider.openai_compatible):
        cfg.base_url = _ask(Q.text("Base URL", default="http://localhost:11434/v1" if provider == Provider.ollama else "http://localhost:1234/v1", style=STYLE))
        cfg.output_mode = OutputMode.prompted
        console.print("  output_mode=prompted (works without tool calling; switch to `tool` if your model supports it)")
    if provider == Provider.bedrock:
        cfg.region = _ask(Q.text("AWS region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"), style=STYLE))
        prof = _ask(Q.text("AWS profile (blank = default credential chain)", default=os.environ.get("AWS_PROFILE", ""), style=STYLE))
        cfg.profile = prof or None
        rc = _ask(Q.text("Command to refresh short-lived credentials, e.g. `gimme-aws-creds --profile x` (blank = none)", default="", style=STYLE))
        cfg.refresh_command = rc or None
    return cfg


def _test_llm(cfg: Config, task: str) -> bool:
    from nbrain.llm.tasks import LLMTasks

    try:
        with console.status(f"Testing {task} model…"):
            says = asyncio.run(LLMTasks(cfg, today=date.today()).ping(task))  # type: ignore[arg-type]
        console.print(f"  [green]ok[/green] → {says}")
        return True
    except Exception as e:  # noqa: BLE001
        console.print(f"  [red]failed:[/red] {str(e)[:300]}")
        return False


# MCP servers worth offering by name in setup, so a user does not have to hand-write JSON for
# the ones nearly everybody wants. Everything else still goes in via the MCP servers step.
# Connectors come from one catalogue shared with the web UI, so the two can never disagree
# about what Glean or Slack needs. Keys are prefixed in the checkbox because a catalogue
# connector and a native source can share a name (Slack is both).
KNOWN_MCP = CONNECTORS
MCP_PREFIX = "mcp:"


def _find_mcp(cfg: Config, name: str) -> MCPServerConfig | None:
    return next((s for s in cfg.mcp_servers if s.name == name), None)


def _known_mcp_step(cfg: Config, vault: Path, key: str, enable: bool) -> None:
    """Set up one catalogue connector and offer to connect it there and then.

    The whole point is that nothing here asks for a token: the user answers at most a domain
    or a client id, and the grant itself happens in their browser."""
    conn = KNOWN_MCP[key]
    existing = _find_mcp(cfg, key)
    if not enable:
        if existing:
            existing.enabled = False
            console.print(f"  [yellow]{conn.label} switched off.[/yellow] Its settings stay in config.yaml.")
        return

    console.print(f"  {conn.blurb}.")
    domain = ""
    if conn.needs_domain:
        current = existing.url.split("://", 1)[-1].split("/")[0] if existing and existing.url else ""
        console.print(f"  Find {conn.domain_hint}.")
        domain = _ask(Q.text(f"{conn.label} backend domain", default=current, style=STYLE)).strip()
        if not domain:
            console.print(f"  [yellow]No domain given; {conn.label} not configured.[/yellow]")
            return

    client_id = ""
    if conn.auth == "confidential":
        console.print(f"  {conn.label} needs an app someone registered, because it does not support")
        console.print("  dynamic client registration. Register this redirect URL with it:")
        console.print(f"    [bold]{redirect_uri(cfg.web.oauth_port)}[/bold]")
        console.print(f"  {cert_advice()}")
        current_id = existing.oauth_client_id if existing else ""
        client_id = _ask(Q.text("Client ID", default=current_id or "", style=STYLE)).strip()
        if not client_id:
            console.print(f"  [yellow]No client id; {conn.label} not configured.[/yellow]")
            return

    server = catalog.apply(cfg, conn, domain=domain, client_id=client_id)
    console.print(f"  {conn.label} set to [bold]{server.url}[/bold], role {', '.join(server.roles)}.")

    if conn.auth == "confidential" and conn.client_secret_env and not get_secret(conn.client_secret_env):
        _secret_step(conn.client_secret_env, vault, prompt=f"{conn.label} client secret")

    st = connector_status(cfg, server)
    if st.blocker:
        console.print(f"  [yellow]Not ready yet:[/yellow] {st.blocker}. Run `nbrain connect {key}` once it is.")
        return
    if st.connected:
        console.print(f"  [green]{conn.label} is already connected.[/green]")
        return
    if not _ask(Q.confirm(f"Open the browser and connect {conn.label} now?", default=True, style=STYLE)):
        console.print(f"  Connect it later with `nbrain connect {key}`.")
        return
    try:
        st = asyncio.run(connector_connect(cfg, server))
    except Exception as e:  # noqa: BLE001 - the wizard reports and carries on
        console.print(f"  [yellow]{conn.label} did not connect:[/yellow] {e}")
        console.print(f"  Everything is saved; retry with `nbrain connect {key}`.")
        return
    allowed = sum(1 for t in st.tools if t.allowed)
    console.print(f"  [green]{conn.label} connected.[/green] {allowed} of {len(st.tools)} tools allowed (writes stay blocked).")


def _source_status(cfg: Config, vault: Path) -> dict[str, str]:
    """A short description of what each source already has, shown next to its checkbox so a
    re-run tells you the current state instead of making you remember it."""
    g = cfg.sources.google
    creds = (vault / "nbrain" / g.credentials_file).exists()
    token = (vault / "nbrain" / g.token_file).exists()
    subs = [n for n, on in (("Gmail", g.gmail), ("Calendar", g.calendar), ("Chat", g.chat), ("notes", g.drive_notes)) if on]
    google = "authorised" if token else "client saved, not authorised" if creds else "nothing yet"
    if subs:
        google += " · " + ", ".join(subs)

    def tok(env: str) -> str:
        return "token set" if get_secret(env) else "no token"

    out = {
        "google": google,
        "gitlab": f"{tok(cfg.sources.gitlab.token_env)} · {cfg.sources.gitlab.url}",
        "slack": tok(cfg.sources.slack.token_env),
        "jira": tok(cfg.sources.jira.token_env) + (f" · {cfg.sources.jira.url}" if cfg.sources.jira.url else ""),
    }
    for key in KNOWN_MCP:
        srv = _find_mcp(cfg, key)
        st = connector_status(cfg, srv) if srv else None
        where = f" · {srv.url}" if srv and srv.url else ""
        out[MCP_PREFIX + key] = ("connected" if st and st.connected else "not connected") + where
    return out


def _sources_step(cfg: Config, vault: Path) -> None:
    labels = {
        "google": "Google Workspace (Gmail, Calendar, Chat, Drive meeting notes)",
        "gitlab": "GitLab",
        "slack": "Slack (API token — the connector below is the newer route)",
        "jira": "Jira",
        **{MCP_PREFIX + k: f"{v.label} — {v.blurb.lower()}" for k, v in KNOWN_MCP.items()},
    }
    enabled = {
        "google": cfg.sources.google.enabled,
        "gitlab": cfg.sources.gitlab.enabled,
        "slack": cfg.sources.slack.enabled,
        "jira": cfg.sources.jira.enabled,
        **{MCP_PREFIX + k: bool(_find_mcp(cfg, k) and _find_mcp(cfg, k).enabled) for k in KNOWN_MCP},
    }
    status = _source_status(cfg, vault)
    console.print("  Ticked means enabled. Unticking turns a source off; its settings are kept.")
    picked = _ask(
        Q.checkbox(
            "Sources to connect",
            choices=[
                Q.Choice(f"{labels[k]}  ({status[k]})", k, checked=enabled[k])
                for k in ("google", "gitlab", "slack", "jira", *(MCP_PREFIX + k for k in KNOWN_MCP))
            ],
            style=STYLE,
        )
    )
    for key, was in enabled.items():
        if was and key not in picked:
            console.print(f"  [yellow]{labels[key]} switched off.[/yellow] Its settings stay in config.yaml.")

    g = cfg.sources.google
    g.enabled = "google" in picked
    if g.enabled:
        creds_path = vault / "nbrain" / g.credentials_file
        replace = True
        if creds_path.exists():
            console.print(f"  OAuth client already saved at nbrain/{g.credentials_file}.")
            replace = _ask(Q.confirm("Replace it with a different credentials.json?", default=False, style=STYLE))
        else:
            console.print("  Google needs an OAuth client (Desktop app) from https://console.cloud.google.com/apis/credentials with the Gmail, Calendar, Chat, Drive and Docs APIs enabled.")
        if replace:
            src = _ask(Q.path("Path to the downloaded credentials.json", style=STYLE))
            src_p = Path(src).expanduser()
            if src_p.exists():
                creds_path.write_bytes(src_p.read_bytes())
                creds_path.chmod(0o600)
            else:
                console.print("  [yellow]File not found; Google left enabled but unauthorised. Run `nbrain auth google` later.[/yellow]")
        if creds_path.exists():
            all_subs = ["gmail", "calendar", "chat", "drive_notes"]
            subs: list[str] = []
            for _ in range(2):
                subs = _ask(
                    Q.checkbox(
                        "Google sub-sources (space to toggle, enter to confirm)",
                        choices=[
                            Q.Choice("Gmail", "gmail", checked=g.gmail),
                            Q.Choice("Calendar", "calendar", checked=g.calendar),
                            Q.Choice("Chat", "chat", checked=g.chat),
                            Q.Choice("Drive meeting notes", "drive_notes", checked=g.drive_notes),
                        ],
                        style=STYLE,
                    )
                )
                if subs:
                    break
                console.print("  [yellow]Pick at least one, or Google has nothing to read and sign-in will fail.[/yellow]")
            if not subs:
                subs = all_subs
                console.print("  Keeping all four enabled.")
            g.gmail, g.calendar, g.chat, g.drive_notes = ("gmail" in subs, "calendar" in subs, "chat" in subs, "drive_notes" in subs)

    gl = cfg.sources.gitlab
    gl.enabled = "gitlab" in picked
    if gl.enabled:
        gl.url = _ask(Q.text("GitLab URL", default=gl.url, style=STYLE))
        gl.username = _ask(Q.text("Your GitLab username", default=gl.username or "", style=STYLE)) or None
        _secret_step(gl.token_env, vault, prompt="GitLab personal access token (read_api scope)")

    sl = cfg.sources.slack
    sl.enabled = "slack" in picked
    if sl.enabled:
        _secret_step(sl.token_env, vault, prompt="Slack user token (xoxp-…, read scopes only)")

    j = cfg.sources.jira
    j.enabled = "jira" in picked
    if j.enabled:
        j.url = _ask(Q.text("Jira URL (https://x.atlassian.net)", default=j.url, style=STYLE))
        j.email = _ask(Q.text("Jira account email (blank for Data Center bearer token)", default=j.email or cfg.user.email, style=STYLE))
        _secret_step(j.token_env, vault, prompt="Jira API token")

    for key in KNOWN_MCP:
        _known_mcp_step(cfg, vault, key, MCP_PREFIX + key in picked)


def _mcp_step(cfg: Config) -> None:
    while _ask(Q.confirm("Add an MCP server?", default=False, style=STYLE)):
        name = _ask(Q.text("Name (short, e.g. linear)", style=STYLE))
        transport = _ask(Q.select("Transport", choices=["stdio", "http", "sse"], style=STYLE))
        srv = MCPServerConfig(name=safe_filename(name).replace(" ", "-").lower(), transport=transport)
        if transport == "stdio":
            cmdline = _ask(Q.text("Command line (e.g. npx -y @modelcontextprotocol/server-x)", style=STYLE)).split()
            srv.command, srv.args = cmdline[0], cmdline[1:]
        else:
            srv.url = _ask(Q.text("URL", style=STYLE))
            hdr = _ask(Q.text("Auth header env var (blank = none), value sent as Authorization: Bearer <env>", default="", style=STYLE))
            if hdr:
                srv.headers_env = {"Authorization": hdr}
        srv.roles = _ask(Q.checkbox("Roles this server covers", choices=[Q.Choice(r, r) for r in ("email", "calendar", "chat", "tickets", "code", "notes", "knowledge", "other")], style=STYLE)) or ["other"]
        srv.instructions = _ask(Q.text("Extra guidance for the collector (optional)", default="", style=STYLE))
        cfg.mcp_servers.append(srv)
        try:
            from nbrain.mcp.registry import MCPRegistry

            cfg2 = cfg.model_copy(deep=True)
            with console.status("Connecting…"):
                tools = asyncio.run(MCPRegistry(cfg2).list_tools(srv.name))
            allowed = [t["name"] for t in tools if t["allowed"]]
            blocked = [t["name"] for t in tools if not t["allowed"]]
            console.print(f"  [green]{len(allowed)} tools usable[/green], {len(blocked)} blocked by the read-only gate" + (f": {', '.join(blocked[:6])}" if blocked else ""))
        except Exception as e:  # noqa: BLE001
            console.print(f"  [yellow]could not connect: {str(e)[:200]} (kept; fix later in settings)[/yellow]")


def _delivery_step(cfg: Config) -> None:
    picked = _ask(Q.checkbox("Deliver the brief via", choices=[
        Q.Choice("Markdown + HTML files in the vault (always on)", "file", checked=True, disabled="always on"),
        Q.Choice("Gmail draft to yourself (never sent)", "gmail_draft"),
        Q.Choice("Email sent to yourself", "gmail_send"),
        Q.Choice("Slack DM to yourself", "slack_dm"),
    ], style=STYLE))
    cfg.delivery.gmail_draft.enabled = "gmail_draft" in picked
    cfg.delivery.gmail_send.enabled = "gmail_send" in picked
    cfg.delivery.slack_dm.enabled = "slack_dm" in picked
    if cfg.delivery.gmail_draft.enabled or cfg.delivery.gmail_send.enabled:
        console.print("  Note: this adds a Gmail compose/send scope, the one write nbrain is allowed. It only ever addresses you.")
        if not cfg.sources.google.enabled:
            console.print("  [yellow]Gmail delivery needs the Google source configured for auth.[/yellow]")
    if cfg.delivery.slack_dm.enabled and not cfg.sources.slack.enabled:
        console.print("  [yellow]Slack DM delivery uses the Slack token from the Slack source; configure it too.[/yellow]")


def _schedule_step(cfg: Config) -> None:
    cfg.sweep.daily_time = _ask(Q.text("Daily brief time (HH:MM)", default=cfg.sweep.daily_time, style=STYLE))
    cfg.sweep.weekdays_only = _ask(Q.confirm("Weekdays only?", default=True, style=STYLE))
    cfg.sweep.weekly.day = _ask(Q.select("Weekly review day", choices=["mon", "tue", "wed", "thu", "fri"], default="fri", style=STYLE))
    cfg.sweep.weekly.time = _ask(Q.text("Weekly review time", default=cfg.sweep.weekly.time, style=STYLE))


def _gap_questions(cfg: Config, store: VaultStore, disc: Any) -> None:
    from nbrain.setup.discovery import DiscoveryResult

    disc = disc or DiscoveryResult()
    # role track
    proposed = disc.suggested_track()
    console.print(f"\nProposed role track: [bold]{proposed}[/bold] (organises {disc.organised_meetings} recurring meetings)")
    cfg.role_track = RoleTrack(_ask(Q.select("Role track", choices=["ic", "lead", "manager"], default=proposed, style=STYLE)))

    # 1. priorities
    console.print("\n[bold]1. Priorities.[/bold] Projects are the lens for everything. Equal weights mean the brief flags when one starves another.")
    candidates = [p.split(" (")[0] for p in disc.projects]
    chosen: list[str] = _ask(Q.checkbox("Projects found (select the live ones)", choices=[Q.Choice(p, p, checked=True) for p in candidates], style=STYLE)) if candidates else []
    extra = _ask(Q.text("Other priorities/projects, comma separated", default="", style=STYLE))
    chosen += [x.strip() for x in extra.split(",") if x.strip()]
    equal = _ask(Q.confirm("Equal weight for all?", default=True, style=STYLE)) if len(chosen) > 1 else True
    for name in chosen:
        weight = 1.0 if equal else float(_ask(Q.text(f"Weight for {name} (1 = normal, 2 = double)", default="1", style=STYLE)) or 1)
        slipping = _ask(Q.text(f"What does 'slipping' look like on {name}? (one line; the index can never know this)", default="", style=STYLE))
        keywords = [name.lower()] + [w for w in name.lower().split() if len(w) > 3]
        pr = Project(title=safe_filename(name), weight=weight, slipping_means=slipping or None, keywords=keywords)
        pr.id = pr.title
        store.save(pr)

    # 2. reply urgency tiers
    console.print("\n[bold]2. Reply urgency.[/bold] Whose message should never wait?")
    people = disc.people[:20]
    tier1 = _ask(Q.checkbox("Tier 1 (proposed from 1:1s and mail volume)", choices=[
        Q.Choice(f"{p.name} <{p.email or '?'}> — {'; '.join(p.evidence[:2])}", p, checked=p.one_to_one or p.score >= 8) for p in people
    ], style=STYLE)) if people else []
    tier1_keys = {(p.email or p.name).lower() for p in tier1}
    manager = _ask(Q.text("Your manager's name (blank if none)", default="", style=STYLE))
    for p in people:
        tier = 1 if (p.email or p.name).lower() in tier1_keys else 2
        person = Person(title=safe_filename(p.name), email=p.email, tier=tier, external=p.external, sla_hours=(8 if tier == 1 else None), aliases=[])
        person.id = person.title
        if manager and manager.lower() in p.name.lower():
            person.relationship = "manager"
            cfg.user.manager = person.title
        store.save(person)
    extra_people = _ask(Q.text("Anyone missing from tier 1? Names (comma separated)", default="", style=STYLE))
    for name in [x.strip() for x in extra_people.split(",") if x.strip()]:
        person = Person(title=safe_filename(name), tier=1, sla_hours=8)
        person.id = person.title
        store.save(person)
    if manager and not cfg.user.manager:
        cfg.user.manager = safe_filename(manager)
        store.ensure_person(cfg.user.manager, tier=1, relationship="manager", sla_hours=4)

    # meetings
    for m in disc.meetings[:25]:
        mt = Meeting(title=safe_filename(m.title), cadence=m.cadence, time=m.time or None, organiser_is_me=m.organiser_is_me, attendees=[safe_filename(a) for a in m.attendees[:12] if a], agenda_required=len(m.attendees) > 1, focus_block=not m.attendees)
        mt.id = mt.title
        store.save(mt)

    # 3. name variants and collisions
    console.print("\n[bold]3. Names.[/bold] Highest-consequence question: a wrong alias attributes someone else's commitments to you.")
    variants = _ask(Q.text("How do transcripts mis-spell your name? (comma separated, blank if unknown)", default=", ".join(disc.name_variants), style=STYLE))
    cfg.user.name_variants = [v.strip() for v in variants.split(",") if v.strip()]
    collisions = _ask(Q.text("Colleagues with a confusable name (comma separated, blank if none)", default="", style=STYLE))
    cfg.user.name_collisions = [v.strip() for v in collisions.split(",") if v.strip()]

    # noise
    cfg.noise.mine = disc.noise_mine
    cfg.noise.suppress = disc.noise_suppress
    if disc.noise_mine or disc.noise_suppress:
        console.print(f"  Noise: {len(disc.noise_mine)} automated senders kept for signal, {len(disc.noise_suppress)} suppressed (edit in settings).")

    # 4. timing was asked already; 5. delivery confirmation
    if cfg.delivery.gmail_draft.enabled or cfg.delivery.gmail_send.enabled:
        if not _ask(Q.confirm("Confirm: nbrain may create an email draft/send addressed only to you. Reply drafts stay excluded.", default=True, style=STYLE)):
            cfg.delivery.gmail_draft.enabled = cfg.delivery.gmail_send.enabled = False


def _write_rules(cfg: Config, store: VaultStore) -> None:
    tpl = (Path(__file__).parent / "rules.template.md").read_text()
    text = (
        tpl.replace("{{USER_NAME}}", cfg.user.name or "you")
        .replace("{{USER_EMAIL}}", cfg.user.email or "you")
        .replace("{{SETUP_DATE}}", date.today().isoformat())
        .replace("{{DROP_AFTER}}", str(cfg.sweep.unconfirmed_drop_after_runs))
        .replace("{{NAME_VARIANTS}}", ", ".join(cfg.user.name_variants) or "none recorded")
        .replace("{{COLLISIONS}}", (f"**{', '.join(cfg.user.name_collisions)} are different people.**" if cfg.user.name_collisions else ""))
        .replace("{{VAULT}}", str(store.root))
    )
    store.write_text("nbrain/rules.md", text)


SETUP_STEPS: tuple[tuple[str, str], ...] = (
    ("profile", "You — name, email, timezone"),
    ("model", "Model provider and per-task overrides"),
    ("sources", "Sources and MCP servers"),
    ("delivery", "Delivery channels"),
    ("schedule", "Sweep and weekly review times"),
    ("google_auth", "Re-authorise Google in the browser"),
    ("discovery", "Discovery and the five questions (People, Projects, Meetings)"),
    ("dryrun", "Dry-run sweep"),
    ("service", "Background service (launchd)"),
)


def _is_configured(cfg: Config) -> bool:
    """A setup worth preserving: someone has already answered the basics."""
    return bool(cfg.user.email and cfg.user.name)


def _choose_steps(cfg: Config, store: VaultStore) -> set[str]:
    """On a re-run, do only what the user asks for. A first run does everything."""
    if not _is_configured(cfg):
        return {k for k, _ in SETUP_STEPS}

    people = len(store.people())
    projects = len(store.projects())
    console.print(
        f"\n[bold]Existing setup found[/bold] for {cfg.user.name} <{cfg.user.email}> · "
        f"{people} people, {projects} projects recorded."
    )
    console.print("  Pick only what you want to change. Anything you skip is left exactly as it is.")
    suggested = {"discovery"} if not (people or projects) else set()
    if suggested:
        console.print("  [yellow]People and Projects are empty, so discovery is pre-selected.[/yellow]")
    chosen = _ask(
        Q.checkbox(
            "What would you like to set up?",
            choices=[Q.Choice(label, key, checked=key in suggested) for key, label in SETUP_STEPS],
            style=STYLE,
        )
    )
    if not chosen:
        console.print("Nothing selected, so nothing changed.")
    return set(chosen)


def run_setup(vault_arg: Path | None, *, defaults: bool = False) -> None:
    from nbrain.config.loader import resolve_vault_path

    console.rule("[bold]nbrain setup")
    default_vault = resolve_vault_path(vault_arg)
    if defaults:
        vault = default_vault
    else:
        vault = Path(_ask(Q.path("Where should the vault live? (an Obsidian vault folder; it persists between runs)", default=str(default_vault), style=STYLE))).expanduser().resolve()
        if _in_git_checkout(vault):
            console.print("[yellow]That path is inside a git checkout. A work journal in a shared repo is a bad day.[/yellow]")
            if not _ask(Q.confirm("Use it anyway?", default=False, style=STYLE)):
                return run_setup(None, defaults=False)
    store = VaultStore(vault)
    store.ensure_layout()
    write_pointer(vault)
    try:
        cfg = load_config(vault)
        console.print(f"Existing config found at {vault}/nbrain/config.yaml; answers pre-filled from it.")
    except Exception:  # noqa: BLE001
        cfg = Config(vault_path=vault)
    cfg.vault_path = vault

    if defaults:
        cfg.user.timezone = _local_tz()
        save_config(cfg)
        _write_rules(cfg, store)
        store.write_text("memory.md", render_memory(store, cfg, date.today()))
        console.print(f"Wrote default config to {vault}/nbrain/config.yaml. Edit it or run `nbrain ui`.")
        return

    steps = _choose_steps(cfg, store)
    if not steps:
        return

    if "profile" in steps:
        console.print("\n[bold]You[/bold]")
        cfg.user.name = _ask(Q.text("Full name", default=cfg.user.name, style=STYLE))
        cfg.user.first_name = _ask(Q.text("What should the brief call you?", default=cfg.user.first_name or cfg.user.name.split(" ")[0], style=STYLE))
        cfg.user.email = _ask(Q.text("Work email", default=cfg.user.email, style=STYLE))
        cfg.user.timezone = _ask(Q.text("Timezone", default=cfg.user.timezone if cfg.user.timezone != "UTC" else _local_tz(), style=STYLE))
        save_config(cfg)

    if "model" in steps:
        console.print("\n[bold]Model[/bold]")
        cfg.llm.default = _llm_step("Default model", vault, cfg.llm.default)
        save_config(cfg)
        if not _test_llm(cfg, "default") and not _ask(Q.confirm("Continue without a working model? (numbers-only briefs until fixed)", default=True, style=STYLE)):
            return
        if _ask(Q.confirm("Use a different (cheaper or local) model for extraction?", default=cfg.llm.extract is not None, style=STYLE)):
            cfg.llm.extract = _llm_step("Extraction model", vault, cfg.llm.extract)
            save_config(cfg)
            _test_llm(cfg, "extract")

    if "sources" in steps:
        console.print("\n[bold]Sources[/bold]")
        _sources_step(cfg, vault)
        save_config(cfg)
        console.print("\n[bold]MCP servers[/bold] (optional; any server, read tools only)")
        _mcp_step(cfg)
        save_config(cfg)

    if "delivery" in steps:
        console.print("\n[bold]Delivery[/bold]")
        _delivery_step(cfg)
        save_config(cfg)

    if "schedule" in steps:
        console.print("\n[bold]Schedule[/bold]")
        _schedule_step(cfg)
        save_config(cfg)

    token_exists = (vault / "nbrain" / cfg.sources.google.token_file).exists()
    creds_exist = (vault / "nbrain" / cfg.sources.google.credentials_file).exists()
    want_auth = "google_auth" in steps or ("sources" in steps and not token_exists)
    if cfg.sources.google.enabled and creds_exist and want_auth:
        try:
            from nbrain.sources.google.auth import GoogleAuth

            console.print("\nOpening the browser for Google authorisation…")
            GoogleAuth(cfg).credentials(interactive=True)
            console.print("  [green]Google authorised.[/green]")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [yellow]Google auth failed: {str(e)[:200]}. Run `nbrain auth google` later.[/yellow]")

    if "discovery" in steps:
        _discovery_and_questions(cfg, store, vault)

    if "dryrun" in steps and _ask(Q.confirm("Run a dry-run sweep now to tune thresholds? (nothing delivered, vault untouched)", default=True, style=STYLE)):
        _dry_run(cfg, store)

    if "service" in steps and _ask(Q.confirm("Install the background service (launchd) so the brief runs on schedule?", default=False, style=STYLE)):
        from nbrain.scheduler.daemon import launchctl, write_launchd_plist

        plist = write_launchd_plist(cfg)
        ok, out = launchctl("load")
        console.print(f"Wrote {plist}; launchctl {'ok' if ok else 'failed: ' + out}")

    console.print("\nDone. Next: `nbrain sweep` for a real run, `nbrain ui` for settings, briefs and the graph, `nbrain doctor` any time.")


def _discovery_and_questions(cfg: Config, store: VaultStore, vault: Path) -> None:
    console.print("\n[bold]Discovery[/bold] — inferring role, people, meetings and noise from your sources")
    disc = None
    try:
        from nbrain.setup.discovery import discover

        with console.status("Reading 14 days of calendar, 30 days of mail, tickets and MRs…"):
            disc = asyncio.run(discover(cfg, store))
        t = Table(title="What discovery found")
        t.add_column("Kind")
        t.add_column("Found")
        t.add_row("People", f"{len(disc.people)} (top: {', '.join(p.name for p in disc.people[:5])})")
        t.add_row("Recurring meetings", f"{len(disc.meetings)} ({disc.organised_meetings} you organise)")
        t.add_row("Projects", ", ".join(disc.projects) or "none from tickets/MRs")
        t.add_row("Noise senders", f"{len(disc.noise_mine)} mine-for-signal, {len(disc.noise_suppress)} suppress")
        for n in disc.notes:
            t.add_row("[yellow]note[/yellow]", n)
        console.print(t)
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Discovery skipped: {str(e)[:200]}[/yellow]")

    console.print("\n[bold]Five things only you can answer[/bold]")
    _gap_questions(cfg, store, disc)
    save_config(cfg)
    _write_rules(cfg, store)
    store.write_text("memory.md", render_memory(store, cfg, date.today()))
    console.print(f"\n[green]Config written[/green] to {vault}/nbrain/config.yaml; People/, Projects/, Meetings/, memory.md and nbrain/rules.md created.")


def _dry_run(cfg: Config, store: VaultStore) -> None:
    from nbrain.sweep.pipeline import Sweep

    try:
        with console.status("Sweeping (dry run)…"):
            rep = asyncio.run(Sweep(cfg, store, dry_run=True).run())
        console.rule("Dry-run brief")
        console.print(rep.markdown, markup=False, highlight=False)
        if rep.not_checked:
            console.print("[yellow]Not checked:[/yellow] " + "; ".join(rep.not_checked))
        for w in rep.warnings:
            console.print(f"[yellow]{w}[/yellow]")
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Dry run failed: {e}[/red]")
    console.print("\nIf the brief over- or under-flags, adjust thresholds with `nbrain config set sweep.<key> <value>` or in `nbrain ui`.")
