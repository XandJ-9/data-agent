"""Feishu private binding and delivery: security, dedupe, commands, outbox pump."""
import base64
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from data_agent.auth import create_user
from data_agent.db import Store, identifier
from data_agent.feishu import (
    REPLY_ALREADY_BOUND,
    REPLY_HELP,
    REPLY_HELP_COMMANDS,
    REPLY_INVALID_CODE,
    REPLY_INVALID_COMMAND,
    REPLY_RATE_LIMITED,
    REPLY_SESSION_SWITCHED,
    REPLY_STOPPED,
    REPLY_UNSUPPORTED,
    FeishuModule,
    apply_feishu_schema,
    generate_code,
    sha256_hex,
)
from data_agent.feishu_delivery import (
    FEISHU_TEXT_LIMIT,
    DeliveryApiError,
    DeliveryTimeout,
    delivery_parts,
    partition_reply,
    process_deliveries,
    process_replies,
)
from data_agent.settings import Settings

APP_ID = 'cli_app'
TENANT = 'tenant_1'
OPEN_ID = 'ou_user'
ENCRYPT_KEY = 'k' * 16
VERIFICATION_TOKEN = 'vt' * 12
APP_SECRET = 'sup3rs3cr3t-value'


def feishu_kwargs(**kw):
    base = {'feishu_app_id': APP_ID, 'feishu_app_secret': APP_SECRET,
            'feishu_verification_token': VERIFICATION_TOKEN, 'feishu_encrypt_key': ENCRYPT_KEY}
    base.update(kw)
    return base


def module(store, user, **kw) -> FeishuModule:
    settings = Settings(database_path=store.path, skills_path=store.path.parent / 'skills',
                        **feishu_kwargs())
    return FeishuModule(store, settings, **kw)


def unconfigured_module(store, user) -> FeishuModule:
    settings = Settings(database_path=store.path, skills_path=store.path.parent / 'skills',
                        **feishu_kwargs(feishu_encrypt_key=''))
    return FeishuModule(store, settings)


def app_settings(store):
    return Settings(database_path=store.path, skills_path=store.path.parent / 'skills',
                    **feishu_kwargs())


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / 't.db')
    store.init_db()
    apply_feishu_schema(store)
    user = create_user(store, 'alice', 'long-test-password')
    other = create_user(store, 'bob', 'another-password')
    return store, user, other


def encrypt_payload(plaintext: str, key: str) -> str:
    from Crypto.Cipher import AES
    data = plaintext.encode()
    iv = b'0123456789abcdef'
    pad = 16 - len(data) % 16
    cipher = AES.new(hashlib.sha256(key.encode()).digest(), AES.MODE_CBC, iv)
    return base64.b64encode(iv + cipher.encrypt(data + bytes([pad]) * pad)).decode()


def signed_body(event: dict, key=ENCRYPT_KEY, timestamp=None, nonce='n1'):
    body = json.dumps({'encrypt': encrypt_payload(json.dumps(event), key)}).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = hashlib.sha256((ts + nonce + key).encode() + body).hexdigest()
    return body, {'X-Lark-Request-Timestamp': ts, 'X-Lark-Request-Nonce': nonce, 'X-Lark-Signature': sig}


def make_event(event_id, message_id, text, msg_type='text', chat_type='p2p',
               sender_type='user', open_id=OPEN_ID, tenant=TENANT, token=VERIFICATION_TOKEN):
    return {
        'schema': '2.0',
        'header': {'event_id': event_id, 'event_type': 'im.message.receive_v1',
                   'create_time': str(int(time.time() * 1000)), 'token': token,
                   'app_id': APP_ID, 'tenant_key': tenant},
        'event': {
            'sender': {'sender_id': {'open_id': open_id, 'union_id': 'un_1', 'user_id': 'u_1'},
                       'sender_type': sender_type, 'tenant_key': 'tk'},
            'message': {'message_id': message_id, 'chat_id': 'oc_chat', 'chat_type': chat_type,
                        'message_type': msg_type, 'content': json.dumps({'text': text})},
        },
    }


