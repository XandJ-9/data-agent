"""Store facade: composition root wiring the Database with domain repositories.

Repositories never open transactions; this facade provides single-repo
conveniences (one transaction each) and composites that must span several
repos atomically (finish_run, consume_binding_code, retry_delivery).
Method signatures are stable for services/api/channels/worker.
"""
from clock import now_iso, now_plus

from errors import ServiceError
from ids import identifier
from storage.database import Database
from storage.repositories.accounts import AccountsRepository
from storage.repositories.channels import ChannelsRepository
from storage.repositories.feishu import FeishuRepository
from storage.repositories.mcp import McpRepository
from storage.repositories.memory import MemoryRepository
from storage.repositories.messages import MessagesRepository
from storage.repositories.outbox import OutboxRepository
from storage.repositories.runs import RunsRepository
from storage.repositories.sessions import SessionsRepository
from storage.repositories.skills import SkillsRepository
from storage.repositories.steps import RunStepsRepository
from storage.schema import deliveries, feishu_replies


class Store:
    def __init__(self, path):
        from pathlib import Path

        from sqlalchemy.engine import URL

        target = URL.create('sqlite', database=str(path)) if (
            isinstance(path, Path) or isinstance(path, str) and '://' not in path) else path
        self.db = Database(target)
        self.path = self.db._sqlite_file() if self.db.is_sqlite and not self.db.in_memory else None
        self.accounts = AccountsRepository()
        self.sessions = SessionsRepository()
        self.runs = RunsRepository()
        self.messages_repo = MessagesRepository()
        self.run_steps = RunStepsRepository()
        self.memory = MemoryRepository()
        self.skills = SkillsRepository()
        self.channels = ChannelsRepository()
        self.mcp = McpRepository()
        self.feishu = FeishuRepository()
        self.outbox_deliveries = OutboxRepository(deliveries, deliveries.c.retry_at)
        self.outbox_replies = OutboxRepository(feishu_replies, feishu_replies.c.created_at)

    # ------------------------------------------------------------ plumbing

    def transaction(self, *, immediate: bool = True):
        return self.db.transaction(immediate=immediate)

    def init_db(self) -> None:
        self.db.init_db()

    def check_schema(self) -> None:
        self.db.check_schema()

    def dispose(self) -> None:
        self.db.dispose()

    # ------------------------------------------------------------ sessions

    def own_session(self, conn, user_id: str, session_id: str) -> dict:
        return self.sessions.own_session(conn, user_id, session_id)

    def create_session(self, user_id: str, title: str = 'New conversation') -> dict:
        result = {'id': identifier(), 'title': title, 'created_at': now_iso()}
        with self.transaction() as conn:
            self.sessions.create(conn, result['id'], user_id, title, result['created_at'])
        return result

    def list_sessions(self, user_id: str, *, archived: bool = False) -> list[dict]:
        with self.transaction(immediate=False) as conn:
            return self.sessions.list_for_user(conn, user_id, archived=archived)

    def rename_session(self, user_id: str, session_id: str, title: str) -> dict:
        with self.transaction() as conn:
            return self.sessions.rename(conn, user_id, session_id, title)

    def archive_session(self, user_id: str, session_id: str) -> dict:
        with self.transaction() as conn:
            self._check_archivable(conn, session_id)
            return self.sessions.set_archived(conn, user_id, session_id, now_iso())

    def restore_session(self, user_id: str, session_id: str) -> dict:
        with self.transaction() as conn:
            return self.sessions.set_archived(conn, user_id, session_id, None)

    @staticmethod
    def _check_archivable(conn, session_id: str) -> None:
        """Raise 409 if the session has active runs or Feishu channel bindings."""
        from sqlalchemy import select as _sel

        from storage.schema import channel_identities, runs
        active = conn.execute(_sel(runs.c.id).where(
            runs.c.session_id == session_id,
            runs.c.status.in_(['queued', 'running']))).first()
        if active is not None:
            raise ServiceError(409, 'Cannot archive a session with active runs')
        binding = conn.execute(_sel(channel_identities.c.id).where(
            channel_identities.c.session_id == session_id)).first()
        if binding is not None:
            raise ServiceError(409, 'Cannot archive a session bound to a Feishu channel')

    # ------------------------------------------------------------ messages & runs

    def messages(self, user_id: str, session_id: str, before_id: int | None = None, limit: int = 100) -> dict:
        with self.transaction(immediate=False) as conn:
            self.sessions.own_session(conn, user_id, session_id)
            page, next_before = self.messages_repo.page(conn, session_id, before_id, limit)
            # 返回会话里所有 run（含尚未关联消息的进行中 run），前端将在消息流中内联展示
            runs = [self._public_run(conn, r) for r in self.runs.by_session(conn, session_id)]
            return {'items': page, 'runs': runs, 'next_before_id': next_before}

    def _public_run(self, conn, run: dict) -> dict:
        return {**{k: run[k] for k in ('id', 'status', 'error_code', 'created_at')},
                'deliveries': self.outbox_deliveries.list_for_run(conn, run['id'])}

    def get_run(self, user_id: str, run_id: str) -> dict:
        with self.transaction(immediate=False) as conn:
            return self._public_run(conn, self.runs.get(conn, user_id, run_id))

    def cancel_run(self, user_id: str, run_id: str) -> dict:
        with self.transaction() as conn:
            self.runs.cancel(conn, user_id, run_id)
        return self.get_run(user_id, run_id)

    def append_step(self, run_id: str, seq: int, event: dict, now: str) -> None:
        with self.transaction() as conn:
            self.run_steps.append(conn, run_id, seq, event, now)

    def list_steps(self, run_id: str) -> dict:
        with self.transaction(immediate=False) as conn:
            return {'items': self.run_steps.list(conn, run_id)}

    # ------------------------------------------------------------ worker lifecycle

    def claim_run(self, owner: str, lease_seconds: int) -> dict | None:
        with self.transaction() as conn:
            return self.runs.claim(conn, owner, lease_seconds, now_iso())

    @staticmethod
    def fence(conn, run: dict) -> bool:
        return RunsRepository.fence(conn, run)

    def heartbeat(self, run: dict, lease_seconds: int) -> bool:
        with self.transaction() as conn:
            return self.runs.heartbeat(conn, run, lease_seconds)

    def finish_run(self, run: dict, content: str | None = None, error_code: str | None = None) -> bool:
        with self.transaction() as conn:
            self.sessions.lock(conn, run['user_id'], run['session_id'])
            if not self.runs.mark_finished(conn, run, error_code):
                return False
            if error_code:
                return True
            self.messages_repo.insert(conn, run['user_id'], run['session_id'], run['id'], 'assistant', content,
                                      now_iso())
            self.memory.mark_dirty(conn, run['session_id'])
            if run['source'] == 'feishu':
                self.outbox_deliveries.enqueue(conn, {
                    'id': identifier(), 'run_id': run['id'], 'binding_id': run['binding_id'],
                    'reply_target': run['reply_target'], 'content': content})
            return True

    # ------------------------------------------------------------ bindings

    def binding(self, user_id: str) -> dict | None:
        with self.transaction(immediate=False) as conn:
            return self.channels.binding_for_user(conn, user_id, 'feishu')

    def consume_binding_code(self, code_hash: str, app_id: str, tenant_id: str, external_user_id: str) -> dict:
        """Trusted channel adapter supplies SHA-256 code digest and verified identity."""
        with self.transaction() as conn:
            return self.consume_binding_code_conn(conn, code_hash, app_id, tenant_id, external_user_id)

    def consume_binding_code_conn(self, conn, code_hash: str, app_id: str, tenant_id: str,
                                  external_user_id: str) -> dict:
        from sqlalchemy.exc import IntegrityError

        try:
            with conn.begin_nested():
                row = self.channels.consume_code(conn, code_hash, now_iso())
                if row is None:
                    raise ServiceError(400, 'Binding code invalid or expired')
                if self.channels.active_conflict(conn, row['user_id'], app_id, tenant_id, external_user_id):
                    raise ServiceError(409, 'Account already bound')
                sid, bid = identifier(), identifier()
                self.sessions.create(conn, sid, row['user_id'], 'Feishu conversation', now_iso())
                self.channels.create(conn, bid, row['user_id'], app_id, tenant_id, external_user_id, sid)
                return self.channels.by_id(conn, bid)
        except IntegrityError:
            raise ServiceError(409, 'Account already bound') from None

    # ------------------------------------------------------------ skills

    def enabled_skills(self, user_id: str) -> dict[str, bool]:
        with self.transaction(immediate=False) as conn:
            return self.skills.enabled_map(conn, user_id)

    def set_skill(self, user_id: str, name: str, enabled: bool) -> None:
        with self.transaction() as conn:
            self.skills.set(conn, user_id, name, enabled)

    # ------------------------------------------------------------ mcp servers

    def list_mcp_servers(self, *, enabled_only: bool = False) -> list[dict]:
        with self.transaction(immediate=False) as conn:
            return self.mcp.list(conn, enabled_only=enabled_only)

    def upsert_mcp_server(self, fields: dict) -> None:
        with self.transaction() as conn:
            self.mcp.upsert(conn, fields)

    def set_mcp_enabled(self, name: str, enabled: bool) -> bool:
        with self.transaction() as conn:
            return self.mcp.set_enabled(conn, name, enabled)

    def delete_mcp_server(self, name: str) -> bool:
        with self.transaction() as conn:
            return self.mcp.delete(conn, name)

    # ------------------------------------------------------------ api-owned composites

    def create_binding_code(self, user_id: str, code_hash: str, expires_at: str) -> None:
        with self.transaction() as conn:
            self.channels.purge_codes(conn, user_id)
            self.channels.create_code(conn, code_hash, user_id, expires_at)

    def unbind_user(self, user_id: str) -> None:
        with self.transaction() as conn:
            self.channels.deactivate_for_user(conn, user_id)
            self.channels.purge_codes(conn, user_id)

    def switch_binding_session(self, user_id: str, session_id: str) -> None:
        with self.transaction() as conn:
            session = self.sessions.own_session(conn, user_id, session_id)
            if session.get('archived_at') is not None:
                raise ServiceError(409, 'Cannot bind to an archived session')
            if not self.channels.set_session_for_user(conn, user_id, session_id):
                raise ServiceError(404, 'Binding not found')

    def retry_delivery(self, user_id: str, delivery_id: str, *, confirm_duplicate_risk: bool = False) -> dict:
        with self.transaction() as conn:
            row = self.outbox_deliveries.get_for_owner(conn, delivery_id, user_id)
            if row is None:
                raise ServiceError(404, 'Delivery not found')
            if row['status'] == 'unknown' and not confirm_duplicate_risk:
                raise ServiceError(409, 'Delivery may have succeeded; confirm duplicate risk')
            if row['status'] not in ('unknown', 'failed'):
                raise ServiceError(409, 'Delivery cannot be retried in its current state')
            active = self.channels.get_active(conn, row['binding_id'])
            if active is None or not active['active']:
                raise ServiceError(409, 'Channel binding is unavailable')
            self.outbox_deliveries.reset_pending(conn, delivery_id)
        return {'id': delivery_id, 'status': 'pending'}
