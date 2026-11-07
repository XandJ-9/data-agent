"""Local accounts and opaque, hashed server-side browser sessions."""
import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from data_agent.db import ServiceError, Store, identifier

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash('non-account-timing-placeholder')
COOKIE_NAME = 'data_agent_session'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(store: Store, username: str, password: str) -> dict:
    username = username.strip()
    if not 1 <= len(username) <= 100 or not 12 <= len(password) <= 1024:
        raise ServiceError(400, 'Username required; password must contain 12 to 1024 characters')
    user = {'id': identifier(), 'username': username}
    password_hash = HASHER.hash(password)
    with store.transaction() as conn:
        if conn.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            raise ServiceError(409, 'Username already exists')
        conn.execute('INSERT INTO users VALUES(?,?,?)', (user['id'], username, password_hash))
        conn.execute('INSERT INTO user_memories(user_id) VALUES(?)', (user['id'],))
    return user


def login(store: Store, username: str, password: str, seconds: int, peer: str) -> tuple[str, dict]:
    now = time.time()
    # Both account and source counters prevent distributed guessing and account spraying.
    keys = [digest('account:' + username), digest('peer:' + peer)]
    with store.transaction() as conn:
        conn.execute('DELETE FROM login_attempts WHERE window_start<?', (now - 300,))
        for key in keys:
            row = conn.execute('SELECT attempts FROM login_attempts WHERE key=?', (key,)).fetchone()
            if row and row['attempts'] >= 20:
                raise ServiceError(429, 'Too many login attempts; try again later')
        for key in keys:
            conn.execute('INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(key) '
                         'DO UPDATE SET attempts=attempts+1', (key, now))
        row = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    try:
        HASHER.verify(row['password_hash'] if row else DUMMY_HASH, password)
    except VerificationError:
        raise ServiceError(401, 'Invalid username or password') from None
    if row is None:
        raise ServiceError(401, 'Invalid username or password')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.transaction() as conn:
        conn.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (now,))
        conn.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)', (digest(token), row['id'], csrf, now + seconds))
    return token, {'user': {'id': row['id'], 'username': row['username']}, 'csrf_token': csrf}


def authenticate(store: Store, token: str | None) -> dict:
    if not token:
        raise ServiceError(401, 'Authentication required')
    with store.transaction(immediate=False) as conn:
        row = conn.execute('SELECT u.id,u.username,a.csrf_token FROM auth_sessions a JOIN users u ON u.id=a.user_id '
                           'WHERE a.token_hash=? AND a.expires_at>?', (digest(token), time.time())).fetchone()
    if row is None:
        raise ServiceError(401, 'Authentication required')
    return {'user': {'id': row['id'], 'username': row['username']}, 'csrf_token': row['csrf_token']}


def logout(store: Store, token: str) -> None:
    with store.transaction() as conn:
        conn.execute('DELETE FROM auth_sessions WHERE token_hash=?', (digest(token),))
