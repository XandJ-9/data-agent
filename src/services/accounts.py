"""Account use cases: local accounts and opaque, hashed server-side browser sessions."""
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from clock import now_iso, now_plus
from errors import ServiceError
from ids import identifier
from storage import Store

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash('non-account-timing-placeholder')
COOKIE_NAME = 'data_agent_session'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(store: Store, username: str, password: str, *, admin: bool = False) -> dict:
    username = username.strip()
    if not 1 <= len(username) <= 100 or not 12 <= len(password) <= 1024:
        raise ServiceError(400, 'Username required; password must contain 12 to 1024 characters')
    user = {'id': identifier(), 'username': username}
    password_hash = HASHER.hash(password)
    with store.transaction() as conn:
        if store.accounts.username_taken(conn, username):
            raise ServiceError(409, 'Username already exists')
        store.accounts.insert_user(conn, user['id'], username, password_hash)
        store.memory.insert_default(conn, user['id'])
        if admin:
            store.accounts.set_admin(conn, user['id'], True)
    return user


def set_admin(store: Store, username: str, admin: bool = True) -> dict:
    with store.transaction() as conn:
        user_id = store.accounts.user_id_by_username(conn, username)
        if user_id is None:
            raise ServiceError(404, 'User not found')
        store.accounts.set_admin(conn, user_id, admin)
        return {'username': username, 'is_admin': admin}


def login(store: Store, username: str, password: str, seconds: int, peer: str) -> tuple[str, dict]:
    now = now_iso()
    # Both account and source counters prevent distributed guessing and account spraying.
    keys = [digest('account:' + username), digest('peer:' + peer)]
    with store.transaction() as conn:
        store.accounts.purge_expired_attempts(conn, now_plus(-300))
        for key in sorted(keys):
            if not store.accounts.record_attempt(conn, key, now):
                raise ServiceError(429, 'Too many login attempts; try again later')
        row = store.accounts.user_by_username(conn, username)
    try:
        HASHER.verify(row['password_hash'] if row else DUMMY_HASH, password)
    except VerificationError:
        raise ServiceError(401, 'Invalid username or password') from None
    if row is None:
        raise ServiceError(401, 'Invalid username or password')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.transaction() as conn:
        store.accounts.purge_expired_sessions(conn, now)
        store.accounts.insert_session(conn, digest(token), row['id'], csrf, now_plus(seconds))
        admin = store.accounts.is_admin(conn, row['id'])
    return token, {'user': {'id': row['id'], 'username': row['username']},
                   'csrf_token': csrf, 'is_admin': admin}


def authenticate(store: Store, token: str | None) -> dict:
    if not token:
        raise ServiceError(401, 'Authentication required')
    with store.transaction(immediate=False) as conn:
        row = store.accounts.identity_by_token(conn, digest(token), now_iso())
    if row is None:
        raise ServiceError(401, 'Authentication required')
    return {'user': {'id': row['id'], 'username': row['username']},
            'csrf_token': row['csrf_token'], 'is_admin': bool(row['is_admin'])}


def logout(store: Store, token: str) -> None:
    with store.transaction() as conn:
        store.accounts.delete_session(conn, digest(token))
