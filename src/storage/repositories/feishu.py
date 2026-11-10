"""feishu_events dedupe / feishu_bind_attempts / feishu_replies enqueue."""
import hashlib

from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

from clock import iso_minus
from storage.repositories.base import BaseRepository
from storage.schema import feishu_bind_attempts, feishu_events, feishu_replies


class FeishuRepository(BaseRepository):
    def lock_event(self, conn: Connection, app_id: str, tenant_id: str, event_id: str,
                   message_id: str) -> None:
        """Transaction locks cover both dedupe keys, including first-ever delivery."""
        if conn.dialect.name != 'postgresql':
            return  # SQLite write transactions already serialize writers.
        keys = [f'event:{app_id}:{tenant_id}:{event_id}']
        if message_id:
            keys = [*keys, f'message:{message_id}']
        lock_ids = sorted({int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],
                                         'big', signed=True) for key in keys})
        for lock_id in lock_ids:
            conn.execute(select(func.pg_advisory_xact_lock(lock_id)))

    def event_seen(self, conn: Connection, app_id: str, tenant_id: str, event_id: str) -> bool:
        return conn.execute(select(1).select_from(feishu_events)
                            .where(feishu_events.c.app_id == app_id, feishu_events.c.tenant_id == tenant_id,
                                   feishu_events.c.event_id == event_id)).first() is not None

    def message_seen(self, conn: Connection, message_id: str) -> bool:
        return conn.execute(select(1).select_from(feishu_events)
                            .where(feishu_events.c.message_id == message_id)).first() is not None

    def record_event(self, conn: Connection, app_id: str, tenant_id: str, event_id: str,
                     message_id: str | None, received_at: str) -> None:
        conn.execute(feishu_events.insert(), {'app_id': app_id, 'tenant_id': tenant_id, 'event_id': event_id,
                                              'message_id': message_id, 'received_at': received_at})

    def bind_attempt_allowed(self, conn: Connection, key: str, window_seconds: int, max_attempts: int,
                             now: str) -> bool:
        """Bounded attempt counter keyed by the verified external identity."""
        from sqlalchemy.dialects import postgresql, sqlite
        if max_attempts <= 0:
            return False
        cutoff = iso_minus(now, window_seconds)
        construct = sqlite.insert if conn.dialect.name == 'sqlite' else postgresql.insert
        table = feishu_bind_attempts
        expired = table.c.window_start <= cutoff
        statement = construct(table).values(identity_key=key, window_start=now, attempts=1)
        result = conn.execute(statement.on_conflict_do_update(
            index_elements=[table.c.identity_key],
            set_={'window_start': case((expired, now), else_=table.c.window_start),
                  'attempts': case((expired, 1), else_=table.c.attempts + 1)},
            where=expired | (table.c.attempts < max_attempts)).returning(table.c.attempts))
        return result.first() is not None

    def enqueue_reply(self, conn: Connection, reply_id: str, app_id: str, tenant_id: str,
                      external_user_id: str, content: str, uuid: str, created_at: str) -> None:
        conn.execute(feishu_replies.insert(), {
            'id': reply_id, 'app_id': app_id, 'tenant_id': tenant_id, 'external_user_id': external_user_id,
            'reply_target': external_user_id, 'content': content, 'uuid': uuid, 'created_at': created_at})
