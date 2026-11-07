"""Tool registry: one place per tool for schema, validation and execution.

A ToolDefinition owns its argument validation (raising ValueError keeps the
result sanitized) and execution; ToolRegistry dispatches by name and bounds
result size. Adding a tool means registering one definition — runtime code
never grows a new dispatch branch.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass

RESULT_BYTES = 36000


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    required: list[str]
    handler: Callable[[dict], object]
    """Validates its own arguments (raise ValueError on invalid input) and returns
    a JSON-serializable value."""

    def schema(self) -> dict:
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description,
            'parameters': {'type': 'object', 'properties': self.parameters,
                           'required': self.required, 'additionalProperties': False}}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def dispatch(self, name: str, arguments: str) -> str:
        """Run one tool call; always returns JSON. Oversized results and any
        handler failure surface as sanitized error payloads, never exceptions."""
        try:
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError('unknown tool')
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError('invalid arguments')
            content = json.dumps(tool.handler(args), ensure_ascii=False, allow_nan=False)
        except Exception:
            return json.dumps({'error': '工具不可用或参数无效'}, ensure_ascii=False)
        if len(content.encode()) > RESULT_BYTES:
            return json.dumps({'error': '工具结果超过预算，请缩小范围'}, ensure_ascii=False)
        return content
