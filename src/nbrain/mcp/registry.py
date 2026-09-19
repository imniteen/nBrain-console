"""Build MCP toolsets from config and gate them behind a write-tool denylist.

Any MCP server can be plugged in. Unless a server is explicitly marked `allow_write: true`,
every tool whose name looks like a write (create/send/update/delete/...) is filtered out before
the model ever sees it. That is the technical half of the read-only guarantee for MCP sources."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset, SSETransport, StdioTransport, StreamableHttpTransport
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from nbrain.config.loader import get_secret
from nbrain.config.schema import Config, MCPServerConfig

log = logging.getLogger(__name__)

WRITE_VERBS = {
    "create", "send", "post", "update", "delete", "remove", "write", "move", "archive", "reply",
    "add", "set", "edit", "patch", "put", "upload", "assign", "transition", "merge", "approve",
    "comment", "close", "reopen", "label", "mark", "trash", "forward", "publish", "schedule",
    "invite", "accept", "decline", "rsvp", "insert", "append", "modify", "rename", "share",
    "unshare", "star", "unstar", "react", "pin", "unpin", "execute", "run", "trigger", "deploy",
    "cancel", "revoke", "grant", "complete", "resolve", "dismiss", "snooze", "draft", "submit",
}
_SPLIT = re.compile(r"[_\-\.\s]+|(?<=[a-z0-9])(?=[A-Z])")


def _tokens(name: str) -> list[str]:
    return [t.lower() for t in _SPLIT.split(name) if t]


def is_write_tool(name: str) -> bool:
    """True when a tool name contains a write-like verb token."""
    toks = _tokens(name)
    return any(t in WRITE_VERBS for t in toks)


@dataclass
class ToolGate:
    server: MCPServerConfig
    denied: list[str]

    def allows(self, name: str) -> bool:
        if self.server.tool_allow and not any(re.search(p, name) for p in self.server.tool_allow):
            return False
        if any(re.search(p, name) for p in self.server.tool_deny):
            return False
        if not self.server.allow_write and is_write_tool(name):
            return False
        return True

    def filter(self, ctx: RunContext[Any], tool: ToolDefinition) -> bool:
        ok = self.allows(tool.name)
        if not ok and tool.name not in self.denied:
            self.denied.append(tool.name)
        return ok


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def build_transport(server: MCPServerConfig) -> Any:
    if server.auth == "oauth" and server.transport == "stdio":
        raise ValueError(f"MCP server {server.name}: oauth needs an http or sse transport, not stdio")
    if server.transport == "stdio":
        if not server.command:
            raise ValueError(f"MCP server {server.name}: stdio transport needs `command`")
        env = {**os.environ, **{k: _expand(v) for k, v in server.env.items()}}
        return StdioTransport(command=server.command, args=[_expand(a) for a in server.args], env=env, cwd=server.cwd)
    if not server.url:
        raise ValueError(f"MCP server {server.name}: {server.transport} transport needs `url`")
    headers = dict(server.headers)
    for header, env_name in server.headers_env.items():
        val = get_secret(env_name)
        if val:
            headers[header] = val if header.lower() != "authorization" or val.lower().startswith(("bearer ", "basic ")) else f"Bearer {val}"
        else:
            log.warning("MCP server %s: header %s references unset env %s", server.name, header, env_name)
    # oauth runs the server's OAuth 2.1 flow (dynamic client registration) in a browser.
    # The token is held in memory only, so every process start re-authenticates: fine when you
    # run a command yourself, useless for the scheduled daemon. Prefer a token in headers_env there.
    auth = "oauth" if server.auth == "oauth" else None
    if server.transport == "sse":
        return SSETransport(server.url, headers=headers or None, auth=auth)
    return StreamableHttpTransport(server.url, headers=headers or None, auth=auth)


class MCPRegistry:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.servers: dict[str, MCPServerConfig] = {s.name: s for s in cfg.enabled_mcp_servers()}
        self._gates: dict[str, ToolGate] = {}

    def names(self) -> list[str]:
        return list(self.servers)

    def gate(self, name: str) -> ToolGate:
        if name not in self._gates:
            self._gates[name] = ToolGate(self.servers[name], [])
        return self._gates[name]

    def raw_toolset(self, name: str) -> MCPToolset:
        server = self.servers[name]
        return MCPToolset(
            build_transport(server),
            tool_error_behavior="retry",
            max_retries=1,
            init_timeout=60,
            read_timeout=120,
        )

    def toolset(self, name: str) -> AbstractToolset[Any]:
        """Gated (denylist applied) and prefixed with the server name."""
        gate = self.gate(name)
        return self.raw_toolset(name).filtered(gate.filter).prefixed(name.replace("-", "_"))

    async def list_tools(self, name: str) -> list[dict[str, Any]]:
        """All tools the server exposes, with the gate decision for each."""
        gate = self.gate(name)
        ts = self.raw_toolset(name)
        async with ts:
            tools = await ts.list_tools()
        return [
            {"name": t.name, "allowed": gate.allows(t.name), "description": (t.description or "")[:160]}
            for t in tools
        ]

    async def call(self, name: str, tool: str, args: dict[str, Any]) -> Any:
        gate = self.gate(name)
        if not gate.allows(tool):
            raise PermissionError(f"tool {tool} on {name} is blocked by the read-only gate (allow_write is false)")
        ts = self.raw_toolset(name)
        async with ts:
            return await ts.direct_call_tool(tool, args)
