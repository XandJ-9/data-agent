"""Behavior contracts for the independently implemented agent runtime."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from data_agent.runtime import AgentRuntime, RuntimeCancelled, RuntimeFailure
from data_agent.settings import Settings


def response(content: str | None = None, calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=calls or [],
                    role="assistant",
                ),
                finish_reason="tool_calls" if calls else "stop",
            )
        ]
    )


def tool(name: str, arguments: Any, call_id: str = "call-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        ),
    )


def client_for(*responses: Any) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=Mock(side_effect=responses),
            )
        )
    )


def settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "llm_api_key": "dummy-test-key",
            "llm_model": "test-model",
            **overrides,
        },
    )


def respond(runtime: AgentRuntime, **overrides: Any) -> str:
    return runtime.respond(
        **{
            "messages": [{"role": "user", "content": "Analyze my data"}],
            "memories": [],
            "skills": [],
            "tool_handler": Mock(return_value={}),
            "cancelled": lambda: False,
            **overrides,
        }
    )


def tool_results(client: SimpleNamespace) -> list[dict]:
    return [
        message for message in client.chat.completions.create.call_args.kwargs["messages"] if message["role"] == "tool"
    ]


def test_respond_preserves_input_and_includes_current_user_and_memory() -> None:
    client = client_for(response("分析完成"))
    messages = [{"role": "user", "content": "比较收入：北京与上海"}]
    memories = [{"text": "Use CNY for revenue", "source_message_ids": ["old-1"]}]
    original = deepcopy((messages, memories))
    assert respond(AgentRuntime(settings(), client=client), messages=messages, memories=memories) == "分析完成"
    sent = client.chat.completions.create.call_args.kwargs
    assert sent["model"] == "test-model"
    assert messages[0] in sent["messages"]
    assert "Use CNY for revenue" in json.dumps(sent["messages"])
    assert (messages, memories) == original


def test_search_history_delegates_arguments_and_returns_correlated_tool_result() -> None:
    client = client_for(response(calls=[tool("search_history", {"query": "收入"})]), response("Found it"))
    handler = Mock(return_value={"matches": ["historical revenue"]})
    assert respond(AgentRuntime(settings(), client=client), tool_handler=handler) == "Found it"
    handler.assert_called_once_with("search_history", {"query": "收入"})
    (result,) = tool_results(client)
    assert result["tool_call_id"] == "call-1"
    assert json.loads(result["content"]) == {"matches": ["historical revenue"]}


def test_statistics_computes_numeric_results_locally() -> None:
    client = client_for(response(calls=[tool("calculate_statistics", {"values": [-2, 0, 3.5, 10]})]), response("Done"))
    handler = Mock(side_effect=AssertionError("Statistics must be local"))
    respond(AgentRuntime(settings(), client=client), tool_handler=handler)
    result = json.loads(tool_results(client)[0]["content"])
    assert result == {"count": 4, "sum": 11.5, "mean": 2.875, "min": -2, "max": 10}
    handler.assert_not_called()


def test_read_skill_uses_only_enabled_snapshot() -> None:
    client = client_for(
        response(
            calls=[
                tool("read_skill", {"name": "revenue"}, "enabled"),
                tool("read_skill", {"name": "disabled"}, "disabled"),
            ]
        ),
        response("Done"),
    )
    handler = Mock(side_effect=AssertionError("Skills must use the snapshot"))
    snapshot = [
        {
            "name": "revenue",
            "description": "Revenue analysis",
            "version": "v1",
            "content": "SNAPSHOT: use net revenue, 含退款",
        }
    ]
    original = deepcopy(snapshot)
    respond(AgentRuntime(settings(), client=client), skills=snapshot, tool_handler=handler)
    results = {item["tool_call_id"]: item["content"] for item in tool_results(client)}
    assert snapshot[0]["content"] in results["enabled"] or snapshot[0]["content"] in str(json.loads(results["enabled"]))
    assert json.loads(results["disabled"]).get("error")
    assert snapshot == original
    handler.assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_history", "{broken"),
        ("search_history", "null"),
        ("unknown_tool", {}),
        ("calculate_statistics", {"values": []}),
        ("calculate_statistics", {"values": None}),
        ("calculate_statistics", {"values": ["__import__('os').getcwd()"]}),
        ("calculate_statistics", {"values": [True]}),
    ],
)
def test_invalid_tool_input_returns_safe_error_without_delegation(name: str, arguments: Any) -> None:
    client = client_for(response(calls=[tool(name, arguments)]), response("Recovered"))
    handler = Mock(side_effect=AssertionError("Invalid tools must not be delegated"))
    assert respond(AgentRuntime(settings(), client=client), tool_handler=handler) == "Recovered"
    assert json.loads(tool_results(client)[0]["content"]).get("error")
    handler.assert_not_called()


def test_tool_failure_does_not_leak_underlying_exception() -> None:
    secret = "private-db-password-internal-host"
    client = client_for(response(calls=[tool("search_history", {"query": "sales"})]), response("Unavailable"))
    handler = Mock(side_effect=ValueError(secret))
    assert respond(AgentRuntime(settings(), client=client), tool_handler=handler) == "Unavailable"
    result = tool_results(client)[0]["content"]
    assert json.loads(result).get("error")
    assert secret not in result
    assert "Traceback" not in result


def test_tool_round_limit_bounds_execution() -> None:
    client = client_for(*(response(calls=[tool("search_history", {"query": "again"}, f"call-{i}")]) for i in range(10)))
    handler = Mock(return_value={"matches": []})
    with pytest.raises(RuntimeFailure):
        respond(AgentRuntime(settings(max_tool_rounds=2), client=client), tool_handler=handler)
    assert handler.call_count <= 2
    assert client.chat.completions.create.call_count <= 3


@pytest.mark.parametrize("during_request", [False, True])
def test_cancellation_stops_before_tools_execute(during_request: bool) -> None:
    client = client_for(response(calls=[tool("search_history", {"query": "sales"})]))
    handler = Mock()

    def cancelled() -> bool:
        return not during_request or client.chat.completions.create.call_count > 0

    with pytest.raises(RuntimeCancelled):
        respond(AgentRuntime(settings(), client=client), tool_handler=handler, cancelled=cancelled)
    handler.assert_not_called()
    assert client.chat.completions.create.call_count == int(during_request)


@pytest.mark.parametrize("missing", [{"llm_api_key": ""}, {"llm_model": ""}])
def test_missing_model_configuration_fails_without_network(missing: dict) -> None:
    client = client_for(response("Should not run"))
    with pytest.raises(RuntimeFailure):
        respond(AgentRuntime(settings(**missing), client=client))
    client.chat.completions.create.assert_not_called()


def test_prompt_budget_keeps_latest_user_message_intact_and_rejects_oversize() -> None:
    client = client_for(response("Done"))
    runtime = AgentRuntime(settings(context_tokens=8000), client=client)
    latest = {"role": "user", "content": "Keep this entire question: 北京 revenue?"}
    history = [
        {"role": "user", "content": "old context " * 20000},
        {"role": "assistant", "content": "old answer " * 20000},
        latest,
    ]
    assert respond(runtime, messages=history) == "Done"
    sent = client.chat.completions.create.call_args.kwargs["messages"]
    assert latest in sent
    assert len(json.dumps(sent)) < len(json.dumps(history))
    with pytest.raises(RuntimeFailure):
        respond(runtime, messages=[{"role": "user", "content": "超出预算 " * 100000}])
    assert client.chat.completions.create.call_count == 1


BATCH = [
    {"id": "user-1", "role": "user", "content": "Please use CNY in reports"},
    {"id": "assistant-1", "role": "assistant", "content": "I assume you live in Beijing"},
]


def test_extract_memory_accepts_current_user_sources_and_empty_entries() -> None:
    entries = [{"text": "Use CNY in reports", "source_message_ids": ["user-1"]}]
    client = client_for(response(json.dumps({"entries": entries})), response('{"entries": []}'))
    runtime = AgentRuntime(settings(), client=client)
    original = deepcopy(BATCH)
    assert runtime.extract_memory(previous=[], messages=BATCH) == entries
    assert runtime.extract_memory(previous=entries, messages=BATCH) == []
    assert BATCH == original


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "null",
        "[]",
        "{}",
        '{"entries": null}',
        json.dumps({"entries": [{"text": "Assumption", "source_message_ids": ["assistant-1"]}]}),
        json.dumps({"entries": [{"text": "Stale", "source_message_ids": ["old-user"]}]}),
        json.dumps({"entries": [{"text": "No source", "source_message_ids": []}]}),
        json.dumps({"entries": [{"text": "", "source_message_ids": ["user-1"]}]}),
        json.dumps({"entries": [{"text": 42, "source_message_ids": ["user-1"]}]}),
    ],
)
def test_extract_memory_rejects_invalid_structure_or_untrusted_sources(payload: str) -> None:
    runtime = AgentRuntime(settings(), client=client_for(response(payload)))
    with pytest.raises(RuntimeFailure):
        runtime.extract_memory(previous=[{"text": "Old", "source_message_ids": ["old-user"]}], messages=BATCH)


def test_extract_memory_filters_sensitive_tokens_and_bounds_entries_and_text() -> None:
    secret = "sk-test-only-" + "x" * 48
    entries = [
        {"text": f"API key: {secret}", "source_message_ids": ["user-1"]},
        {"text": "Authorization: Bearer test-only-access-token", "source_message_ids": ["user-1"]},
    ]
    safe = [{"text": f"Preference {i}: " + "x" * 600, "source_message_ids": ["user-1"]} for i in range(25)]
    runtime = AgentRuntime(settings(), client=client_for(response(json.dumps({"entries": entries + safe}))))
    # Rejecting an over-limit model result is also safe; silently accepting it is not.
    try:
        result = runtime.extract_memory(previous=[], messages=BATCH)
    except RuntimeFailure:
        pass
    else:
        assert len(result) <= 20
        assert all(0 < len(entry["text"]) <= 500 for entry in result)
        assert secret not in json.dumps(result)
        assert "test-only-access-token" not in json.dumps(result)
    clean_client = client_for(
        response(
            json.dumps(
                {
                    "entries": entries
                    + [
                        {"text": "Use CNY", "source_message_ids": ["user-1"]},
                    ]
                }
            )
        )
    )
    cleaned = AgentRuntime(settings(), client=clean_client).extract_memory(previous=[], messages=BATCH)
    assert cleaned == [{"text": "Use CNY", "source_message_ids": ["user-1"]}]
