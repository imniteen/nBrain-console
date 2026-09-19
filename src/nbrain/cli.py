"""nbrain command line."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from nbrain import __version__
from nbrain.config.loader import (  # noqa: E402 - grouped for clarity
    ConfigError,
    coerce_scalar,
    config_file,
    get_dotted,
    load_config,
    quote_times,
    resolve_vault_path,
    save_config,
    set_dotted,
    set_secret,
)
from nbrain.config.schema import Config
from nbrain.vault.store import VaultStore

app = typer.Typer(help="nbrain — your second brain, standalone. Read-only towards every source.", no_args_is_help=True)
config_app = typer.Typer(help="Read or change settings in config.yaml")
mcp_app = typer.Typer(help="Inspect configured MCP servers")
llm_app = typer.Typer(help="Test model providers")
items_app = typer.Typer(help="List, dismiss or resolve tracked items")
service_app = typer.Typer(help="Install nbrain as a background service (macOS launchd)")
auth_app = typer.Typer(help="Authorise sources")
graph_app = typer.Typer(help="Relationship graph")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")
app.add_typer(llm_app, name="llm")
app.add_typer(items_app, name="items")
app.add_typer(service_app, name="service")
app.add_typer(auth_app, name="auth")
app.add_typer(graph_app, name="graph")

console = Console()
VaultOpt = Annotated[Path | None, typer.Option("--vault", "-v", help="Vault path (default: NBRAIN_VAULT, ~/.config/nbrain/vault_path, ~/nbrain-vault)")]


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=False)],
    )
    for noisy in ("httpx", "httpcore", "googleapiclient", "urllib3", "botocore", "boto3", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load(vault: Path | None) -> tuple[Config, VaultStore]:
    try:
        cfg = load_config(vault)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    return cfg, VaultStore(cfg.vault)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", help="Debug logging")] = False) -> None:
    _logging(verbose)


@app.command()
def version() -> None:
    console.print(f"nbrain {__version__}")


# ---------- setup ----------


@app.command()
def setup(vault: VaultOpt = None, defaults: Annotated[bool, typer.Option("--defaults", help="Write a default config without asking questions")] = False) -> None:
    """Guided first-time setup: vault, model, sources, delivery, schedule, discovery, dry run."""
    from nbrain.setup.wizard import run_setup

    run_setup(vault, defaults=defaults)


# ---------- sweep / brief / weekly ----------


def _print_report(rep: Any) -> None:
    console.rule(f"[bold]Daily brief — {rep.today}{' (dry run)' if rep.dry_run else ''}")
    console.print(rep.markdown, markup=False, highlight=False)
    console.rule()
    t = Table(show_header=False, box=None)
    t.add_row("Sources", ", ".join(f"{n}{'' if s.ok else ' ✗'}" for n, s in rep.source_status.items()) or "none")
    if rep.per_source:
        t.add_row("Collected", "\n".join(f"{k}: {v}" for k, v in sorted(rep.per_source.items())))
    if rep.verify:
        t.add_row("Verified", str(rep.verify.counts()))
    t.add_row("Items", f"{rep.created} new · {rep.updated} updated · {rep.reopened} reopened · {rep.skipped_dismissed} dismissed-skipped")
    t.add_row("Model", f"{rep.model} · {rep.llm_calls} calls · {rep.llm_tokens[0]}/{rep.llm_tokens[1]} tokens")
    t.add_row("Duration", f"{rep.duration_seconds:.1f}s")
    if rep.deliveries:
        t.add_row("Delivered", "; ".join(f"{d.channel}: {'ok' if d.ok else 'FAILED'} {d.detail}" for d in rep.deliveries))
    if rep.warnings:
        t.add_row("[yellow]Warnings[/yellow]", "\n".join(rep.warnings))
    if rep.not_checked:
        t.add_row("[yellow]Not checked[/yellow]", "\n".join(rep.not_checked))
    console.print(t)
    if rep.md_path:
        console.print(f"Written: {rep.md_path}")


@app.command()
def sweep(
    vault: VaultOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run everything against a throwaway copy of the vault; deliver nothing")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Skip the model; numbers-only brief")] = False,
    source: Annotated[list[str] | None, typer.Option("--source", "-s", help="Only these sources (repeatable)")] = None,
    no_deliver: Annotated[bool, typer.Option("--no-deliver", help="Write the vault files but skip delivery channels")] = False,
    day: Annotated[str | None, typer.Option("--date", help="Pretend today is YYYY-MM-DD")] = None,
) -> None:
    """Run the daily sweep now."""
    from nbrain.sweep.pipeline import Sweep

    cfg, store = _load(vault)
    today = date.fromisoformat(day) if day else None
    s = Sweep(cfg, store, today=today, dry_run=dry_run, only_sources=source, skip_llm=no_llm, deliver=not no_deliver)
    with console.status("Sweeping…"):
        rep = asyncio.run(s.run())
    _print_report(rep)


@app.command()
def brief(vault: VaultOpt = None, day: Annotated[str | None, typer.Option("--date")] = None) -> None:
    """Print the latest (or a given day's) brief."""
    _, store = _load(vault)
    text = store.read_text(f"Briefs/{day}.md" if day else "Briefs/latest.md")
    if text is None:
        console.print("[yellow]No brief yet. Run `nbrain sweep`.[/yellow]")
        raise typer.Exit(1)
    console.print(text, markup=False, highlight=False)


@app.command()
def weekly(vault: VaultOpt = None, no_llm: bool = typer.Option(False, "--no-llm"), no_deliver: bool = typer.Option(False, "--no-deliver")) -> None:
    """Write the weekly review now."""
    from nbrain.sweep.weekly import run_weekly

    cfg, store = _load(vault)
    with console.status("Reviewing the week…"):
        rep = asyncio.run(run_weekly(cfg, store, skip_llm=no_llm, deliver=not no_deliver))
    console.print(rep.markdown, markup=False, highlight=False)
    console.print(f"Written: {rep.path}")
    for w in rep.warnings:
        console.print(f"[yellow]{w}[/yellow]")


# ---------- daemon / ui ----------


@app.command()
def daemon(vault: VaultOpt = None, no_web: bool = typer.Option(False, "--no-web", help="Do not start the web UI")) -> None:
    """Run the scheduler in the foreground (launchd/Docker run this)."""
    from nbrain.scheduler.daemon import run_daemon

    cfg, _ = _load(vault)
    console.print(f"Daily {cfg.sweep.daily_time} · weekly {cfg.sweep.weekly.day} {cfg.sweep.weekly.time} · {cfg.user.timezone}" + (f" · UI http://{cfg.web.host}:{cfg.web.port}" if cfg.web.enabled and not no_web else ""))
    run_daemon(cfg.vault, with_web=not no_web)


@app.command()
def ui(vault: VaultOpt = None, port: int | None = typer.Option(None, "--port")) -> None:
    """Serve the local settings, brief and graph pages."""
    from nbrain.web.app import serve

    cfg, _ = _load(vault)
    if port:
        cfg.web.port = port
    console.print(f"http://{cfg.web.host}:{cfg.web.port}")
    serve(cfg)


# ---------- doctor ----------


@app.command()
def doctor(vault: VaultOpt = None, no_llm: bool = typer.Option(False, "--no-llm", help="Skip model round-trips")) -> None:
    """Check vault, config, model providers, sources, MCP servers and delivery."""
    from nbrain.llm.factory import describe_model
    from nbrain.sweep.pipeline import build_native_sources

    vpath = resolve_vault_path(vault)
    t = Table(title=f"nbrain doctor — {vpath}", show_lines=False)
    t.add_column("Check")
    t.add_column("Result")
    ok_all = True

    def row(name: str, ok: bool, detail: str) -> None:
        nonlocal ok_all
        ok_all &= ok
        t.add_row(name, ("[green]ok[/green] " if ok else "[red]FAIL[/red] ") + detail)

    row("vault", (vpath / "nbrain").is_dir(), str(vpath))
    try:
        cfg = load_config(vpath)
        row("config", True, str(config_file(vpath)))
    except ConfigError as e:
        row("config", False, str(e).splitlines()[0])
        console.print(t)
        raise typer.Exit(1) from None
    store = VaultStore(cfg.vault)
    row("user", bool(cfg.user.email), f"{cfg.user.name} <{cfg.user.email}> {cfg.user.timezone} · track {cfg.role_track.value}")

    async def checks() -> None:
        from nbrain.config.schema import Provider

        for task in ("default", "extract", "write", "mcp"):
            lcfg = cfg.llm.for_task(task)  # type: ignore[arg-type]
            if task != "default" and getattr(cfg.llm, task) is None:
                continue
            label = f"llm.{task}"
            if lcfg.provider == Provider.bedrock:
                from nbrain.llm.aws_creds import check_credentials

                st = check_credentials(lcfg)
                row(f"{label} aws", st.ok, f"{st.identity or ''} {st.detail}" + (f" · expires in {st.minutes_left:.0f} min" if st.minutes_left is not None else ""))
            if no_llm:
                row(label, True, describe_model(lcfg) + " (not called)")
                continue
            from nbrain.llm.tasks import LLMTasks

            try:
                says = await asyncio.wait_for(LLMTasks(cfg, today=date.today()).ping(task), 120)  # type: ignore[arg-type]
                row(label, True, f"{describe_model(lcfg)} → “{says}”")
            except Exception as e:  # noqa: BLE001
                row(label, False, f"{describe_model(lcfg)}: {str(e)[:160]}")
        for src in build_native_sources(cfg, store):
            try:
                st = await asyncio.wait_for(src.healthcheck(), 60)
                row(f"source {src.name}", st.ok, st.detail + (" · [yellow]token can write[/yellow]" if st.write_capable else ""))
            except Exception as e:  # noqa: BLE001
                row(f"source {src.name}", False, str(e)[:160])
        from nbrain.mcp.registry import MCPRegistry

        reg = MCPRegistry(cfg)
        for name in reg.names():
            try:
                tools = await asyncio.wait_for(reg.list_tools(name), 60)
                allowed = sum(1 for x in tools if x["allowed"])
                row(f"mcp {name}", allowed > 0, f"{allowed} usable, {len(tools) - allowed} blocked (read-only gate)")
            except Exception as e:  # noqa: BLE001
                row(f"mcp {name}", False, str(e)[:160])

    with console.status("Checking…"):
        asyncio.run(checks())
    d = cfg.delivery
    row("delivery", True, ", ".join(n for n, c in (("file", d.file), ("gmail_draft", d.gmail_draft), ("gmail_send", d.gmail_send), ("slack_dm", d.slack_dm)) if c.enabled) or "none")
    row("schedule", True, f"daily {cfg.sweep.daily_time} {'mon-fri' if cfg.sweep.weekdays_only else 'daily'} · weekly {cfg.sweep.weekly.day} {cfg.sweep.weekly.time}")
    console.print(t)
    raise typer.Exit(0 if ok_all else 1)


# ---------- config ----------


@config_app.command("path")
def config_path(vault: VaultOpt = None) -> None:
    console.print(str(config_file(resolve_vault_path(vault))))


@config_app.command("show")
def config_show(vault: VaultOpt = None) -> None:
    cfg, _ = _load(vault)
    data = cfg.model_dump(mode="json", exclude={"vault_path"})
    quote_times(data)
    console.print(yaml.safe_dump(data, sort_keys=False))


@config_app.command("get")
def config_get(key: str, vault: VaultOpt = None) -> None:
    cfg, _ = _load(vault)
    try:
        console.print(json.dumps(get_dotted(cfg.model_dump(mode="json"), key), default=str))
    except (KeyError, IndexError, ValueError):
        console.print(f"[red]no such key: {key}[/red]")
        raise typer.Exit(1) from None


@config_app.command("set")
def config_set(key: str, value: str, vault: VaultOpt = None) -> None:
    """Set a dotted key, e.g. `nbrain config set sweep.daily_time "08:30"`."""
    cfg, _ = _load(vault)
    data = cfg.model_dump(mode="json")
    set_dotted(data, key, coerce_scalar(value))
    try:
        new = Config.model_validate(data)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]invalid: {e}[/red]")
        raise typer.Exit(1) from None
    save_config(new)
    console.print(f"{key} = {json.dumps(get_dotted(new.model_dump(mode='json'), key), default=str)}")


