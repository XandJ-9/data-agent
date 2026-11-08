"""Durable worker with heartbeat fencing; interrupted runs are never replayed.

Concurrency model: a fixed-size thread pool executes runs in parallel while
the main thread handles claim, memory extraction, and Feishu delivery.
Each thread owns its Store instance so no connection is shared across threads.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from channels.feishu.delivery import SdkSender, process_deliveries, process_replies
from domains.data import DataAgentRuntime
from errors import ServiceError
from ids import identifier
from mcp_gateway.manager import McpGateway
from services.chat import prompt_messages, search_history
from services.memory import compress_one, get_memory
from settings import Settings
from storage import Store

logger = logging.getLogger(__name__)


def _execute(settings: Settings, run: dict, owner: str, *,
             runtime: Any = None, mcp_gateway: McpGateway | None = None) -> None:
    """Execute a single run in a thread. Owns its Store for thread safety."""
    store = Store(settings.resolved_database_url)
    try:
        _execute_with_store(store, settings, run, owner, runtime=runtime, mcp_gateway=mcp_gateway)
    finally:
        store.dispose()


def _execute_with_store(store: Store, settings: Settings, run: dict, owner: str, *,
                        runtime: Any = None, mcp_gateway: McpGateway | None = None) -> None:
    started = time.time()
    logger.info('Run %s started (model %s thread=%s)', run['id'],
               json.loads(run['config_snapshot']).get('llm_model'), threading.current_thread().name)

    stop = threading.Event()
    thread = threading.Thread(target=_heartbeat_loop, args=(store, run, settings.lease_seconds, stop), daemon=True)
    thread.start()
    try:
        config = json.loads(run['config_snapshot'])
        if runtime is None:
            if not settings.llm_api_key.get_secret_value() or not config.get('llm_model'):
                store.finish_run(run, error_code='configuration_required')
                return
            runtime = DataAgentRuntime(settings.model_copy(update=config))
        memory = get_memory(store, run['user_id'])
        with store.transaction() as conn:
            if not store.fence(conn, run):
                return
            store.runs.set_memory_revision(conn, run['id'], memory['revision'])
        enabled = store.enabled_skills(run['user_id'])
        skills = [s for s in json.loads(run['skills_snapshot']) if enabled.get(s['name'], True)]

        mcp_tools = _resolve_mcp_tools(mcp_gateway, store)

        reply = runtime.respond(
            messages=prompt_messages(store, run), memories=memory['entries'], skills=skills,
            tool_handler=lambda name, args: _tool(store, run, name, args),
            cancelled=lambda: _cancelled(store, run),
            on_event=_step_recorder(store, run),
            mcp_tools=mcp_tools)
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 64000:
            raise ValueError('Invalid runtime reply')
        store.finish_run(run, reply)
        logger.info('Run %s succeeded in %.1fs (thread=%s)', run['id'], time.time() - started,
                   threading.current_thread().name)
    except Exception:
        logger.error('Run %s failed after %.1fs (thread=%s)', run['id'], time.time() - started,
                    threading.current_thread().name, exc_info=True)
        store.finish_run(run, error_code='execution_failed')
    finally:
        stop.set()
        thread.join(timeout=2)


def _heartbeat_loop(store: Store, run: dict, lease_seconds: int, stop: threading.Event) -> None:
    while not stop.wait(max(1, lease_seconds / 3)):
        try:
            if not store.heartbeat(run, lease_seconds):
                return
        except Exception:
            logger.warning('Heartbeat failed for run %s', run['id'])
            return


def _cancelled(store: Store, run: dict) -> bool:
    with store.transaction(immediate=False) as conn:
        return not store.fence(conn, run)


def _tool(store: Store, run: dict, name: str, args: dict):
    if _cancelled(store, run):
        raise ServiceError(409, 'Run is no longer active')
    snapshot = json.loads(run['skills_snapshot'])
    enabled = store.enabled_skills(run['user_id'])
    if any(not enabled.get(skill['name'], True) for skill in snapshot):
        raise ServiceError(403, 'Skill was disabled during execution')
    if name == 'search_history':
        return search_history(store, run, args)
    raise ServiceError(403, 'Tool is not available')


def _step_recorder(store: Store, run: dict):
    seq = 0

    def on_event(event: dict) -> None:
        nonlocal seq
        seq += 1
        if event['kind'] == 'tool_call':
            logger.info('Run %s tool %s(%s)', run['id'], event['name'], event['content'][:200])
        try:
            store.append_step(run['id'], seq, event, time.time())
        except Exception:
            logger.warning('Step persist failed for run %s seq %s', run['id'], seq)

    return on_event


def _resolve_mcp_tools(mcp_gateway: McpGateway | None, store: Store) -> list:
    if mcp_gateway is not None:
        try:
            return mcp_gateway.tool_definitions()
        except Exception:
            logger.warning('MCP tool discovery failed')
    return []


class Worker:
    """Durable worker that claims queued runs and executes them with heartbeat fencing.

    Use `run_forever()` for the long-lived process loop. Tests inject `runtime=`
    and `mcp_gateway=` to bypass real LLM / MCP calls.
    """

    def __init__(self, settings: Settings, *, runtime: Any = None,
                 mcp_gateway: McpGateway | None = None):
        self.settings = settings
        self.store = Store(settings.resolved_database_url)
        self.owner = identifier()
        self.runtime = runtime
        self._mcp_gateway = mcp_gateway

    def run_once(self) -> bool:
        """Claim and execute a single run synchronously (used in tests)."""
        run = self.store.claim_run(self.owner, self.settings.lease_seconds)
        if run is None:
            return False
        _execute_with_store(self.store, self.settings, run, self.owner,
                           runtime=self.runtime, mcp_gateway=self._mcp_gateway)
        return True

    def memory_once(self) -> bool:
        if self.runtime is not None:
            return compress_one(self.store, self.runtime)
        if not self.settings.llm_api_key.get_secret_value() or not self.settings.llm_model:
            return False
        config = {'llm_model': self.settings.llm_model}
        return compress_one(self.store, DataAgentRuntime(self.settings.model_copy(update=config)))

    def deliver_once(self) -> bool:
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret.get_secret_value():
            return False
        secret = self.settings.feishu_app_secret.get_secret_value()
        sender = SdkSender(self.settings.feishu_app_id, secret)
        sent = (process_replies(self.store, sender, app_id=self.settings.feishu_app_id, app_secret=secret,
                                owner=self.owner, lease_seconds=self.settings.lease_seconds)
                + process_deliveries(self.store, sender, app_id=self.settings.feishu_app_id, app_secret=secret,
                                     owner=self.owner, lease_seconds=self.settings.lease_seconds))
        if sent:
            logger.info('Feishu delivery pump sent %s row(s)', sent)
        return sent > 0

    def run_forever(self) -> None:
        """Main loop: claim runs on the main thread, execute in a thread pool,
        and interleave memory extraction and Feishu delivery."""
        self.store.check_schema()
        concurrency = self.settings.worker_concurrency
        logger.info('Worker started (owner=%s poll=%ss lease=%ss concurrency=%s backend=%s)',
                   self.owner, self.settings.poll_seconds, self.settings.lease_seconds,
                   concurrency, 'postgresql' if not self.store.db.is_sqlite else 'sqlite')

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='agent') as pool:
            futures: dict[Any, dict] = {}  # future -> run
            while True:
                # Reap completed futures
                done = [f for f in futures if f.done()]
                for f in done:
                    run = futures.pop(f)
                    try:
                        f.result()
                    except Exception:
                        logger.error('Run %s raised unhandled exception', run['id'], exc_info=True)

                # Claim and submit until pool is full
                while len(futures) < concurrency:
                    run = self.store.claim_run(self.owner, self.settings.lease_seconds)
                    if run is None:
                        break
                    future = pool.submit(_execute, self.settings, run, self.owner, self._mcp_gateway)
                    futures[future] = run

                # Background work
                worked = self.memory_once()
                worked = self.deliver_once() or worked

                if not futures and not worked:
                    time.sleep(self.settings.poll_seconds)

    def close(self) -> None:
        self.store.dispose()