def insert_code(store, user, code):
    with store.transaction() as conn:
        conn.execute('INSERT INTO binding_codes(code_hash,user_id,expires_at) VALUES(?,?,?)',
                     (sha256_hex(code), user['id'], time.time() + 600))


def outbox(store):
    with store.transaction(immediate=False) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM feishu_replies ORDER BY created_at,id')]


def runs(store):
    with store.transaction(immediate=False) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM runs')]


def events(store):
    with store.transaction(immediate=False) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM feishu_events')]


def binding_of(store, user_id):
    with store.transaction(immediate=False) as conn:
        row = conn.execute('SELECT * FROM channel_identities WHERE user_id=? AND channel="feishu" AND active=1',
                           (user_id,)).fetchone()
        return dict(row) if row else None


def db_dump(store) -> str:
    conn = sqlite3.connect(store.path)
    parts = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            parts.extend(str(v) for v in row)
    conn.close()
    return '\n'.join(parts)


# ---------------------------------------------------------------- webhook security

def test_webhook_rejects_bad_signature(env):
    store, user, _ = env
    mod = module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi'))
    resp = mod.handle_webhook(body, {**headers, 'X-Lark-Signature': '0' * 64})
    assert resp.status_code == 401
    assert events(store) == [] and runs(store) == []


def test_webhook_rejects_stale_signed_timestamp(env):
    store, user, _ = env
    mod = module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi'), timestamp=int(time.time()) - 3600)
    assert mod.handle_webhook(body, headers).status_code == 401


def test_webhook_rejects_wrong_verification_token(env):
    store, user, _ = env
    mod = module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi', token='wrong-token'))
    assert mod.handle_webhook(body, headers).status_code == 401
    assert events(store) == []


def test_webhook_rejects_oversized_body(env):
    store, user, _ = env
    mod = module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi'))
    assert mod.handle_webhook(body + b' ' * 70000, headers).status_code == 413


def test_webhook_rejects_unconfigured_module(env):
    store, user, _ = env
    mod = unconfigured_module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi'))
    assert mod.handle_webhook(body, headers).status_code == 503


def test_webhook_challenge_returns_challenge(env):
    store, user, _ = env
    mod = module(store, user)
    challenge = {'type': 'url_verification', 'challenge': 'ch_123',
                 'token': VERIFICATION_TOKEN, 'app_id': APP_ID, 'tenant_key': TENANT}
    body, headers = signed_body(challenge)
    resp = mod.handle_webhook(body, headers)
    assert resp.status_code == 200
    assert json.loads(resp.content)['challenge'] == 'ch_123'


def test_webhook_accepts_lowercase_and_mixed_case_headers(env):
    store, user, _ = env
    mod = module(store, user)
    code = generate_code()
    insert_code(store, user, code)
    body, headers = signed_body(make_event('e1', 'm1', f'/bind {code}'))
    lower = {k.lower(): v for k, v in headers.items()}
    resp = mod.handle_webhook(body, lower)
    assert resp.status_code == 200
    assert binding_of(store, user['id']) is not None


def test_webhook_500_response_is_sanitized_not_sdk_detail(env):
    store, user, _ = env
    mod = module(store, user)
    body, headers = signed_body(make_event('e1', 'm1', 'hi', token='bad'))
    resp = mod.handle_webhook(body, headers)
    assert 'signature' not in resp.content.decode().lower()
    assert 'encrypt' not in resp.content.decode().lower()


# ---------------------------------------------------------------- ingest dedupe and submit

def bound_mod(store, user):
    mod = module(store, user)
    code = generate_code()
    insert_code(store, user, code)
    assert mod.ingest(make_event('bind1', 'bm1', f'/bind {code}')) is None
    return mod


def test_ingest_p2p_text_creates_feishu_run(env):
    store, user, _ = env
    mod = bound_mod(store, user)
    assert mod.ingest(make_event('e1', 'm1', 'hello')) is None
    created = runs(store)
    assert len(created) == 1
    run = created[0]
    assert run['source'] == 'feishu' and run['reply_target'] == OPEN_ID
    assert run['binding_id'] == binding_of(store, user['id'])['id']
    assert len(events(store)) == 2


