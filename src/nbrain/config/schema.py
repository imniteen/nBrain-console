"""Configuration schema. Lives in <vault>/nbrain/config.yaml; secrets stay in env / Keychain."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator


def _as_time(v: object) -> object:
    """YAML 1.1 reads a bare `8:30` as 510 (sexagesimal minutes). Convert it back so a
    hand-edited config.yaml cannot silently turn a time into a number."""
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v // 60:02d}:{v % 60:02d}"
    if isinstance(v, str) and ":" in v:
        h, _, m = v.partition(":")
        if h.strip().isdigit() and m.strip()[:2].isdigit():
            return f"{int(h):02d}:{int(m.strip()[:2]):02d}"
    return v


TimeStr = Annotated[str, BeforeValidator(_as_time)]


class Provider(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    openrouter = "openrouter"
    ollama = "ollama"
    openai_compatible = "openai_compatible"
    bedrock = "bedrock"


class OutputMode(StrEnum):
    tool = "tool"  # register the schema as a tool (most reliable on frontier models)
    native = "native"  # provider JSON-schema response format
    prompted = "prompted"  # schema in the prompt; the only option for models without tool calling


class RoleTrack(StrEnum):
    ic = "ic"
    lead = "lead"
    manager = "manager"


class LLMConfig(BaseModel):
    provider: Provider = Provider.anthropic
    model: str = "claude-opus-5"
    api_key_env: str | None = None
    base_url: str | None = None
    # Bedrock
    region: str | None = None
    profile: str | None = None
    refresh_command: str | None = None
    refresh_when_expiring_within_minutes: int = 30
    # generation
    output_mode: OutputMode = OutputMode.tool
    max_tokens: int = 16000
    temperature: float | None = None
    timeout_seconds: float = 300.0
    # anthropic-only: enable server-side refusal fallbacks on Opus 5 / Fable 5.1
    anthropic_fallbacks: bool = True

    def default_api_key_env(self) -> str | None:
        if self.api_key_env:
            return self.api_key_env
        return {
            Provider.anthropic: "ANTHROPIC_API_KEY",
            Provider.openai: "OPENAI_API_KEY",
            Provider.openrouter: "OPENROUTER_API_KEY",
            Provider.ollama: None,
            Provider.openai_compatible: "NBRAIN_LLM_API_KEY",
            Provider.bedrock: None,
        }[self.provider]


class LLMSection(BaseModel):
    default: LLMConfig = Field(default_factory=LLMConfig)
    extract: LLMConfig | None = None
    write: LLMConfig | None = None
    mcp: LLMConfig | None = None

    def for_task(self, task: Literal["default", "extract", "write", "mcp"]) -> LLMConfig:
        return getattr(self, task) or self.default


class WorkingHours(BaseModel):
    start: TimeStr = "09:00"
    end: TimeStr = "18:00"


class UserConfig(BaseModel):
    name: str = ""
    first_name: str = ""
    email: str = ""
    timezone: str = "UTC"
    working_hours: WorkingHours = Field(default_factory=WorkingHours)
    name_variants: list[str] = Field(default_factory=list)
    name_collisions: list[str] = Field(default_factory=list)
    manager: str | None = None

    @field_validator("first_name", mode="before")
    @classmethod
    def _default_first(cls, v: str | None, info) -> str:  # type: ignore[no-untyped-def]
        if v:
            return v
        name = info.data.get("name") or ""
        return name.split(" ")[0] if name else ""


class GoogleSourceConfig(BaseModel):
    enabled: bool = False
    credentials_file: str = "credentials.json"  # relative to <vault>/nbrain/
    token_file: str = "google-token.json"
    gmail: bool = True
    calendar: bool = True
    chat: bool = True
    drive_notes: bool = True
    notes_query: str = "(name contains 'Notes by Gemini' or name contains 'Meeting notes')"
    max_threads: int = 60
    max_chat_messages: int = 80
    max_notes_docs: int = 6


class GitLabSourceConfig(BaseModel):
    enabled: bool = False
    url: str = "https://gitlab.com"
    token_env: str = "GITLAB_TOKEN"
    projects: list[str] = Field(default_factory=list)  # path_with_namespace; empty = all visible
    username: str | None = None


class SlackSourceConfig(BaseModel):
    enabled: bool = False
    token_env: str = "SLACK_TOKEN"
    user_id: str | None = None
    channels: list[str] = Field(default_factory=list)  # channel ids to scan; empty = DMs + member channels
    max_messages: int = 200


class JiraSourceConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    email: str = ""
    token_env: str = "JIRA_TOKEN"
    jql_assigned: str = "assignee = currentUser() AND statusCategory != Done ORDER BY updated ASC"
    jql_reported: str = "reporter = currentUser() AND statusCategory != Done ORDER BY updated ASC"
    max_issues: int = 50


class SourcesConfig(BaseModel):
    google: GoogleSourceConfig = Field(default_factory=GoogleSourceConfig)
    gitlab: GitLabSourceConfig = Field(default_factory=GitLabSourceConfig)
    slack: SlackSourceConfig = Field(default_factory=SlackSourceConfig)
    jira: JiraSourceConfig = Field(default_factory=JiraSourceConfig)


Role = Literal["email", "calendar", "chat", "tickets", "code", "notes", "knowledge", "other"]


class MCPServerConfig(BaseModel):
    name: str
    enabled: bool = True
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    headers_env: dict[str, str] = Field(default_factory=dict)  # header name -> env var holding value
    roles: list[Role] = Field(default_factory=lambda: ["other"])
    tool_allow: list[str] = Field(default_factory=list)  # regexes; empty = all
    tool_deny: list[str] = Field(default_factory=list)  # regexes, in addition to the write denylist
    allow_write: bool = False
    max_tool_calls: int = 12
    instructions: str = ""  # extra guidance for the bounded collector, e.g. which project to look at


class FileDelivery(BaseModel):
    enabled: bool = True
    html: bool = True


class GmailDraftDelivery(BaseModel):
    enabled: bool = False
    subject: str = "[nbrain] Daily — {date}"


class GmailSendDelivery(BaseModel):
    enabled: bool = False
    subject: str = "[nbrain] Daily — {date}"


class SlackDMDelivery(BaseModel):
    enabled: bool = False
    channel: str | None = None  # DM channel id or user id; None = self


class DeliveryConfig(BaseModel):
    file: FileDelivery = Field(default_factory=FileDelivery)
    gmail_draft: GmailDraftDelivery = Field(default_factory=GmailDraftDelivery)
    gmail_send: GmailSendDelivery = Field(default_factory=GmailSendDelivery)
    slack_dm: SlackDMDelivery = Field(default_factory=SlackDMDelivery)


class WeeklyConfig(BaseModel):
    enabled: bool = True
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "fri"
    time: TimeStr = "16:00"


Extra = Literal["priority_balance", "delivery_score", "changed_since_yesterday", "tomorrow_preview"]


class SweepConfig(BaseModel):
    daily_time: TimeStr = "08:15"
    weekdays_only: bool = True
    weekly: WeeklyConfig = Field(default_factory=WeeklyConfig)
    email_lookback_days: int = 7
    commitment_lookback_days: int = 30
    awaiting_reply_hours: int = 48
    ticket_stale_days: int = 5
    review_age_days: int = 3
    urgent_max_lines: int = 6
    brief_word_cap: int = 550
    unconfirmed_drop_after_runs: int = 3
    priority_imbalance_ratio: float = 0.70
    per_source_timeout_seconds: float = 120.0
    extras: list[Extra] = Field(
        default_factory=lambda: [
            "priority_balance",
            "delivery_score",
            "changed_since_yesterday",
            "tomorrow_preview",
        ]
    )
    missed_run_grace_hours: int = 6


class NoiseConfig(BaseModel):
    mine: list[str] = Field(default_factory=list)  # automated senders that carry state
    suppress: list[str] = Field(default_factory=list)  # never surface
    phishing: list[str] = Field(default_factory=list)


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


class Config(BaseModel):
    version: int = 1
    vault_path: Path = Field(default=Path("~/nbrain-vault"))
    user: UserConfig = Field(default_factory=UserConfig)
    role_track: RoleTrack = RoleTrack.ic
    llm: LLMSection = Field(default_factory=LLMSection)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    mcp_config_file: str | None = None  # optional Claude-Desktop-style mcpServers JSON
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    sweep: SweepConfig = Field(default_factory=SweepConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    @property
    def vault(self) -> Path:
        return self.vault_path.expanduser().resolve()

    @property
    def nbrain_dir(self) -> Path:
        return self.vault / "nbrain"

    def enabled_mcp_servers(self) -> list[MCPServerConfig]:
        return [s for s in self.mcp_servers if s.enabled]