@app.command()
def secret(env_name: str, value: str | None = typer.Argument(None, help="Omit to be prompted"), keyring: bool = typer.Option(False, "--keyring", help="Store in the OS keyring instead of nbrain/.env"), vault: VaultOpt = None) -> None:
    """Store a token or API key under an env var name (never in config.yaml)."""
    vpath = resolve_vault_path(vault)
    if value is None:
        value = typer.prompt(f"Value for {env_name}", hide_input=True)
    where = set_secret(env_name, value, use_keyring=keyring, vault=vpath)
    console.print(f"Stored {env_name} in {where}")


# ---------- items ----------


@items_app.command("list")
def items_list(vault: VaultOpt = None, status: str = typer.Option("open", "--status", help="open|watch|resolved|dismissed|all")) -> None:
    from nbrain.vault.schema import ItemStatus

    cfg, store = _load(vault)
    today = date.today()
    items = store.items() if status == "all" else store.items(ItemStatus(status))
    t = Table(title=f"{len(items)} {status} items")
    for c in ("id", "type", "title", "person", "due", "age", "verified", "source"):
        t.add_column(c)
    for i in sorted(items, key=lambda x: (x.priority, x.due or date.max)):
        t.add_row(i.id, i.type.value, i.title[:60], (i.promised_to or "")[2:-2], str(i.due or ""), str(i.age_days(today) or ""), i.verified_label(), i.source)
    console.print(t)


