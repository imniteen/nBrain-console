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
