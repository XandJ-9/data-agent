"""Repository base: repos take an open connection and never manage transactions.

Transaction boundaries live in the services layer (or the Store facade for
single-repo conveniences); a repository method must stay usable inside a
caller's transaction for multi-repo composition. Rows cross repo boundaries
as plain dicts.
"""
from sqlalchemy.engine import Connection


class BaseRepository:
    @staticmethod
    def _row(result) -> dict | None:
        """First row as dict, or None."""
        row = result.mappings().first()
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(result) -> list[dict]:
        return [dict(r) for r in result.mappings()]

    @staticmethod
    def _upsert(conn: Connection, table, values: dict, *, conflict_keys: list[str],
                update_keys: list[str]) -> None:
        """Dialect-agnostic INSERT ... ON CONFLICT DO UPDATE (SQLite and PostgreSQL)."""
        from sqlalchemy.dialects import postgresql, sqlite
        construct = sqlite.insert if conn.dialect.name == 'sqlite' else postgresql.insert
        statement = construct(table).values(**values).on_conflict_do_update(
            index_elements=[table.c[k] for k in conflict_keys],
            set_={table.c[k]: values[k] for k in update_keys})
        conn.execute(statement)

    @staticmethod
    def _insert_ignore(conn: Connection, table, values: dict, *, conflict_keys: list[str]) -> None:
        """Dialect-agnostic INSERT ... ON CONFLICT DO NOTHING."""
        from sqlalchemy.dialects import postgresql, sqlite
        construct = sqlite.insert if conn.dialect.name == 'sqlite' else postgresql.insert
        conn.execute(construct(table).values(**values)
                     .on_conflict_do_nothing(index_elements=conflict_keys))
