"""Storage regression contracts shared with the optional PostgreSQL suite."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, update

from api import create_app
from clock import SENTINEL, now_iso, now_plus
from errors import ServiceError
from services.chat import submit_message
from settings import Settings
from storage import Store
from storage.schema import _schema_version, auth_sessions, binding_codes, runs, sessions


def test_attempt_counter_increments_and_is_bounded(env):
    _, store, _, _ = env
    with store.transaction() as conn:
        for _ in range(20):
            assert store.accounts.record_attempt(conn, 'key', now_iso())
        assert not store.accounts.record_attempt(conn, 'key', now_iso())
        assert store.accounts.attempt_count(conn, 'key') == 20


def test_database_url_settings_and_path(tmp_path):
    path = tmp_path / 'with ? and #.db'
    settings = Settings(_env_file=None, database_url=None, database_path=path)
    store = Store(settings.resolved_database_url)
    try:
        store.init_db()
        store.check_schema()
        assert path.exists()
    finally:
        store.dispose()
    settings = Settings(_env_file=None, database_url='postgresql://user:private-db-password@localhost/test')
    assert 'private-db-password' not in repr(settings)
    assert settings.resolved_database_url.get_secret_value().startswith('postgresql')


def test_init_rejects_unversioned_without_mutation(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE users (id TEXT)')
        conn.execute("INSERT INTO users VALUES ('preserved')")
    store = Store(path)
    with pytest.raises(RuntimeError, match='unversioned'):
        store.init_db()
    assert inspect(store.db.engine).get_table_names() == ['users']
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT id FROM users').fetchone() == ('preserved',)
    store.dispose()


def test_init_rejects_newer_schema_before_creating_tables(tmp_path):
    store = Store(tmp_path / 'future.db')
    _schema_version.create(store.db.engine)
    with store.transaction() as conn:
        conn.execute(_schema_version.insert().values(version=999))
    with pytest.raises(RuntimeError):
        store.init_db()
    assert inspect(store.db.engine).get_table_names() == ['_schema_version']
    store.dispose()


def test_epoch_is_double_precision():
    """Timestamp columns are now Text; this test verifies the schema change is deliberate."""
    from sqlalchemy import Text as SAText
    created_at_col = auth_sessions.c.expires_at
    assert isinstance(created_at_col.type, SAText)


def test_fenced_heartbeat_and_finish(env):
    settings, store, user, _ = env
    sid = store.create_session(user['id'])['id']
    submit_message(store, settings, user['id'], sid, 'question', 'one')
    run = store.claim_run('worker', 60)
    assert store.heartbeat(run, 60)
    assert not store.heartbeat({**run, 'generation': run['generation'] + 1}, 60)
    with store.transaction() as conn:
        conn.execute(update(runs).where(runs.c.id == run['id']).values(lease_until=now_plus(-1)))
    assert not store.heartbeat(run, 60)
    assert not store.finish_run(run, 'stale')
    assert len(store.messages(user['id'], sid)['items']) == 1


def test_binding_conflict_savepoint_preserves_outer_transaction(env):
    _, store, user, other = env
    store.create_binding_code(user['id'], 'first', now_plus(600))
    store.consume_binding_code('first', 'app', 'tenant', 'external')
    store.create_binding_code(other['id'], 'second', now_plus(600))
    with store.transaction() as conn:
        with pytest.raises(ServiceError) as exc:
            store.consume_binding_code_conn(conn, 'second', 'app', 'tenant', 'external')
        assert exc.value.status == 409
        store.sessions.create(conn, 'outer', other['id'], 'still usable', now_iso())
        assert conn.execute(select(binding_codes.c.consumed_at).where(
            binding_codes.c.code_hash == 'second')).scalar_one() is None
    assert store.list_sessions(other['id'])[0]['id'] == 'outer'


def test_outbox_rejects_expired_sender(env):
    _, store, _, _ = env
    with store.transaction() as conn:
        store.feishu.enqueue_reply(conn, 'reply', 'app', 'tenant', 'external', 'hello', 'uuid', now_iso())
        row = store.outbox_replies.claim_batch(conn, 'sender', 10, now_iso(), 1)[0]
        assert not store.outbox_replies.finish(conn, row, 'sent', now=now_plus(30))
        assert store.outbox_replies.finish(conn, row, 'sent', now=now_iso())
        assert not store.outbox_replies.finish(conn, row, 'sent', now=now_iso())


def test_extraction_cas_and_claim_are_atomic(env):
    _, store, user, _ = env
    sid = store.create_session(user['id'])['id']
    claim_value = now_iso()
    with store.transaction() as conn:
        conn.execute(update(sessions).where(sessions.c.id == sid).values(memory_dirty=1, memory_retry_at=claim_value))
        assert not store.memory.apply_extraction(conn, sid, user['id'], '[]', 42, False,
                                                revision=0, cursor=0, claim='2099-01-01 00:00:00')
        assert store.memory.read(conn, user['id'])['revision'] == 0
        assert store.memory.bump_entries(conn, user['id'], '[]', 0)
        assert not store.memory.apply_extraction(conn, sid, user['id'], '[]', 42, False,
                                                revision=0, cursor=0, claim=claim_value)
        session = store.sessions.own_session(conn, user['id'], sid)
        assert session['memory_cursor'] == 0 and session['memory_retry_at'] == claim_value
        assert store.memory.apply_extraction(conn, sid, user['id'], '[]', 42, False,
                                            revision=1, cursor=0, claim=claim_value)
        session = store.sessions.own_session(conn, user['id'], sid)
        assert session['memory_cursor'] == 42 and session['memory_retry_at'] == SENTINEL
        assert store.memory.read(conn, user['id'])['revision'] == 2


def test_api_binding_switch_returns_updated_binding(env):
    settings, store, user, _ = env
    store.create_binding_code(user['id'], 'code', now_plus(600))
    store.consume_binding_code('code', 'app', 'tenant', 'external')
    sid = store.create_session(user['id'])['id']
    with TestClient(create_app(settings)) as client:
        client.headers['Origin'] = settings.public_origin
        response = client.post('/api/v1/auth/login', json={
            'username': 'alice', 'password': 'long-test-password'})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        response = client.put('/api/v1/bindings/feishu/session', json={'session_id': sid})
        assert response.status_code == 200
        assert response.json()['binding']['session_id'] == sid
