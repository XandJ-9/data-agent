"""Durable worker with heartbeat fencing; interrupted runs are never replayed."""
import json
import logging
import threading
import time
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


class Worker:
    def __init__(self, settings: Settings, *, runtime: Any = None,
                 mcp_gateway: McpGateway | None = None):
        self.settings = settings
        self.store = Store(settings.resolved_database_url)
        self.owner = identifier()
        self.runtime = runtime
        self._mcp_gateway = mcp_gateway

    def _mcp_tools(self) -> list:
        """MCP tool definitions for the live configuration; injected gateways
        (tests) and fake runtimes keep this path controllable."""
        if self.runtime is not None and self._mcp_gateway is None:
            return []
        if self._mcp_gateway is None:
            self._mcp_gateway = McpGateway(self.store)
        try:
            return self._mcp_gateway.tool_definitions()
        except Exception:
            logger.warning('MCP tool discovery failed')
            return []

    def _runtime(self, run: dict | None = None):
        if self.runtime is not None:
            return self.runtime
        config = json.loads(run['config_snapshot']) if run else {}
        return DataAgentRuntime(self.settings.model_copy(update=config))

    def _cancelled(self, run: dict) -> bool:
        with self.store.transaction(immediate=False) as conn:
            return not self.store.fence(conn, run)

    def _tool(self, run: dict, name: str, args: dict):
        if self._cancelled(run):
            raise ServiceError(409, 'Run is no longer active')
        snapshot = json.loads(run['skills_snapshot'])
        enabled = self.store.enabled_skills(run['user_id'])
        # Revoking any skill used by this run invalidates further tool dispatch.
        if any(not enabled.get(skill['name'], True) for skill in snapshot):
            raise ServiceError(403, 'Skill was disabled during execution')
        if name == 'search_history':
            return search_history(self.store, run, args)
        raise ServiceError(403, 'Tool is not available')

    def _heartbeat_loop(self, run: dict, stop: threading.Event) -> None:
        while not stop.wait(max(1, self.settings.lease_seconds / 3)):
            try:
                if not self.store.heartbeat(run, self.settings.lease_seconds):
                    return
            except Exception:
                logger.warning('Heartbeat failed for run %s', run['id'])
                return

    def _step_recorder(self, run: dict):
        """Persist each runtime event as a run_step; trace failures never break the run."""
        seq = 0

        def on_event(event: dict) -> None:
            nonlocal seq
            seq += 1
            if event['kind'] == 'tool_call':
                logger.info('Run %s tool %s(%s)', run['id'], event['name'], event['content'][:200])
            try:
                self.store.append_step(run['id'], seq, event, time.time())
            except Exception:
                logger.warning('Step persist failed for run %s seq %s', run['id'], seq)

        return on_event

    def run_once(self) -> bool:
        run = self.store.claim_run(self.owner, self.settings.lease_seconds)
        if run is None:
            return False
        started = time.time()
        logger.info('Run %s claimed (model %s)', run['id'],
                    json.loads(run['config_snapshot']).get('llm_model'))
        stop = threading.Event()
        thread = threading.Thread(target=self._heartbeat_loop, args=(run, stop), daemon=True)
        thread.start()
        try:
            if self.runtime is None and (not self.settings.llm_api_key.get_secret_value()
                                         or not json.loads(run['config_snapshot'])['llm_model']):
                self.store.finish_run(run, error_code='configuration_required')
                return True
            memory = get_memory(self.store, run['user_id'])
            with self.store.transaction() as conn:
                if not self.store.fence(conn, run):
                    return True
                self.store.runs.set_memory_revision(conn, run['id'], memory['revision'])
            enabled = self.store.enabled_skills(run['user_id'])
            skills = [s for s in json.loads(run['skills_snapshot']) if enabled.get(s['name'], True)]
            reply = self._runtime(run).respond(
                messages=prompt_messages(self.store, run), memories=memory['entries'], skills=skills,
                tool_handler=lambda name, args: self._tool(run, name, args),
                cancelled=lambda: self._cancelled(run), on_event=self._step_recorder(run),
                mcp_tools=self._mcp_tools())
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 64000:
                raise ValueError('Invalid runtime reply')
            self.store.finish_run(run, reply)
            logger.info('Run %s succeeded in %.1fs', run['id'], time.time() - started)
        except Exception:
            logger.error('Run %s failed after %.1fs', run['id'], time.time() - started, exc_info=True)
            self.store.finish_run(run, error_code='execution_failed')
        finally:
            stop.set()
            thread.join(timeout=2)
        return True

    def memory_once(self) -> bool:
        if self.runtime is None and (not self.settings.llm_api_key.get_secret_value() or not self.settings.llm_model):
            return False
        return compress_one(self.store, self._runtime())

    def run_forever(self) -> None:
        self.store.check_schema()
        logger.info('Worker started (owner=%s poll=%ss lease=%ss)',
                   self.owner, self.settings.poll_seconds, self.settings.lease_seconds)
        while True:
            worked = self.run_once()
            worked = self.memory_once() or worked
            worked = self.deliver_once() or worked
            if not worked:
                time.sleep(self.settings.poll_seconds)

    def deliver_once(self) -> bool:
        """Send due Feishu replies and data deliveries; never re-executes a Run."""
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret.get_secret_value():
            return False
        secret = self.settings.feishu_app_secret.get_secret_value()
        sender = SdkSender(self.settings.feishu_app_id, secret)
        sent = (process_replies(self.store, sender, app_id=self.settings.feishu_app_id, app_secret=secret,
                                owner=self.owner, lease_seconds=self.settings.lease_seconds)
                + process_deliveries(self.store, sender, app_id=self.settings.feishu_app_id, app_secret=secret,
                                     owner=self.owner, lease_seconds=self.settings.lease_seconds))
        logger.info('Feishu delivery pump sent %s row(s)', sent)
        return sent > 0
