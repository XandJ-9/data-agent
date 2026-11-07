"""Engine lifecycle, explicit schema initialization and backend transactions."""
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from storage.schema import SCHEMA_VERSION, _schema_version, metadata

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str | URL | SecretStr):
        raw = url.get_secret_value() if isinstance(url, SecretStr) else url
        try:
            parsed = make_url(raw)
        except (ArgumentError, ValueError):
            raise ValueError('Invalid database URL') from None
        if parsed.drivername in ('postgres', 'postgresql'):
            parsed = parsed.set(drivername='postgresql+psycopg')
        if parsed.drivername not in ('sqlite', 'sqlite+pysqlite', 'postgresql+psycopg'):
            raise ValueError('Only SQLite and PostgreSQL (psycopg) databases are supported')
        self.url = parsed
        self.is_sqlite = parsed.get_backend_name() == 'sqlite'
        self.in_memory = self.is_sqlite and parsed.database in (None, '', ':memory:')
        kwargs = ({'poolclass': StaticPool if self.in_memory else NullPool,
                   'connect_args': {'timeout': 15, 'check_same_thread': False}} if self.is_sqlite else
                  {'poolclass': QueuePool, 'pool_pre_ping': True, 'pool_size': 5,
                   'max_overflow': 10, 'isolation_level': 'READ COMMITTED'})
        self.engine = create_engine(parsed, **kwargs)
        if self.is_sqlite:
            self._install_sqlite_pragmas()

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine, 'connect')
        def on_connect(dbapi_conn, _record):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')
            dbapi_conn.execute('PRAGMA busy_timeout=15000')

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            try:
                if self.is_sqlite:
                    conn.exec_driver_sql('BEGIN IMMEDIATE' if immediate else 'BEGIN')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _check_baseline(self, conn: Connection) -> bool:
        """True when the DB has at least one table and _schema_version exists."""
        tables = set(inspect(conn).get_table_names())
        if not tables:
            return False
        if '_schema_version' not in tables:
            raise RuntimeError('Database is unversioned; initialize a new database without overwriting existing data')
        return True

    def init_db(self) -> None:
        if self.is_sqlite and not self.in_memory:
            self._sqlite_file().parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            if self._check_baseline(conn):
                self._migrate(conn)
            else:
                metadata.create_all(conn)
                conn.execute(_schema_version.insert().values(version=SCHEMA_VERSION))
            self._validate_schema(conn)
        if self.is_sqlite and not self.in_memory:
            with self.engine.connect() as conn:
                conn.exec_driver_sql('PRAGMA journal_mode=WAL')
                conn.commit()
            self._sqlite_file().chmod(0o600)

    def _migrate(self, conn: Connection) -> None:
        current = conn.execute(select(_schema_version.c.version)).scalar()
        if current is None or current >= SCHEMA_VERSION:
            return
        logger.info('Migrating schema from version %s to %s', current, SCHEMA_VERSION)
        if current < 2:
            from sqlalchemy import text as _text
            conn.execute(_text('ALTER TABLE sessions ADD COLUMN archived_at DOUBLE'))
            conn.execute(_schema_version.update().values(version=SCHEMA_VERSION))
        logger.info('Schema migration complete')

    def _validate_schema(self, conn: Connection) -> None:
        tables = set(inspect(conn).get_table_names())
        if not set(metadata.tables).issubset(tables):
            raise RuntimeError('Database schema is incomplete')
        versions = conn.execute(select(_schema_version.c.version)).scalars().all()
        if versions != [SCHEMA_VERSION]:
            raise RuntimeError('Unsupported database schema version; use a compatible application or a new database')

    def check_schema(self) -> None:
        if self.is_sqlite and not self.in_memory and not self._sqlite_file().is_file():
            raise RuntimeError('Initialize the database with data-agent init-db first')
        with self.transaction(immediate=False) as conn:
            self._validate_schema(conn)

    def _sqlite_file(self) -> Path:
        if not self.is_sqlite or self.in_memory:
            raise ValueError('Database does not use a SQLite file')
        return Path(self.url.database)

    def dispose(self) -> None:
        self.engine.dispose()