def test_ingest_replay_event_id_deduped_even_after_session_switch(env):
    store, user, _ = env
    mod = bound_mod(store, user)
    mod.ingest(make_event('e1', 'm1', 'hello'))
    sid = store.create_session(user['id'])['id']
    with store.transaction() as conn:
        conn.execute('UPDATE channel_identities SET session_id=? WHERE user_id=?', (sid, user['id']))
    assert mod.ingest(make_event('e1', 'm1', 'hello')) is None
    assert len(runs(store)) == 1


def test_ingest_same_message_id_different_event_id_deduped(env):
    store, user, _ = env
    mod = bound_mod(store, user)
    mod.ingest(make_event('e1', 'm1', 'hello'))
    assert mod.ingest(make_event('e2', 'm1', 'hello')) is None
    assert len(runs(store)) == 1
    assert len(events(store)) == 3


def test_ingest_unbound_message_enqueues_help_reply(env):
    store, user, _ = env
    mod = module(store, user)
    assert mod.ingest(make_event('e1', 'm1', 'hello')) is None
    rows = outbox(store)
    assert len(rows) == 1 and rows[0]['content'] == REPLY_HELP
    assert rows[0]['reply_target'] == OPEN_ID and rows[0]['status'] == 'pending'
    assert rows[0]['uuid'] and runs(store) == []


# ---------------------------------------------------------------- binding

def test_bind_success_consumes_code_and_confirms(env):
    store, user, _ = env
    mod = module(store, user)
    code = generate_code()
    insert_code(store, user, code)
    assert mod.ingest(make_event('e1', 'm1', f'/bind {code}')) is None
    binding = binding_of(store, user['id'])
    assert binding and binding['app_id'] == APP_ID and binding['tenant_id'] == TENANT
    assert binding['external_user_id'] == OPEN_ID
    with store.transaction(immediate=False) as conn:
        consumed = conn.execute('SELECT consumed_at FROM binding_codes WHERE code_hash=?',
                                (sha256_hex(code),)).fetchone()
    assert consumed['consumed_at'] is not None
    rows = outbox(store)
    assert len(rows) == 1 and REPLY_HELP_COMMANDS in rows[0]['content']
    assert code not in db_dump(store) and APP_SECRET not in db_dump(store)


def test_bind_invalid_code_replies_and_counts_attempt(env):
    store, user, _ = env
    mod = module(store, user)
    assert mod.ingest(make_event('e1', 'm1', '/bind nope')) is None
    rows = outbox(store)
    assert rows[0]['content'] == REPLY_INVALID_CODE
    with store.transaction(immediate=False) as conn:
        attempts = conn.execute('SELECT attempts FROM feishu_bind_attempts').fetchall()
    assert len(attempts) == 1 and attempts[0]['attempts'] == 1


def test_bind_conflict_replies_already_bound(env):
    store, user, other = env
    mod = module(store, user)
    first = generate_code()
    insert_code(store, user, first)
    mod.ingest(make_event('e0', 'm0', f'/bind {first}'))
    second = generate_code()
    insert_code(store, other, second)
    mod.ingest(make_event('e1', 'm1', f'/bind {second}'))
    rows = outbox(store)
    assert rows[-1]['content'] == REPLY_ALREADY_BOUND
    assert binding_of(store, user['id']) is not None
    assert binding_of(store, other['id']) is None
    with store.transaction(immediate=False) as conn:
        unconsumed = conn.execute('SELECT consumed_at FROM binding_codes WHERE code_hash=?',
                                  (sha256_hex(second),)).fetchone()
    assert unconsumed['consumed_at'] is None  # conflicting code is never burned


def test_bind_rate_limited_per_identity(env):
    store, user, _ = env
    mod = module(store, user, bind_max_attempts=3, bind_window_seconds=3600)
    for i in range(4):
        mod.ingest(make_event(f'e{i}', f'm{i}', '/bind bad'))
    rows = outbox(store)
    assert sum(1 for r in rows if r['content'] == REPLY_INVALID_CODE) == 3
    assert rows[-1]['content'] == REPLY_RATE_LIMITED


