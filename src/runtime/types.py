"""Runtime contracts: immutable configuration and normalized model exchange types.

These are the only types the reusable loop and its providers share; keeping
them free of any SDK, settings or storage import preserves core independence.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RuntimeConfig:
    """Execution budgets for one runtime; immutable by design."""

    model_name: str
    max_tokens: int = 4096
    context_tokens: int = 24000
    max_tool_rounds: int = 8


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    """Provider-normalized assistant message."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str | None = None  # thinking trace when the model exposes one


class RuntimeFailure(Exception):
    """Public, credential-free runtime failure."""


class RuntimeCancelled(RuntimeFailure):
    pass


class ModelProvider(Protocol):
    """Model transport injected into the runtime; raises RuntimeFailure on errors."""

    def complete(self, config: RuntimeConfig, messages: list[dict], tools: list[dict] | None) -> ModelReply: ...
