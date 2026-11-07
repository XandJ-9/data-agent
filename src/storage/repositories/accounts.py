"""users / auth_sessions / login_attempts / admins."""
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import admins, auth_sessions, login_attempts, users


class AccountsRepository(BaseRepository):
    def username_taken(self, conn: Connection, username: str) -> bool:
        return conn.execute(select(1).select_from(users).where(users.c.username == username)).first() is not None

    def insert_user(self, conn: Connection, user_id: str, username: str, password_hash: str) -> None:
        conn.execute(insert(users), {'id': user_id, 'username': username, 'password_hash': password_hash})

    def user_by_username(self, conn: Connection, username: str) -> dict | None:
        return self._row(conn.execute(select(users).where(users.c.username == username)))

    def user_id_by_username(self, conn: Connection, username: str) -> str | None:
        return conn.execute(select(users.c.id).where(users.c.username == username)).scalar()

    def is_admin(self, conn: Connection, user_id: str) -> bool:
        return conn.execute(select(1).select_from(admins)
                            .where(admins.c.user_id == user_id)).first() is not None

    def set_admin(self, conn: Connection, user_id: str, admin: bool) -> None:
        if admin:
            self._insert_ignore(conn, admins, {'user_id': user_id}, conflict_keys=['user_id'])
        else:
            conn.execute(delete(admins).where(admins.c.user_id == user_id))

    def purge_expired_attempts(self, conn: Connection, before: float) -> None:
        conn.execute(delete(login_attempts).where(login_attempts.c.window_start < before))

    def attempt_count(self, conn: Connection, key: str) -> int | None:
        return conn.execute(select(login_attempts.c.attempts).where(login_attempts.c.key == key)).scalar()

    def record_attempt(self, conn: Connection, key: str, now: float) -> bool:
        from sqlalchemy.dialects import postgresql, sqlite
        construct = sqlite.insert if conn.dialect.name == 'sqlite' else postgresql.insert
        statement = construct(login_attempts).values(key=key, attempts=1, window_start=now)
        return conn.execute(statement.on_conflict_do_update(
            index_elements=[login_attempts.c.key],
            set_={'attempts': login_attempts.c.attempts + 1},
            where=login_attempts.c.attempts < 20).returning(login_attempts.c.key)).first() is not None

    def purge_expired_sessions(self, conn: Connection, now: float) -> None:
        conn.execute(delete(auth_sessions).where(auth_sessions.c.expires_at <= now))

    def insert_session(self, conn: Connection, token_hash: str, user_id: str, csrf_token: str,
                       expires_at: float) -> None:
        conn.execute(insert(auth_sessions), {'token_hash': token_hash, 'user_id': user_id,
                                                    'csrf_token': csrf_token, 'expires_at': expires_at})

    def identity_by_token(self, conn: Connection, token_hash: str, now: float) -> dict | None:
        return self._row(conn.execute(
            select(users.c.id, users.c.username, auth_sessions.c.csrf_token,
                   admins.c.user_id.is_not(None).label('is_admin'))
            .select_from(auth_sessions)
            .join(users, users.c.id == auth_sessions.c.user_id)
            .outerjoin(admins, admins.c.user_id == users.c.id)
            .where(auth_sessions.c.token_hash == token_hash, auth_sessions.c.expires_at > now)))

    def delete_session(self, conn: Connection, token_hash: str) -> None:
        conn.execute(delete(auth_sessions).where(auth_sessions.c.token_hash == token_hash))
