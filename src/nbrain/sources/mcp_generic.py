"""A source backed by any MCP server: a bounded tool-using agent collects Signals for the roles
the server is configured for. Bounded = capped tool calls, gated tools, structured output."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits

from nbrain.config.schema import Config, MCPServerConfig
from nbrain.llm.factory import build_model, output_spec
from nbrain.llm.tasks import LLMTasks, usage_of
from nbrain.mcp.registry import MCPRegistry
from nbrain.sources.base import (
    BaseSource,
    CollectResult,
    Signal,
    SourceStatus,
    SourceText,
    VerifyResult,
    Window,
)
from nbrain.vault.schema import Item, ItemType

log = logging.getLogger(__name__)

ROLE_BRIEFS = {
    "email": "Find inbound threads addressed to the user that await their reply beyond the threshold, and threads where they asked and got no answer.",
    "calendar": "List today's and tomorrow's meetings; flag meetings the user organises that have no agenda.",
    "chat": "Find the user's own recent messages that contain commitments ('I will', 'I'll send', 'by Friday'), and direct questions to the user with no reply from them.",
    "tickets": "Find tickets assigned to the user that are past due or untouched beyond the stale threshold, and tickets they reported that are stuck with someone else.",
    "code": "Find merge/pull requests awaiting the user's review, the user's own open MRs older than the review threshold, and failing pipelines on them.",
    "notes": "Find recent meeting-notes documents; return their 'next steps' text so commitments can be extracted.",
    "knowledge": "Do not collect; this server is for on-demand search only.",
    "other": "Find anything that looks like work waiting on the user or a commitment they made.",
}


class MCPSignal(BaseModel):
    type: Literal["commitment", "waiting-on-me", "waiting-on-them", "slipping", "meeting-action", "watch"]
    title: str
    source_id: str = Field(description="a stable identifier from the server (id, key, URL) usable to re-check this later")
    url: str | None = None
    person: str | None = None
    due: date | None = None
    observed_at: datetime | None = None
    cause: str | None = None
    evidence: str | None = None
    priority: int = Field(ge=1, le=3, default=2)
    confidence: float = Field(ge=0, le=1, default=0.8)


class MCPText(BaseModel):
    source_id: str
    title: str = ""
    url: str | None = None
    author: str | None = None
    author_is_me: bool = False
    observed_at: datetime | None = None
    text: str = Field(description="verbatim text, up to ~4000 characters")


class MCPCollectOutput(BaseModel):
    signals: list[MCPSignal] = Field(default_factory=list)
    texts: list[MCPText] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="coverage caveats: what could not be checked and why")


class MCPVerifyOutput(BaseModel):
    state: Literal["open", "resolved", "unknown"]
    note: str | None = None


class MCPSource(BaseSource):
    def __init__(self, cfg: Config, server: MCPServerConfig, registry: MCPRegistry, tasks: LLMTasks):
        self.cfg = cfg
        self.server = server
        self.registry = registry
        self.tasks = tasks
        self.name = f"mcp:{server.name}"
        self.roles = set(server.roles)
        self.verifiable = True

    def _instructions(self, window: Window, mode: str) -> str:
        u = self.cfg.user
        role_lines = "\n".join(f"- {r}: {ROLE_BRIEFS.get(r, ROLE_BRIEFS['other'])}" for r in self.server.roles)
        return (
            self.tasks.rules()
            + f"\n\nYou are collecting from the MCP server '{self.server.name}' for {u.name} ({u.email}).\n"
            f"Today is {window.today.isoformat()} ({window.tz}). Look back {window.commitment_lookback_days} days for "
            f"commitments and {window.email_lookback_days} days for threads. Reply threshold {window.awaiting_reply_hours}h, "
            f"ticket stale after {window.ticket_stale_days} days, review stale after {window.review_age_days} days.\n"
            f"Roles to cover:\n{role_lines}\n"
            + (f"Extra guidance from the user: {self.server.instructions}\n" if self.server.instructions else "")
            + f"You may make at most {self.server.max_tool_calls} tool calls; prefer list/search tools with filters over "
            "reading items one by one. Only read-capable tools are available; never attempt to change anything. "
            "Every signal needs a stable source_id and, where the server gives one, a URL. If you cannot cover a role, "
            "say so in notes rather than guessing."
            + (" Return the answer as structured output when done." if mode == "collect" else "")
        )

    async def healthcheck(self) -> SourceStatus:
        try:
            tools = await self.registry.list_tools(self.server.name)
        except Exception as e:
            return SourceStatus(self.name, False, f"cannot connect: {e}")
        allowed = [t["name"] for t in tools if t["allowed"]]
        denied = [t["name"] for t in tools if not t["allowed"]]
        detail = f"{len(allowed)} tools usable, {len(denied)} blocked by the read-only gate"
        return SourceStatus(self.name, bool(allowed), detail, write_capable=bool(denied) and self.server.allow_write)

    async def collect(self, window: Window) -> CollectResult:
        if self.roles <= {"knowledge"}:
            return CollectResult(notes=[f"{self.name}: knowledge-only server, not swept"])
        lcfg = self.cfg.llm.for_task("mcp")
        model = build_model(lcfg)
        toolset = self.registry.toolset(self.server.name)
        agent: Agent[None, MCPCollectOutput] = Agent(
            model,
            output_type=output_spec(lcfg, MCPCollectOutput, name="collect_result"),
            instructions=self._instructions(window, "collect"),
            toolsets=[toolset],
            retries=1,
        )
        limits = UsageLimits(request_limit=self.server.max_tool_calls + 4, tool_calls_limit=self.server.max_tool_calls)
        async with agent:
            result = await agent.run("Collect now.", usage_limits=limits)
        self.tasks.usage.add(usage_of(result), f"{lcfg.provider.value}:{lcfg.model}")
        out = result.output
        res = CollectResult()
        for s in out.signals:
            res.signals.append(
                Signal(
                    type=ItemType(s.type),
                    title=s.title[:120],
                    source=self.name,
                    source_id=s.source_id,
                    url=s.url,
                    person=s.person,
                    due=s.due,
                    observed_at=s.observed_at,
                    cause=s.cause,
                    evidence=s.evidence,
                    priority=s.priority,
                    confidence=s.confidence,
                )
            )
        for t in out.texts:
            res.texts.append(
                SourceText(
                    source=self.name,
                    source_id=t.source_id,
                    url=t.url,
                    kind="other",
                    title=t.title,
                    author=t.author,
                    author_is_me=t.author_is_me,
                    observed_at=t.observed_at,
                    text=t.text[:6000],
                )
            )
        res.notes += [f"{self.name}: {n}" for n in out.notes]
        gate = self.registry.gate(self.server.name)
        if gate.denied:
            res.findings[self.name] = f"blocked write tools: {', '.join(sorted(gate.denied)[:8])}"
        return res

    async def verify(self, item: Item) -> VerifyResult:
        lcfg = self.cfg.llm.for_task("mcp")
        model = build_model(lcfg)
        toolset = self.registry.toolset(self.server.name)
        window = Window(
            today=date.today(),
            email_lookback_days=7,
            commitment_lookback_days=30,
            awaiting_reply_hours=48,
            ticket_stale_days=5,
            review_age_days=3,
            tz=self.cfg.user.timezone,
        )
        agent: Agent[None, MCPVerifyOutput] = Agent(
            model,
            output_type=output_spec(lcfg, MCPVerifyOutput, name="verify_result"),
            instructions=self._instructions(window, "verify")
            + "\nYou are verifying ONE previously found item. Use at most 3 tool calls. "
            "'resolved' only if the source clearly shows it done/closed/replied; 'unknown' if you cannot tell.",
            toolsets=[toolset],
            retries=1,
        )
        prompt = f"Item: {item.title}\nsource_id: {item.source_id}\nurl: {item.url or 'n/a'}\ntype: {item.type.value}\nIs it still open?"
        try:
            async with agent:
                result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=6, tool_calls_limit=3))
        except Exception as e:
            log.warning("%s verify failed for %s: %s", self.name, item.id, e)
            return VerifyResult(state="unknown", note=str(e)[:120])
        self.tasks.usage.add(usage_of(result), f"{lcfg.provider.value}:{lcfg.model}")
        return VerifyResult(state=result.output.state, note=result.output.note)


def build_mcp_sources(cfg: Config, registry: MCPRegistry, tasks: LLMTasks) -> list[MCPSource]:
    return [MCPSource(cfg, s, registry, tasks) for s in cfg.enabled_mcp_servers()]


def native_role_map(sources: list[Any]) -> dict[str, set[str]]:
    return {s.name: set(s.roles) for s in sources if not str(s.name).startswith("mcp:")}
