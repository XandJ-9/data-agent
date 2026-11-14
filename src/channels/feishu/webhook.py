"""Feishu channel: verified HTTP webhook, private binding commands, durable event dedupe.

Inbound events are decrypted and authenticated by the official lark-oapi
EventDispatcherHandler; this module adds the guarantees the SDK does not
provide: bounded body size, signature timestamp replay window, sanitized
error responses/log records, and durable dedupe by (app, tenant, event_id)
plus an independent message_id guard.

Every accepted event is fully persisted (dedupe record, agent run or reply
outbox row) in one transaction before the HTTP ACK is returned, so a
redelivery after a crash can never double-submit. Binding codes are only
ever stored as SHA-256 digests. Raw inbound secrets (ciphertext, tokens,
binding codes) are never logged: SDK error logging is suppressed around
verification and replaced with one sanitized record.

Production wiring is FeishuModule(store, settings) with the shared
Settings; optional keyword overrides exist for tests.
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

from clock import now_iso
from errors import ServiceError
from ids import identifier
from services.chat import submit_message_conn
from storage import Store

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

_webhook_logger = logging.getLogger('channels.feishu.webhook')


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
    only touched by the delivery pump (channels.feishu.delivery), never here.
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
            _webhook_logger.warning('Webhook body too large: %s bytes', len(body))
            return FeishuResponse(413, _BODY_413, {'Content-Type': 'application/json'})
        normalized = _normalize_headers(headers)
        if not self._timestamp_is_fresh(normalized):
            _webhook_logger.warning('Webhook timestamp out of tolerance')
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
            _webhook_logger.error('Webhook persistence failed: %s', self._callback_error.error)
            return FeishuResponse(503, _BODY_503, {'Content-Type': 'application/json'})
        if response.status_code == 200:
            return FeishuResponse(200, response.content or b'',
                                  {'Content-Type': response.headers.get('Content-Type', 'application/json')})
        # SDK 500 means verification failure or an unsupported/unregistered event
        # type; the SDK body would embed error details, so it is replaced.
        _webhook_logger.warning('Webhook SDK rejected (status=%s)', response.status_code)
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
            self.store.feishu.lock_event(conn, s.feishu_app_id, tenant_id, event_id, message_id)
            if self.store.feishu.event_seen(conn, s.feishu_app_id, tenant_id, event_id):
                return None
            if message_id and self.store.feishu.message_seen(conn, message_id):
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
        self.store.feishu.record_event(conn, self.settings.feishu_app_id, tenant_id, event_id,
                                       message_id or None, now_iso())

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
            self.store.sessions.create(conn, session_id, binding['user_id'], 'Feishu conversation', now_iso())
            self.store.channels.set_session(conn, binding['id'], session_id)
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_SESSION_SWITCHED)
        if command == '/switch':
            target = parts[1] if len(parts) == 2 else ''
            if not self.store.sessions.exists_for_user(conn, target, binding['user_id']):
                return self._enqueue_reply(conn, tenant_id, open_id, REPLY_INVALID_COMMAND)
            self.store.channels.set_session(conn, binding['id'], target)
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_SESSION_SWITCHED)
        if command == '/stop':
            self.store.channels.deactivate(conn, binding['id'])
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_STOPPED)
        if command == '/help':
            return self._enqueue_reply(conn, tenant_id, open_id, REPLY_HELP_COMMANDS)
        return self._enqueue_reply(conn, tenant_id, open_id, REPLY_INVALID_COMMAND)

    def _bind_attempt_allowed(self, conn, tenant_id: str, open_id: str) -> bool:
        """Bounded attempt counter keyed by the verified external identity."""
        key = f'{self.settings.feishu_app_id}:{tenant_id}:{open_id}'
        return self.store.feishu.bind_attempt_allowed(conn, key, self.bind_window_seconds,
                                                      self.bind_max_attempts, now_iso())

    def _consume_code(self, conn, code_hash: str, tenant_id: str, open_id: str) -> dict:
        """Shared binding logic (Store.consume_binding_code_conn) in the caller's transaction."""
        return self.store.consume_binding_code_conn(conn, code_hash, self.settings.feishu_app_id,
                                                    tenant_id, open_id)

    # ------------------------------------------------------------ agent submission

    def _submit(self, conn, binding: dict, text: str, event_id: str) -> str | None:
        """Same as chat.submit_message(source='feishu') in the caller's transaction.

        Dedupe + run + event record commit together, so a crash before submit
        leaves no dedupe row and Feishu redelivery re-runs the pipeline. The
        event_id doubles as client_message_id, giving run-level idempotency.
        """
        if len(text) > MAX_TEXT_CHARS:
            return self._enqueue_reply(conn, binding['tenant_id'], binding['external_user_id'], REPLY_TOO_LONG)
        submit_message_conn(conn, self.store, self.settings, binding['user_id'], binding['session_id'],
                            text, event_id, source='feishu', binding_id=binding['id'],
                            reply_target=binding['external_user_id'])
        return None

    # ------------------------------------------------------------ reply outbox

    def _enqueue_reply(self, conn, tenant_id: str, open_id: str, content: str) -> str:
        reply_id = identifier()
        self.store.feishu.enqueue_reply(conn, reply_id, self.settings.feishu_app_id, tenant_id, open_id,
                                        content, f'rp-{reply_id}', now_iso())
        return content

    def _binding(self, conn, tenant_id: str, open_id: str) -> dict | None:
        return self.store.channels.active_identity(conn, self.settings.feishu_app_id, tenant_id, open_id)
