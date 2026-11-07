"""OpenAI transport: client construction, request mapping, response normalization.

Owns the OpenAI SDK so the runtime core never imports it; exposes a
provider_from_settings factory that validates model configuration up front.
"""
import logging
from typing import Any

from openai import OpenAI

from runtime.types import ModelReply, RuntimeConfig, RuntimeFailure, ToolCall
from settings import Settings

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """ModelProvider over an OpenAI-compatible chat completions client."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None) -> ModelReply:
        kwargs: dict[str, Any] = {"model": config.model_name, "messages": messages,
                                  "max_tokens": config.max_tokens}
        if tools:
            kwargs["tools"] = tools
        try:
            t0 = __import__('time').time()
            result = self.client.chat.completions.create(**kwargs)
            elapsed = __import__('time').time() - t0
            logger.info('LLM %s %s messages %s tools -> %.1fs', config.model_name,
                       len(messages), len(tools) if tools else 0, elapsed)
            choice = result.choices[0]
            if getattr(choice, "finish_reason", None) in {"length", "content_filter"}:
                raise RuntimeFailure("模型未生成完整回复")
        except RuntimeFailure:
            raise
        except Exception as exc:
            logger.error('LLM request failed: %s', exc)
            raise RuntimeFailure("模型请求失败，请稍后重试") from None
        message = choice.message
        return ModelReply(content=message.content, tool_calls=tuple(
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments)
            for c in (message.tool_calls or [])),
            reasoning=getattr(message, 'reasoning_content', None))


def provider_from_settings(settings: Settings, client: Any = None) -> OpenAIProvider:
    """Validate configuration before any request; a client may be injected for tests."""
    if not settings.llm_api_key.get_secret_value() or not settings.llm_model:
        raise RuntimeFailure("模型尚未配置，请联系管理员")
    return OpenAIProvider(client or OpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        max_retries=0))