def test_concurrent_binds_respect_rate_limit_and_consume_once(env):
    store, user, _ = env
    mod = module(store, user, bind_max_attempts=5)
    codes = [generate_code() for _ in range(12)]
    for c in codes[:-1]:
        insert_code(store, user, c)
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda c: mod.ingest(make_event(f'e-{c[:4]}', f'm-{c[:4]}', f'/bind {c}')), codes))
    with store.transaction(immediate=False) as conn:
        consumed = conn.execute('SELECT count(*) FROM binding_codes WHERE consumed_at IS NOT NULL').fetchone()[0]
    assert consumed <= 1
    assert len([b for b in [binding_of(store, user['id'])] if b]) == 1


def test_generate_code_high_entropy():
    codes = {generate_code() for _ in range(50)}
    assert len(codes) == 50 and all(len(c) >= 20 for c in codes)


# ---------------------------------------------------------------- commands

def test_commands_new_switch_stop_help(env):
    store, user, _ = env
    mod = bound_mod(store, user)
    original = binding_of(store, user['id'])
    other_sid = store.create_session(user['id'], 'Web conversation')['id']

    mod.ingest(make_event('c1', 'cm1', '/new'))
    rebound = binding_of(store, user['id'])
    assert rebound['id'] == original['id'] and rebound['session_id'] != original['session_id']

    mod.ingest(make_event('c2', 'cm2', f'/switch {other_sid}'))
    assert binding_of(store, user['id'])['session_id'] == other_sid

    mod.ingest(make_event('c3', 'cm3', '/help'))
    assert outbox(store)[-1]['content'] == REPLY_HELP_COMMANDS

    mod.ingest(make_event('c4', 'cm4', '/stop'))
    assert binding_of(store, user['id']) is None
    assert outbox(store)[-1]['content'] == REPLY_STOPPED

    mod.ingest(make_event('c5', 'cm5', 'hello again'))
    assert outbox(store)[-1]['content'] == REPLY_HELP
    assert len(runs(store)) == 0


def test_switch_to_foreign_or_missing_session_rejected(env):
    store, user, other = env
    mod = bound_mod(store, user)  # bind confirmation is outbox row 0
    foreign = store.create_session(other['id'])['id']
    mod.ingest(make_event('c1', 'cm1', f'/switch {foreign}'))
    mod.ingest(make_event('c2', 'cm2', '/switch missing-session'))
    rows = outbox(store)
    assert len(rows) == 3
    assert all(r['content'] == REPLY_INVALID_COMMAND for r in rows[1:])
    sid = store.create_session(user['id'])['id']
    mod.ingest(make_event('c3', 'cm3', f'/switch {sid}'))
    assert binding_of(store, user['id'])['session_id'] == sid
    assert outbox(store)[-1]['content'] == REPLY_SESSION_SWITCHED


def test_unknown_command_replies_invalid(env):
    store, user, _ = env
    mod = bound_mod(store, user)
    mod.ingest(make_event('c1', 'cm1', '/nonsense'))
    assert outbox(store)[-1]['content'] == REPLY_INVALID_COMMAND


# ---------------------------------------------------------------- unsupported inputs

def test_group_and_nontext_rejected_without_agent_run(env):
    store, user, _ = env
    mod = module(store, user)
    assert mod.ingest(make_event('g1', 'gm1', 'hello', chat_type='group')) is None
    assert mod.ingest(make_event('g2', 'gm2', 'hello', msg_type='image')) is None
    assert runs(store) == []
    assert len(events(store)) == 2
    rows = outbox(store)
    assert len(rows) == 1 and rows[0]['content'] == REPLY_UNSUPPORTED  # p2p non-text only


def test_non_user_sender_ignored(env):
    store, user, _ = env
    mod = module(store, user)
    assert mod.ingest(make_event('s1', 'sm1', 'hello', sender_type='app')) is None
    assert runs(store) == [] and outbox(store) == [] and len(events(store)) == 1


