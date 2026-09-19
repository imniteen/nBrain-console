"""Exercise the LLM task plumbing with pydantic-ai's offline models: no network, real Agent code."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from nbrain.config.schema import Config, OutputMode
from nbrain.llm import tasks as tasks_mod
from nbrain.llm.tasks import BriefDraft, LLMTasks
from nbrain.sources.base import BaseSource, CollectResult, SourceStatus, SourceText, Window
from nbrain.sweep import pipeline
from nbrain.vault.schema import ItemStatus, ItemType, Person
from nbrain.vault.store import VaultStore


def _extraction_model():
    """A FunctionModel that returns one commitment via the output tool, whatever the prompt."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = next(t for t in info.output_tools)
        payload = {
            "items": [
                {
                    "text_id": "spaces/A/messages/1",
                    "type": "commitment",
                    "title": "Send the dashboard numbers to Ada",
                    "owner_is_me": True,
                    "owner_name": "Test User",
                    "promised_to": "Ada Lovelace",
                    "due": "2026-09-18",
                    "promised_on": "2026-09-16",
                    "evidence": "I'll send you the dashboard numbers tomorrow.",
                    "confidence": 0.95,
                    "project_hint": "dashboard",
                },
                {
                    "text_id": "spaces/A/messages/1",
                    "type": "commitment",
                    "title": "Ada will review the spec",
                    "owner_is_me": False,
                    "owner_name": "Ada Lovelace",
                    "evidence": "I'll review the spec.",
                    "confidence": 0.9,
                },
                {
                    "text_id": "not-in-batch",
                    "type": "commitment",
                    "title": "Hallucinated",
                    "owner_is_me": True,
                    "evidence": "x",
                    "confidence": 1.0,
                },
            ]
        }
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=json.dumps(payload))])

    return FunctionModel(respond)


async def test_extract_filters_by_batch_and_owner(cfg: Config, today, monkeypatch):
    monkeypatch.setattr(tasks_mod, "build_model", lambda lcfg: _extraction_model())
    t = LLMTasks(cfg, today=today)
    texts = [
        SourceText(source="google-chat", source_id="spaces/A/messages/1", kind="chat", author="Test User", author_is_me=True,
                   observed_at=datetime.now(UTC) - timedelta(days=1), text="I'll send you the dashboard numbers tomorrow.")
    ]
    found = await t.extract_commitments(texts)
    assert [c.title for c in found] == ["Send the dashboard numbers to Ada", "Ada will review the spec"]  # hallucinated id dropped
    assert t.usage.calls >= 1


def _prompted_brief_model():
    """Prompted mode: the model answers with plain JSON text, as a local model without tools would."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.output_tools  # prompted mode registers no output tool
        payload = {
            "one_thing": "Send Ada the dashboard numbers before the 10:00 sync.",
            "assessment": "One commitment is due today. Nothing else moved.",
            "actions": [{"text": "Send the numbers", "effort_hint": "ten minutes"}],
            "meeting_notes": [],
            "tomorrow_note": [],
            "retractions": [],
        }
        return ModelResponse(parts=[TextPart(json.dumps(payload))])

    return FunctionModel(respond)


async def test_prompted_output_mode_parses_json_text(cfg: Config, today, monkeypatch):
    cfg.llm.default.output_mode = OutputMode.prompted
    monkeypatch.setattr(tasks_mod, "build_model", lambda lcfg: _prompted_brief_model())
    t = LLMTasks(cfg, today=today)
    draft = await t.write_brief({"today": today.isoformat(), "due_or_overdue": []})
    assert isinstance(draft, BriefDraft)
    assert draft.actions[0].effort_hint == "ten minutes"


class ChatOnly(BaseSource):
    name = "google-chat"
    roles = {"chat"}
    verifiable = True

    async def healthcheck(self) -> SourceStatus:
        return SourceStatus(self.name, True)

    async def collect(self, window: Window) -> CollectResult:
        return CollectResult(texts=[
            SourceText(source="google-chat", source_id="spaces/A/messages/1", kind="chat", author="Test User", author_is_me=True,
                       observed_at=datetime.now(UTC) - timedelta(days=1), text="I'll send you the dashboard numbers tomorrow.",
                       url="https://chat.google.com/room/A/1")
        ])


async def test_pipeline_with_model_extracts_and_writes(vault: VaultStore, cfg: Config, today, monkeypatch):
    vault.save(Person(title="Ada Lovelace", email="ada@example.com", tier=1))
    from nbrain.vault.schema import Project

    vault.save(Project(title="Dashboard", keywords=["dashboard"]))

    def fake_build(lcfg):
        return _extraction_model()

    # extraction uses the FunctionModel; brief writing uses TestModel (schema-valid random output)
    calls = {"n": 0}

    def build(lcfg):
        calls["n"] += 1
        return _extraction_model() if calls["n"] == 1 else TestModel()

    monkeypatch.setattr(tasks_mod, "build_model", build)
    monkeypatch.setattr(pipeline, "build_native_sources", lambda c, s: [ChatOnly()])
    monkeypatch.setattr(pipeline.Sweep, "_check_bedrock", lambda self: None)
    rep = await pipeline.Sweep(cfg, vault, today=today, deliver=False).run()
    assert rep.created == 1  # only my own commitment became an item
    item = vault.items()[0]
    assert item.type == ItemType.commitment and item.status == ItemStatus.open
    assert item.promised_to == "[[Ada Lovelace]]" and item.project == "[[Dashboard]]"
    assert item.due.isoformat() == "2026-09-18" and item.priority == 1  # tier-1 bump
    assert rep.draft is not None and rep.llm_calls >= 2
    assert "Send the dashboard numbers" in rep.markdown


@pytest.mark.parametrize("mode", ["tool", "native", "prompted"])
def test_output_spec_modes(cfg: Config, mode):
    from nbrain.llm.factory import output_spec

    cfg.llm.default.output_mode = OutputMode(mode)
    spec = output_spec(cfg.llm.default, BriefDraft, name="brief")
    assert type(spec).__name__.lower().startswith(mode)


def test_text_part_import_smoke():
    assert TextPart("x").content == "x"
