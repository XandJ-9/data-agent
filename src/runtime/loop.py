"""Reusable agent loop: bounded model/tool cycle over an injected provider.

The runtime knows nothing about settings, storage, providers or domains; the
model transport, tool registry, system prompt and budgets are all injected.
"""
import json
from collections.abc import Callable
from typing import Any

from runtime.context import build_context, fits
from runtime.types import ModelProvider, RuntimeCancelled, RuntimeConfig, RuntimeFailure
from tools.registry import ToolRegistry

MAX_CALLS_PER_ROUND = 8
MAX_REPLY_CHARS = 16000  # ~4096 tokens * 4 chars/token (conservative English upper bound)
TOOL_TRIM_FLOOR = 800  # minimum chars kept before replacing with placeholder
TOOL_TRIM_HEAD = 3200  # Phase 1 truncation target (TOOL_TRIM_FLOOR * 4)


def _tool_placeholder(content: str) -> str:
    """Build an informative placeholder for a tool result that must be dropped.

    Includes line count and a brief structural hint (JSON keys, first line)
    so the model retains some awareness of what was omitted.
    """
    lines = content.split('\n')
    hint = f"[工具结果因上下文预算被省略。原{len(lines)}行/约{len(content)}字符"
    if lines:
        first = lines[0].strip()
        if first:
            hint += f"，首行：{first[:80]}"
    # For JSON results, expose top-level keys as a structural hint.
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            keys = [k for k in data if not k.startswith('_')][:8]
            if keys:
                hint += f"，字段：{', '.join(keys)}"
    except (json.JSONDecodeError, TypeError):
        pass
    return hint + "]"


def _trim_tool_context(config: RuntimeConfig, context: list[dict], schemas: list[dict]) -> list[dict]:
    """Progressively shrink context until it fits the budget, newest-first.

    Phase 1 — truncate oversized tool results to TOOL_TRIM_HEAD chars.
    Phase 1.5 — further truncate to TOOL_TRIM_FLOOR chars.
    Phase 2 — replace still-overflowing tool results with structural placeholders.
    Phase 3 — drop the oldest complete assistant+tool round entirely.

    System, user and assistant skeleton messages are never mutated; only tool
    results are compressed and only when necessary.
    """
    # ── Phase 1 & 1.5 & 2: trim individual tool result contents ──
    for i in range(len(context) - 1, -1, -1):
        msg = context[i]
        if msg.get("role") != "tool":
            continue
        orig = msg["content"]
        if orig.startswith("[工具结果"):
            continue

        # Phase 1: truncate to head
        if len(orig) > TOOL_TRIM_HEAD:
            truncated = orig[:TOOL_TRIM_HEAD] + "\n\n[结果过长已截断]"
            context[i] = {**msg, "content": truncated}
            if fits(config, context, schemas):
                return context

        # Phase 1.5: truncate to floor
        if len(orig) > TOOL_TRIM_FLOOR:
            truncated = orig[:TOOL_TRIM_FLOOR] + "\n\n[结果已大幅截断]"
            if len(truncated) < len(orig):
                context[i] = {**msg, "content": truncated}
                if fits(config, context, schemas):
                    return context

        # Phase 2: structural placeholder
        placeholder = _tool_placeholder(orig)
        if len(placeholder) < len(orig):
            context[i] = {**msg, "content": placeholder}
            if fits(config, context, schemas):
                return context

    # ── Phase 3: drop the oldest complete assistant+tool round ──
    while True:
        dropped = False
        for i, msg in enumerate(context):
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            # Find the end of this round: assistant + its tool results.
            j = i + 1
            while j < len(context) and context[j].get("role") == "tool":
                j += 1
            context = [*context[:i], *context[j:]]
            dropped = True
            break
        if not dropped or fits(config, context, schemas):
            break

    return context


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

        def _on_text(delta: str) -> None:
            """Forward every text delta as a streaming chunk event."""
            if on_event is not None:
                on_event({'kind': 'text', 'content': delta})

        def _on_reasoning(delta: str) -> None:
            """Forward every reasoning delta as a streaming chunk event."""
            if on_event is not None:
                on_event({'kind': 'reasoning', 'content': delta})

        for _ in range(self.config.max_tool_rounds):
            if cancelled():
                raise RuntimeCancelled("执行已取消")
            if not fits(self.config, context, schemas):
                context = _trim_tool_context(self.config, context, schemas)
                if not fits(self.config, context, schemas):
                    raise RuntimeFailure("工具结果超出上下文预算")

            reply = self.provider.stream_complete(self.config, context, schemas,
                                                  on_text=_on_text, on_reasoning=_on_reasoning)
            if cancelled():
                raise RuntimeCancelled("执行已取消")
            calls = reply.tool_calls
            if not calls:
                if not isinstance(reply.content, str) or not reply.content.strip():
                    raise RuntimeFailure("模型未返回完整回复")
                if len(reply.content) > MAX_REPLY_CHARS:
                    raise RuntimeFailure("模型回复过长")
                return reply.content
            if len(calls) > MAX_CALLS_PER_ROUND:
                raise RuntimeFailure("单轮工具调用过多")
            if on_event is not None:
                on_event({'kind': 'assistant', 'content': reply.content or ''})
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
