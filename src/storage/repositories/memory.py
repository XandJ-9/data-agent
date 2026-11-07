"""user_memories / memory_exclusions, plus the memory-claim columns on sessions
(memory_cursor/dirty/retry_at/failures/error) which belong to this use case."""
from sqlalchemy import case, insert, select, update
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import memory_exclusions, sessions, user_memories


class MemoryRepository(BaseRepository):
    # ------------------------------------------------------------ user_memories

    def insert_default(self, conn: Connection, user_id: str) -> None:
        conn.execute(insert(user_memories), {'user_id': user_id})

    def read(self, conn: Connection, user_id: str) -> dict:
        row = conn.execute(select(user_memories.c.revision, user_memories.c.entries)
                           .where(user_memories.c.user_id == user_id)).mappings().one()
        return dict(row)

    def bump_entries(self, conn: Connection, user_id: str, entries_json: str, revision: int) -> bool:
        return conn.execute(update(user_memories).where(
            user_memories.c.user_id == user_id, user_memories.c.revision == revision)
            .values(entries=entries_json, revision=user_memories.c.revision + 1)
            .returning(user_memories.c.user_id)).first() is not None

    def advance_cutoff(self, conn: Connection, user_id: str, cutoff: int) -> None:
        # Portable scalar max: MAX(a,b) exists in SQLite but PostgreSQL needs GREATEST.
        conn.execute(update(user_memories).where(user_memories.c.user_id == user_id)
                     .values(excluded_before=case(
                         (user_memories.c.excluded_before < cutoff, cutoff),
                         else_=user_memories.c.excluded_before)))

    # ------------------------------------------------------------ exclusions

    def add_exclusion(self, conn: Connection, user_id: str, message_id: int) -> None:
        self._insert_ignore(conn, memory_exclusions, {'user_id': user_id, 'message_id': message_id},
                            conflict_keys=['user_id', 'message_id'])

    def exclusion_ids(self, conn: Connection, user_id: str) -> set[int]:
        return set(conn.execute(select(memory_exclusions.c.message_id)
                                .where(memory_exclusions.c.user_id == user_id)).scalars())

    # ------------------------------------------------------------ session claim fields

    def claim_batch(self, conn: Connection, now: float) -> dict | None:
        """Oldest due dirty session joined with its memory row; claims it via retry_at."""
        row = conn.execute(
            select(sessions, user_memories.c.revision, user_memories.c.entries, user_memories.c.excluded_before)
            .join(user_memories, user_memories.c.user_id == sessions.c.user_id)
            .where(sessions.c.memory_dirty == 1, sessions.c.memory_retry_at <= now)
            .order_by(sessions.c.memory_retry_at, sessions.c.created_at).limit(1)
            .with_for_update(of=sessions, skip_locked=True)).mappings().first()
        if row is None:
            return None
        return dict(row)

    def clear_dirty(self, conn: Connection, session_id: str) -> None:
        conn.execute(update(sessions).where(sessions.c.id == session_id).values(memory_dirty=0))

    def mark_dirty(self, conn: Connection, session_id: str) -> None:
        conn.execute(update(sessions).where(sessions.c.id == session_id).values(memory_dirty=1))

    def set_claim(self, conn: Connection, session_id: str, claim: float, previous: float) -> bool:
        return conn.execute(update(sessions).where(
            sessions.c.id == session_id, sessions.c.memory_dirty == 1,
            sessions.c.memory_retry_at == previous).values(memory_retry_at=claim)
            .returning(sessions.c.id)).first() is not None

    def reset_claim(self, conn: Connection, session_id: str, claim: float) -> None:
        conn.execute(update(sessions)
                     .where(sessions.c.id == session_id, sessions.c.memory_retry_at == claim)
                     .values(memory_retry_at=0))

    def advance_cursor(self, conn: Connection, session_id: str, end: int, claim: float) -> None:
        """Advance cursor and clear claim without touching memory entries (no new facts found)."""
        conn.execute(update(sessions)
                     .where(sessions.c.id == session_id, sessions.c.memory_retry_at == claim)
                     .values(memory_cursor=end, memory_dirty=0, memory_retry_at=0,
                             memory_failures=0, memory_error=None))

    def apply_extraction(self, conn: Connection, session_id: str, user_id: str, entries_json: str,
                         end: int, dirty: bool, *, revision: int, cursor: int, claim: float) -> bool:
        session = conn.execute(select(sessions.c.id).where(
            sessions.c.id == session_id, sessions.c.user_id == user_id,
            sessions.c.memory_cursor == cursor, sessions.c.memory_retry_at == claim)
            .with_for_update()).first()
        if session is None or not self.bump_entries(conn, user_id, entries_json, revision):
            return False
        conn.execute(update(sessions).where(sessions.c.id == session_id)
                     .values(memory_cursor=end, memory_dirty=int(dirty), memory_retry_at=0,
                             memory_failures=0, memory_error=None))
        return True

    def register_failure(self, conn: Connection, session_id: str, error: str, retry_at: float,
                         claim: float) -> None:
        conn.execute(update(sessions)
                     .where(sessions.c.id == session_id, sessions.c.memory_retry_at == claim)
                     .values(memory_failures=sessions.c.memory_failures + 1,
                             memory_error=error, memory_retry_at=retry_at))
