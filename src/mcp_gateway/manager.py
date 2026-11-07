"""McpGateway: sync bridge to MCP servers running on a dedicated asyncio thread.

The runtime tool loop is fully synchronous; all MCP SDK calls run on one
background event loop and `call()` bridges with run_coroutine_threadsafe +
a bounded wait. Sessions are created lazily per enabled server and rebuilt
after failures; a single server failing never breaks a Run — the dispatch
error surfaces to the model as a sanitized tool error.
"""
import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from mcp_gateway.definitions import to_definitions
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

CALL_TIMEOUT = 30.0
REFRESH_SECONDS = 60.0


class McpError(Exception):
    """MCP tool call failed (transport, timeout, or server-side error)."""


class _ServerSession:
    """One server's live ClientSession plus the context managers that own it."""

    def __init__(self, config: dict, session: Any, teardowns: list) -> None:
        self.config = config
        self.session = session
        self.teardowns = teardowns
        self.failed = False


class McpGateway:
    def __init__(self, store, *, session_factory: Callable | None = None,
                 call_timeout: float = CALL_TIMEOUT,
                 refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._store = store
        self._session_factory = session_factory  # tests inject (session, teardowns)
        self._call_timeout = call_timeout
        self._refresh_seconds = refresh_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._sessions: dict[str, _ServerSession] = {}
        self._definitions: list[ToolDefinition] = []
        self._definitions_at = 0.0
        self._config_at: int | None = None

    # ------------------------------------------------------------ lifecycle

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._ready.clear()

            def run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(target=run, name='mcp-gateway', daemon=True)
            self._thread.start()
            self._ready.wait(timeout=10)
            if self._loop is None or not self._loop.is_running():
                raise McpError('MCP gateway loop failed to start')
            return self._loop

    def close(self) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
            sessions, self._sessions = self._sessions, {}
            self._definitions, self._config_at = [], None
        if loop is not None and loop.is_running():
            logger.info('MCP gateway shutting down')
            for entry in sessions.values():
                self._teardown(loop, entry)
            loop.call_soon_threadsafe(loop.stop)

    def _teardown(self, loop: asyncio.AbstractEventLoop, entry: _ServerSession) -> None:
        async def shutdown() -> None:
            for context in reversed(entry.teardowns):
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    pass
        asyncio.run_coroutine_threadsafe(shutdown(), loop)

    # ------------------------------------------------------------ sessions

    async def _open(self, config: dict) -> _ServerSession:
        if self._session_factory is not None:
            session, teardowns = self._session_factory(config)
            return _ServerSession(config, session, teardowns)
        if config['transport'] == 'stdio':
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=config['command'], args=json.loads(config['args_json']),
                env={str(k): str(v) for k, v in json.loads(config['env_json']).items()})
            transport = stdio_client(params)
            read, write = await transport.__aenter__()
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            await session.initialize()
            return _ServerSession(config, session, [session_cm, transport])
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = json.loads(config['headers_json'])
        transport = streamablehttp_client(config['url'], headers=headers or None)
        read, write, _ = await transport.__aenter__()
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await session.initialize()
        return _ServerSession(config, session, [session_cm, transport])

    def _session_for(self, loop: asyncio.AbstractEventLoop, config: dict) -> _ServerSession:
        name = config['name']
        entry = self._sessions.get(name)
        if entry is not None and not entry.failed and entry.config == config:
            return entry
        if entry is not None:
            self._teardown(loop, entry)
        opened = asyncio.run_coroutine_threadsafe(self._open(config), loop) \
            .result(timeout=self._call_timeout)
        self._sessions[name] = opened
        return opened

    # ------------------------------------------------------------ public API

    def tool_definitions(self, *, refresh: bool = False) -> list[ToolDefinition]:
        """Aggregate MCP tools from every enabled server; cached until the
        configuration changes or the refresh window expires."""
        configs = self._store.list_mcp_servers(enabled_only=True)
        version = hash(tuple((c['name'], c['transport'], c['command'], c['url'],
                              c['args_json'], c['env_json'], c['headers_json']) for c in configs))
        now = time.time()
        if not configs:
            self._definitions, self._config_at, self._definitions_at = [], version, now
            return []
        if (not refresh and self._definitions and version == self._config_at
                and now - self._definitions_at < self._refresh_seconds):
            return self._definitions
        loop = self._ensure_loop()
        definitions: list[ToolDefinition] = []
        for config in configs:
            try:
                tools = self._list_tools(loop, config)
            except Exception:
                continue  # one dead server must not hide the others
            definitions.extend(to_definitions(config['name'], tools,
                                              lambda tool, cfg=config: self._handler(cfg, tool)))
        self._definitions = definitions
        self._config_at = version
        self._definitions_at = now
        return definitions

    def _list_tools(self, loop: asyncio.AbstractEventLoop, config: dict) -> list[dict]:
        entry = self._session_for(loop, config)

        async def listing() -> list[dict]:
            result = await entry.session.list_tools()
            return [tool.model_dump(exclude_none=True) for tool in result.tools]

        try:
            return asyncio.run_coroutine_threadsafe(listing(), loop) \
                .result(timeout=self._call_timeout)
        except Exception as exc:
            entry.failed = True
            logger.warning('MCP server %s list_tools failed: %s', config['name'], exc)
            raise

    def _handler(self, config: dict, tool: str) -> Callable[[dict], object]:
        def handler(args: dict) -> object:
            return self.call(config, tool, args)
        return handler

    def call(self, config: dict, tool: str, args: dict) -> object:
        """Synchronous MCP tool call; raises McpError on transport/timeout/server error."""
        loop = self._ensure_loop()
        entry = self._session_for(loop, config)

        async def invocation() -> object:
            result = await entry.session.call_tool(tool, args)
            if getattr(result, 'isError', False):
                raise McpError('MCP tool reported an error')
            structured = getattr(result, 'structuredContent', None)
            if structured is not None:
                return structured
            texts = [block.text for block in (result.content or [])
                     if getattr(block, 'type', '') == 'text']
            if len(texts) == 1:
                return texts[0]
            return texts or {}

        future = asyncio.run_coroutine_threadsafe(invocation(), loop)
        try:
            return future.result(timeout=self._call_timeout)
        except Exception as exc:
            entry.failed = True
            raise McpError(getattr(exc, 'args', ['MCP call failed'])[0]
                           if isinstance(exc, McpError) else 'MCP call failed') from exc
