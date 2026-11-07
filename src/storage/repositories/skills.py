"""user_skills enablement."""
from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import user_skills


class SkillsRepository(BaseRepository):
    def enabled_map(self, conn: Connection, user_id: str) -> dict[str, bool]:
        return {r['name']: bool(r['enabled']) for r in self._rows(
            conn.execute(select(user_skills.c.name, user_skills.c.enabled)
                         .where(user_skills.c.user_id == user_id)))}

    def set(self, conn: Connection, user_id: str, name: str, enabled: bool) -> None:
        self._upsert(conn, user_skills, {'user_id': user_id, 'name': name, 'enabled': int(enabled)},
                     conflict_keys=['user_id', 'name'], update_keys=['enabled'])
