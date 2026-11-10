"""Reusable runtime core contracts: injection-based, no Settings, storage or network."""

import json
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from runtime import (
    ModelReply,
    Runtime,
    RuntimeCancelled,
    RuntimeConfig,
    RuntimeFailure,
    ToolCall,
)
from tools.builtin import register_statistics
from tools.registry import ToolDefinition, ToolRegistry


class FakeProvider:
    """ModelProvider double: records calls, replays scripted ModelReply objects."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None) -> ModelReply:
        self.calls.append({"config": config, "messages": [dict(m) for m in messages], "tools": tools})
        return self.replies.pop(0)

    def stream_complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None,
                        on_text: Callable[[str], None],
                        on_reasoning: Callable[[str], None] | None = None) -> ModelReply:
        self.calls.append({"config": config, "messages": [dict(m) for m in messages], "tools": tools})
        reply = self.replies.pop(0)
        if reply.reasoning and on_reasoning is not None:
            for char in reply.reasoning:
                on_reasoning(char)
        if reply.content:
            for char in reply.content:
                on_text(char)
        return reply


def reply(content: str | None = None, *calls: ToolCall) -> ModelReply:
    return ModelReply(content=content, tool_calls=tuple(calls))


def echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="echo", description="Echo the given text.",
        parameters={"text": {"type": "string"}}, required=["text"],
        handler=lambda args: {"echo": args["text"]}))
    return registry


def stats_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_statistics(registry)
    return registry


def make_runtime(provider: FakeProvider, *, system_prompt: str = "BASE PROMPT", **overrides: object) -> Runtime:
    config = RuntimeConfig(model_name="core-model", **{
        "max_tokens": 256, "context_tokens": 8000, **overrides})  # type: ignore[arg-type]
    return Runtime(config=config, provider=provider, system_prompt=system_prompt)


def respond(runtime: Runtime, registry: ToolRegistry, **overrides: object) -> str:
    return runtime.respond(**{
        "messages": [{"role": "user", "content": "question"}],
        "memories": [],
        "skills": [],
        "registry": registry,
        "cancelled": lambda: False,
        **overrides,
    })


def test_system_prompt_and_config_are_injected_per_runtime() -> None:
    first = FakeProvider(reply("a"))
    second = FakeProvider(reply("b"))
    respond(make_runtime(first, system_prompt="PROMPT ONE"), echo_registry())
    respond(make_runtime(second, system_prompt="PROMPT TWO", max_tokens=99), echo_registry())
    assert first.calls[0]["messages"][0] == {"role": "system", "content": "PROMPT ONE"}
    assert second.calls[0]["messages"][0] == {"role": "system", "content": "PROMPT TWO"}
    assert first.calls[0]["config"].model_name == "core-model"
    assert second.calls[0]["config"].max_tokens == 99


def test_injected_registry_schemas_and_dispatch_flow() -> None:
    provider = FakeProvider(reply(None, ToolCall(id="c1", name="echo", arguments='{"text": "hi"}')), reply("final"))
    registry = echo_registry()
    assert respond(make_runtime(provider), registry) == "final"
    sent_tools = provider.calls[0]["tools"]
    assert [t["function"]["name"] for t in sent_tools] == ["echo"]
    assistant, tool_message = provider.calls[1]["messages"][-2:]
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"
    assert tool_message == {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"echo": "hi"})}


def test_same_core_with_a_different_generic_toolset() -> None:
    provider = FakeProvider(
        reply(None, ToolCall(id="s1", name="calculate_statistics", arguments='{"values": [1, 3]}')),
        reply("stats done"),
    )
    assert respond(make_runtime(provider), stats_registry()) == "stats done"
    tool_message = provider.calls[1]["messages"][-1]
    assert json.loads(tool_message["content"]) == {"count": 2, "sum": 4, "mean": 2, "min": 1, "max": 3}


def test_memory_and_skill_reference_stay_out_of_the_system_prompt() -> None:
    provider = FakeProvider(reply("ok"))
    respond(make_runtime(provider), echo_registry(),
            memories=[{"text": "prefers CNY"}],
            skills=[{"name": "sales", "description": "Sales skill"}])
    sent = provider.calls[0]["messages"]
    assert sent[0]["role"] == "system" and "prefers CNY" not in sent[0]["content"]
    reference = sent[1]
    assert reference["role"] == "user"
    assert "prefers CNY" in reference["content"] and "Sales skill" in reference["content"]


def test_tool_round_budget_bounds_execution() -> None:
    replies = [reply(None, ToolCall(id=f"c{i}", name="echo", arguments='{"text": "x"}')) for i in range(5)]
    provider = FakeProvider(*replies)
    with pytest.raises(RuntimeFailure):
        respond(make_runtime(provider, max_tool_rounds=2), echo_registry())
    assert provider.calls[-1]["messages"][-1]["role"] == "tool"
    assert len(provider.calls) == 2


def test_cancellation_stops_before_completion_and_dispatch() -> None:
    provider = FakeProvider(reply(None, ToolCall(id="c1", name="echo", arguments='{"text": "x"}')))
    handler = Mock(side_effect=AssertionError("must not run"))
    registry = echo_registry()
    registry.register(ToolDefinition(name="echo", description="d", parameters={},
                                     required=[], handler=handler))
    with pytest.raises(RuntimeCancelled):
        respond(make_runtime(provider), registry, cancelled=lambda: True)
    assert provider.calls == []


def test_empty_or_oversized_final_reply_is_rejected() -> None:
    provider = FakeProvider(reply("   "))
    with pytest.raises(RuntimeFailure):
        respond(make_runtime(provider), echo_registry())
    big = FakeProvider(reply("x" * 64001))
    with pytest.raises(RuntimeFailure):
        respond(make_runtime(big), echo_registry())


def test_context_budget_trims_history_but_keeps_current_question() -> None:
    provider = FakeProvider(reply("done"))
    latest = {"role": "user", "content": "current question"}
    history = [{"role": "user", "content": "old " * 9000}, {"role": "assistant", "content": "answer " * 9000}]
    assert respond(make_runtime(provider), echo_registry(), messages=[*history, latest]) == "done"
    sent = provider.calls[0]["messages"]
    assert latest in sent
    assert "old " * 9000 not in json.dumps(sent, ensure_ascii=False)


def test_on_event_observes_assistant_tool_calls_and_results() -> None:
    provider = FakeProvider(
        ModelReply(content='先检索', reasoning='需要历史', tool_calls=(
            ToolCall(id="c1", name="echo", arguments='{"text": "hi"}'),)),
        reply("final"))
    events: list[dict] = []
    assert respond(make_runtime(provider), echo_registry(), on_event=events.append) == "final"
    assert events == [
        {'kind': 'reasoning', 'content': '需'},
        {'kind': 'reasoning', 'content': '要'},
        {'kind': 'reasoning', 'content': '历'},
        {'kind': 'reasoning', 'content': '史'},
        {'kind': 'text', 'content': '先'},
        {'kind': 'text', 'content': '检'},
        {'kind': 'text', 'content': '索'},
        {'kind': 'assistant', 'content': '先检索'},
        {'kind': 'tool_call', 'name': 'echo', 'content': '{"text": "hi"}'},
        {'kind': 'tool_result', 'name': 'echo', 'content': json.dumps({"echo": "hi"})},
        {'kind': 'text', 'content': 'f'},
        {'kind': 'text', 'content': 'i'},
        {'kind': 'text', 'content': 'n'},
        {'kind': 'text', 'content': 'a'},
        {'kind': 'text', 'content': 'l'},
    ]


def test_final_round_reasoning_is_emitted_without_duplicating_content() -> None:
    provider = FakeProvider(ModelReply(content='done', reasoning='deduced the answer'))
    events: list[dict] = []
    assert respond(make_runtime(provider), echo_registry(), on_event=events.append) == "done"
    assert events == [
        {'kind': 'reasoning', 'content': 'd'},
        {'kind': 'reasoning', 'content': 'e'},
        {'kind': 'reasoning', 'content': 'd'},
        {'kind': 'reasoning', 'content': 'u'},
        {'kind': 'reasoning', 'content': 'c'},
        {'kind': 'reasoning', 'content': 'e'},
        {'kind': 'reasoning', 'content': 'd'},
        {'kind': 'reasoning', 'content': ' '},
        {'kind': 'reasoning', 'content': 't'},
        {'kind': 'reasoning', 'content': 'h'},
        {'kind': 'reasoning', 'content': 'e'},
        {'kind': 'reasoning', 'content': ' '},
        {'kind': 'reasoning', 'content': 'a'},
        {'kind': 'reasoning', 'content': 'n'},
        {'kind': 'reasoning', 'content': 's'},
        {'kind': 'reasoning', 'content': 'w'},
        {'kind': 'reasoning', 'content': 'e'},
        {'kind': 'reasoning', 'content': 'r'},
        {'kind': 'text', 'content': 'd'},
        {'kind': 'text', 'content': 'o'},
        {'kind': 'text', 'content': 'n'},
        {'kind': 'text', 'content': 'e'},
    ]


def test_no_events_without_observer() -> None:
    provider = FakeProvider(reply(None, ToolCall(id="c1", name="echo", arguments='{"text": "hi"}')), reply("final"))
    assert respond(make_runtime(provider), echo_registry()) == "final"  # default path stays silent


def test_complete_is_a_single_provider_round_trip() -> None:
    provider = FakeProvider(reply("plain"))
    runtime = make_runtime(provider)
    result = runtime.complete([{"role": "user", "content": "hi"}])
    assert result.content == "plain" and result.tool_calls == ()
    assert provider.calls[0]["tools"] is None
    assert provider.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_oversized_tool_result_is_trimmed_instead_of_rejected() -> None:
    """When a large tool result causes context overflow, the runtime trims
    it rather than raising RuntimeFailure immediately."""
    # Use a tight context budget so the oversized tool result overflows.
    provider = FakeProvider(
        reply(None, ToolCall(id="b1", name="big_echo", arguments='{"text": "x"}')),
        reply("recovered after trim"),
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="big_echo", description="Returns a huge payload.",
        parameters={"text": {"type": "string"}}, required=["text"],
        handler=lambda _: {"data": "y" * 8000},
    ))
    # 8K chars ≈ 8K+ bytes > the 8000-byte context_tokens default.
    result = respond(make_runtime(provider, context_tokens=4000), registry)
    assert result == "recovered after trim"
    # The second call's context should contain the trimmed tool result
    # (either Phase 1.5 truncation or Phase 2 placeholder, both carry a marker).
    tool_msgs = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs
    trimmed = tool_msgs[0]["content"]
    assert len(trimmed) < 8200  # definitely shorter than the 8000+ char original
    assert "截断" in trimmed or "省略" in trimmed or "工具结果" in trimmed


def test_phase_three_drops_oldest_round_when_placeholders_not_enough() -> None:
    """When every tool result is too small to trim but the cumulative
    assistant+tool rounds overflow the budget, phase 3 drops the oldest
    complete round."""
    provider = FakeProvider(
        reply(None, ToolCall(id="r1", name="echo", arguments='{"text": "a"}')),
        reply(None, ToolCall(id="r2", name="echo", arguments='{"text": "b"}')),
        reply("done after round drop"),
    )
    registry = echo_registry()
    # Large system prompt + two rounds overflows; echo results are tiny so
    # phases 1–2 cannot help.  Phase 3 drops round 1, leaving exactly one
    # assistant round for the final completion.
    result = respond(
        make_runtime(provider, system_prompt="X" * 3000, context_tokens=4000),
        registry,
    )
    assert result == "done after round drop"
    messages_after_trim = provider.calls[2]["messages"]
    assistant_count = sum(
        1 for m in messages_after_trim
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert assistant_count == 1
    assert any(m.get("role") == "system" for m in messages_after_trim)
    assert any(m.get("content") == "question" for m in messages_after_trim)