@items_app.command("dismiss")
def items_dismiss(item_id: str, reason: str = typer.Option(..., "--reason", "-r"), vault: VaultOpt = None) -> None:
    """Never raise this item again unless genuinely new activity appears (Rule 8)."""
    from nbrain.vault.schema import Item

    cfg, store = _load(vault)
    item = store.load(Item, item_id)
    if not item:
        console.print(f"[red]no item {item_id}[/red]")
        raise typer.Exit(1)
    store.dismiss_item(item, date.today(), reason)
    console.print(f"Dismissed: {item.title}")


@items_app.command("resolve")
def items_resolve(item_id: str, vault: VaultOpt = None, note: str = typer.Option("Marked done by you.", "--note")) -> None:
    from nbrain.vault.schema import Item

    cfg, store = _load(vault)
    item = store.load(Item, item_id)
    if not item:
        console.print(f"[red]no item {item_id}[/red]")
        raise typer.Exit(1)
    store.resolve_item(item, date.today(), note)
    console.print(f"Resolved: {item.title}")


# ---------- llm ----------


@llm_app.command("test")
def llm_test(vault: VaultOpt = None, task: str = typer.Option("default", "--task", help="default|extract|write|mcp")) -> None:
    from nbrain.llm.factory import describe_model
    from nbrain.llm.tasks import LLMTasks

    cfg, _ = _load(vault)
    lcfg = cfg.llm.for_task(task)  # type: ignore[arg-type]
    console.print(describe_model(lcfg))
    if lcfg.provider.value == "bedrock":
        from nbrain.llm.aws_creds import ensure_credentials

        st = ensure_credentials(lcfg)
        console.print(f"AWS: {'ok' if st.ok else 'FAIL'} {st.identity or ''} {st.detail}" + (f" · expires in {st.minutes_left:.0f} min" if st.minutes_left is not None else ""))
        if not st.ok:
            raise typer.Exit(1)
    try:
        says = asyncio.run(LLMTasks(cfg, today=date.today()).ping(task))  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]ok[/green] → {says}")


