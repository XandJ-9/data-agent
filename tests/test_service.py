import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from data_agent.api import create_app
from data_agent.auth import create_user
from data_agent.chat import submit_message
from data_agent.db import ServiceError, Store
from data_agent.memory import compress_one, get_memory, replace_memory
from data_agent.settings import Settings
from data_agent.worker import Worker


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_path=tmp_path / 'test.db', skills_path=tmp_path / 'skills',
                        public_origin='http://testserver')
    store = Store(settings.database_path)
    store.init_db()
    user = create_user(store, 'alice', 'long-test-password')
    other = create_user(store, 'bob', 'another-password')
    return settings, store, user, other


class FakeRuntime:
    def __init__(self):
        self.prompts = []

    def respond(self, **kwargs):
        self.prompts.append(kwargs)
        return 'answer'

    def extract_memory(self, **kwargs):
        return [{'text': 'prefers tests', 'source_message_ids': [kwargs['messages'][0]['id']]}]


def session(store, user):
    return store.create_session(user['id'], 'test')['id']


def enqueue(settings, store, user, sid, text='hello', key='k'):
    return submit_message(store, settings, user['id'], sid, text, key)


def login(client):
    client.headers['Origin'] = 'http://testserver'
    response = client.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'long-test-password'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response.json()


def test_auth_origin_csrf_and_ownership(env):
    settings, store, user, other = env
    client = TestClient(create_app(settings))
    bad = client.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'long-test-password'})
    assert bad.status_code == 403
    login(client)
    assert client.get('/api/v1/auth/me').json()['user']['id'] == user['id']
    foreign = session(store, other)
    assert client.get(f'/api/v1/sessions/{foreign}/messages').status_code == 404
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/sessions', json={}).status_code == 403
    assert 'password' not in str(client.get('/api/v1/auth/me').json())


def test_atomic_dedupe_and_serial_claim(env):
    settings, store, user, _ = env
    sid = session(store, user)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: enqueue(settings, store, user, sid), range(8)))
    assert len({r['run_id'] for r in results}) == 1
    enqueue(settings, store, user, sid, 'future', 'second')
    first = store.claim_run('worker-a', 120)
    assert first['id'] == results[0]['run_id']
    assert store.claim_run('worker-b', 120) is None
    assert store.finish_run(first, 'answer')
    assert store.claim_run('worker-b', 120)['input_message_id'] > first['input_message_id']


def test_cancel_and_expired_worker_cannot_commit(env):
    settings, store, user, _ = env
    sid = session(store, user)
    item = enqueue(settings, store, user, sid)
    run = store.claim_run('old', 120)
    store.cancel_run(user['id'], item['run_id'])
    assert not store.finish_run(run, 'late')
    enqueue(settings, store, user, sid, key='second')
    run = store.claim_run('old', 120)
    with store.transaction() as conn:
        conn.execute('UPDATE runs SET lease_until=? WHERE id=?', (time.time() - 1, run['id']))
    assert store.claim_run('new', 120) is None
    assert store.get_run(user['id'], run['id'])['status'] == 'interrupted'
    assert not store.finish_run(run, 'late')


def test_worker_prompt_excludes_future_and_memory_preserves_raw(env):
    settings, store, user, _ = env
    sid = session(store, user)
    enqueue(settings, store, user, sid, 'first')
    enqueue(settings, store, user, sid, 'future', 'second')
    runtime = FakeRuntime()
    assert Worker(settings, runtime=runtime).run_once()
    assert runtime.prompts[0]['messages'] == [{'role': 'user', 'content': 'first'}]
    assert compress_one(store, runtime)
    memory = get_memory(store, user['id'])
    assert memory['revision'] == 1
    assert memory['entries'][0]['text'] == 'prefers tests'
    assert len(store.messages(user['id'], sid)['items']) == 3
    assert not compress_one(store, runtime)


def test_memory_cas_and_exclusion_before_extraction(env):
    settings, store, user, _ = env
    sid = session(store, user)
    enqueue(settings, store, user, sid)
    Worker(settings, runtime=FakeRuntime()).run_once()
    original = get_memory(store, user['id'])
    replace_memory(store, user['id'], 0, [{'id': 'manual', 'text': 'manual', 'source_message_ids': []}])
    with pytest.raises(ServiceError):
        replace_memory(store, user['id'], original['revision'], [])
    class EditingRuntime(FakeRuntime):
        def extract_memory(self, **kwargs):
            replace_memory(store, user['id'], 1, [{'id': 'manual', 'text': 'edited', 'source_message_ids': []}])
            return super().extract_memory(**kwargs)
    assert not compress_one(store, EditingRuntime())
    assert get_memory(store, user['id'])['entries'][0]['text'] == 'edited'
    assert compress_one(store, FakeRuntime())


def test_web_contract_end_to_end(env):
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={}).json()['id']
    response = client.post(f'/api/v1/sessions/{sid}/messages', json={'text': 'hello', 'client_message_id': 'one'})
    assert response.status_code == 202
    run_id = response.json()['run_id']
    Worker(settings, runtime=FakeRuntime()).run_once()
    assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'succeeded'
    history = client.get(f'/api/v1/sessions/{sid}/messages').json()
    assert [m['role'] for m in history['items']] == ['user', 'assistant']
    assert history['runs'][0]['deliveries'] == []
    assert client.get('/api/v1/memory').json() == {'revision': 0, 'entries': []}
    assert client.post('/api/v1/auth/logout').status_code == 200
    assert client.get('/api/v1/auth/me').status_code == 401


def test_feishu_binding_hooks_and_delivery(env):
    import hashlib

    settings, store, user, other = env
    code = 'binding-secret'
    with store.transaction() as conn:
        conn.execute('INSERT INTO binding_codes(code_hash,user_id,expires_at) VALUES(?,?,?)',
                     (hashlib.sha256(code.encode()).hexdigest(), user['id'], time.time() + 600))
    identity = store.consume_binding_code(hashlib.sha256(code.encode()).hexdigest(), 'app', 'tenant', 'ext-1')
    assert identity['user_id'] == user['id']
    with pytest.raises(ServiceError):
        store.consume_binding_code(hashlib.sha256(code.encode()).hexdigest(), 'app', 'tenant', 'ext-2')
    item = submit_message(store, settings, user['id'], identity['session_id'], 'hi', 'f1',
                          source='feishu', binding_id=identity['id'], reply_target='chat-1')
    assert Worker(settings, runtime=FakeRuntime()).run_once()
    run = store.get_run(user['id'], item['run_id'])
    assert run['status'] == 'succeeded' and run['deliveries'][0]['status'] == 'pending'
