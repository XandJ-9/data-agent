"""Reusable agent runtime core: contracts, context budgeting and the tool loop.

Depends only on the neutral tool registry (tools). Model providers,
settings, storage, services, domains and channels are injected by callers so
the core stays domain-neutral and testable without any external system.
"""
from runtime.context import build_context, fits
from runtime.loop import Runtime
from runtime.types import (
    ModelProvider,
    ModelReply,
    RuntimeCancelled,
    RuntimeConfig,
    RuntimeFailure,
    ToolCall,
)

__all__ = [
    "ModelProvider",
    "ModelReply",
    "Runtime",
    "RuntimeCancelled",
    "RuntimeConfig",
    "RuntimeFailure",
    "ToolCall",
    "build_context",
    "fits",
]