# ---------- mcp ----------


@mcp_app.command("list")
def mcp_list(vault: VaultOpt = None) -> None:
    cfg, _ = _load(vault)
    t = Table("name", "transport", "target", "roles", "allow_write")
    for s in cfg.mcp_servers:
        t.add_row(s.name + ("" if s.enabled else " (disabled)"), s.transport, s.command or s.url or "", ",".join(s.roles), str(s.allow_write))
    console.print(t)


@mcp_app.command("tools")
def mcp_tools(name: str, vault: VaultOpt = None) -> None:
    from nbrain.mcp.registry import MCPRegistry

    cfg, _ = _load(vault)
    tools = asyncio.run(MCPRegistry(cfg).list_tools(name))
    t = Table("tool", "gate", "description")
    for x in tools:
        t.add_row(x["name"], "[green]allowed[/green]" if x["allowed"] else "[red]blocked[/red]", x["description"])
    console.print(t)


@mcp_app.command("call")
def mcp_call(name: str, tool: str, args: str = typer.Argument("{}"), vault: VaultOpt = None) -> None:
    """Call one read tool directly, e.g. `nbrain mcp call linear list_issues '{"limit": 5}'`."""
    from nbrain.mcp.registry import MCPRegistry

    cfg, _ = _load(vault)
    try:
        res = asyncio.run(MCPRegistry(cfg).call(name, tool, json.loads(args)))
    except PermissionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print_json(json.dumps(res, default=str))


