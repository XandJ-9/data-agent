from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import sql
from fastapi.testclient import TestClient

from api import create_app
from clock import now_plus
from errors import ServiceError
from services.chat import submit_message
from services.memory import compress_one, get_memory, replace_memory
from worker import Worker


class FakeRuntime:
    def __init__(self):
        self.prompts = []

    def respond(self, **kwargs):
        self.prompts.append(kwargs)
        return 'answer'

    def extract_memory(self, **kwargs):
        return [{'text': 'prefers tests', 'source_message_ids': [kwargs['messages'][0]['id']]}]


class SteppingRuntime(FakeRuntime):
    """FakeRuntime that emits the same event stream the real runtime produces."""

    def respond(self, **kwargs):
        super().respond(**kwargs)
        on_event = kwargs.get('on_event')
        if on_event:
            on_event({'kind': 'assistant', 'content': '需要检索历史', 'reasoning': '用户引用了第一条消息'})
            on_event({'kind': 'tool_call', 'name': 'search_history', 'content': '{"query": "first"}'})
            on_event({'kind': 'tool_result', 'name': 'search_history', 'content': '[]'})
        return 'answer'


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
        sql(conn, 'UPDATE runs SET lease_until=? WHERE id=?', now_plus(-1), run['id'])
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
    # Old messages are now below the exclusion cutoff (advance_cutoff in replace_memory),
    # so no new candidates can be extracted from this batch.
    assert not compress_one(store, FakeRuntime())
    assert get_memory(store, user['id'])['entries'][0]['text'] == 'edited'


def test_worker_persists_run_steps(env):
    settings, store, user, _ = env
    sid = session(store, user)
    item = enqueue(settings, store, user, sid)
    Worker(settings, runtime=SteppingRuntime()).run_once()
    steps = store.list_steps(item['run_id'])['items']
    assert [s['kind'] for s in steps] == ['assistant', 'tool_call', 'tool_result']
    assert [s['seq'] for s in steps] == [1, 2, 3]
    assert steps[0]['reasoning'] == '用户引用了第一条消息'
    assert steps[1]['name'] == 'search_history' and steps[2]['name'] == 'search_history'


def test_steps_endpoint_ownership(env):
    settings, store, user, other = env
    client = TestClient(create_app(settings))
    login(client)
    item = enqueue(settings, store, user, session(store, user))
    Worker(settings, runtime=SteppingRuntime()).run_once()
    body = client.get(f"/api/v1/runs/{item['run_id']}/steps")
    assert body.status_code == 200
    assert [s['kind'] for s in body.json()['items']] == ['assistant', 'tool_call', 'tool_result']
    foreign = enqueue(settings, store, other, session(store, other))
    assert client.get(f"/api/v1/runs/{foreign['run_id']}/steps").status_code == 404


def test_admin_skill_upload_delete_and_snapshot(env):
    from services.accounts import set_admin
    settings, store, user, other = env
    set_admin(store, 'alice')
    client = TestClient(create_app(settings))
    login(client)
    assert client.get('/api/v1/auth/me').json()['is_admin'] is True
    document = '---\nname: sales\ndescription: Sales data conventions\n---\n# Sales\n口径说明。'

    plain = TestClient(create_app(settings))
    plain.headers['Origin'] = 'http://testserver'
    plain.post('/api/v1/auth/login', json={'username': 'bob', 'password': 'another-password'})
    bob_csrf = plain.post('/api/v1/auth/login',
                          json={'username': 'bob', 'password': 'another-password'}).json()['csrf_token']
    plain.headers['X-CSRF-Token'] = bob_csrf
    forbidden = plain.post('/api/v1/skills', json={'content': document})
    assert forbidden.status_code == 403

    bad = client.post('/api/v1/skills', json={'content': 'no frontmatter'})
    assert bad.status_code == 400
    created = client.post('/api/v1/skills', json={'content': document}).json()
    assert created['name'] == 'sales'
    listed = [s['name'] for s in client.get('/api/v1/skills').json()['items']]
    assert 'sales' in listed

    sid = session(store, user)
    runtime = FakeRuntime()
    enqueue(settings, store, user, sid)
    Worker(settings, runtime=runtime).run_once()
    assert [s['name'] for s in runtime.prompts[0]['skills']] == ['sales']

    assert client.delete('/api/v1/skills/sales').json()['deleted'] is True
    assert client.delete('/api/v1/skills/sales').status_code == 404
    listed = [s['name'] for s in client.get('/api/v1/skills').json()['items']]
    assert 'sales' not in listed


