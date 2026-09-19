"""Turn an LLMConfig into a pydantic-ai Model. One place knows about providers."""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from nbrain.config.loader import get_secret
from nbrain.config.schema import LLMConfig, OutputMode, Provider

log = logging.getLogger(__name__)


class LLMConfigError(RuntimeError):
    pass


def _settings(cfg: LLMConfig) -> dict[str, Any]:
    s: dict[str, Any] = {"max_tokens": cfg.max_tokens, "timeout": cfg.timeout_seconds}
    if cfg.temperature is not None:
        s["temperature"] = cfg.temperature
    return s


def _anthropic(cfg: LLMConfig) -> Model:
    from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
    from pydantic_ai.providers.anthropic import AnthropicProvider

    key = get_secret(cfg.default_api_key_env())
    # No key is fine: the official SDK resolves ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
    provider = AnthropicProvider(api_key=key, base_url=cfg.base_url) if (key or cfg.base_url) else "anthropic"
    settings: dict[str, Any] = _settings(cfg)
    if cfg.anthropic_fallbacks and any(t in cfg.model for t in ("opus-5", "fable-5", "mythos-5")):
        # Server-side refusal fallbacks: route a safety refusal to a fallback model instead of failing.
        settings["anthropic_betas"] = ["server-side-fallback-2026-07-01"]
        settings["extra_body"] = {"fallbacks": "default"}
    return AnthropicModel(cfg.model, provider=provider, settings=AnthropicModelSettings(**settings))


def _openai_like(cfg: LLMConfig) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
    from pydantic_ai.providers.openai import OpenAIProvider

    key = get_secret(cfg.default_api_key_env())
    if cfg.provider == Provider.openai:
        if not key:
            raise LLMConfigError("OpenAI needs an API key: set OPENAI_API_KEY (or llm.*.api_key_env)")
        provider: Any = OpenAIProvider(api_key=key, base_url=cfg.base_url)
    elif cfg.provider == Provider.openrouter:
        from pydantic_ai.providers.openrouter import OpenRouterProvider

        if not key:
            raise LLMConfigError("OpenRouter needs an API key: set OPENROUTER_API_KEY")
        provider = OpenRouterProvider(api_key=key, app_title="nbrain")
    elif cfg.provider == Provider.ollama:
        from pydantic_ai.providers.ollama import OllamaProvider

        provider = OllamaProvider(base_url=cfg.base_url or "http://localhost:11434/v1", api_key=key)
    else:  # openai_compatible: LM Studio, vLLM, llama.cpp server, LiteLLM proxy...
        if not cfg.base_url:
            raise LLMConfigError("openai_compatible needs base_url (e.g. http://localhost:1234/v1)")
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=key or "not-needed")
    return OpenAIChatModel(cfg.model, provider=provider, settings=OpenAIChatModelSettings(**_settings(cfg)))


def _bedrock(cfg: LLMConfig) -> Model:
    from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
    from pydantic_ai.providers.bedrock import BedrockProvider

    provider = BedrockProvider(
        profile_name=cfg.profile,
        region_name=cfg.region,
        aws_read_timeout=cfg.timeout_seconds,
        aws_connect_timeout=30,
    )
    return BedrockConverseModel(cfg.model, provider=provider, settings=BedrockModelSettings(**_settings(cfg)))


def build_model(cfg: LLMConfig) -> Model:
    if cfg.provider == Provider.anthropic:
        return _anthropic(cfg)
    if cfg.provider == Provider.bedrock:
        return _bedrock(cfg)
    return _openai_like(cfg)


def output_spec(cfg: LLMConfig, type_: Any, *, name: str | None = None) -> Any:
    """Pick the structured-output strategy for this model."""
    if cfg.output_mode == OutputMode.prompted:
        return PromptedOutput(type_, name=name)
    if cfg.output_mode == OutputMode.native:
        return NativeOutput(type_, name=name)
    return ToolOutput(type_, name=name)


def describe_model(cfg: LLMConfig) -> str:
    bits = [f"{cfg.provider.value}:{cfg.model}", f"output={cfg.output_mode.value}"]
    if cfg.base_url:
        bits.append(cfg.base_url)
    if cfg.provider == Provider.bedrock:
        bits.append(f"region={cfg.region or 'default'} profile={cfg.profile or 'default'}")
    return " ".join(bits)


def model_settings_for(cfg: LLMConfig) -> ModelSettings:
    return ModelSettings(**_settings(cfg))  # type: ignore[typeddict-item]
