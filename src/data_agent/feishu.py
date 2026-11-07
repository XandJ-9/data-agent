"""Feishu channel: verified HTTP webhook, private binding commands, durable event dedupe.

Inbound events are decrypted and authenticated by the official lark-oapi
EventDispatcherHandler; this module adds the guarantees the SDK does not
provide: bounded body size, signature timestamp replay window, sanitized
error responses/log records, and durable dedupe by (app, tenant, event_id)
plus an independent message_id guard.

Every accepted event is fully persisted (dedupe record, agent run or reply
outbox row) in one SQLite transaction before the HTTP ACK is returned, so a
redelivery after a crash can never double-submit. Binding codes are only
ever stored as SHA-256 digests. Raw inbound secrets (ciphertext, tokens,
binding codes) are never logged: SDK error logging is suppressed around
verification and replaced with one sanitized record.

Production wiring is FeishuModule(store, settings) with the shared
data_agent Settings; optional keyword overrides exist for tests.
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from lark_oapi.core.model import RawRequest
from lark_oapi.event.custom import CustomizedEvent
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from data_agent.chat import CONFIG_FIELDS, installed_skills
from data_agent.db import ServiceError, Store, identifier

MESSAGE_EVENT = 'im.message.receive_v1'
MAX_TEXT_CHARS = 16000

REPLY_HELP_COMMANDS = '可用命令：/new 新建会话；/switch <会话ID> 切换会话；/stop 解绑；/help 帮助。'
REPLY_HELP = '未绑定账号。请先在网页端生成绑定码，然后发送 /bind <绑定码> 完成绑定。'
REPLY_BOUND = '绑定成功。' + REPLY_HELP_COMMANDS
REPLY_INVALID_CODE = '绑定码无效或已过期，请在网页端重新生成。'
REPLY_ALREADY_BOUND = '该账号已绑定，或此飞书用户已绑定其他账号。'
REPLY_RATE_LIMITED = '尝试次数过多，请稍后再试。'
REPLY_INVALID_COMMAND = '无法识别的命令。' + REPLY_HELP_COMMANDS
REPLY_SESSION_SWITCHED = '已切换会话。' + REPLY_HELP_COMMANDS
REPLY_STOPPED = '已解绑。重新绑定请发送 /bind <绑定码>。'
REPLY_UNSUPPORTED = '暂仅支持私聊文本消息。'
REPLY_TOO_LONG = '消息过长，请分条发送（上限 16000 字符）。'

# Appended by data-agent init-db inside MIGRATION_1 (no deployed database yet).
FEISHU_SCHEMA = """
CREATE TABLE feishu_events(app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, event_id TEXT NOT NULL,
 message_id TEXT, received_at REAL NOT NULL, PRIMARY KEY(app_id,tenant_id,event_id));
