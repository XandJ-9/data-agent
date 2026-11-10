"""sessions CRUD and ownership guard. Memory-claim columns (memory_*) of the
sessions table are owned by the memory repository; this repo owns identity/CRUD."""
from sqlalchemy import select, update

from errors import ServiceError
from storage.repositories.base import BaseRepository
from storage.schema import sessions


class SessionsRepository(BaseRepository):
    def lock(self, conn, user_id: str, session_id: str) -> dict:
        row = self._row(conn.execute(select(sessions).where(
            sessions.c.id == session_id, sessions.c.user_id == user_id).with_for_update()))
        if row is None:
            raise ServiceError(404, 'Session not found')
        return row

    def own_session(self, conn, user_id: str, session_id: str) -> dict:
        row = self._row(conn.execute(select(sessions).where(sessions.c.id == session_id,
                                                            sessions.c.user_id == user_id)))
        if row is None:
            raise ServiceError(404, 'Session not found')
        return row

    def create(self, conn, session_id: str, user_id: str, title: str, created_at: str) -> dict:
        conn.execute(sessions.insert(), {'id': session_id, 'user_id': user_id, 'title': title,
                                         'created_at': created_at})
        return {'id': session_id, 'title': title, 'created_at': created_at}

    def list_for_user(self, conn, user_id: str, *, archived: bool = False) -> list[dict]:
        stmt = select(sessions.c.id, sessions.c.title, sessions.c.created_at, sessions.c.archived_at).where(
            sessions.c.user_id == user_id)
        if archived:
            stmt = stmt.where(sessions.c.archived_at.is_not(None))
        else:
            stmt = stmt.where(sessions.c.archived_at.is_(None))
        return self._rows(conn.execute(stmt.order_by(sessions.c.created_at.desc(), sessions.c.id).limit(200)))

    def exists_for_user(self, conn, session_id: str, user_id: str) -> bool:
        return conn.execute(select(1).select_from(sessions)
                            .where(sessions.c.id == session_id, sessions.c.user_id == user_id)).first() is not None

    def rename(self, conn, user_id: str, session_id: str, title: str) -> dict:
        row = self._row(conn.execute(
            update(sessions).where(sessions.c.id == session_id, sessions.c.user_id == user_id)
            .values(title=title).returning(sessions)))
        if row is None:
            raise ServiceError(404, 'Session not found')
        return row

    def set_archived(self, conn, user_id: str, session_id: str, archived_at: str | None) -> dict:
        row = self._row(conn.execute(
            update(sessions).where(sessions.c.id == session_id, sessions.c.user_id == user_id)
            .values(archived_at=archived_at).returning(sessions)))
        if row is None:
            raise ServiceError(404, 'Session not found')
        return row
