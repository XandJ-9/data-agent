"""runs: durable state machine rows plus claim/fence/heartbeat primitives.

Single-repo conveniences (one transaction) live on the Store facade; composite
lifecycle transitions (finish_run composing messages/outbox/memory) are also
facade methods because they span repos inside one transaction.
"""
from clock import now_iso, now_plus

from sqlalchemy import exists, func, select, update
from sqlalchemy.engine import Connection

from errors import ServiceError
from storage.repositories.base import BaseRepository
from storage.schema import runs, sessions


class RunsRepository(BaseRepository):
    def insert(self, conn: Connection, run: dict) -> None:
        conn.execute(runs.insert(), {
            'id': run['id'], 'user_id': run['user_id'], 'session_id': run['session_id'],
            'client_message_id': run['client_message_id'], 'created_at': run['created_at'],
            'definition_hash': run['definition_hash'], 'skills_snapshot': run['skills_snapshot'],
            'config_snapshot': run['config_snapshot'], 'source': run['source'],
            'binding_id': run.get('binding_id'), 'reply_target': run.get('reply_target')})

    def set_input_message(self, conn: Connection, run_id: str, message_id: int) -> None:
        conn.execute(update(runs).where(runs.c.id == run_id).values(input_message_id=message_id))

    def by_client_message_id(self, conn: Connection, user_id: str, session_id: str,
                             client_message_id: str) -> dict | None:
        return self._row(conn.execute(select(runs).where(
            runs.c.user_id == user_id, runs.c.session_id == session_id,
            runs.c.client_message_id == client_message_id)))

    def get(self, conn: Connection, user_id: str, run_id: str) -> dict:
        row = self._row(conn.execute(select(runs).where(runs.c.id == run_id, runs.c.user_id == user_id)))
        if row is None:
            raise ServiceError(404, 'Run not found')
        return row

    def by_session(self, conn: Connection, session_id: str, limit: int = 100) -> list[dict]:
        return self._rows(conn.execute(select(runs).where(runs.c.session_id == session_id)
                                       .order_by(runs.c.input_message_id.desc()).limit(limit)))

    def succeeded_inputs_before(self, conn: Connection, session_id: str, input_message_id: int,
                                limit: int = 12) -> list[int]:
        """Previous successful rounds by INPUT sequence; replies may carry later IDs."""
        return [r for r in conn.execute(
            select(runs.c.id).where(runs.c.session_id == session_id, runs.c.status == 'succeeded',
                                    runs.c.input_message_id < input_message_id)
            .order_by(runs.c.input_message_id.desc()).limit(limit)).scalars()]

    def memory_rounds(self, conn: Connection, session_id: str, after_cursor: int, limit: int = 8) -> list[dict]:
        """Succeeded runs of the session that end after the memory cursor."""
        from storage.schema import messages
        end_id = select(func.max(messages.c.id)).where(messages.c.run_id == runs.c.id) \
            .correlate(runs).scalar_subquery()
        return self._rows(conn.execute(
            select(runs.c.id, runs.c.input_message_id, end_id.label('end_id'))
            .where(runs.c.session_id == session_id, runs.c.status == 'succeeded', end_id > after_cursor)
            .order_by(runs.c.input_message_id).limit(limit)))

    def cancel(self, conn: Connection, user_id: str, run_id: str) -> None:
        conn.execute(update(runs)
                     .where(runs.c.id == run_id, runs.c.user_id == user_id,
                            runs.c.status.in_(['queued', 'running']))
                     .values(status='cancelled', generation=runs.c.generation + 1))

    def claim(self, conn: Connection, owner: str, lease_seconds: int, now: str) -> dict | None:
        conn.execute(update(runs)
                     .where(runs.c.status == 'running', runs.c.lease_until <= now)
                     .values(status='interrupted', error_code='worker_lost', generation=runs.c.generation + 1))
        prev = runs.alias('prev')
        candidate = conn.execute(
            select(runs.c.id).where(
                runs.c.status == 'queued',
                ~exists(select(1).where(
                    prev.c.session_id == runs.c.session_id,
                    (prev.c.status == 'running')
                    | ((prev.c.status == 'queued') & (prev.c.input_message_id < runs.c.input_message_id)))))
            .order_by(runs.c.input_message_id).limit(1)).scalar()
        if candidate is None:
            return None
        session_id = conn.execute(select(runs.c.session_id).where(runs.c.id == candidate)).scalar_one()
        locked = conn.execute(select(sessions.c.id).where(sessions.c.id == session_id)
                              .with_for_update(skip_locked=True)).first()
        if locked is None:
            return None
        row = conn.execute(
            update(runs).where(runs.c.id == candidate, runs.c.status == 'queued',
                ~exists(select(1).where(prev.c.session_id == session_id,
                    (prev.c.status == 'running') | ((prev.c.status == 'queued') &
                    (prev.c.input_message_id < runs.c.input_message_id)))))
            .values(status='running', owner=owner, generation=runs.c.generation + 1,
                    lease_until=now_plus(lease_seconds))
            .returning(runs)).mappings().first()
        return dict(row) if row is not None else None

    @staticmethod
    def fence(conn: Connection, run: dict) -> bool:
        return conn.execute(
            select(1).select_from(runs).where(
                runs.c.id == run['id'], runs.c.status == 'running', runs.c.owner == run['owner'],
                runs.c.generation == run['generation'], runs.c.lease_until > now_iso())
        ).first() is not None

    @staticmethod
    def _fenced_update(run: dict):
        return update(runs).where(
            runs.c.id == run['id'], runs.c.status == 'running', runs.c.owner == run['owner'],
            runs.c.generation == run['generation'], runs.c.lease_until > now_iso())

    def heartbeat(self, conn: Connection, run: dict, lease_seconds: int) -> bool:
        return conn.execute(self._fenced_update(run)
                            .values(lease_until=now_plus(lease_seconds))
                            .returning(runs.c.id)).first() is not None

    def mark_finished(self, conn: Connection, run: dict, error_code: str | None) -> bool:
        return conn.execute(self._fenced_update(run)
                            .values(status='failed' if error_code else 'succeeded', error_code=error_code)
                            .returning(runs.c.id)).first() is not None

    def set_memory_revision(self, conn: Connection, run_id: str, revision: int) -> None:
        conn.execute(update(runs).where(runs.c.id == run_id).values(memory_revision=revision))
