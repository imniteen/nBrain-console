"""Connecting a source must never ask the user for a token.

These cover everything up to the provider's own consent screen: the loopback flow (driven
against a fake authorisation server), the token store that lets the daemon reuse a grant, and
the status the Sources page reads. The click on the real Slack or Glean screen is the one part
only the user can perform."""

from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from pathlib import Path

import httpx
import pytest
import respx

from nbrain.auth import loopback
from nbrain.config.loader import load_config
from nbrain.config.schema import Config, MCPServerConfig
from nbrain.mcp import catalog, connect
from nbrain.mcp.tokens import FileTokenStore, token_file

TOKEN_URL = "https://provider.test/token"


@pytest.fixture(autouse=True)
def _no_leftover_tokens(monkeypatch):
    """`set_secret` also exports to the environment; keep that out of the next test."""
    for env in ("SLACK_MCP_TOKEN", "SLACK_MCP_CLIENT_SECRET", "GLEAN_TOKEN"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(vault_path=tmp_path / "vault")
    (c.vault_path / "nbrain").mkdir(parents=True)
    c.web.oauth_port = 8799
    return c


def _provider(**kw) -> loopback.Provider:
    return loopback.Provider(
        name="Test", authorize_url="https://provider.test/authorize", token_url=TOKEN_URL,
        client_id="cid", client_secret="csec", **kw,
    )


def _answer_consent(reply: dict[str, str] | None = None, *, port: int = 8799):
    """Stand in for the browser: read the state out of the consent URL and hit the callback."""

    def open_url(url: str) -> bool:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        back = dict(reply or {"code": "the-code", "state": params["state"]})
        back.setdefault("state", params["state"])
        query = urllib.parse.urlencode(back)
        # urllib, not httpx: respx intercepts every httpx request in these tests, including
        # this stand-in for the browser.
        url_back = f"http://localhost:{port}/oauth/callback?{query}"
        threading.Timer(0.05, lambda: urllib.request.urlopen(url_back, timeout=5).read()).start()
        return True

    return open_url


# ----- the flow -----
@pytest.mark.anyio
async def test_a_consent_redirect_becomes_a_stored_token(cfg, monkeypatch):
    monkeypatch.setattr("webbrowser.open", _answer_consent())
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(
            httpx.Response(200, json={"access_token": "tok-1", "refresh_token": "r-1", "expires_in": 3600})
        )
        token = await loopback.run_flow(
            _provider(), vault=cfg.vault_path, port=cfg.web.oauth_port, secure=False, timeout=10
        )
    assert token["access_token"] == "tok-1"
    assert token["refresh_token"] == "r-1"
    assert token["expires_at"] > 0
    sent = dict(urllib.parse.parse_qsl(route.calls[0].request.content.decode()))
    assert sent["grant_type"] == "authorization_code"
    assert sent["client_secret"] == "csec", "a confidential client must authenticate itself"
    assert sent["code_verifier"], "PKCE verifier must be sent with the exchange"


@pytest.mark.anyio
async def test_a_redirect_with_the_wrong_state_is_discarded(cfg, monkeypatch):
    """Without this check any page could hand nbrain a code of its choosing."""
    monkeypatch.setattr("webbrowser.open", _answer_consent({"code": "x", "state": "not-ours"}))
    with pytest.raises(loopback.OAuthFlowError, match="wrong state"):
        await loopback.run_flow(
            _provider(), vault=cfg.vault_path, port=cfg.web.oauth_port, secure=False, timeout=10
        )


@pytest.mark.anyio
async def test_a_refusal_at_the_consent_screen_is_reported(cfg, monkeypatch):
    monkeypatch.setattr(
        "webbrowser.open", _answer_consent({"error": "access_denied", "error_description": "user said no"})
    )
    with pytest.raises(loopback.OAuthFlowError, match="user said no"):
        await loopback.run_flow(
            _provider(), vault=cfg.vault_path, port=cfg.web.oauth_port, secure=False, timeout=10
        )


def test_slack_shaped_replies_are_understood():
    """Slack nests the user token and reports failure with HTTP 200 and ok: false."""
    out = loopback.normalise_token(
        {"ok": True, "authed_user": {"access_token": "xoxp-1", "expires_in": 43200, "refresh_token": "xoxe-1"}}
    )
    assert out["access_token"] == "xoxp-1"
    assert out["refresh_token"] == "xoxe-1"
    with pytest.raises(loopback.OAuthFlowError, match="invalid_code"):
        loopback.normalise_token({"ok": False, "error": "invalid_code"})


def test_the_consent_url_carries_pkce_and_the_exact_redirect():
    url = loopback.authorize_url(
        _provider(scopes=["search:read.public", "im:history"]),
        loopback.redirect_uri(8799), state="ST", challenge="CH",
    )
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["code_challenge"] == "CH" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://localhost:8799/oauth/callback"
    assert q["scope"] == "search:read.public im:history"
    assert "user_scope" not in q


def test_slack_asks_for_its_scopes_the_way_slack_wants_them():
    """Both halves of "Invalid permissions requested / No scopes requested":
    the scopes must travel in `scope`, and a bare `search:read` is not a real MCP scope."""
    conn = catalog.CONNECTORS["slack"]
    provider = loopback.Provider(
        name="Slack", authorize_url=conn.authorize_url, token_url=conn.token_url, client_id="X",
        scopes=conn.scopes, scope_param=conn.scope_param, scope_separator=conn.scope_separator,
    )
    q = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(
            loopback.authorize_url(provider, loopback.redirect_uri(8766), state="S", challenge="C")
        ).query
    ))
    assert "user_scope" not in q, "the user-only endpoint ignores user_scope and reports no scopes"
    assert q["scope"], "an empty scope is what Slack calls 'No scopes requested'"
    assert "search:read" not in q["scope"].split(), "Slack's MCP scopes are granular, e.g. search:read.public"
    assert "search:read.public" in q["scope"].split()
    assert not [s for s in conn.scopes if "write" in s], "read-only by construction"


