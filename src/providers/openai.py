"""OpenAI transport: client construction, request mapping, response normalization.

Owns the OpenAI SDK so the runtime core never imports it; exposes a
provider_from_settings factory that validates model configuration up front.
"""
import logging
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from runtime.types import ModelReply, RuntimeConfig, RuntimeFailure, ToolCall
from settings import Settings

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """ModelProvider over an OpenAI-compatible chat completions client."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _usage_tokens(self, result: Any) -> tuple[int, int]:
        """Extract (prompt_tokens, completion_tokens) from the API response."""
        usage = getattr(result, 'usage', None)
        if usage is not None:
            return (getattr(usage, 'prompt_tokens', 0) or 0,
                    getattr(usage, 'completion_tokens', 0) or 0)
        return (0, 0)

    def complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None) -> ModelReply:
        kwargs: dict[str, Any] = {"model": config.model_name, "messages": messages,
                                  "max_tokens": config.max_tokens}
        if tools:
            kwargs["tools"] = tools
        try:
            t0 = __import__('time').time()
            result = self.client.chat.completions.create(**kwargs)
            elapsed = __import__('time').time() - t0
            prompt_tokens, completion_tokens = self._usage_tokens(result)
            logger.info('LLM %s %s messages %s tools -> %.1fs (prompt=%s, completion=%s tokens)',
                       config.model_name, len(messages), len(tools) if tools else 0,
                       elapsed, prompt_tokens, completion_tokens)
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
            reasoning=getattr(message, 'reasoning_content', None),
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    def stream_complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None,
                        on_text: Callable[[str], None],
                        on_reasoning: Callable[[str], None] | None = None) -> ModelReply:
        """Streaming completion: calls `on_text` for each text delta and
        `on_reasoning` for each reasoning delta. Returns the accumulated
        ModelReply with tool calls assembled from stream chunks."""
        kwargs: dict[str, Any] = {"model": config.model_name, "messages": messages,
                                  "max_tokens": config.max_tokens, "stream": True,
                                  "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
        collected_content: list[str] = []
        tool_call_buffers: dict[int, dict] = {}
        reasoning_buf: list[str] = []
        finish_reason: str | None = None
        prompt_tokens = 0
        completion_tokens = 0
        try:
            t0 = __import__('time').time()
            stream = self.client.chat.completions.create(**kwargs)
            for chunk in stream:
                # Usage may appear in the final chunk (OpenAI stream with include_usage)
                usage = getattr(chunk, 'usage', None)
                if usage is not None:
                    prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
                    completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason or finish_reason
                # Text delta
                if getattr(delta, 'content', None):
                    collected_content.append(delta.content)
                    on_text(delta.content)
                # Reasoning delta — forward in real-time
                rc = getattr(delta, 'reasoning_content', None)
                if rc:
                    reasoning_buf.append(rc)
                    if on_reasoning is not None:
                        on_reasoning(rc)
                # Tool call deltas (OpenAI streams them index-by-index)
                for tc in getattr(delta, 'tool_calls', None) or []:
                    idx = tc.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {'id': tc.id or '', 'name': '', 'arguments': ''}
                    buf = tool_call_buffers[idx]
                    if tc.id:
                        buf['id'] = tc.id
                    if getattr(tc.function, 'name', None):
                        buf['name'] += tc.function.name
                    if getattr(tc.function, 'arguments', None):
                        buf['arguments'] += tc.function.arguments
            elapsed = __import__('time').time() - t0
            logger.info('LLM(stream) %s %s messages %s tools -> %.1fs %s chars (prompt=%s, completion=%s tokens)',
                       config.model_name, len(messages), len(tools) if tools else 0,
                       elapsed, sum(len(c) for c in collected_content),
                       prompt_tokens, completion_tokens)
        except RuntimeFailure:
            raise
        except Exception as exc:
            logger.error('LLM stream failed: %s', exc)
            raise RuntimeFailure("模型请求失败，请稍后重试") from None
        if finish_reason in {"length", "content_filter"}:
            raise RuntimeFailure("模型未生成完整回复")
        content = ''.join(collected_content)
        reasoning = ''.join(reasoning_buf) if reasoning_buf else None
        tool_calls = tuple(
            ToolCall(id=buf['id'], name=buf['name'],
                     arguments=buf['arguments'])
            for buf in tool_call_buffers.values() if buf['id'] and buf['name']
        ) if tool_call_buffers else ()
        return ModelReply(content=content or None, tool_calls=tool_calls, reasoning=reasoning,
                          prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def provider_from_settings(settings: Settings, client: Any = None) -> OpenAIProvider:
    """Validate configuration before any request; a client may be injected for tests."""
    if not settings.llm_api_key.get_secret_value() or not settings.llm_model:
        raise RuntimeFailure("模型尚未配置，请联系管理员")
    return OpenAIProvider(client or OpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        max_retries=0))