# ---------- auth ----------


@auth_app.command("google")
def auth_google(vault: VaultOpt = None) -> None:
    """Run the Google OAuth flow in your browser (read-only scopes unless Gmail delivery is on)."""
    from nbrain.sources.google.auth import GoogleAuth

    cfg, _ = _load(vault)
    ga = GoogleAuth(cfg)
    console.print("Scopes: " + ", ".join(s.rsplit("/", 1)[-1] for s in ga.scopes))
    ga.credentials(interactive=True)
    console.print(f"[green]Authorised.[/green] Token stored at {ga.token_path}")


# ---------- service ----------


@service_app.command("install")
def service_install(vault: VaultOpt = None) -> None:
    from nbrain.scheduler.daemon import launchctl, write_launchd_plist

    cfg, _ = _load(vault)
    path = write_launchd_plist(cfg)
    ok, out = launchctl("load")
    console.print(f"Wrote {path}\nlaunchctl: {'ok' if ok else 'FAILED'} {out}")
    console.print("The daemon runs while you are logged in; runs missed during sleep are caught up within the grace window.")


@service_app.command("uninstall")
def service_uninstall() -> None:
    from nbrain.scheduler.daemon import launchctl, plist_path

    ok, out = launchctl("unload")
    plist_path().unlink(missing_ok=True)
    console.print(f"launchctl: {'ok' if ok else out}\nRemoved {plist_path()}")


@service_app.command("status")
def service_status() -> None:
    from nbrain.scheduler.daemon import launchctl

    ok, out = launchctl("print")
    console.print("[green]running[/green]" if ok else "[yellow]not loaded[/yellow]")
    if out:
        console.print(out)


# ---------- graph ----------


@graph_app.command("export")
def graph_export(vault: VaultOpt = None, out: Path | None = typer.Option(None, "--out", help="Directory for graph.json and graph.graphml")) -> None:
    from nbrain.vault.graph import VaultGraph

    cfg, store = _load(vault)
    out_dir = out or cfg.nbrain_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    g = VaultGraph(store)
    g.export(out_dir / "graph.json", out_dir / "graph.graphml")
    for p in g.patterns(date.today())[:10]:
        console.print(f"- {p.text}")
    console.print(f"Written {out_dir / 'graph.json'} and {out_dir / 'graph.graphml'}")


if __name__ == "__main__":
    app()