def test_mcp_server_admin_crud(env):
    from services.accounts import set_admin
    settings, store, user, other = env
    set_admin(store, 'alice')
    client = TestClient(create_app(settings))
    login(client)

    plain = TestClient(create_app(settings))
    plain.headers['Origin'] = 'http://testserver'
    plain.headers['X-CSRF-Token'] = plain.post(
        '/api/v1/auth/login', json={'username': 'bob', 'password': 'another-password'}).json()['csrf_token']
    assert plain.get('/api/v1/mcp/servers').status_code == 403
    assert plain.post('/api/v1/mcp/servers',
                      json={'name': 'x', 'transport': 'stdio', 'command': 'x'}).status_code == 403

    assert client.post('/api/v1/mcp/servers',
                       json={'name': 'bad', 'transport': 'stdio'}).status_code == 400
    assert client.post('/api/v1/mcp/servers',
                       json={'name': 'bad', 'transport': 'http'}).status_code == 400
    created = client.post('/api/v1/mcp/servers', json={
        'name': 'docs', 'transport': 'stdio', 'command': 'npx',
        'args': ['-y', 'server'], 'env': {'TOKEN': 'secret'}}).json()
    assert created == {'name': 'docs', 'transport': 'stdio', 'command': 'npx',
                       'args': ['-y', 'server'], 'env': {'TOKEN': 'secret'},
                       'url': None, 'headers': {}, 'enabled': True}
    items = client.get('/api/v1/mcp/servers').json()['items']
    assert [item['name'] for item in items] == ['docs']

    updated = client.put('/api/v1/mcp/servers/docs', json={
        'transport': 'http', 'url': 'https://example.com/mcp', 'enabled': False}).json()
    assert updated['transport'] == 'http' and updated['enabled'] is False
    assert store.list_mcp_servers(enabled_only=True) == []

    assert client.post('/api/v1/mcp/servers/missing/test').status_code == 404
    assert client.delete('/api/v1/mcp/servers/docs').json()['deleted'] is True
    assert client.delete('/api/v1/mcp/servers/docs').status_code == 404


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
        sql(conn, 'INSERT INTO binding_codes(code_hash,user_id,expires_at) VALUES(?,?,?)',
            hashlib.sha256(code.encode()).hexdigest(), user['id'], now_plus(600))
    identity = store.consume_binding_code(hashlib.sha256(code.encode()).hexdigest(), 'app', 'tenant', 'ext-1')
    assert identity['user_id'] == user['id']
    with pytest.raises(ServiceError):
        store.consume_binding_code(hashlib.sha256(code.encode()).hexdigest(), 'app', 'tenant', 'ext-2')
    item = submit_message(store, settings, user['id'], identity['session_id'], 'hi', 'f1',
                          source='feishu', binding_id=identity['id'], reply_target='chat-1')
    assert Worker(settings, runtime=FakeRuntime()).run_once()
    run = store.get_run(user['id'], item['run_id'])
    assert run['status'] == 'succeeded' and run['deliveries'][0]['status'] == 'pending'


def test_session_rename(env):
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={'title': 'old title'}).json()['id']
    updated = client.patch(f'/api/v1/sessions/{sid}', json={'title': 'new title'}).json()
    assert updated['title'] == 'new title'
    items = client.get('/api/v1/sessions').json()['items']
    assert items[0]['title'] == 'new title'


def test_session_archive_and_restore(env):
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={'title': 'to archive'}).json()['id']
    assert client.post(f'/api/v1/sessions/{sid}/archive').status_code == 200
    assert len(client.get('/api/v1/sessions').json()['items']) == 0
    archived = client.get('/api/v1/sessions?archived=true').json()['items']
    assert len(archived) == 1 and archived[0]['id'] == sid
    assert archived[0]['archived_at'] is not None
    assert client.post(f'/api/v1/sessions/{sid}/restore').status_code == 200
    assert len(client.get('/api/v1/sessions').json()['items']) == 1


def test_archive_blocked_by_active_run(env):
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={}).json()['id']
    client.post(f'/api/v1/sessions/{sid}/messages', json={'text': 'hello', 'client_message_id': 'one'})
    assert client.post(f'/api/v1/sessions/{sid}/archive').status_code == 409


def test_archived_session_rejects_messages(env):
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={}).json()['id']
    client.post(f'/api/v1/sessions/{sid}/archive')
    response = client.post(f'/api/v1/sessions/{sid}/messages',
                           json={'text': 'hello', 'client_message_id': 'one'})
    assert response.status_code == 409


def test_archived_session_rejects_feishu_switch(env):
    import hashlib
    settings, store, user, _ = env
    client = TestClient(create_app(settings))
    login(client)
    sid = client.post('/api/v1/sessions', json={}).json()['id']
    client.post(f'/api/v1/sessions/{sid}/archive')
    code = 'switch-secret'
    with store.transaction() as conn:
        sql(conn, 'INSERT INTO binding_codes(code_hash,user_id,expires_at) VALUES(?,?,?)',
            hashlib.sha256(code.encode()).hexdigest(), user['id'], now_plus(600))
    store.consume_binding_code(hashlib.sha256(code.encode()).hexdigest(), 'app2', 't2', 'ext-sw')
    assert client.put('/api/v1/bindings/feishu/session', json={'session_id': sid}).status_code == 409

    # Clean up
    store.unbind_user(user['id'])


def test_rename_not_found_and_ownership(env):
    settings, store, user, other = env
    client = TestClient(create_app(settings))
    login(client)
    foreign = session(store, other)
    assert client.patch(f'/api/v1/sessions/{foreign}', json={'title': 'x'}).status_code == 404
    assert client.patch('/api/v1/sessions/nonexistent', json={'title': 'x'}).status_code == 404