# ---------------------------------------------------------------- delivery segmentation

def test_delivery_parts_splits_utf8_preserves_order_and_stable():
    text = ('ab' * 100 + '中' * 200) * 5
    parts = delivery_parts(text)
    assert parts == delivery_parts(text)
    assert all(len(p.encode()) <= FEISHU_TEXT_LIMIT for p in parts)
    assert ''.join(parts) == text
    assert delivery_parts('short') == ['short']
    exact = delivery_parts('a' * FEISHU_TEXT_LIMIT)
    assert exact == ['a' * FEISHU_TEXT_LIMIT]
    tricky = '中' * 2000
    assert all(len(p.encode()) <= FEISHU_TEXT_LIMIT for p in delivery_parts(tricky))
    assert ''.join(delivery_parts(tricky)) == tricky


def test_partition_reply_marks_only_multi_segment():
    single = partition_reply('short')
    assert single == ['short']
    parts = partition_reply('x' * 7000)
    assert len(parts) == 3
    assert parts[0].endswith(' (1/3)') and parts[2].endswith(' (3/3)')
    assert all(len(p.encode()) <= FEISHU_TEXT_LIMIT for p in parts)


# ---------------------------------------------------------------- delivery pump

def deliverable_run(store, user, content, session_id=None):
    from data_agent.chat import submit_message
    binding = binding_of(store, user['id'])
    sid = session_id or binding['session_id']
    submit_message(store, app_settings(store), user['id'], sid, 'q', identifier(),
                   source='feishu', binding_id=binding['id'], reply_target=OPEN_ID)
    run = store.claim_run('w', 120)
    store.finish_run(run, content)


class FakeSender:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, app_id, app_secret, receive_id, content, uuid):
        self.calls.append({'app_id': app_id, 'app_secret': app_secret, 'receive_id': receive_id,
                           'content': content, 'uuid': uuid})
        outcome = self.results.get(uuid)
        if outcome is not None:
            raise outcome
        return 'om_receipt'


def deliveries(store):
    with store.transaction(immediate=False) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM deliveries ORDER BY run_id,part')]


def test_pump_delivers_parts_in_order_with_stable_uuid(env):
    store, user, _ = env
    bound_mod(store, user)
    deliverable_run(store, user, 'x' * 7000)
    sender = FakeSender()
    process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120)
    assert [c['content'] for c in sender.calls] == partition_reply('x' * 7000)
    assert sender.calls[0]['app_id'] == APP_ID and sender.calls[0]['app_secret'] == APP_SECRET
    assert sender.calls[0]['receive_id'] == OPEN_ID
    row = deliveries(store)[0]
    assert row['status'] == 'delivered' and row['receipt'] == 'om_receipt'
    parts = partition_reply('x' * 7000)
    assert {c['uuid'] for c in sender.calls} == {f"dl-{row['id']}:{i}" for i in range(len(parts))}


def test_pump_timeout_marks_unknown_and_never_retries(env):
    store, user, _ = env
    bound_mod(store, user)
    deliverable_run(store, user, 'answer')
    row = deliveries(store)[0]
    sender = FakeSender(results={f"dl-{row['id']}:0": DeliveryTimeout()})
    process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120)
    assert deliveries(store)[0]['status'] == 'unknown'
    process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w2', lease_seconds=120)
    assert len(sender.calls) == 1
    assert deliveries(store)[0]['status'] == 'unknown'


def test_pump_api_error_backoff_then_failed(env):
    store, user, _ = env
    bound_mod(store, user)
    deliverable_run(store, user, 'answer')
    row = deliveries(store)[0]
    uuid = f"dl-{row['id']}:0"
    clock = {'now': 1000.0}
    sender = FakeSender(results={uuid: DeliveryApiError('99991663')})
    process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120,
                       clock=lambda: clock['now'])
    failed = deliveries(store)[0]
    assert failed['status'] == 'pending' and failed['attempts'] == 1
    assert failed['error_code'] == '99991663' and failed['retry_at'] >= 1002
    # before backoff elapses nothing is claimed
    assert process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w2', lease_seconds=120,
                              clock=lambda: clock['now']) == 0
    clock['now'] += 10
    for _ in range(7):
        process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w3', lease_seconds=120,
                           clock=lambda: clock['now'])
        clock['now'] += 400  # exceeds the capped backoff so every retry is claimable
    assert deliveries(store)[0]['status'] == 'failed'
    assert deliveries(store)[0]['attempts'] == 8


