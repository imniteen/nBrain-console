"""Connect an MCP server the way a chat app connects a connector.

The user clicks Connect, a browser opens at the provider's consent screen, and approving it
leaves a token in the vault that every later sweep reuses. No token is typed or pasted.

Two routes, chosen by what the provider supports:

* **dynamic client registration** — `fastmcp` discovers the authorisation server, registers
  nbrain on the spot, opens the browser and stores the token. Nothing to configure (Glean).
* **confidential client** — the provider wants a client an admin registered beforehand, and
  Slack additionally refuses an `http` redirect, which rules out fastmcp's built-in flow. So
  nbrain runs the authorisation-code exchange itself over a TLS loopback listener and saves
  the resulting bearer, which the transport then sends as an ordinary header.

Either way `status()` answers the same question — connected or not — without touching the
network, because the Sources page asks it on every render."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import Client

from nbrain.auth import loopback
from nbrain.config.loader import KEYRING_SERVICE, get_secret, save_config, set_secret
from nbrain.config.schema import Config, MCPServerConfig
from nbrain.mcp.catalog import CONNECTORS, Connector, find
from nbrain.mcp.registry import OAuthError, ToolGate, build_transport, is_write_tool
from nbrain.mcp.tokens import FileTokenStore, token_file

log = logging.getLogger(__name__)

ConnectError = OAuthError

# The consent screen is a human at a browser: allow for a slow SSO login, but do not hang for ever.
CONSENT_TIMEOUT = 300.0


@dataclass
class ToolInfo:
    """One row of the tool-permission list."""

    name: str
    description: str = ""
    write: bool = False
    allowed: bool = True

    @property
    def label(self) -> str:
        """`slack_search_public` -> "Search public". The raw name still shows underneath,
        because it is what you would put in `tool_deny` by hand."""
        words = self.name.split("_")
        if len(words) > 1 and words[0] in {"slack", "glean", "mcp"}:
            words = words[1:]
        return " ".join(words).replace("-", " ").strip().capitalize() or self.name


@dataclass
class Status:
    """What the Sources page needs to draw one connector row."""

    name: str
    label: str = ""
    style: str = ""  # dcr | confidential | token
    connected: bool = False  # a grant is stored
    usable: bool = False  # ...and the server actually answered with it
    checked: bool = False  # whether we have ever asked; unknown is not the same as broken
    error: str = ""  # why the server refused, when it did
    expires_at: float | None = None
    detail: str = ""
    blocker: str = ""  # non-empty when Connect cannot work yet, e.g. a client secret is missing
    tools: list[ToolInfo] = field(default_factory=list)

    @property
    def expires_in(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()

    @property
    def needs_check(self) -> bool:
        """Worth asking the server. Either we never have, or what we recorded is unusable —
        a record written before tool lists were kept leaves a working connector with no list."""
        return self.connected and (not self.checked or (self.usable and not self.tools))


def store_for(vault: Path) -> FileTokenStore:
    return FileTokenStore(token_file(vault))


def connector_for(server: MCPServerConfig) -> Connector | None:
    return CONNECTORS.get(server.name)


def server_for(cfg: Config, key: str) -> MCPServerConfig:
    server = find(cfg, key)
    if server is None:
        raise ConnectError(f"no MCP server named {key} is configured")
    return server


def bearer_env(server: MCPServerConfig) -> str | None:
    return server.headers_env.get("Authorization") or next(iter(server.headers_env.values()), None)


# ----- status -----
def status(cfg: Config, server: MCPServerConfig) -> Status:
    """Whether this server is connected. Cheap: reads two files, no network, no browser."""
    conn = connector_for(server)
    st = Status(name=server.name, label=conn.label if conn else server.name, style=conn.auth if conn else "token")

    if server.auth == "oauth":
        st.style = "dcr"
        if not server.url:
            st.blocker = "no endpoint configured"
        else:
            store = store_for(cfg.vault_path)
            st.connected = store.holds(server.url)
            st.expires_at = store.expires_at(server.url)
    else:
        env = bearer_env(server)
        st.connected = bool(get_secret(env))
        if conn is not None and conn.auth == "confidential":
            saved = _saved_token(cfg, server)
            st.expires_at = saved.get("expires_at") if saved else None
            if not server.oauth_client_id:
                st.blocker = "add the Client ID from your provider app"
            elif conn.client_secret_env and not get_secret(conn.client_secret_env):
                st.blocker = f"{conn.client_secret_env} is not set"
        elif not st.connected and env:
            st.blocker = f"{env} is not set"

    # Holding a token and being able to use it are different things: Slack hands out a grant
    # and then refuses the MCP endpoint until an admin enables the app. Reporting that as
    # "connected" sends people looking in the wrong place.
    health = _health(cfg, server) if st.connected else None
    st.checked = health is not None
    st.usable = bool(health and health.get("ok"))
    st.error = "" if st.usable else (health or {}).get("error", "")
    st.tools = _rank(server, _tools_from(health))
    if not st.connected:
        st.detail = "not connected"
    elif st.usable:
        st.detail = "connected"
    elif st.checked:
        st.detail = "authorised, but the server refused"
    else:
        st.detail = "authorised, checking"
    return st


def statuses(cfg: Config) -> list[Status]:
    """One row per configured connector, catalogue entries first."""
    servers = {s.name: s for s in cfg.mcp_servers}
    ordered = [servers[k] for k in CONNECTORS if k in servers]
    ordered += [s for name, s in servers.items() if name not in CONNECTORS]
    return [status(cfg, s) for s in ordered]


# ----- connecting -----
async def connect(
    cfg: Config,
    server: MCPServerConfig,
    *,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> Status:
    """Run whichever consent flow this server needs, then list the tools it offers."""
    conn = connector_for(server)
    if conn is not None and conn.auth == "confidential":
        await _connect_confidential(
            cfg, server, conn, open_browser=open_browser, on_url=on_url, cancel=cancel
        )
    elif server.auth != "oauth" and not get_secret(bearer_env(server)):
        # A DCR server gets its token as a side effect of `probe` opening the client below; a
        # plain token server has no flow to run, so say so rather than failing later with a 401.
        raise ConnectError(
            f"{server.name} authenticates with a token and none is stored — add it under "
            "Settings → Secrets, or use a connector that supports OAuth"
        )
    return await probe(cfg, server)


async def _connect_confidential(
    cfg: Config,
    server: MCPServerConfig,
    conn: Connector,
    *,
    open_browser: bool,
    on_url: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Authorisation code + PKCE over nbrain's own TLS loopback, because Slack refuses http."""
    if not server.oauth_client_id:
        raise ConnectError(f"{conn.label} needs the Client ID from your provider app before it can connect")
    secret = get_secret(conn.client_secret_env)
    if conn.client_secret_env and not secret:
        raise ConnectError(f"{conn.label} needs {conn.client_secret_env} set before it can connect")

    provider = loopback.Provider(
        name=conn.label,
        authorize_url=conn.authorize_url,
        token_url=conn.token_url,
        client_id=server.oauth_client_id,
        client_secret=secret,
        scopes=server.oauth_scopes or list(conn.scopes),
        scope_param=conn.scope_param,
        scope_separator=conn.scope_separator,
    )
    token = await loopback.run_flow(
        provider,
        vault=cfg.vault_path,
        port=cfg.web.oauth_port,
        open_browser=open_browser,
        timeout=CONSENT_TIMEOUT,
        on_url=on_url,
        cancel=cancel,
    )
    await _save_token(cfg, server, conn, token)


