"""Reusable agent loop: bounded model/tool cycle over an injected provider.

The runtime knows nothing about settings, storage, providers or domains; the
model transport, tool registry, system prompt and budgets are all injected.
"""
from collections.abc import Callable
from typing import Any

from runtime.context import build_context, fits
from runtime.types import ModelProvider, RuntimeCancelled, RuntimeConfig, RuntimeFailure
from tools.registry import ToolRegistry

MAX_CALLS_PER_ROUND = 8
MAX_REPLY_CHARS = 64000


class Runtime:
    """Domain-neutral tool loop over any ModelProvider and ToolRegistry."""

    def __init__(self, *, config: RuntimeConfig, provider: ModelProvider, system_prompt: str) -> None:
        self.config = config
        self.provider = provider
        self.system_prompt = system_prompt

    def complete(self, messages: list[dict], *, tools: list[dict] | None = None) -> Any:
        """Single model completion through the injected provider (e.g. for
        domain policies such as memory extraction)."""
        return self.provider.complete(self.config, messages, tools)

    def respond(self, *, messages: list[dict], memories: list[dict], skills: list[dict],
                registry: ToolRegistry, cancelled: Callable[[], bool],
                on_event: Callable[[dict], None] | None = None) -> str:
        schemas = registry.schemas()
        context = build_context(self.config, self.system_prompt, messages, memories, skills, schemas)
        for _ in range(self.config.max_tool_rounds):
            if cancelled():
                raise RuntimeCancelled("执行已取消")
            if not fits(self.config, context, schemas):
                raise RuntimeFailure("工具结果超出上下文预算")
            reply = self.complete(context, tools=schemas)
            if cancelled():
                raise RuntimeCancelled("执行已取消")
            calls = reply.tool_calls
            if not calls:
                if not isinstance(reply.content, str) or not reply.content.strip():
                    raise RuntimeFailure("模型未返回完整回复")
                if len(reply.content) > MAX_REPLY_CHARS:
                    raise RuntimeFailure("模型回复过长")
                if on_event is not None and reply.reasoning:
                    # Final-round thinking is part of the trace; the visible text
                    # already lands as the assistant message, so only reasoning is sent.
                    on_event({'kind': 'assistant', 'content': '', 'reasoning': reply.reasoning})
                return reply.content
            if len(calls) > MAX_CALLS_PER_ROUND:
                raise RuntimeFailure("单轮工具调用过多")
            if on_event is not None:
                on_event({'kind': 'assistant', 'content': reply.content or '',
                          'reasoning': reply.reasoning})
            context = [*context, {"role": "assistant", "content": reply.content,
                                  "tool_calls": [{"id": c.id, "type": "function", "function": {
                                      "name": c.name, "arguments": c.arguments}} for c in calls]}]
            for call in calls:
                if cancelled():
                    raise RuntimeCancelled("执行已取消")
                if on_event is not None:
                    on_event({'kind': 'tool_call', 'name': call.name, 'content': call.arguments})
                result = registry.dispatch(call.name, call.arguments)
                if on_event is not None:
                    on_event({'kind': 'tool_result', 'name': call.name, 'content': result})
                context = [*context, {"role": "tool", "tool_call_id": call.id, "content": result}]
        raise RuntimeFailure("执行达到工具调用轮数上限")
