"""Pure conversion: MCP tool listings -> ToolDefinition records.

No I/O here so naming and schema adaptation stay unit-testable; the manager
supplies handlers that bridge to live MCP sessions.
"""
import re

from tools.registry import ToolDefinition

SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]")
MAX_NAME = 64


def tool_name(server: str, tool: str) -> str:
    """Namespaced, model-safe function name: mcp_<server>_<tool>."""
    def cleaned(value: str) -> str:
        return SAFE_NAME.sub('_', value).strip('_')
    name = f"mcp_{cleaned(server)}_{cleaned(tool)}"
    return name[:MAX_NAME]


def to_definitions(server: str, tools: list[dict], handler_factory) -> list[ToolDefinition]:
    """Adapt MCP tool descriptors (name/description/inputSchema) into
    ToolDefinitions. Tools whose inputSchema is not a JSON object shape are
    skipped rather than registered with a broken schema."""
    definitions = []
    for tool in tools:
        raw_name = tool.get('name')
        if not isinstance(raw_name, str) or not raw_name:
            continue
        schema = tool.get('inputSchema') or {}
        properties = schema.get('properties')
        if schema.get('type') != 'object' or not isinstance(properties, dict):
            continue
        required = schema.get('required')
        if not isinstance(required, list):
            required = []
        name = tool_name(server, raw_name)
        if not name or name == 'mcp_':
            continue
        definitions.append(ToolDefinition(
            name=name,
            description=(tool.get('description') or f"MCP tool {raw_name} from {server}").strip()[:1000],
            parameters=properties,
            required=[str(item) for item in required],
            handler=handler_factory(raw_name)))
    return definitions