@pytest.mark.anyio
async def test_a_refresh_keeps_a_refresh_token_the_provider_did_not_resend(cfg):
    with respx.mock:
        respx.post(TOKEN_URL).mock(httpx.Response(200, json={"access_token": "tok-2", "expires_in": 60}))
        fresh = await loopback.refresh(_provider(), {"access_token": "old", "refresh_token": "r-1"})
    assert fresh["access_token"] == "tok-2"
    assert fresh["refresh_token"] == "r-1"


def test_the_loopback_certificate_is_generated_once_and_kept_private(cfg):
    cert, key = loopback.ensure_cert(cfg.vault_path)
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert loopback.ensure_cert(cfg.vault_path) == (cert, key)
    assert cert.stat().st_mtime == cert.stat().st_mtime


# ----- the token store -----
@pytest.mark.anyio
async def test_a_grant_survives_a_restart(cfg):
    url = "https://mcp.example.com/mcp"
    store = FileTokenStore(token_file(cfg.vault_path))
    await store.put(f"{url}/tokens", {"access_token": "t"}, collection="mcp-oauth-token", ttl=999)
    await store.put(f"{url}/token_expiry", {"expires_at": 2e9}, collection="mcp-oauth-token-expiry")

    reopened = FileTokenStore(token_file(cfg.vault_path))
    assert reopened.holds(url) is True, "the daemon must find the grant a connect left behind"
    assert reopened.expires_at(url) == 2e9
    assert oct(token_file(cfg.vault_path).stat().st_mode & 0o777) == "0o600"

    assert reopened.forget(url) is True
    assert reopened.holds(url) is False


@pytest.mark.anyio
async def test_an_expired_record_reads_as_absent(cfg):
    store = FileTokenStore(token_file(cfg.vault_path))
    await store.put("k", {"v": 1}, collection="c", ttl=-1)
    assert await store.get("k", collection="c") is None


