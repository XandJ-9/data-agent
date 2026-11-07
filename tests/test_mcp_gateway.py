"""McpGateway unit tests with an injected fake session factory (no network)."""


import pytest

from mcp_gateway.manager import McpError, McpGateway


class FakeResult:
    def __init__(self, content=None, structured=None, is_error=False):
        self.content = content or []
        self.structuredContent = structured
        self.isError = is_error


class FakeBlock:
    def __init__(self, text):
        self.type = 'text'
        self.text = text


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = f'{name} description'
        self.inputSchema = {'type': 'object', 'properties': {'x': {'type': 'integer'}},
                            'required': ['x']}

    def model_dump(self, exclude_none=None):
        return {'name': self.name, 'description': self.description, 'inputSchema': self.inputSchema}


class FakeSession:
    def __init__(self, tools=None, results=None, error_tools=()):
        self._tools = tools or []
        self._results = results or {}
        self._error_tools = error_tools
        self.calls = []

    async def initialize(self):
        pass

    async def list_tools(self):
        class Result:
            tools = self._tools
        return Result()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self._error_tools:
            return FakeResult(content=[FakeBlock('boom')], is_error=True)
        return self._results.get(name, FakeResult(content=[FakeBlock('ok')]))


class FakeStore:
    def __init__(self, servers):
        self._servers = servers

    def list_mcp_servers(self, *, enabled_only=False):
        return [s for s in self._servers if s['enabled'] or not enabled_only]


def make_config(name='docs', transport='stdio', **extra):
    base = {'name': name, 'transport': transport, 'command': 'fake', 'args_json': '[]',
            'env_json': '{}', 'url': None, 'headers_json': '{}', 'enabled': 1}
    base.update(extra)
    return base


@pytest.fixture
def gateway_factory():
    made = []

    def make(session, config):
        def factory(cfg):
            made.append(cfg)
            return session, []
        store = FakeStore([config])
        return McpGateway(store, session_factory=factory), made
    yield make


def test_tool_definitions_lists_and_namespaces_tools(gateway_factory):
    session = FakeSession(tools=[FakeTool('search')])
    gateway, made = gateway_factory(session, make_config())
    definitions = gateway.tool_definitions()
    assert [d.name for d in definitions] == ['mcp_docs_search']
    assert definitions[0].required == ['x']
    assert made == [made[0]]  # session opened once
    assert gateway.tool_definitions() is not None  # cached path does not reopen
    assert len(made) == 1
    gateway.close()


def test_call_bridges_sync_to_async_and_returns_text(gateway_factory):
    session = FakeSession(tools=[FakeTool('search')], results={'search': FakeResult(content=[FakeBlock('42 rows')])})
    gateway, _ = gateway_factory(session, make_config())
    (definition,) = gateway.tool_definitions()
    assert definition.handler({'x': 1}) == '42 rows'
    assert session.calls == [('search', {'x': 1})]
    gateway.close()


def test_call_error_result_raises_mcp_error(gateway_factory):
    session = FakeSession(tools=[FakeTool('search')], error_tools=('search',))
    gateway, _ = gateway_factory(session, make_config())
    (definition,) = gateway.tool_definitions()
    with pytest.raises(McpError):
        definition.handler({'x': 1})
    gateway.close()


def test_structured_content_preferred_over_text(gateway_factory):
    session = FakeSession(tools=[FakeTool('search')], results={'search': FakeResult(structured={'rows': 3})})
    gateway, _ = gateway_factory(session, make_config())
    (definition,) = gateway.tool_definitions()
    assert definition.handler({'x': 1}) == {'rows': 3}
    gateway.close()


def test_disabled_servers_are_ignored(gateway_factory):
    session = FakeSession(tools=[FakeTool('search')])
    gateway, made = gateway_factory(session, make_config(enabled=0))
    assert gateway.tool_definitions() == []
    assert made == []
    gateway.close()


def test_broken_server_does_not_hide_working_one():
    def factory(config):
        if config['name'] == 'broken':
            raise RuntimeError('cannot connect')
        return FakeSession(tools=[FakeTool('search')]), []
    store = FakeStore([make_config('broken'), make_config('good')])
    gateway = McpGateway(store, session_factory=factory)
    assert [d.name for d in gateway.tool_definitions()] == ['mcp_good_search']
    gateway.close()


def test_worker_registers_and_calls_mcp_tool(env):
    from test_service import FakeRuntime
    from test_service import session as make_session

    from services.chat import submit_message
    from worker import Worker

    settings, store, user, _ = env
    sid = make_session(store, user)
    submit_message(store, settings, user['id'], sid, 'hi', 'k1')

    captured = {}

    class McpRuntime(FakeRuntime):
        def respond(self, **kwargs):
            captured.update(kwargs)
            definitions = kwargs.get('mcp_tools') or []
            if definitions:
                captured['result'] = definitions[0].handler({'x': 1})
            return 'answer'

    session = FakeSession(tools=[FakeTool('search')],
                          results={'search': FakeResult(content=[FakeBlock('found 3 rows')])})
    with store.transaction() as conn:
        store.mcp.upsert(conn, {'id': 'srv-1', **make_config('docs'), 'created_at': 0.0})
    gateway = McpGateway(store, session_factory=lambda cfg: (session, []))
    worker = Worker(settings, runtime=McpRuntime(), mcp_gateway=gateway)
    assert worker.run_once()
    assert [d.name for d in captured['mcp_tools']] == ['mcp_docs_search']
    assert captured['result'] == 'found 3 rows'
    gateway.close()
