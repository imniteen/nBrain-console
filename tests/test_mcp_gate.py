from nbrain.config.schema import MCPServerConfig
from nbrain.mcp.registry import ToolGate, is_write_tool


def test_write_verbs_detected():
    for name in ("create_issue", "sendMessage", "update-page", "jira_transition_issue", "gmail_send_email", "deleteFile", "postComment"):
        assert is_write_tool(name), name
    for name in ("list_issues", "search", "get_page", "read_thread", "fetch_calendar_events", "query_data_source"):
        assert not is_write_tool(name), name


def test_gate_respects_allow_write_and_patterns():
    gate = ToolGate(MCPServerConfig(name="x", tool_deny=["^admin_"]), [])
    assert gate.allows("list_issues")
    assert not gate.allows("create_issue")
    assert not gate.allows("admin_list")
    open_gate = ToolGate(MCPServerConfig(name="y", allow_write=True), [])
    assert open_gate.allows("create_issue")
    narrow = ToolGate(MCPServerConfig(name="z", tool_allow=["^search"]), [])
    assert narrow.allows("search_pages")
    assert not narrow.allows("list_pages")


def _server(**kw):
    from nbrain.config.schema import MCPServerConfig

    base = dict(name="glean", transport="http", url="https://be.glean.com/mcp/default", roles=["knowledge"])
    return MCPServerConfig(**{**base, **kw})


def test_bearer_token_header_is_built_for_a_remote_server(monkeypatch):
    """Glean and friends authenticate with `Authorization: Bearer <token>`; the token never
    goes in config.yaml, only the name of the variable holding it."""
    from nbrain.mcp.registry import build_transport

    monkeypatch.setenv("GLEAN_TOKEN", "glean-abc123")
    t = build_transport(_server(headers_env={"Authorization": "GLEAN_TOKEN"}))
    assert t.headers["Authorization"] == "Bearer glean-abc123"
    assert t.auth is None

    # an already-prefixed value is not double-prefixed
    monkeypatch.setenv("GLEAN_TOKEN", "Bearer glean-abc123")
    t = build_transport(_server(headers_env={"Authorization": "GLEAN_TOKEN"}))
    assert t.headers["Authorization"] == "Bearer glean-abc123"


def test_missing_token_does_not_invent_a_header(monkeypatch):
    from nbrain.mcp.registry import build_transport

    monkeypatch.delenv("GLEAN_TOKEN", raising=False)
    t = build_transport(_server(headers_env={"Authorization": "GLEAN_TOKEN"}))
    assert not (t.headers or {}).get("Authorization")


def test_oauth_is_attached_to_the_transport_and_refused_on_stdio():
    import pytest

    from nbrain.mcp.registry import build_transport

    t = build_transport(_server(auth="oauth"))
    assert t.auth is not None, "oauth must reach the transport, not be silently dropped"

    with pytest.raises(ValueError, match="oauth needs an http or sse transport"):
        build_transport(_server(transport="stdio", command="x", url=None, auth="oauth"))


def test_glean_read_tools_pass_the_gate_but_writes_do_not():
    """Glean exposes search/chat/read; its agent tools are often named run_* and are blocked
    by the write denylist, so they need an explicit tool_allow."""
    from nbrain.mcp.registry import ToolGate, is_write_tool

    for name in ("search", "chat", "read_document", "code_search", "people", "list_agents"):
        assert not is_write_tool(name), name
    for name in ("run_agent", "create_artifact", "send_message"):
        assert is_write_tool(name), name

    gate = ToolGate(_server(tool_allow=["^run_agent_profiler$"]), [])
    assert gate.allows("run_agent_profiler") is False, "tool_allow does not override the write gate"
    opened = ToolGate(_server(tool_allow=["^run_agent_profiler$"], allow_write=True), [])
    assert opened.allows("run_agent_profiler") is True
    assert opened.allows("run_agent_other") is False, "allow-list still narrows it"


def test_a_vault_makes_the_oauth_token_outlive_the_process(tmp_path):
    """Without persistent storage every sweep would reopen a browser, so the daemon could never run."""
    from nbrain.mcp.registry import build_oauth
    from nbrain.mcp.tokens import FileTokenStore, token_file

    oauth = build_oauth(_server(auth="oauth"), tmp_path)
    store = oauth.context.storage._key_value_store
    assert isinstance(store, FileTokenStore)
    assert store.path == token_file(tmp_path)
