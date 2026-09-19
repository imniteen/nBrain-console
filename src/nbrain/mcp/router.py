"""Map roles (email, calendar, chat, tickets, code, notes, knowledge) to the servers and native
sources that cover them, and report gaps honestly."""

from __future__ import annotations

from dataclasses import dataclass, field

from nbrain.config.schema import Config, MCPServerConfig

ROLES = ("email", "calendar", "chat", "tickets", "code", "notes", "knowledge", "other")


@dataclass
class Coverage:
    native: dict[str, list[str]] = field(default_factory=dict)  # role -> source names
    mcp: dict[str, list[str]] = field(default_factory=dict)  # role -> server names

    def sources_for(self, role: str) -> list[str]:
        return self.native.get(role, []) + [f"mcp:{n}" for n in self.mcp.get(role, [])]

    def gaps(self) -> list[str]:
        missing = [r for r in ("email", "calendar", "chat", "tickets", "code", "notes") if not self.sources_for(r)]
        if not missing:
            return []
        if len(missing) == 6:
            return ["No sources are connected, so this brief sees nothing."]
        return ["Not connected: " + ", ".join(missing) + "."]


class MCPRouter:
    def __init__(self, cfg: Config, native_roles: dict[str, set[str]]):
        """native_roles: source name -> roles it covers."""
        self.cfg = cfg
        self.native_roles = native_roles

    def coverage(self) -> Coverage:
        cov = Coverage()
        for name, roles in self.native_roles.items():
            for r in roles:
                cov.native.setdefault(r, []).append(name)
        for s in self.cfg.enabled_mcp_servers():
            for r in s.roles:
                cov.mcp.setdefault(r, []).append(s.name)
        return cov

    def servers_for_role(self, role: str) -> list[MCPServerConfig]:
        return [s for s in self.cfg.enabled_mcp_servers() if role in s.roles]

    def server_for_item_source(self, source: str) -> MCPServerConfig | None:
        if not source.startswith("mcp:"):
            return None
        name = source[4:]
        return next((s for s in self.cfg.enabled_mcp_servers() if s.name == name), None)

    def knowledge_servers(self) -> list[MCPServerConfig]:
        return self.servers_for_role("knowledge")
