"""messages: append-only conversation content. Writes happen only via insert()."""
from sqlalchemy import case, func, insert, select
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import messages, runs

PAGE_LIMIT = 100


class MessagesRepository(BaseRepository):
    def insert(self, conn: Connection, user_id: str, session_id: str, run_id: str, role: str,
               content: str, created_at: float) -> int:
        result = conn.execute(insert(messages), {
            'user_id': user_id, 'session_id': session_id, 'run_id': run_id,
            'role': role, 'content': content, 'created_at': created_at})
        return result.inserted_primary_key[0]

    def page(self, conn: Connection, session_id: str, before_id: int | None,
             limit: int) -> tuple[list[dict], int | None]:
        """Returns (page oldest-first, next_before_id or None)."""
        bound = min(limit, PAGE_LIMIT)
        statement = select(messages.c.id, messages.c.role, messages.c.content,
                           messages.c.created_at, messages.c.run_id).where(messages.c.session_id == session_id)
        max_id = 2 ** (31 if conn.dialect.name == 'postgresql' else 63) - 1
        if before_id is not None and before_id <= max_id:
            statement = statement.where(messages.c.id < before_id)
        rows = conn.execute(statement.order_by(messages.c.id.desc()).limit(bound + 1)).mappings().all()
        page = [dict(r) for r in rows[:bound]]
        next_before = page[-1]['id'] if len(rows) > bound else None
        return list(reversed(page)), next_before

    def by_run_ordered(self, conn: Connection, run_id: str) -> list[dict]:
        return self._rows(conn.execute(
            select(messages.c.id, messages.c.role, messages.c.content)
            .where(messages.c.run_id == run_id)
            .order_by(case((messages.c.role == 'user', 0), else_=1))))

    def by_id(self, conn: Connection, message_id: int) -> dict | None:
        return self._row(conn.execute(select(messages.c.role, messages.c.content)
                                      .where(messages.c.id == message_id)))

    def exists_for_user(self, conn: Connection, message_id: int, user_id: str) -> bool:
        return conn.execute(select(1).select_from(messages)
                            .where(messages.c.id == message_id, messages.c.user_id == user_id)).first() is not None

    def search(self, conn: Connection, user_id: str, pattern: str, max_input_message_id: int) -> list[dict]:
        return self._rows(conn.execute(
            select(messages.c.id, messages.c.role, func.substr(messages.c.content, 1, 1500).label('content'))
            .join(runs, runs.c.id == messages.c.run_id)
            .where(messages.c.user_id == user_id,
                   messages.c.content.like(pattern, escape='\\'),
                   runs.c.input_message_id <= max_input_message_id)
            .order_by(messages.c.id.desc()).limit(10)))

    def max_id_for_user(self, conn: Connection, user_id: str) -> int:
        return conn.execute(select(func.coalesce(func.max(messages.c.id), 0))
                            .where(messages.c.user_id == user_id)).scalar_one()

    def has_succeeded_after(self, conn: Connection, session_id: str, after_message_id: int) -> bool:
        return conn.execute(select(1).select_from(messages).join(runs, runs.c.id == messages.c.run_id)
                            .where(messages.c.session_id == session_id, messages.c.id > after_message_id,
                                   messages.c.role == 'assistant', runs.c.status == 'succeeded')
                            .limit(1)).first() is not None
