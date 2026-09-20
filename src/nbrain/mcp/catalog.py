"""The connectors nbrain offers by name, and how each one authenticates.

One catalogue, read by the setup wizard and by the Sources page, so the two can never drift
apart on what Glean or Slack needs.

Three auth styles show up, in descending order of how pleasant they are:

``dcr``
    The server advertises a registration endpoint, so the client registers itself during the
    first connect. Nothing to configure, nothing to paste — click Connect and approve.
``confidential``
    The provider insists on a client id and secret that an admin registered beforehand. Still
    one click to connect, but someone creates an app once. Slack works this way and says so:
    "We do not support SSE-based connections or Dynamic Client Registration at this time."
``token``
    A long-lived API token, pasted by hand. The fallback when a provider offers nothing better,
    and the thing this whole module exists to avoid."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from nbrain.config.schema import Config, MCPServerConfig, Role

AuthStyle = Literal["dcr", "confidential", "token"]


@dataclass(frozen=True)
class Connector:
    key: str
    label: str
    blurb: str
    roles: list[Role]
    auth: AuthStyle
    url: str | None = None  # fixed endpoint
    # `confidential` connectors need the provider's own endpoints, because there is no
    # registration document to discover them from.
    authorize_url: str = ""
    token_url: str = ""
    scope_param: str = "scope"  # Slack's user-only authorize endpoint reads `scope`, not `user_scope`
    scope_separator: str = " " 
    url_template: str | None = None  # "https://{domain}/mcp/default" for per-tenant endpoints
    domain_hint: str = ""
    client_id_hint: str = ""
    client_secret_env: str | None = None
    scopes: list[str] = field(default_factory=list)
    token_env: str | None = None  # the `token` fallback, and what `token` style uses
    token_hint: str = ""
    brand: str = "#4f46e5"  # tile colour in the connector header
    instructions: str = ""
    docs_url: str = ""
    setup_steps: list[str] = field(default_factory=list)

    @property
    def needs_domain(self) -> bool:
        return self.url_template is not None

    def endpoint(self, domain: str = "") -> str:
        if self.url:
            return self.url
        assert self.url_template
        return self.url_template.format(domain=domain.replace("https://", "").replace("http://", "").strip("/"))


CONNECTORS: dict[str, Connector] = {
    "slack": Connector(
        key="slack",
        label="Slack",
        blurb="Messages, threads, channels and people, read through Slack's own MCP server",
        roles=["chat"],
        auth="confidential",
        url="https://mcp.slack.com/mcp",
        client_id_hint="Client ID from your Slack app's Basic Information page",
        client_secret_env="SLACK_MCP_CLIENT_SECRET",
        authorize_url="https://slack.com/oauth/v2_user/authorize",
        token_url="https://slack.com/api/oauth.v2.user.access",
        token_env="SLACK_MCP_TOKEN",
        # Slack's MCP guide lists these granularly: a bare `search:read` is refused with
        # "Invalid permissions requested". Read-only by construction, as everywhere else.
        scopes=[
            "search:read.public", "search:read.private", "search:read.im", "search:read.mpim",
            "search:read.files", "search:read.users",
            "channels:history", "groups:history", "im:history", "mpim:history",
            "channels:read", "groups:read", "im:read", "mpim:read",
            "users:read", "team:read",
        ],
        brand="#611f69",
        instructions="Prefer search over walking channel history, and name the channel and author of every message you quote.",
        docs_url="https://docs.slack.dev/ai/slack-mcp-server/",
        setup_steps=[
            "At api.slack.com/apps, open your Slack app (or create one for this workspace).",
            "Slack only allows directory-published or internal apps to use MCP, so keep it internal to your workspace.",
            "Under OAuth & Permissions, add the redirect URL nbrain shows on this page.",
            "Copy the Client ID and Client Secret from Basic Information into the fields here.",
            "Your workspace admin approves the app, then click Connect.",
        ],
    ),
    "glean": Connector(
        key="glean",
        label="Glean",
        blurb="Company knowledge: search, chat, documents, code and people",
        roles=["knowledge"],
        auth="dcr",
        url_template="https://{domain}/mcp/default",
        domain_hint="your Glean backend domain, shown at app.glean.com/admin/about-glean",
        token_env="GLEAN_TOKEN",
        token_hint="Glean user-scoped API token, with the MCP and SEARCH scopes",
        brand="#343ce5",
        instructions="Prefer search over chat, and name the document each answer came from.",
        docs_url="https://docs.glean.com/administration/platform/mcp/about",
        setup_steps=[
            "Enter your Glean backend domain and click Connect.",
            "Glean registers nbrain on the spot and asks you to sign in with your usual SSO.",
            "If your admin has not enabled Glean's OAuth server, fall back to an API token below.",
        ],
    ),
}


def find(cfg: Config, key: str) -> MCPServerConfig | None:
    return next((s for s in cfg.mcp_servers if s.name == key), None)


def apply(cfg: Config, conn: Connector, *, domain: str = "", client_id: str = "") -> MCPServerConfig:
    """Create or update the MCP entry for a catalogue connector, preserving anything hand-edited."""
    server = find(cfg, conn.key)
    if server is None:
        server = MCPServerConfig(name=conn.key)
        cfg.mcp_servers.append(server)
    server.enabled = True
    server.transport = "http"
    server.url = conn.endpoint(domain)
    server.roles = list(conn.roles)
    if not server.instructions:
        server.instructions = conn.instructions
    if conn.auth == "dcr":
        # fastmcp registers a client on the spot and holds the token; no header to set.
        server.auth = "oauth"
        server.oauth_scopes = list(conn.scopes)
        server.headers_env = {}
    else:
        # Confidential and token connectors both end up as a bearer header. For the confidential
        # ones nbrain runs the consent flow itself and writes the result into `token_env`, so the
        # transport needs no OAuth handler — it just sends the header.
        server.auth = "none"
        server.oauth_scopes = server.oauth_scopes or list(conn.scopes)
        if conn.token_env:
            server.headers_env = {"Authorization": conn.token_env}
        if conn.auth == "confidential":
            server.oauth_client_secret_env = conn.client_secret_env
            if client_id:
                server.oauth_client_id = client_id
    return server


def disable(cfg: Config, key: str) -> MCPServerConfig | None:
    """Switch a connector off without losing how it was set up."""
    server = find(cfg, key)
    if server is not None:
        server.enabled = False
    return server
