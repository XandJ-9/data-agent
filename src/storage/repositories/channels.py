"""channel_identities / binding_codes: cross-channel identity binding.

Binding codes are stored only as hashes. Compose inside the caller's
transaction; concurrent double-bind is rejected by the partial unique
indexes (surfacing as IntegrityError at the facade).
"""
from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import binding_codes, channel_identities


class ChannelsRepository(BaseRepository):
    # ------------------------------------------------------------ binding codes

    def purge_codes(self, conn: Connection, user_id: str) -> None:
        conn.execute(binding_codes.delete().where(binding_codes.c.user_id == user_id))

    def create_code(self, conn: Connection, code_hash: str, user_id: str, expires_at: float) -> None:
        conn.execute(binding_codes.insert(), {'code_hash': code_hash, 'user_id': user_id,
                                              'expires_at': expires_at})

    def find_code(self, conn: Connection, code_hash: str, now: float) -> dict | None:
        return self._row(conn.execute(
            select(binding_codes).where(binding_codes.c.code_hash == code_hash,
                                        binding_codes.c.consumed_at.is_(None),
                                        binding_codes.c.expires_at > now)))

    def consume_code(self, conn: Connection, code_hash: str, now: float) -> dict | None:
        return self._row(conn.execute(update(binding_codes).where(
            binding_codes.c.code_hash == code_hash, binding_codes.c.consumed_at.is_(None),
            binding_codes.c.expires_at > now).values(consumed_at=now).returning(binding_codes)))

    # ------------------------------------------------------------ identities

    def create(self, conn: Connection, binding_id: str, user_id: str, app_id: str, tenant_id: str,
               external_user_id: str, session_id: str) -> None:
        conn.execute(channel_identities.insert(), {
            'id': binding_id, 'user_id': user_id, 'app_id': app_id, 'tenant_id': tenant_id,
            'external_user_id': external_user_id, 'session_id': session_id})

    def by_id(self, conn: Connection, binding_id: str) -> dict | None:
        return self._row(conn.execute(select(channel_identities).where(channel_identities.c.id == binding_id)))

    def active_conflict(self, conn: Connection, user_id: str, app_id: str, tenant_id: str,
                        external_user_id: str) -> bool:
        return conn.execute(
            select(1).select_from(channel_identities).where(
                channel_identities.c.active == 1,
                (channel_identities.c.user_id == user_id)
                | ((channel_identities.c.app_id == app_id)
                   & (channel_identities.c.tenant_id == tenant_id)
                   & (channel_identities.c.external_user_id == external_user_id)))).first() is not None

    def binding_for_user(self, conn: Connection, user_id: str, channel: str) -> dict | None:
        return self._row(conn.execute(select(channel_identities).where(
            channel_identities.c.user_id == user_id, channel_identities.c.channel == channel,
            channel_identities.c.active == 1)))

    def active_identity(self, conn: Connection, app_id: str, tenant_id: str, external_user_id: str) -> dict | None:
        return self._row(conn.execute(
            select(channel_identities.c.id, channel_identities.c.user_id, channel_identities.c.session_id,
                   channel_identities.c.tenant_id, channel_identities.c.external_user_id)
            .where(channel_identities.c.app_id == app_id, channel_identities.c.tenant_id == tenant_id,
                   channel_identities.c.external_user_id == external_user_id, channel_identities.c.active == 1)))

    def get_active(self, conn: Connection, binding_id: str) -> dict | None:
        return self._row(conn.execute(select(channel_identities.c.active, channel_identities.c.external_user_id)
                                      .where(channel_identities.c.id == binding_id)))

    def identity_of_binding(self, conn: Connection, binding_id: str, user_id: str, session_id: str) -> bool:
        """True if the binding is active and belongs to user/session (submit guard)."""
        return conn.execute(
            select(1).select_from(channel_identities).where(
                channel_identities.c.id == binding_id, channel_identities.c.user_id == user_id,
                channel_identities.c.session_id == session_id, channel_identities.c.active == 1)
        ).first() is not None

    def set_session(self, conn: Connection, binding_id: str, session_id: str) -> None:
        conn.execute(update(channel_identities).where(channel_identities.c.id == binding_id)
                     .values(session_id=session_id))

    def set_session_for_user(self, conn: Connection, user_id: str, session_id: str) -> int:
        result = conn.execute(update(channel_identities)
                              .where(channel_identities.c.user_id == user_id,
                                     channel_identities.c.active == 1)
                              .values(session_id=session_id))
        return result.rowcount

    def deactivate(self, conn: Connection, binding_id: str) -> None:
        conn.execute(update(channel_identities)
                     .where(channel_identities.c.id == binding_id, channel_identities.c.active == 1)
                     .values(active=0))

    def deactivate_for_user(self, conn: Connection, user_id: str) -> None:
        conn.execute(update(channel_identities).where(channel_identities.c.user_id == user_id)
                     .values(active=0))