def _token_key(server: MCPServerConfig) -> str:
    return f"{(server.url or server.name).rstrip('/')}/connector"


def _health_key(server: MCPServerConfig) -> str:
    return f"{(server.url or server.name).rstrip('/')}/health"


def _health(cfg: Config, server: MCPServerConfig) -> dict | None:
    return store_for(cfg.vault_path).peek(loopback.COLLECTION, _health_key(server))


async def _record_health(
    cfg: Config,
    server: MCPServerConfig,
    *,
    ok: bool,
    error: str = "",
    tools: list[ToolInfo] | None = None,
) -> None:
    await store_for(cfg.vault_path).put(
        _health_key(server),
        {
            "ok": ok,
            "error": error,
            "at": time.time(),
            # Names and descriptions only. Whether each one is allowed is decided from config
            # every time it is read, so a permission change shows up without another round-trip.
            "tools": [{"name": t.name, "description": t.description, "write": t.write} for t in tools or []],
        },
        collection=loopback.COLLECTION,
    )


def _saved_token(cfg: Config, server: MCPServerConfig) -> dict | None:
    """Expiry and refresh token, kept beside the MCP tokens so renewals need no browser."""
    return store_for(cfg.vault_path).peek(loopback.COLLECTION, _token_key(server))


async def _save_token(cfg: Config, server: MCPServerConfig, conn: Connector, token: dict) -> None:
    """Access token into the secret store, so the transport sends it; the rest into oauth.json."""
    env = conn.token_env or f"{conn.key.upper()}_MCP_TOKEN"
    set_secret(env, token["access_token"], use_keyring=False, vault=cfg.vault_path)
    if server.headers_env.get("Authorization") != env:
        server.headers_env = {"Authorization": env}
        save_config(cfg)
    rest = {k: v for k, v in token.items() if k != "access_token"}
    await store_for(cfg.vault_path).put(_token_key(server), rest, collection=loopback.COLLECTION)