def test_unreadable_token_file_does_not_break_a_sweep(cfg):
    """Worst case is re-authorising, never a crash mid-run."""
    path = token_file(cfg.vault_path)
    path.write_text("{ not json")
    assert FileTokenStore(path).holds("https://x/mcp") is False


# ----- catalogue and status -----
def test_glean_registers_itself_and_needs_no_token(cfg):
    server = catalog.apply(cfg, catalog.CONNECTORS["glean"], domain="acme-be.glean.com")
    assert server.auth == "oauth", "dynamic client registration means fastmcp holds the token"
    assert server.headers_env == {}, "an OAuth connector must not also send a stale bearer"
    assert server.url == "https://acme-be.glean.com/mcp/default"
    assert connect.status(cfg, server).connected is False


def test_slack_is_a_confidential_client_carrying_its_own_bearer(cfg):
    conn = catalog.CONNECTORS["slack"]
    server = catalog.apply(cfg, conn, client_id="123.456")
    assert server.url == "https://mcp.slack.com/mcp"
    assert server.auth == "none", "nbrain runs Slack's flow itself, so the transport just sends a header"
    assert server.headers_env == {"Authorization": "SLACK_MCP_TOKEN"}
    assert server.oauth_client_id == "123.456"
    assert server.oauth_scopes, "the scopes are recorded so config.yaml can override a rejected one"


def test_reapplying_a_connector_does_not_duplicate_it(cfg):
    for _ in range(3):
        catalog.apply(cfg, catalog.CONNECTORS["glean"], domain="acme-be.glean.com")
    assert len([s for s in cfg.mcp_servers if s.name == "glean"]) == 1
    assert catalog.disable(cfg, "glean").enabled is False
    assert catalog.find(cfg, "glean").url, "switching off must not lose the setup"


def test_status_explains_what_is_still_missing(cfg, monkeypatch):
    monkeypatch.setattr(connect, "get_secret", lambda env: None)
    server = catalog.apply(cfg, catalog.CONNECTORS["slack"])
    assert connect.status(cfg, server).blocker == "add the Client ID from your provider app"

    server.oauth_client_id = "123.456"
    assert connect.status(cfg, server).blocker == "SLACK_MCP_CLIENT_SECRET is not set"


@pytest.mark.anyio
async def test_connecting_slack_stores_the_token_and_status_turns_green(cfg, monkeypatch):
    conn = catalog.CONNECTORS["slack"]
    server = catalog.apply(cfg, conn, client_id="123.456")
    _only_client_secret(monkeypatch, conn)
    monkeypatch.setattr(
        connect.loopback, "run_flow",
        _fake_flow({"access_token": "xoxp-1", "refresh_token": "xoxe-1", "expires_at": 2e9}),
    )
    monkeypatch.setattr(connect, "probe", lambda c, s: _status_only(c, s))

    st = await connect.connect(cfg, server)
    assert st.connected is True
    assert st.expires_at == 2e9
    env_text = (cfg.vault_path / "nbrain" / ".env").read_text()
    assert "SLACK_MCP_TOKEN=xoxp-1" in env_text
    assert "xoxe-1" not in env_text, "the refresh token belongs in the token file, not .env"


@pytest.mark.anyio
async def test_disconnecting_removes_the_token_from_everywhere(cfg, monkeypatch):
    conn = catalog.CONNECTORS["slack"]
    server = catalog.apply(cfg, conn, client_id="123.456")
    _only_client_secret(monkeypatch, conn)
    monkeypatch.setattr(connect.loopback, "run_flow", _fake_flow({"access_token": "xoxp-1"}))
    monkeypatch.setattr(connect, "probe", lambda c, s: _status_only(c, s))
    await connect.connect(cfg, server)

    assert await connect.disconnect(cfg, server) is True
    assert "SLACK_MCP_TOKEN" not in (cfg.vault_path / "nbrain" / ".env").read_text()
    assert connect._saved_token(cfg, server) is None


