"""Feishu delivery pump: segmented sending with lease-based claims and honest outcomes.

Owns the durable half of channel delivery. Runs are never re-executed here:
the pump only sends already-persisted delivery rows (created by
Store.finish_run) and reply outbox rows (created by the webhook pipeline).

State machine per row: pending -> sending (lease claimed) -> delivered |
failed (bounded API retries with backoff) | unknown (transport timeout, or a
stale 'sending' whose lease expired). 'unknown' is terminal for the pump --
only a user-triggered retry in the core API resets it -- because a timeout
may have actually delivered the message and a blind resend would duplicate.

Every send carries a stable uuid (deliveries: dl-<delivery_id>:<part index>;
replies: the uuid stored at enqueue time) so Feishu's own idempotency window
absorbs resends of the same logical message. SDK calls happen strictly
outside database transactions; each claimed row is re-validated (binding
still active and target unchanged) immediately before sending.
"""
import json
import threading
import time
from collections.abc import Callable

FEISHU_TEXT_LIMIT = 3000
MAX_ATTEMPTS = 8
BACKOFF_BASE = 2.0
BACKOFF_CAP = 300.0
DELIVERY_TABLE = 'deliveries'
REPLY_TABLE = 'feishu_replies'


class DeliveryError(Exception):
    """Base class for transport-side send outcomes."""


class DeliveryTimeout(DeliveryError):
    """Transport timeout / connection failure: the message may or may not have arrived."""


