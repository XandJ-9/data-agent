"""Unit tests for the pure MCP tool -> ToolDefinition conversion."""

from mcp_gateway.definitions import to_definitions, tool_name


def handler_factory(tool):
    return lambda args: {'called': tool}


def test_tool_name_namespaces_and_sanitizes() -> None:
    assert tool_name('fs-server', 'list_directory') == 'mcp_fs_server_list_directory'
    assert tool_name('db', 'query!rows') == 'mcp_db_query_rows'
    assert len(tool_name('s' * 80, 't' * 80)) <= 64


def test_to_definitions_adapts_schema_and_keeps_description() -> None:
    tools = [{'name': 'search', 'description': 'Search documents.',
              'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}},
                              'required': ['q']}}]
    (definition,) = to_definitions('docs', tools, handler_factory)
    assert definition.name == 'mcp_docs_search'
    assert definition.description == 'Search documents.'
    assert definition.parameters == {'q': {'type': 'string'}}
    assert definition.required == ['q']
    assert definition.handler({'x': 1}) == {'called': 'search'}


def test_non_object_schemas_and_nameless_tools_are_skipped() -> None:
    tools = [
        {'name': 'a', 'inputSchema': {'type': 'string'}},
        {'name': 'b', 'inputSchema': {}},
        {'name': 'c'},
        {'inputSchema': {'type': 'object', 'properties': {'x': {}}}},
        {'name': 'd', 'description': 'No schema at all'},
    ]
    assert to_definitions('srv', tools, handler_factory) == []


def test_unlisted_required_defaults_to_empty() -> None:
    tools = [{'name': 't', 'description': 'd',
              'inputSchema': {'type': 'object', 'properties': {'x': {}}}}]
    (definition,) = to_definitions('srv', tools, handler_factory)
    assert definition.required == []


def test_missing_description_gets_fallback() -> None:
    tools = [{'name': 't', 'inputSchema': {'type': 'object', 'properties': {'x': {}}}}]
    (definition,) = to_definitions('srv', tools, handler_factory)
    assert definition.description == 'MCP tool t from srv'