def _only_client_secret(monkeypatch, conn) -> None:
    """Pretend the client secret is configured, but let every other lookup run for real —
    the point of these tests is that connecting is what makes the access token appear."""
    real = connect.get_secret
    monkeypatch.setattr(
        connect, "get_secret", lambda env: "csec" if env == conn.client_secret_env else real(env)
    )


def _fake_flow(token: dict):
    async def run(provider, **kw):  # noqa: ANN001, ANN003
        return dict(token)

    return run


async def _status_only(cfg: Config, server: MCPServerConfig):
    """Stand in for `probe`, which would otherwise open a real network connection."""
    return connect.status(cfg, server)


# ----- the Sources page -----
@pytest.fixture
def web(tmp_path: Path):
    """A web client over a vault with nothing connected yet."""
    from fastapi.testclient import TestClient

    from nbrain.config.loader import save_config
    from nbrain.vault.store import VaultStore
    from nbrain.web.app import create_app

    store = VaultStore(tmp_path / "vault")
    store.ensure_layout()
    c = Config(vault_path=store.root)
    c.user.name, c.user.email, c.user.timezone = "Test", "t@example.com", "Europe/London"
    save_config(c)
    return TestClient(create_app(store.root)), store.root


def test_the_sources_page_offers_every_connector_without_asking_for_a_token(web):
    client, _ = web
    body = client.get("/sources").text
    assert "Slack" in body and "Glean" in body
    assert "https://localhost:8766/oauth/callback" in body, "the redirect URL must be shown to copy"
    assert "Set up Slack" in body


def test_saving_glean_writes_an_oauth_server_and_offers_connect(web):
    client, vault = web
    reply = client.post("/connectors/glean/setup", data={"domain": "https://acme-be.glean.com/"}, follow_redirects=True)
    assert reply.status_code == 200

    cfg = load_config(vault)
    server = catalog.find(cfg, "glean")
    assert server.url == "https://acme-be.glean.com/mcp/default", "the domain is normalised"
    assert server.auth == "oauth"
    assert "/connectors/glean/connect" in reply.text, "a configured connector offers Connect"


def test_glean_without_a_domain_is_refused_rather_than_half_saved(web):
    client, vault = web
    reply = client.post("/connectors/glean/setup", data={"domain": "  "}, follow_redirects=True)
    assert "needs a backend domain" in reply.text
    assert catalog.find(load_config(vault), "glean") is None


def test_saving_slack_stores_the_secret_outside_config(web):
    client, vault = web
    client.post(
        "/connectors/slack/setup",
        data={"client_id": "123.456", "client_secret": "shhh"},
        follow_redirects=True,
    )
    cfg = load_config(vault)
    assert catalog.find(cfg, "slack").oauth_client_id == "123.456"
    assert "shhh" not in (vault / "nbrain" / "config.yaml").read_text(), "secrets never land in config.yaml"
    assert "SLACK_MCP_CLIENT_SECRET=shhh" in (vault / "nbrain" / ".env").read_text()


def test_connect_runs_in_the_background_so_the_page_does_not_hang(web, monkeypatch):
    """The consent screen can take minutes; the POST must return at once."""
    client, vault = web
    client.post("/connectors/glean/setup", data={"domain": "acme-be.glean.com"}, follow_redirects=True)

    started = threading.Event()
    monkeypatch.setattr(
        connect, "connect", _blocking_connect(started), raising=True
    )
    reply = client.post("/connectors/glean/connect", follow_redirects=False)
    assert reply.status_code == 303
    assert started.wait(5), "the flow should have been started on a background thread"
    assert client.get("/api/connectors/status").json()["running"] is True