class DeliveryApiError(DeliveryError):
    """Feishu API rejected the send with a business error code; it did not arrive."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def delivery_parts(text: str, limit: int = FEISHU_TEXT_LIMIT) -> list[str]:
    """Split text into ordered parts of at most `limit` UTF-8 bytes; join(parts) == text."""
    if limit < 1:
        raise ValueError('limit must be positive')
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        char_size = len(char.encode())
        if current and size + char_size > limit:
            parts.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += char_size
    if current:
        parts.append(''.join(current))
    return parts


def partition_reply(text: str, limit: int = FEISHU_TEXT_LIMIT) -> list[str]:
    """Like delivery_parts, but multi-part replies carry stable ' (i/n)' markers."""
    parts = delivery_parts(text, limit)
    if len(parts) == 1:
        return parts
    count = len(parts)
    while True:
        overhead = len(f' (1/{count})')
        parts = delivery_parts(text, max(limit - overhead, 1))
        if len(parts) == count:
            break
        count = len(parts)
    return [part + f' ({index}/{count})' for index, part in enumerate(parts, start=1)]


def _claim_batch(store, table: str, order_column: str, owner: str, lease_seconds: int,
                 clock: Callable[[], float], limit: int) -> list[dict]:
    now = clock()
    with store.transaction() as conn:
        conn.execute(f"UPDATE {table} SET status='unknown',generation=generation+1 "
                     "WHERE status='sending' AND lease_until<=?", (now,))
        rows = conn.execute(f'SELECT id FROM {table} WHERE status="pending" AND retry_at<=? '
                            f'ORDER BY {order_column},id LIMIT ?', (now, limit)).fetchall()
        claimed = []
        for row in rows:
            cursor = conn.execute(f"UPDATE {table} SET status='sending',owner=?,generation=generation+1,"
                                  'lease_until=? WHERE id=? AND status="pending"',
                                  (owner, now + lease_seconds, row['id']))
            if cursor.rowcount == 1:
                claimed.append(dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (row['id'],)).fetchone()))
        return claimed


def _finish(store, table: str, row: dict, status: str, *, receipt: str | None = None,
            error_code: str | None = None, retry_at: float = 0,
            clock: Callable[[], float] = time.time) -> bool:
    """Fenced transition out of 'sending'; a lost lease means another owner moved on."""
    with store.transaction() as conn:
        cursor = conn.execute(f'UPDATE {table} SET status=?,receipt=?,error_code=?,attempts=attempts+1,'
                              'retry_at=? WHERE id=? AND status="sending" AND owner=? AND generation=?',
                              (status, receipt, error_code, retry_at, row['id'], row['owner'], row['generation']))
        return cursor.rowcount == 1


def _binding_valid(store, binding_id: str, reply_target: str) -> bool:
    """Re-validate before any network send: binding active and target still the bound open_id."""
    with store.transaction(immediate=False) as conn:
        row = conn.execute('SELECT active,external_user_id FROM channel_identities WHERE id=?',
                           (binding_id,)).fetchone()
    return row is not None and bool(row['active']) and row['external_user_id'] == reply_target


def _backoff(attempts: int, clock: Callable[[], float]) -> float:
    return clock() + min(BACKOFF_BASE ** max(attempts, 1), BACKOFF_CAP)


def process_deliveries(store, sender, *, app_id: str, app_secret: str, owner: str = 'feishu-delivery',
                       lease_seconds: int = 120, clock: Callable[[], float] = time.time,
                       limit: int = 50) -> int:
    """Claim pending deliveries and send every <=3000-byte part in order."""
    claimed = _claim_batch(store, DELIVERY_TABLE, 'retry_at', owner, lease_seconds, clock, limit)
    sent = 0
    for row in claimed:
        if not _binding_valid(store, row['binding_id'], row['reply_target']):
            if _finish(store, DELIVERY_TABLE, row, 'failed', error_code='binding_revoked', clock=clock):
                sent += 1
            continue
        parts = partition_reply(row['content'])
        try:
            receipt = None
            for index, part in enumerate(parts):
                receipt = sender(app_id, app_secret, row['reply_target'], part, f"dl-{row['id']}:{index}")
        except DeliveryTimeout:
            if _finish(store, DELIVERY_TABLE, row, 'unknown', error_code='timeout', clock=clock):
                sent += 1
            continue
        except DeliveryApiError as exc:
            attempts = row['attempts'] + 1
            if attempts >= MAX_ATTEMPTS:
                done = _finish(store, DELIVERY_TABLE, row, 'failed', error_code=exc.code, clock=clock)
            else:
                done = _finish(store, DELIVERY_TABLE, row, 'pending', error_code=exc.code,
                               retry_at=_backoff(attempts, clock), clock=clock)
            if done:
                sent += 1
            continue
        if _finish(store, DELIVERY_TABLE, row, 'delivered', receipt=receipt, clock=clock):
            sent += 1
    return sent


def process_replies(store, sender, *, app_id: str, app_secret: str, owner: str = 'feishu-delivery',
                    lease_seconds: int = 120, clock: Callable[[], float] = time.time,
                    limit: int = 50) -> int:
    """Claim pending reply-outbox rows and send them; the enqueue-time uuid is reused verbatim.

    Unlike data deliveries, replies are command acknowledgements and are NOT
    re-validated against the binding: a /stop confirmation must still arrive
    after the binding was deactivated.
    """
    claimed = _claim_batch(store, REPLY_TABLE, 'created_at', owner, lease_seconds, clock, limit)
    sent = 0
    for row in claimed:
        try:
            receipt = sender(app_id, app_secret, row['reply_target'], row['content'], row['uuid'])
        except DeliveryTimeout:
            if _finish(store, REPLY_TABLE, row, 'unknown', error_code='timeout', clock=clock):
                sent += 1
            continue
        except DeliveryApiError as exc:
            attempts = row['attempts'] + 1
            if attempts >= MAX_ATTEMPTS:
                done = _finish(store, REPLY_TABLE, row, 'failed', error_code=exc.code, clock=clock)
            else:
                done = _finish(store, REPLY_TABLE, row, 'pending', error_code=exc.code,
                               retry_at=_backoff(attempts, clock), clock=clock)
            if done:
                sent += 1
            continue
        if _finish(store, REPLY_TABLE, row, 'delivered', receipt=receipt, clock=clock):
            sent += 1
    return sent


def run_delivery_worker(store, sender, *, app_id: str, app_secret: str, owner: str = 'feishu-delivery',
                        poll_seconds: float = 1.0, lease_seconds: int = 120,
                        clock: Callable[[], float] = time.time,
                        stop: Callable[[], bool] | None = None) -> None:
    """Blocking pump loop for the Worker process; replies first, then data deliveries."""
    while stop is None or not stop():
        sent = (process_replies(store, sender, app_id=app_id, app_secret=app_secret, owner=owner,
                                lease_seconds=lease_seconds, clock=clock)
                + process_deliveries(store, sender, app_id=app_id, app_secret=app_secret, owner=owner,
                                     lease_seconds=lease_seconds, clock=clock))
        if not sent:
            time.sleep(poll_seconds)


class SdkSender:
    """Production transport adapter backed by the official lark-oapi Client.

    Construction performs no I/O; the client is built lazily on first send.
    Transport failures (including SDK timeouts) surface as DeliveryTimeout so
    the pump records an honest 'unknown' instead of retrying blindly.
    """

    def __init__(self, app_id: str, app_secret: str, timeout: float = 10.0):
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                from lark_oapi import Client
                self._client = Client.builder().app_id(self._app_id).app_secret(self._app_secret) \
                    .timeout(self._timeout).build()
            return self._client

    def __call__(self, app_id: str, app_secret: str, receive_id: str, content: str, uuid: str) -> str:
        if app_id != self._app_id or app_secret != self._app_secret:
            raise DeliveryApiError('credential_mismatch')
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        client = self._get_client()
        request = CreateMessageRequest.builder().receive_id_type('open_id').request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id).msg_type('text')
            .content(json.dumps({'text': content})).uuid(uuid).build()).build()
        try:
            response = client.im.v1.message.create(request)
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryTimeout from exc
        if not response.success():
            raise DeliveryApiError(str(response.code))
        return getattr(response.data, 'message_id', None) or 'sent'