CREATE INDEX feishu_events_message ON feishu_events(app_id,tenant_id,message_id);
CREATE TABLE feishu_replies(id TEXT PRIMARY KEY, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 external_user_id TEXT NOT NULL, reply_target TEXT NOT NULL, content TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', receipt TEXT, error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, owner TEXT, generation INTEGER NOT NULL DEFAULT 0, lease_until REAL,
 uuid TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
CREATE INDEX feishu_replies_due ON feishu_replies(status,retry_at);
CREATE TABLE feishu_bind_attempts(identity_key TEXT PRIMARY KEY, window_start REAL NOT NULL,
 attempts INTEGER NOT NULL);
"""


def apply_feishu_schema(store: Store) -> None:
    """Idempotent bootstrap for tests and standalone stores already migrated without channel tables."""
    with store.transaction() as conn:
        applied = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feishu_events'").fetchone()
        if applied:
            return None
        for statement in FEISHU_SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_code() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class FeishuResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class _SuppressLarkErrors:
    """Thread-safe suppression of Lark SDK log records while verifying untrusted input.

    SDK verification failures log exception details that can embed ciphertext
    fragments; records at WARNING or above are dropped while any webhook is in
    flight and replaced by one sanitized summary record.
    """

    _lock = threading.Lock()
    _depth = 0
    _dropped = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            _SuppressLarkErrors._dropped += 1
            return False
        return True

    def __enter__(self) -> '_SuppressLarkErrors':
        with _SuppressLarkErrors._lock:
            _SuppressLarkErrors._depth += 1
            if _SuppressLarkErrors._depth == 1:
                logging.getLogger('Lark').addFilter(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        dropped = 0
        with _SuppressLarkErrors._lock:
            _SuppressLarkErrors._depth -= 1
            if _SuppressLarkErrors._depth == 0:
                logging.getLogger('Lark').removeFilter(self)
                dropped = _SuppressLarkErrors._dropped
                _SuppressLarkErrors._dropped = 0
        if dropped:
            logging.getLogger(__name__).warning(
                'feishu webhook rejected %d sdk log record(s); details suppressed', dropped)


# Lowercase -> exact names the SDK reads from RawRequest.headers.
_SDK_HEADERS = {
    'x-lark-request-timestamp': 'X-Lark-Request-Timestamp',
    'x-lark-request-nonce': 'X-Lark-Request-Nonce',
    'x-lark-signature': 'X-Lark-Signature',
    'x-request-id': 'X-Request-Id',
}

_BODY_413 = b'{"msg":"request too large"}'
_BODY_401 = b'{"msg":"rejected"}'
_BODY_503 = b'{"msg":"temporarily unavailable"}'


def _normalize_headers(headers: Mapping[str, str]) -> dict:
    normalized = {}
    for key, value in headers.items():
        normalized[_SDK_HEADERS.get(key.lower(), key)] = value
    return normalized


def _configured(settings) -> bool:
    return bool(settings.feishu_app_id and settings.feishu_app_secret.get_secret_value()
                and settings.feishu_verification_token.get_secret_value()
                and settings.feishu_encrypt_key.get_secret_value())


class FeishuModule:
    """Owns the Feishu webhook boundary and the durable side of inbound events.

    No network I/O happens inside database transactions: the SDK transport is
    only touched by the delivery pump (feishu_delivery), never here.
    """

    def __init__(self, store: Store, settings, *, webhook_max_bytes: int = 65536,
                 timestamp_tolerance: int = 300, bind_max_attempts: int = 10,
                 bind_window_seconds: int = 3600, now: Callable[[], float] = time.time):
        self.store = store
        self.settings = settings
        self.webhook_max_bytes = webhook_max_bytes
        self.timestamp_tolerance = timestamp_tolerance
        self.bind_max_attempts = bind_max_attempts
        self.bind_window_seconds = bind_window_seconds
        self._now = now
        self._handler: EventDispatcherHandler | None = None
        self._handler_lock = threading.Lock()
        self._callback_error = threading.local()

    # ------------------------------------------------------------ HTTP webhook

    def handle_webhook(self, body: bytes, headers: Mapping[str, str]) -> FeishuResponse:
        if not _configured(self.settings):
            return FeishuResponse(503, _BODY_503, {'Content-Type': 'application/json'})
        if len(body) > self.webhook_max_bytes:
            return FeishuResponse(413, _BODY_413, {'Content-Type': 'application/json'})
        normalized = _normalize_headers(headers)
        if not self._timestamp_is_fresh(normalized):
            return FeishuResponse(401, _BODY_401, {'Content-Type': 'application/json'})
        raw = RawRequest()
        raw.uri = '/webhook/feishu'
        raw.headers = normalized
        raw.body = body
        self._callback_error.error = None
        with _SuppressLarkErrors():
            response = self._dispatcher().do(raw)
        if getattr(self._callback_error, 'error', None) is not None:
            # The event passed verification but persistence failed; ask Feishu to retry.
            return FeishuResponse(503, _BODY_503, {'Content-Type': 'application/json'})
        if response.status_code == 200:
            return FeishuResponse(200, response.content or b'',
                                  {'Content-Type': response.headers.get('Content-Type', 'application/json')})
        # SDK 500 means verification failure or an unsupported/unregistered event
        # type; the SDK body would embed error details, so it is replaced.
        return FeishuResponse(401, _BODY_401, {'Content-Type': 'application/json'})

    def _timestamp_is_fresh(self, headers: dict) -> bool:
        """Replay bound for signed callbacks; the SDK checks the signature but not its age."""
        if not headers.get('X-Lark-Signature'):
            return True  # legacy unencrypted token-only callbacks; create_time is checked in ingest
        raw_ts = headers.get('X-Lark-Request-Timestamp')
        try:
            ts = int(raw_ts)
        except (TypeError, ValueError):
            return False
        return abs(self._now() - ts) <= self.timestamp_tolerance

    def _dispatcher(self) -> EventDispatcherHandler:
        with self._handler_lock:
            if self._handler is None:
                builder = EventDispatcherHandler.builder(
                    self.settings.feishu_encrypt_key.get_secret_value(),
                    self.settings.feishu_verification_token.get_secret_value())
                builder.register_p2_customized_event(MESSAGE_EVENT, self._on_event)
                self._handler = builder.build()
            return self._handler

    def _on_event(self, event: CustomizedEvent) -> None:
        header = event.header
        payload = {'schema': '2.0',
                   'header': {'event_id': header.event_id, 'event_type': header.event_type,
                              'create_time': header.create_time, 'token': header.token,
                              'app_id': header.app_id, 'tenant_key': header.tenant_key},
                   'event': event.event}
        try:
            self.ingest(payload)
        except BaseException as exc:  # surfaced by handle_webhook as a sanitized 503
            self._callback_error.error = exc
            raise

    # ------------------------------------------------------------ event pipeline

    def ingest(self, event: dict) -> None:
        """Persist one verified event atomically (run, outbox rows, dedupe record).

        Dedupe is durable and independent of the binding session: (app_id,
        tenant_id, event_id) plus a separate message_id guard, so replays are
        swallowed even across /new and /switch. Replies become feishu_replies
        outbox rows; there is no direct network call and no return payload.
        """
        s = self.settings
        header = event.get('header') or {}
        body = event.get('event') or {}
        if header.get('event_type') != MESSAGE_EVENT or header.get('app_id') != s.feishu_app_id:
            return None
        event_id = header.get('event_id') or ''
        tenant_id = header.get('tenant_key') or ''
        message_id = (body.get('message') or {}).get('message_id') or ''
        sender = body.get('sender') or {}
        open_id = (sender.get('sender_id') or {}).get('open_id') or ''
        if not event_id or not tenant_id or not open_id:
            return None
        if not self._create_time_is_fresh(header.get('create_time')):
            return None
        with self.store.transaction() as conn:
            seen = conn.execute('SELECT 1 FROM feishu_events WHERE app_id=? AND tenant_id=? AND event_id=?',
                                (s.feishu_app_id, tenant_id, event_id)).fetchone()
            if seen:
                return None
            if message_id and conn.execute('SELECT 1 FROM feishu_events WHERE message_id=?',
                                           (message_id,)).fetchone():
                self._record_event(conn, tenant_id, event_id, message_id)
                return None
            self._dispatch(conn, tenant_id, open_id, sender, body, event_id)
            self._record_event(conn, tenant_id, event_id, message_id)

    def _create_time_is_fresh(self, create_time) -> bool:
        try:
            created_ms = int(create_time)
        except (TypeError, ValueError):
            return False
        return abs(self._now() - created_ms / 1000) <= self.timestamp_tolerance * 2

    def _record_event(self, conn, tenant_id: str, event_id: str, message_id: str) -> None:
        conn.execute('INSERT INTO feishu_events(app_id,tenant_id,event_id,message_id,received_at) '
                     'VALUES(?,?,?,?,?)', (self.settings.feishu_app_id, tenant_id, event_id,
                                           message_id or None, self._now()))

    def _dispatch(self, conn, tenant_id: str, open_id: str, sender: dict, body: dict,
                  event_id: str) -> str | None:
        message = body.get('message') or {}
        chat_type = message.get('chat_type')
        msg_type = message.get('message_type')
        if sender.get('sender_type') != 'user':
            return None  # bot or app echo; never reply to avoid loops
        if chat_type != 'p2p' or msg_type != 'text':
            if chat_type == 'p2p':
                return self._enqueue_reply(conn, tenant_id, open_id, REPLY_UNSUPPORTED)
            return None  # groups are not addressed to avoid echoing into the chat
        try:
            text = json.loads(message.get('content') or '{}').get('text') or ''
        except ValueError:
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_UNSUPPORTED)
        text = text.strip()
        binding = self._binding(conn, tenant_id, open_id)
        if text.startswith('/'):
            return self._handle_command(conn, binding, tenant_id, open_id, text)
        if not text:
            return None
        if binding is None:
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_HELP)
        return self._submit(conn, binding, text, event_id)

    # ------------------------------------------------------------ commands

    def _handle_command(self, conn, binding, tenant_id: str, open_id: str, text: str) -> str | None:
        parts = text.split()
        command = parts[0].lower()
        if command == '/bind':
            if binding is not None:
                return self._enqueue_reply(conn, tenant_id, open_id, REPLY_ALREADY_BOUND)
            if not self._bind_attempt_allowed(conn, tenant_id, open_id):
                return self._enqueue_reply(conn, tenant_id, open_id, REPLY_RATE_LIMITED)
            code = parts[1] if len(parts) == 2 else ''
            try:
                self._consume_code(conn, sha256_hex(code), tenant_id, open_id)
            except ServiceError as exc:
                reply = REPLY_ALREADY_BOUND if exc.status == 409 else REPLY_INVALID_CODE
                return self._enqueue_reply(conn, tenant_id, open_id, reply)
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_BOUND)
        if binding is None:
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_HELP)
        if command == '/new':
            session_id = identifier()
            conn.execute('INSERT INTO sessions(id,user_id,title,created_at) VALUES(?,?,?,?)',
                         (session_id, binding['user_id'], 'Feishu conversation', self._now()))
            conn.execute('UPDATE channel_identities SET session_id=? WHERE id=?', (session_id, binding['id']))
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_SESSION_SWITCHED)
        if command == '/switch':
            target = parts[1] if len(parts) == 2 else ''
            owned = conn.execute('SELECT 1 FROM sessions WHERE id=? AND user_id=?',
                                 (target, binding['user_id'])).fetchone()
            if not owned:
                return self._enqueue_reply(conn, tenant_id, open_id, REPLY_INVALID_COMMAND)
            conn.execute('UPDATE channel_identities SET session_id=? WHERE id=?', (target, binding['id']))
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_SESSION_SWITCHED)
        if command == '/stop':
            conn.execute('UPDATE channel_identities SET active=0 WHERE id=? AND active=1', (binding['id'],))
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_STOPPED)
        if command == '/help':
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_HELP_COMMANDS)
        return self._enqueue_reply(conn, tenant_id, open_id, REPLY_INVALID_COMMAND)

    def _bind_attempt_allowed(self, conn, tenant_id: str, open_id: str) -> bool:
        """Bounded attempt counter keyed by the verified external identity."""
        key = f'{self.settings.feishu_app_id}:{tenant_id}:{open_id}'
        now = self._now()
        row = conn.execute('SELECT window_start,attempts FROM feishu_bind_attempts WHERE identity_key=?',
                           (key,)).fetchone()
        if row is None or now - row['window_start'] >= self.bind_window_seconds:
            conn.execute('INSERT INTO feishu_bind_attempts(identity_key,window_start,attempts) VALUES(?,?,?) '
                         'ON CONFLICT(identity_key) DO UPDATE SET window_start=excluded.window_start,attempts=1',
                         (key, now, 1))
            return self.bind_max_attempts > 0
        conn.execute('UPDATE feishu_bind_attempts SET attempts=attempts+1 WHERE identity_key=?', (key,))
        return row['attempts'] + 1 <= self.bind_max_attempts

    def _consume_code(self, conn, code_hash: str, tenant_id: str, open_id: str) -> dict:
        """Same SQL as Store.consume_binding_code, executed in the caller's transaction.

        Integration follow-up: expose a connection-based variant in db.Store so
        this copy disappears; the core file was owned by another agent.
        """
        now = self._now()
        row = conn.execute('SELECT * FROM binding_codes WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?',
                           (code_hash, now)).fetchone()
        if row is None:
            raise ServiceError(400, 'Binding code invalid or expired')
        user_id = row['user_id']
        conflict = conn.execute('SELECT 1 FROM channel_identities WHERE active=1 AND '
                                '(user_id=? OR (app_id=? AND tenant_id=? AND external_user_id=?))',
                                (self.settings.feishu_app_id, self.settings.feishu_app_id,
                                 tenant_id, open_id)).fetchone()
        if conflict:
            raise ServiceError(409, 'Account already bound')
        session_id, binding_id = identifier(), identifier()
        conn.execute('INSERT INTO sessions(id,user_id,title,created_at) VALUES(?,?,?,?)',
                     (session_id, user_id, 'Feishu conversation', now))
        conn.execute('INSERT INTO channel_identities(id,user_id,app_id,tenant_id,external_user_id,session_id) '
                     'VALUES(?,?,?,?,?,?)', (binding_id, user_id, self.settings.feishu_app_id,
                                             tenant_id, open_id, session_id))
        conn.execute('UPDATE binding_codes SET consumed_at=? WHERE code_hash=?', (now, code_hash))
        return {'id': binding_id, 'user_id': user_id, 'session_id': session_id}

    # ------------------------------------------------------------ agent submission

    def _submit(self, conn, binding: dict, text: str, event_id: str) -> str | None:
        """Same SQL as chat.submit_message(source='feishu') in the caller's transaction.

        Dedupe + run + event record commit together, so a crash before submit
        leaves no dedupe row and Feishu redelivery re-runs the pipeline. The
        event_id doubles as client_message_id, giving run-level idempotency.
        """
        if len(text) > MAX_TEXT_CHARS:
            return self._enqueue_reply(conn, binding['tenant_id'], binding['external_user_id'], REPLY_TOO_LONG)
        duplicate = conn.execute('SELECT 1 FROM runs WHERE user_id=? AND session_id=? AND client_message_id=?',
                                 (binding['user_id'], binding['session_id'], event_id)).fetchone()
        if duplicate:
            return None
        settings = self.settings
        skills = installed_skills(settings)
        enabled = {r['name']: bool(r['enabled']) for r in conn.execute(
            'SELECT name,enabled FROM user_skills WHERE user_id=?', (binding['user_id'],))}
        snapshot = [s for s in skills if enabled.get(s['name'], True)]
        config = {k: getattr(settings, k) for k in CONFIG_FIELDS}
        definition = json.dumps({'version': 1, 'config': config, 'skills': snapshot}, sort_keys=True)
        run_id = identifier()
        now = self._now()
        conn.execute('INSERT INTO runs(id,user_id,session_id,client_message_id,created_at,definition_hash,'
                     'skills_snapshot,config_snapshot,source,binding_id,reply_target) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (run_id, binding['user_id'], binding['session_id'], event_id, now,
                      hashlib.sha256(definition.encode()).hexdigest(), json.dumps(snapshot), json.dumps(config),
                      'feishu', binding['id'], binding['external_user_id']))
        message_id = conn.execute('INSERT INTO messages(user_id,session_id,run_id,role,content,created_at) '
                                  'VALUES(?,?,?,?,?,?)',
                                  (binding['user_id'], binding['session_id'], run_id, 'user', text, now)).lastrowid
        conn.execute('UPDATE runs SET input_message_id=? WHERE id=?', (message_id, run_id))
        return None

    # ------------------------------------------------------------ reply outbox

    def _enqueue_reply(self, conn, tenant_id: str, open_id: str, content: str) -> str:
        reply_id = identifier()
        conn.execute('INSERT INTO feishu_replies(id,app_id,tenant_id,external_user_id,reply_target,content,'
                     'uuid,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (reply_id, self.settings.feishu_app_id, tenant_id, open_id, open_id, content,
                      f'rp-{reply_id}', self._now()))
        return content

    def _binding(self, conn, tenant_id: str, open_id: str) -> dict | None:
        row = conn.execute('SELECT id,user_id,session_id,tenant_id,external_user_id FROM channel_identities '
                           'WHERE app_id=? AND tenant_id=? AND external_user_id=? AND active=1',
                           (self.settings.feishu_app_id, tenant_id, open_id)).fetchone()
        return dict(row) if row else None
