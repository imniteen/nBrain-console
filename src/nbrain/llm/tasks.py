"""Bounded LLM calls. Each has a pydantic output type, no tools, and wraps source content in
<data> blocks. Usage is accumulated for the sweep log."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from nbrain.config.schema import Config, LLMConfig, RoleTrack
from nbrain.llm.factory import build_model, output_spec
from nbrain.sources.base import SourceText

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


# ---------- output models ----------


class ExtractedCommitment(BaseModel):
    text_id: str = Field(description="the `id` of the text this came from")
    type: Literal["commitment", "meeting-action", "waiting-on-me", "waiting-on-them"]
    title: str = Field(description="short imperative title, under 90 characters")
    owner_is_me: bool
    owner_name: str | None = None
    promised_to: str | None = None
    due: date | None = None
    promised_on: date | None = None
    evidence: str = Field(description="exact quoted wording, one or two lines")
    confidence: float = Field(ge=0, le=1)
    project_hint: str | None = None


class ExtractionResult(BaseModel):
    items: list[ExtractedCommitment] = Field(default_factory=list)


class ThreadClass(BaseModel):
    thread_id: str
    category: Literal[
        "needs_my_reply", "waiting_on_them", "automation_signal", "fyi", "noise", "phishing_shaped"
    ]
    urgency: int = Field(ge=1, le=3, default=2)
    ask: str | None = None
    summary: str | None = None


class ClassificationResult(BaseModel):
    threads: list[ThreadClass] = Field(default_factory=list)


class Action(BaseModel):
    text: str
    effort_hint: str = ""
    item_id: str | None = None


class BriefDraft(BaseModel):
    one_thing: str
    assessment: str
    actions: list[Action] = Field(default_factory=list)
    meeting_notes: list[str] = Field(default_factory=list)
    tomorrow_note: list[str] = Field(default_factory=list)
    retractions: list[str] = Field(default_factory=list)


class ItemAssist(BaseModel):
    """What the drawer shows when the user opens one item."""

    situation: str = Field(description="at most 2 sentences on what this is")
    changed: str = Field(default="", description="one line if the source moved since the brief")
    draft: str = Field(default="", description="a ready-to-paste reply, or empty")
    suggested_comment: str = Field(default="", description="a ticket/MR comment, or empty")
    next_step: str = Field(default="", description="one imperative line")
    open_questions: list[str] = Field(default_factory=list)
    risk: str = Field(default="")


class WeeklyDraft(BaseModel):
    narrative: str
    slipped: list[str] = Field(default_factory=list)
    delivery_comment: str = ""
    process_improvement: str = ""
    patterns: list[str] = Field(default_factory=list)


# ---------- usage ----------


@dataclass
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)

    def add(self, usage: Any, model_name: str) -> None:
        self.calls += int(getattr(usage, "requests", 1) or 1)
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.models.add(model_name)


# ---------- tasks ----------


def usage_of(result: Any) -> Any:
    """pydantic-ai exposes `usage` as an attribute in 2.x and a method in 1.x."""
    u = getattr(result, "usage", None)
    return u() if callable(u) else u


def _fill(template: str, **kw: str) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def _data_block(obj: Any) -> str:
    return "<data>\n" + json.dumps(obj, default=str, ensure_ascii=False, indent=1) + "\n</data>"


ROLE_GUIDANCE = {
    RoleTrack.ic: "Lead with the user's own delivery: due items, reviews awaiting them, stale tickets.",
    RoleTrack.lead: "Rank 'you are the bottleneck' items (others waiting on your review/decision) above the user's own delivery.",
    RoleTrack.manager: "Decisions waiting on the user first, then team blockers framed around the work (never per-person metrics), then commitments, own tickets last. Hiring as stage counts only.",
}


class LLMTasks:
    def __init__(self, cfg: Config, *, today: date):
        self.cfg = cfg
        self.today = today
        self.usage = UsageTotals()
        self._models: dict[str, Model] = {}

    # -- plumbing --

    def _model(self, task: Literal["default", "extract", "write", "mcp"]) -> tuple[Model, LLMConfig]:
        lcfg = self.cfg.llm.for_task(task)
        key = f"{task}:{lcfg.provider}:{lcfg.model}"
        if key not in self._models:
            self._models[key] = build_model(lcfg)
        return self._models[key], lcfg

    def rules(self) -> str:
        u = self.cfg.user
        collision = (
            f"COLLISION WARNING: {', '.join(u.name_collisions)} are DIFFERENT PEOPLE from {u.first_name}; never merge them."
            if u.name_collisions
            else ""
        )
        return _fill(
            (PROMPTS / "rules.md").read_text(),
            USER_NAME=u.name or "the user",
            USER_EMAIL=u.email,
            USER_FIRST_NAME=u.first_name or "the user",
            NAME_VARIANTS=", ".join(u.name_variants) or "none recorded",
            COLLISION_WARNING=collision,
        )

    async def _run(self, task: Literal["default", "extract", "write", "mcp"], instructions: str, prompt: str, out_type: type[BaseModel], *, name: str) -> Any:
        model, lcfg = self._model(task)
        agent: Agent[None, Any] = Agent(
            model,
            output_type=output_spec(lcfg, out_type, name=name),
            instructions=self.rules() + "\n\n" + instructions,
            retries=2,
        )
        result = await agent.run(prompt)
        self.usage.add(usage_of(result), f"{lcfg.provider.value}:{lcfg.model}")
        return result.output

    # -- tasks --

    async def extract_commitments(self, texts: list[SourceText], *, batch_size: int = 12) -> list[ExtractedCommitment]:
        if not texts:
            return []
        u = self.cfg.user
        instructions = _fill(
            (PROMPTS / "extract.md").read_text(),
            USER_FIRST_NAME=u.first_name or "the user",
            TODAY=self.today.isoformat(),
        )
        out: list[ExtractedCommitment] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            payload = [
                {
                    "id": t.source_id,
                    "kind": t.kind,
                    "title": t.title,
                    "author": t.author,
                    "author_is_me": t.author_is_me,
                    "participants": t.participants,
                    "observed_at": t.observed_at.isoformat() if t.observed_at else None,
                    "meeting": t.meeting,
                    "text": t.text[:6000],
                }
                for t in batch
            ]
            try:
                res: ExtractionResult = await self._run(
                    "extract", instructions, _data_block(payload), ExtractionResult, name="extraction"
                )
            except Exception as e:  # a failed batch must not kill the sweep
                log.warning("extract_commitments batch %d failed: %s", i // batch_size, e)
                continue
            valid_ids = {t.source_id for t in batch}
            out += [c for c in res.items if c.text_id in valid_ids]
        return out

    async def classify_threads(self, threads: list[dict[str, Any]], *, batch_size: int = 25) -> list[ThreadClass]:
        if not threads:
            return []
        u = self.cfg.user
        instructions = _fill(
            (PROMPTS / "classify.md").read_text(),
            USER_FIRST_NAME=u.first_name or "the user",
            USER_EMAIL=u.email,
        )
        out: list[ThreadClass] = []
        for i in range(0, len(threads), batch_size):
            batch = threads[i : i + batch_size]
            try:
                res: ClassificationResult = await self._run(
                    "extract", instructions, _data_block(batch), ClassificationResult, name="classification"
                )
            except Exception as e:
                log.warning("classify_threads batch %d failed: %s", i // batch_size, e)
                continue
            ids = {str(t.get("id")) for t in batch}
            out += [c for c in res.threads if c.thread_id in ids]
        return out

    async def write_brief(self, brief_input: dict[str, Any]) -> BriefDraft:
        u = self.cfg.user
        instructions = _fill(
            (PROMPTS / "brief.md").read_text(),
            USER_FIRST_NAME=u.first_name or "the user",
            IMBALANCE=f"{int(self.cfg.sweep.priority_imbalance_ratio * 100)}/{int(100 - self.cfg.sweep.priority_imbalance_ratio * 100)}",
            ROLE_TRACK=self.cfg.role_track.value,
            ROLE_GUIDANCE=ROLE_GUIDANCE[self.cfg.role_track],
        )
        return await self._run("write", instructions, _data_block(brief_input), BriefDraft, name="brief")

    async def write_weekly(self, weekly_input: dict[str, Any]) -> WeeklyDraft:
        u = self.cfg.user
        instructions = _fill((PROMPTS / "weekly.md").read_text(), USER_FIRST_NAME=u.first_name or "the user")
        return await self._run("write", instructions, _data_block(weekly_input), WeeklyDraft, name="weekly")

    async def assist(self, item_payload: dict[str, Any], context_payload: dict[str, Any]) -> ItemAssist:
        """Draft help for one item from its LIVE context. Never writes anything anywhere."""
        u = self.cfg.user
        instructions = _fill(
            (PROMPTS / "assist.md").read_text(),
            USER_FIRST_NAME=u.first_name or "the user",
        )
        prompt = (
            "ITEM (as recorded in the ledger):\n"
            + _data_block(item_payload)
            + "\n\nLIVE CONTEXT (re-read from the source just now):\n"
            + _data_block(context_payload)
        )
        return await self._run("write", instructions, prompt, ItemAssist, name="item_assist")

    async def explain(self, question: str, data: dict[str, Any]) -> str:
        model, lcfg = self._model("write")
        agent: Agent[None, str] = Agent(model, instructions=self.rules() + "\nAnswer in under 150 words, plain prose.")
        result = await agent.run(question + "\n\n" + _data_block(data))
        self.usage.add(usage_of(result), f"{lcfg.provider.value}:{lcfg.model}")
        return result.output

    async def ping(self, task: Literal["default", "extract", "write", "mcp"] = "default") -> str:
        class Pong(BaseModel):
            ok: bool
            model_says: str

        res: Pong = await self._run(task, "Reply with ok=true and a three-word greeting.", "ping", Pong, name="pong")
        return res.model_says