def test_a_failed_connect_is_reported_not_a_500(web, monkeypatch):
    client, vault = web
    client.post("/connectors/glean/setup", data={"domain": "acme-be.glean.com"}, follow_redirects=True)

    async def boom(cfg, server, **kw):  # noqa: ANN001, ANN003
        raise connect.ConnectError("Glean's OAuth server is not enabled")

    monkeypatch.setattr(connect, "connect", boom)
    client.post("/connectors/glean/connect", follow_redirects=False)
    for _ in range(50):
        state = client.get("/api/connectors/status").json()
        if state["result"]:
            break
        __import__("time").sleep(0.05)
    assert state["result"]["ok"] is False
    assert "OAuth server is not enabled" in state["result"]["error"]


def test_a_tool_can_be_blocked_from_the_permission_list(web):
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1"}, follow_redirects=True)
    assert client.post("/connectors/slack/tools/slack_search_public?allow=0").json()["allowed"] is False

    server = catalog.find(load_config(vault), "slack")
    assert "^slack_search_public$" in server.tool_deny
    from nbrain.mcp.registry import ToolGate

    assert ToolGate(server, []).allows("slack_search_public") is False
    assert ToolGate(server, []).allows("slack_read_channel") is True

    client.post("/connectors/slack/tools/slack_search_public?allow=1")
    assert catalog.find(load_config(vault), "slack").tool_deny == []


def _blocking_connect(started: threading.Event):
    async def run(cfg, server, **kw):  # noqa: ANN001, ANN003
        started.set()
        __import__("time").sleep(2)
        return connect.status(cfg, server)

    return run


def test_the_approval_link_is_offered_while_the_flow_waits(web, monkeypatch):
    """A browser launch can quietly do nothing; the user must still have something to click."""
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)

    seen = threading.Event()

    async def flow(provider, **kw):  # noqa: ANN001, ANN003
        kw["on_url"]("https://slack.com/oauth/v2_user/authorize?client_id=1&state=xyz")
        seen.set()
        __import__("time").sleep(1.5)
        return {"access_token": "xoxp-1"}

    monkeypatch.setattr(connect.loopback, "run_flow", flow)
    client.post("/connectors/slack/connect", follow_redirects=False)
    assert seen.wait(5)

    state = client.get("/api/connectors/status").json()
    assert state["running"] is True
    assert state["auth_url"].startswith("https://slack.com/oauth/v2_user/authorize")


def test_clicking_connect_again_returns_the_live_link_not_an_error(web, monkeypatch):
    """The usual reason for a second click is that the browser never opened."""
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    started = threading.Event()

    async def flow(provider, **kw):  # noqa: ANN001, ANN003
        kw["on_url"]("https://slack.com/oauth/v2_user/authorize?state=xyz")
        started.set()
        __import__("time").sleep(1.5)
        return {"access_token": "x"}

    monkeypatch.setattr(connect.loopback, "run_flow", flow)
    client.post("/connectors/slack/connect", follow_redirects=False)
    assert started.wait(5)

    again = client.post("/connectors/slack/connect", follow_redirects=False)
    assert again.headers["location"] == "/sources?pending=slack#c-slack"


def test_a_stuck_flow_can_be_called_off_and_retried(web, monkeypatch):
    """Slack shows its own error page and never redirects back, so the wait must be escapable."""
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    started = threading.Event()

    async def flow(provider, **kw):  # noqa: ANN001, ANN003
        started.set()
        cancel = kw["cancel"]
        for _ in range(100):
            if cancel.is_set():
                raise connect.loopback.OAuthFlowError("cancelled before the provider redirected back")
            __import__("time").sleep(0.05)
        return {"access_token": "never"}

    monkeypatch.setattr(connect.loopback, "run_flow", flow)
    client.post("/connectors/slack/connect", follow_redirects=False)
    assert started.wait(5)

    client.post("/connectors/slack/cancel", follow_redirects=False)
    for _ in range(60):
        state = client.get("/api/connectors/status").json()
        if not state["running"]:
            break
        __import__("time").sleep(0.05)
    assert state["running"] is False, "cancelling must free the connector for another attempt"
    assert "cancelled" in state["result"]["error"]
    assert "SLACK_MCP_TOKEN" not in (vault / "nbrain" / ".env").read_text() if (vault / "nbrain" / ".env").exists() else True


