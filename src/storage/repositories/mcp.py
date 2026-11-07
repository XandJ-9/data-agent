"""mcp_servers: admin-managed external tool sources."""
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import mcp_servers


class McpRepository(BaseRepository):
    def list(self, conn: Connection, *, enabled_only: bool = False) -> list[dict]:
        statement = select(mcp_servers.c.id, mcp_servers.c.name, mcp_servers.c.transport,
                           mcp_servers.c.command, mcp_servers.c.args_json, mcp_servers.c.env_json,
                           mcp_servers.c.url, mcp_servers.c.headers_json, mcp_servers.c.enabled,
                           mcp_servers.c.created_at).order_by(mcp_servers.c.name)
        if enabled_only:
            statement = statement.where(mcp_servers.c.enabled == 1)
        return self._rows(conn.execute(statement))

    def by_name(self, conn: Connection, name: str) -> dict | None:
        return self._row(conn.execute(select(mcp_servers).where(mcp_servers.c.name == name)))

    def upsert(self, conn: Connection, fields: dict) -> None:
        self._upsert(conn, mcp_servers, fields,
                     conflict_keys=['name'],
                     update_keys=['transport', 'command', 'args_json', 'env_json',
                                  'url', 'headers_json', 'enabled'])

    def set_enabled(self, conn: Connection, name: str, enabled: bool) -> bool:
        cursor = conn.execute(update(mcp_servers).where(mcp_servers.c.name == name)
                              .values(enabled=int(enabled)))
        return cursor.rowcount == 1

    def delete(self, conn: Connection, name: str) -> bool:
        return conn.execute(delete(mcp_servers).where(mcp_servers.c.name == name)).rowcount == 1