async def probe(cfg: Config, server: MCPServerConfig) -> Status:
    """Open the server and list its tools. This is what triggers OAuth on a DCR connector."""
    transport = build_transport(server, vault=cfg.vault_path)
    try:
        async with Client(transport, init_timeout=CONSENT_TIMEOUT) as client:
            tools = await client.list_tools()
    except Exception as err:
        # Remember the refusal so the Sources page keeps saying so after a reload, instead of
        # showing a green pill that contradicts the error underneath it.
        await _record_health(cfg, server, ok=False, error=str(err))
        raise
    found = [
        ToolInfo(
            name=t.name,
            description=(t.description or "").strip().split("\n")[0],
            write=is_write_tool(t.name),
        )
        for t in tools
    ]
    await _record_health(cfg, server, ok=True, tools=found)
    st = status(cfg, server)
    st.tools = _rank(server, found)
    return st


def _tools_from(health: dict | None) -> list[ToolInfo]:
    """Read the recorded tool list, tolerating records written by an older version.

    An earlier build stored only a count here, and a stored record outlives the code that
    wrote it — so anything unexpected means "no list", not a crash on every page load."""
    raw = (health or {}).get("tools")
    if not isinstance(raw, list):
        return []
    out: list[ToolInfo] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            out.append(
                ToolInfo(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    write=bool(item.get("write")),
                )
            )
    return out


def _rank(server: MCPServerConfig, tools: list[ToolInfo]) -> list[ToolInfo]:
    """Apply the current gate and order reads before writes, as the page groups them."""
    gate = ToolGate(server, [])
    for t in tools:
        t.allowed = gate.allows(t.name)
    return sorted(tools, key=lambda t: (t.write, t.name))


async def check(cfg: Config, server: MCPServerConfig, *, timeout: float = 20.0) -> Status:
    """Ask the server whether the stored grant actually works. Never opens a browser, never raises.

    This is what turns "authorised, not yet checked" into an answer, so nobody has to know that
    Reconnect is the button that would have told them."""
    import asyncio

    try:
        return await asyncio.wait_for(probe(cfg, server), timeout)
    except TimeoutError:
        await _record_health(cfg, server, ok=False, error=f"the server did not answer within {int(timeout)}s")
    except Exception as err:  # noqa: BLE001 - the message is the whole point
        await _record_health(cfg, server, ok=False, error=str(err))
    return status(cfg, server)


async def disconnect(cfg: Config, server: MCPServerConfig) -> bool:
    """Forget the stored credentials. The grant itself lives on until revoked at the provider."""
    removed = False
    store = store_for(cfg.vault_path)
    if server.url:
        removed |= store.forget(server.url)
    removed |= await store.delete(_token_key(server), collection=loopback.COLLECTION)
    await store.delete(_health_key(server), collection=loopback.COLLECTION)
    for env in server.headers_env.values():
        removed |= forget_secret(cfg, env)
    return removed


def forget_secret(cfg: Config, env: str) -> bool:
    """Remove one secret from .env, this process's environment and the keyring."""
    gone = False
    env_path = cfg.vault_path / "nbrain" / ".env"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        keep = [ln for ln in lines if not ln.startswith(f"{env}=")]
        if len(keep) != len(lines):
            env_path.write_text("\n".join(keep) + ("\n" if keep else ""))
            gone = True
    gone |= os.environ.pop(env, None) is not None
    try:
        import keyring

        if keyring.get_password(KEYRING_SERVICE, env):
            keyring.delete_password(KEYRING_SERVICE, env)
            gone = True
    except Exception:  # noqa: BLE001 - no keyring backend available, or nothing stored there
        pass
    return gone