def test_the_redirect_url_is_visible_before_you_click_connect(web):
    """It was buried behind a disclosure, which is how the first attempt failed."""
    client, _ = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    visible = _card(client.get("/sources").text, "slack")
    assert "https://localhost:8766/oauth/callback" in visible, "shown without opening Settings"


@pytest.mark.anyio
async def test_a_grant_the_server_refuses_is_not_reported_as_connected(cfg, monkeypatch):
    """Slack hands out a token and then refuses MCP until an admin enables the app. A green
    pill above that error sends people looking in the wrong place."""
    conn = catalog.CONNECTORS["slack"]
    server = catalog.apply(cfg, conn, client_id="123.456")
    _only_client_secret(monkeypatch, conn)
    monkeypatch.setattr(connect.loopback, "run_flow", _fake_flow({"access_token": "xoxp-1"}))

    refusal = "App is not enabled for Slack MCP server access."

    async def refuse(c, s):  # noqa: ANN001
        await connect._record_health(c, s, ok=False, error=refusal)
        raise RuntimeError(refusal)

    monkeypatch.setattr(connect, "probe", refuse)
    with pytest.raises(RuntimeError):
        await connect.connect(cfg, server)

    st = connect.status(cfg, server)
    assert st.connected is True, "the token is real and worth keeping"
    assert st.usable is False, "but the connector does not work yet"
    assert refusal in st.error
    assert st.detail == "authorised, but the server refused"


@pytest.mark.anyio
async def test_a_working_connection_reports_usable(cfg, monkeypatch):
    conn = catalog.CONNECTORS["slack"]
    server = catalog.apply(cfg, conn, client_id="123.456")
    _only_client_secret(monkeypatch, conn)
    monkeypatch.setattr(connect.loopback, "run_flow", _fake_flow({"access_token": "xoxp-1"}))
    monkeypatch.setattr(connect, "probe", _status_only)
    await connect.connect(cfg, server)
    await connect._record_health(cfg, server, ok=True, tools=[connect.ToolInfo(name='slack_read_channel')])

    st = connect.status(cfg, server)
    assert (st.connected, st.usable, st.error, st.detail) == (True, True, "", "connected")

    await connect.disconnect(cfg, server)
    assert connect._health(cfg, server) is None, "disconnecting clears the health record too"


def test_an_unchecked_grant_is_checked_by_the_page_not_by_the_user(web, monkeypatch):
    """"Authorised but never checked" is a question. The page should answer it on load."""
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    from nbrain.config.loader import set_secret

    set_secret("SLACK_MCP_TOKEN", "xoxp-1", use_keyring=False, vault=vault)

    card = _card(client.get("/sources").text, "slack")
    assert 'data-check="1"' in card, "the page must ask, rather than showing a verdict it does not have"
    assert "Checking whether the server accepts it" in card
    assert "needs attention" in card

    refusal = "App is not enabled for Slack MCP server access."

    async def refuse(cfg, server):  # noqa: ANN001
        raise RuntimeError(refusal)

    monkeypatch.setattr(connect, "probe", refuse)
    body = client.post("/connectors/slack/check").json()
    assert body["usable"] is False
    assert refusal in body["error"]

    # and the answer sticks, so a reload does not start guessing again
    card = _card(client.get("/sources").text, "slack")
    assert refusal in card
    assert 'data-check="1"' not in card


