"""SQLite persistence. init_db is explicit; transaction() is the integration boundary.

Channel adapters use consume_binding_code(), binding(), and chat.submit_message()
with source='feishu', binding_id and reply_target. Successful runs atomically create
pending deliveries. Adapters own sending/receipt reconciliation, never rerun Agent.
"""
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def identifier() -> str:
    return str(uuid.uuid4())


MIGRATION_1 = """
CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 csrf_token TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE login_attempts(key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_start REAL NOT NULL);
CREATE TABLE sessions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 created_at REAL NOT NULL, memory_cursor INTEGER NOT NULL DEFAULT 0, memory_dirty INTEGER NOT NULL DEFAULT 0,
 memory_retry_at REAL NOT NULL DEFAULT 0, memory_failures INTEGER NOT NULL DEFAULT 0,
 memory_error TEXT);
CREATE TABLE runs(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), input_message_id INTEGER REFERENCES messages(id),
 client_message_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error_code TEXT,
 created_at REAL NOT NULL, owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_until REAL,
 definition_hash TEXT NOT NULL, skills_snapshot TEXT NOT NULL, config_snapshot TEXT NOT NULL,
 memory_revision INTEGER, source TEXT NOT NULL DEFAULT 'web', binding_id TEXT REFERENCES channel_identities(id),
 reply_target TEXT, UNIQUE(user_id,session_id,client_message_id));
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT NOT NULL REFERENCES runs(id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(run_id,role));
CREATE INDEX messages_session_order ON messages(session_id,id);
CREATE INDEX runs_queue ON runs(status,input_message_id);
CREATE TABLE user_memories(user_id TEXT PRIMARY KEY REFERENCES users(id), revision INTEGER NOT NULL DEFAULT 0,
 entries TEXT NOT NULL DEFAULT '[]', excluded_before INTEGER NOT NULL DEFAULT 0);
CREATE TABLE memory_exclusions(user_id TEXT NOT NULL REFERENCES users(id), message_id INTEGER NOT NULL
 REFERENCES messages(id), PRIMARY KEY(user_id,message_id));
CREATE TABLE user_skills(user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 enabled INTEGER NOT NULL, PRIMARY KEY(user_id,name));
CREATE TABLE channel_identities(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 channel TEXT NOT NULL DEFAULT 'feishu', app_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 external_user_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
 active INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX channel_external_active ON channel_identities(channel,app_id,tenant_id,external_user_id)
 WHERE active=1;
CREATE UNIQUE INDEX channel_user_active ON channel_identities(user_id,channel) WHERE active=1;
CREATE TABLE binding_codes(code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 expires_at REAL NOT NULL, consumed_at REAL);
CREATE TABLE deliveries(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 binding_id TEXT NOT NULL REFERENCES channel_identities(id), reply_target TEXT NOT NULL,
 content TEXT NOT NULL, part INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
 receipt TEXT, error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_until REAL,
 UNIQUE(run_id,part));
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """Apply numbered migrations only on explicit administrative invocation."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Channel schema lives with its owning module; the migration chain stays single.
        from data_agent.feishu import FEISHU_SCHEMA
        conn = self.connect()
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            # executescript commits a pending transaction, so include BEGIN in script.
            conn.execute('BEGIN EXCLUSIVE')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Database schema is newer than this application')
            if version == 0:
                for statement in MIGRATION_1.split(';'):
                    if statement.strip():
                        conn.execute(statement)
                for statement in FEISHU_SCHEMA.split(';'):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute('PRAGMA user_version=1')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.path.chmod(0o600)

    def check_schema(self) -> None:
        if not self.path.is_file():
            raise RuntimeError('Initialize the database with data-agent init-db first')
        with self.transaction(immediate=False) as conn:
            if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise RuntimeError('Run data-agent init-db to migrate the database')

    @staticmethod
    def own_session(conn: sqlite3.Connection, user_id: str, session_id: str) -> dict:
        row = conn.execute('SELECT * FROM sessions WHERE id=? AND user_id=?', (session_id, user_id)).fetchone()
        if row is None:
            raise ServiceError(404, 'Session not found')
        return dict(row)

    def create_session(self, user_id: str, title: str = 'New conversation') -> dict:
        result = {'id': identifier(), 'title': title, 'created_at': time.time()}
        with self.transaction() as conn:
            conn.execute('INSERT INTO sessions(id,user_id,title,created_at) VALUES(?,?,?,?)',
                         (result['id'], user_id, title, result['created_at']))
        return result

    def list_sessions(self, user_id: str) -> list[dict]:
        with self.transaction(immediate=False) as conn:
            return [dict(r) for r in conn.execute(
                'SELECT id,title,created_at FROM sessions WHERE user_id=? ORDER BY created_at DESC,id LIMIT 200',
                (user_id,))]

    def messages(self, user_id: str, session_id: str, before_id: int | None = None, limit: int = 100) -> dict:
        with self.transaction(immediate=False) as conn:
            self.own_session(conn, user_id, session_id)
            rows = conn.execute('SELECT id,role,content,created_at,run_id FROM messages WHERE session_id=? '
                                'AND id<? ORDER BY id DESC LIMIT ?',
                                (session_id, before_id or 9223372036854775807, min(limit, 100) + 1)).fetchall()
            page = rows[:min(limit, 100)]
            run_ids = {r['run_id'] for r in page}
            runs = [self._public_run(conn, r) for r in conn.execute(
                'SELECT * FROM runs WHERE session_id=? ORDER BY input_message_id DESC LIMIT 100', (session_id,))
                    if r['id'] in run_ids]
            return {'items': [dict(r) for r in reversed(page)], 'runs': runs,
                    'next_before_id': page[-1]['id'] if len(rows) > len(page) else None}

    @staticmethod
    def _public_run(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        return {**{k: row[k] for k in ('id', 'status', 'error_code', 'created_at')},
                'deliveries': [dict(r) for r in conn.execute(
                    'SELECT id,status FROM deliveries WHERE run_id=? ORDER BY part', (row['id'],))]}

    def get_run(self, user_id: str, run_id: str) -> dict:
        with self.transaction(immediate=False) as conn:
            row = conn.execute('SELECT * FROM runs WHERE id=? AND user_id=?', (run_id, user_id)).fetchone()
            if row is None:
                raise ServiceError(404, 'Run not found')
            return self._public_run(conn, row)

    def cancel_run(self, user_id: str, run_id: str) -> dict:
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET status='cancelled',generation=generation+1 WHERE id=? AND user_id=? "
                         "AND status IN ('queued','running')", (run_id, user_id))
        return self.get_run(user_id, run_id)

    def claim_run(self, owner: str, lease_seconds: int) -> dict | None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET status='interrupted',error_code='worker_lost',generation=generation+1 "
                         "WHERE status='running' AND lease_until<=?", (now,))
            row = conn.execute("SELECT r.* FROM runs r WHERE r.status='queued' AND NOT EXISTS "
                               "(SELECT 1 FROM runs p WHERE p.session_id=r.session_id AND "
                               "(p.status='running' OR (p.status='queued' AND p.input_message_id<r.input_message_id))) "
                               "ORDER BY r.input_message_id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE runs SET status='running',owner=?,generation=generation+1,lease_until=? WHERE id=?",
                         (owner, now + lease_seconds, row['id']))
            return dict(conn.execute('SELECT * FROM runs WHERE id=?', (row['id'],)).fetchone())

    @staticmethod
    def fence(conn: sqlite3.Connection, run: dict) -> bool:
        return conn.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND owner=? AND generation=? "
                            'AND lease_until>?',
                            (run['id'], run['owner'], run['generation'], time.time())).fetchone() is not None

    def heartbeat(self, run: dict, lease_seconds: int) -> bool:
        with self.transaction() as conn:
            if not self.fence(conn, run):
                return False
            conn.execute('UPDATE runs SET lease_until=? WHERE id=?', (time.time() + lease_seconds, run['id']))
            return True

    def finish_run(self, run: dict, content: str | None = None, error_code: str | None = None) -> bool:
        with self.transaction() as conn:
            if not self.fence(conn, run):
                return False
            status = 'failed' if error_code else 'succeeded'
            conn.execute('UPDATE runs SET status=?,error_code=? WHERE id=?', (status, error_code, run['id']))
            if error_code:
                return True
            conn.execute('INSERT INTO messages(user_id,session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?)',
                         (run['user_id'], run['session_id'], run['id'], 'assistant', content, time.time()))
            conn.execute('UPDATE sessions SET memory_dirty=1 WHERE id=?', (run['session_id'],))
            if run['source'] == 'feishu':
                conn.execute('INSERT INTO deliveries(id,run_id,binding_id,reply_target,content) VALUES(?,?,?,?,?)',
                             (identifier(), run['id'], run['binding_id'], run['reply_target'], content))
            return True

    def binding(self, user_id: str) -> dict | None:
        with self.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM channel_identities WHERE user_id=? AND channel='feishu' AND active=1",
                               (user_id,)).fetchone()
            return dict(row) if row else None

    def consume_binding_code(self, code_hash: str, app_id: str, tenant_id: str, external_user_id: str) -> dict:
        """Trusted channel adapter supplies SHA-256 code digest and verified identity."""
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM binding_codes WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?',
                               (code_hash, time.time())).fetchone()
            if row is None:
                raise ServiceError(400, 'Binding code invalid or expired')
            user_id = row['user_id']
            conflict = conn.execute('SELECT 1 FROM channel_identities WHERE active=1 AND '
                                    '(user_id=? OR (app_id=? AND tenant_id=? AND external_user_id=?))',
                                    (user_id, app_id, tenant_id, external_user_id)).fetchone()
            if conflict:
                raise ServiceError(409, 'Account already bound')
            sid, bid = identifier(), identifier()
            conn.execute('INSERT INTO sessions(id,user_id,title,created_at) VALUES(?,?,?,?)',
                         (sid, user_id, 'Feishu conversation', time.time()))
            conn.execute('INSERT INTO channel_identities(id,user_id,app_id,tenant_id,external_user_id,session_id) '
                         'VALUES(?,?,?,?,?,?)', (bid, user_id, app_id, tenant_id, external_user_id, sid))
            conn.execute('UPDATE binding_codes SET consumed_at=? WHERE code_hash=?', (time.time(), code_hash))
            return dict(conn.execute('SELECT * FROM channel_identities WHERE id=?', (bid,)).fetchone())

    def enabled_skills(self, user_id: str) -> dict[str, bool]:
        with self.transaction(immediate=False) as conn:
            return {r['name']: bool(r['enabled']) for r in conn.execute(
                'SELECT name,enabled FROM user_skills WHERE user_id=?', (user_id,))}

    def set_skill(self, user_id: str, name: str, enabled: bool) -> None:
        with self.transaction() as conn:
            conn.execute('INSERT INTO user_skills VALUES(?,?,?) ON CONFLICT(user_id,name) '
                         'DO UPDATE SET enabled=excluded.enabled', (user_id, name, int(enabled)))
