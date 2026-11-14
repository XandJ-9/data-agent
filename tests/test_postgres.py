"""Real PostgreSQL READ COMMITTED regressions; opt in with --run-postgres."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import inspect, select

from clock import SENTINEL, now_iso, now_plus
from errors import ServiceError
from services.chat import submit_message
from services.memory import compress_one, get_memory, replace_memory
from storage.schema import binding_codes, deliveries, messages, runs

pytestmark = pytest.mark.postgres


@pytest.fixture
def env(pg_env):
    return pg_env


# The same storage and API contracts must hold on both engines.
from test_storage import (  # noqa: E402, F401
    test_api_binding_switch_returns_updated_binding,
    test_attempt_counter_increments_and_is_bounded,
    test_binding_conflict_savepoint_preserves_outer_transaction,
    test_extraction_cas_and_claim_are_atomic,
    test_fenced_heartbeat_and_finish,
    test_outbox_rejects_expired_sender,
)


def race(fn, count=4):
    barrier = Barrier(count)

    def call(index):
        barrier.wait(timeout=10)
        return fn(index)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(call, range(count)))


def test_pg_schema_fks_defaults_and_precision(env):
    _, store, user, _ = env
    store.check_schema()
    inspector = inspect(store.db.engine)
    assert any(fk['referred_table'] == 'messages' for fk in inspector.get_foreign_keys('runs'))
    assert any(fk['referred_table'] == 'runs' for fk in inspector.get_foreign_keys('messages'))
    with store.transaction() as conn:
        stamp = now_iso()
        conn.execute(runs.insert().values(id='precision', user_id=user['id'],
                     session_id=store.sessions.create(conn, 'sid', user['id'], 'test', stamp)['id'],
                     client_message_id='one', created_at=stamp, definition_hash='hash',
                     skills_snapshot='[]', config_snapshot='{}'))
        row = conn.execute(select(runs).where(runs.c.id == 'precision')).mappings().one()
        assert row['created_at'] == stamp
        assert row['status'] == 'queued' and row['generation'] == 0


def test_pg_submission_idempotency_and_session_order(env):
    settings, store, user, _ = env
    sid = store.create_session(user['id'])['id']
    results = race(lambda _: submit_message(store, settings, user['id'], sid, 'hello', 'same'))
    assert len({r['run_id'] for r in results}) == 1
    submit_message(store, settings, user['id'], sid, 'second', 'two')
    claims = race(lambda i: store.claim_run(str(i), 120))
    claimed = [row for row in claims if row]
    assert len(claimed) == 1
    assert claimed[0]['id'] == results[0]['run_id']
    assert store.finish_run(claimed[0], 'answer')
    assert store.claim_run('next', 120)['input_message_id'] > claimed[0]['input_message_id']


def test_pg_parallel_login_attempts_are_bounded(env):
    _, store, _, _ = env

    def attempt(_):
        with store.transaction() as conn:
            return store.accounts.record_attempt(conn, 'shared', now_iso())

    with ThreadPoolExecutor(8) as pool:
        accepted = list(pool.map(attempt, range(40)))
    assert sum(accepted) == 20
    with store.transaction() as conn:
        assert store.accounts.attempt_count(conn, 'shared') == 20


def test_pg_memory_edit_cas(env):
    _, store, user, _ = env

    def edit(index):
        try:
            replace_memory(store, user['id'], 0, [{'id': str(index), 'text': str(index),
                                                 'source_message_ids': []}])
            return 200
        except ServiceError as exc:
            return exc.status

    assert sorted(race(edit)) == [200, 409, 409, 409]


def test_pg_parallel_extraction_cas_preserves_losing_cursor(env):
    settings, store, user, _ = env
    session_ids = [store.create_session(user['id'])['id'] for _ in range(2)]
    for sid in session_ids:
        submit_message(store, settings, user['id'], sid, 'question', 'one')
        assert store.finish_run(store.claim_run('worker', 120), 'answer')
    barrier = Barrier(2)

    class Runtime:
        def extract_memory(self, **kwargs):
            barrier.wait(timeout=10)
            return [{'text': 'fact', 'source_message_ids': [kwargs['messages'][0]['id']]}]

    assert sorted(race(lambda _: compress_one(store, Runtime()), 2)) == [False, True]
    assert get_memory(store, user['id'])['revision'] == 1
    with store.transaction() as conn:
        rows = [store.sessions.own_session(conn, user['id'], sid) for sid in session_ids]
        assert sum(row['memory_cursor'] > 0 for row in rows) == 1
        assert all(row['memory_retry_at'] == SENTINEL for row in rows)


def test_pg_binding_conflict_does_not_burn_code(env):
    _, store, user, other = env
    for index, account in enumerate((user, other)):
        store.create_binding_code(account['id'], str(index), now_plus(600))

    def bind(index):
        with store.transaction() as conn:
            try:
                store.consume_binding_code_conn(conn, str(index), 'app', 'tenant', 'external')
                return index, 200
            except ServiceError as exc:
                # Savepoint must keep both the code and outer transaction usable.
                assert conn.execute(select(binding_codes.c.consumed_at).where(
                    binding_codes.c.code_hash == str(index))).scalar_one() is None
                store.sessions.create(conn, f'outer-{index}', (user, other)[index]['id'], 'outer', now_iso())
                return index, exc.status

    results = race(bind, 2)
    assert sorted(status for _, status in results) == [200, 409]


def test_pg_finish_cancel_race_has_no_partial_reply(env):
    settings, store, user, _ = env
    sid = store.create_session(user['id'])['id']
    submit_message(store, settings, user['id'], sid, 'hello', 'one')
    run = store.claim_run('worker', 120)
    race(lambda index: store.finish_run(run, 'answer') if index == 0 else
         store.cancel_run(user['id'], run['id']), 2)
    with store.transaction() as conn:
        status = store.runs.get(conn, user['id'], run['id'])['status']
        replies = conn.execute(select(messages).where(messages.c.run_id == run['id'],
                                                       messages.c.role == 'assistant')).all()
    assert (status, len(replies)) in [('cancelled', 0), ('succeeded', 1)]
    assert not store.heartbeat(run, 120)


def test_pg_outbox_claims_do_not_overlap(env):
    settings, store, user, _ = env
    store.create_binding_code(user['id'], 'code', now_plus(600))
    binding = store.consume_binding_code('code', 'app', 'tenant', 'external')
    for index in range(4):
        submit_message(store, settings, user['id'], binding['session_id'], 'hello', str(index),
                       source='feishu', binding_id=binding['id'], reply_target='external')
        assert store.finish_run(store.claim_run('worker', 120), 'answer')
    def claim(index):
        with store.transaction() as conn:
            return store.outbox_deliveries.claim_batch(conn, str(index), 60, now_iso(), 1)
    claims = race(claim)
    ids = [row['id'] for batch in claims for row in batch]
    assert len(ids) == len(set(ids))
    assert ids
    with store.transaction() as conn:
        assert len(conn.execute(select(deliveries)).all()) == 4


def test_pg_webhook_dedupes_across_instances(env):
    settings, store, user, _ = env
    from test_feishu import APP_ID, OPEN_ID, TENANT, make_event

    from channels.feishu.webhook import FeishuModule

    settings = settings.model_copy(update={'feishu_app_id': APP_ID})
    store.create_binding_code(user['id'], 'code', now_plus(600))
    store.consume_binding_code('code', APP_ID, TENANT, OPEN_ID)
    race(lambda index: FeishuModule(store, settings).ingest(make_event(f'event-{index}', 'message', 'hello')))
    with store.transaction() as conn:
        assert len(conn.execute(select(runs)).all()) == 1
    # Identical command events must also create just one reply and one new session.
    race(lambda _: FeishuModule(store, settings).ingest(make_event('new-event', 'new-message', '/new')))
    assert len(store.list_sessions(user['id'])) == 2