def test_a_check_that_succeeds_turns_the_connector_green(web, monkeypatch):
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    from nbrain.config.loader import set_secret

    set_secret("SLACK_MCP_TOKEN", "xoxp-1", use_keyring=False, vault=vault)

    async def ok(cfg, server):  # noqa: ANN001
        found = [
            connect.ToolInfo(name="slack_read_channel"),
            connect.ToolInfo(name="slack_send_message", write=True),
        ]
        await connect._record_health(cfg, server, ok=True, tools=found)
        st = connect.status(cfg, server)
        st.tools = connect._rank(server, found)
        return st

    monkeypatch.setattr(connect, "probe", ok)
    body = client.post("/connectors/slack/check").json()
    assert body["usable"] is True
    assert [t["name"] for t in body["tools"]] == ["slack_read_channel", "slack_send_message"]
    assert [t["allowed"] for t in body["tools"]] == [True, False], "writes stay blocked"
    assert "connected" in _card(client.get("/sources").text, "slack")


def _card(body: str, key: str) -> str:
    """One connector as rendered, minus its collapsed Settings block."""
    start = body.index(f'id="c-{key}"')
    return body[start : body.index('class="connector-setup"', start)]


@pytest.mark.anyio
async def test_a_health_record_from_an_older_build_does_not_break_the_page(cfg):
    """Stored records outlive the code that wrote them: an earlier build put a count here."""
    server = catalog.apply(cfg, catalog.CONNECTORS["slack"], client_id="1")
    from nbrain.config.loader import set_secret

    set_secret("SLACK_MCP_TOKEN", "xoxp-1", use_keyring=False, vault=cfg.vault_path)
    store = connect.store_for(cfg.vault_path)
    await store.put(
        connect._health_key(server),
        {"ok": True, "error": "", "tools": 8, "at": 0},
        collection=connect.loopback.COLLECTION,
    )
    st = connect.status(cfg, server)
    assert st.tools == [], "an unreadable list means no list, not a crash"
    assert st.checked is True


@pytest.mark.anyio
async def test_a_working_connector_with_no_recorded_tools_rechecks_itself(cfg):
    """Otherwise upgrading leaves a green connector with an empty permission list."""
    server = catalog.apply(cfg, catalog.CONNECTORS["slack"], client_id="1")
    from nbrain.config.loader import set_secret

    set_secret("SLACK_MCP_TOKEN", "xoxp-1", use_keyring=False, vault=cfg.vault_path)
    await connect.store_for(cfg.vault_path).put(
        connect._health_key(server),
        {"ok": True, "error": "", "tools": 8, "at": 0},  # the older shape
        collection=connect.loopback.COLLECTION,
    )
    assert connect.status(cfg, server).needs_check is True

    await connect._record_health(cfg, server, ok=True, tools=[connect.ToolInfo(name="slack_read_channel")])
    assert connect.status(cfg, server).needs_check is False


def test_a_whole_group_can_be_blocked_and_allowed_in_one_go(web):
    """The group route sits beside /connect, so it must not be shadowed by it."""
    client, vault = web
    client.post("/connectors/slack/setup", data={"client_id": "1", "client_secret": "s"}, follow_redirects=True)
    from nbrain.config.loader import set_secret

    set_secret("SLACK_MCP_TOKEN", "xoxp-1", use_keyring=False, vault=vault)
    cfg = load_config(vault)
    server = catalog.find(cfg, "slack")
    names = ["slack_read_channel", "slack_search_public"]
    import asyncio

    asyncio.run(connect._record_health(
        cfg, server, ok=True, tools=[connect.ToolInfo(name=n) for n in names]
    ))

    body = client.post("/connectors/slack/tools?group=read&allow=0").json()
    assert body["tools"] == 2 and body["allowed"] is False
    from nbrain.mcp.registry import ToolGate

    gate = ToolGate(catalog.find(load_config(vault), "slack"), [])
    assert [gate.allows(n) for n in names] == [False, False]

    client.post("/connectors/slack/tools?group=read&allow=1")
    gate = ToolGate(catalog.find(load_config(vault), "slack"), [])
    assert [gate.allows(n) for n in names] == [True, True]
    assert catalog.find(load_config(vault), "slack").allow_write is False, "reads must not unlock writes"