def test_pump_revalidates_binding_before_send(env):
    store, user, _ = env
    bound_mod(store, user)
    deliverable_run(store, user, 'answer')
    with store.transaction() as conn:
        conn.execute("UPDATE channel_identities SET active=0 WHERE user_id=?", (user['id'],))
    sender = FakeSender()
    process_deliveries(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120)
    assert sender.calls == []
    row = deliveries(store)[0]
    assert row['status'] == 'failed' and row['error_code'] == 'binding_revoked'


def test_stale_sending_delivery_becomes_unknown(env):
    store, user, _ = env
    bound_mod(store, user)
    deliverable_run(store, user, 'answer')
    row = deliveries(store)[0]
    with store.transaction() as conn:
        conn.execute("UPDATE deliveries SET status='sending',owner='dead',generation=1,lease_until=1 "
                     "WHERE id=?", (row['id'],))
    process_deliveries(store, FakeSender(), app_id=APP_ID, app_secret=APP_SECRET, owner='w9', lease_seconds=120)
    assert deliveries(store)[0]['status'] == 'unknown'


def test_reply_pump_sends_and_reuses_uuid_on_retry(env):
    store, user, _ = env
    mod = module(store, user)
    mod.ingest(make_event('e1', 'm1', 'hello'))
    rows = outbox(store)
    uuid = rows[0]['uuid']
    sender = FakeSender(results={uuid: DeliveryTimeout()})
    process_replies(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120)
    assert outbox(store)[0]['status'] == 'unknown'
    assert len(sender.calls) == 1 and sender.calls[0]['uuid'] == uuid
    # user-triggered retry (core endpoint) resets to pending; same uuid is reused
    with store.transaction() as conn:
        conn.execute("UPDATE feishu_replies SET status='pending' WHERE uuid=?", (uuid,))
    sender.results.clear()
    process_replies(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w2', lease_seconds=120)
    assert sender.calls[1]['uuid'] == uuid
    assert outbox(store)[0]['status'] == 'delivered'


def test_reply_pump_retries_api_error_with_backoff(env):
    store, user, _ = env
    mod = module(store, user)
    mod.ingest(make_event('e1', 'm1', 'hello'))
    uuid = outbox(store)[0]['uuid']
    clock = {'now': 500.0}
    sender = FakeSender(results={uuid: DeliveryApiError('99991672')})
    process_replies(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w1', lease_seconds=120,
                    clock=lambda: clock['now'])
    assert outbox(store)[0]['status'] == 'pending' and outbox(store)[0]['retry_at'] > 500
    clock['now'] += 10
    sender.results.clear()
    process_replies(store, sender, app_id=APP_ID, app_secret=APP_SECRET, owner='w2', lease_seconds=120,
                    clock=lambda: clock['now'])
    assert outbox(store)[0]['status'] == 'delivered'


def test_delivery_parts_rejects_nonpositive_limit():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        delivery_parts('x', limit=0)


def test_sdk_sender_rejects_credential_mismatch_without_network():
    from data_agent.feishu_delivery import SdkSender
    sender = SdkSender(APP_ID, APP_SECRET)
    with pytest.raises(DeliveryApiError) as excinfo:
        sender(APP_ID, 'wrong-secret', OPEN_ID, 'text', 'u1')
    assert excinfo.value.code == 'credential_mismatch'


def test_sdk_sender_builds_offline_client():
    from data_agent.feishu_delivery import SdkSender
    sender = SdkSender(APP_ID, APP_SECRET)
    client = sender._get_client()
    assert client.config.app_id == APP_ID and client is sender._get_client()
