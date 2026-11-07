"""Standalone browser API. All mutation requests require an exact trusted Origin."""
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from data_agent import auth
from data_agent.chat import installed_skills, submit_message
from data_agent.db import ServiceError, Store
from data_agent.feishu import FeishuModule, generate_code, sha256_hex
from data_agent.memory import forget_memory, get_memory, replace_memory
from data_agent.settings import Settings


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class NewSession(Input):
    title: str = Field(default='New conversation', min_length=1, max_length=200)


class Message(Input):
    text: str = Field(min_length=1, max_length=16000)
    client_message_id: str = Field(min_length=1, max_length=200)


class MemoryEntry(Input):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1000)
    source_message_ids: list[int] = Field(default_factory=list, max_length=50)


class MemoryEdit(Input):
    expected_revision: int = Field(ge=0)
    entries: list[MemoryEntry] = Field(max_length=100)


class Forget(Input):
    expected_revision: int = Field(ge=0)
    entry_id: str = Field(min_length=1, max_length=100)


class Enable(Input):
    enabled: bool


class SwitchSession(Input):
    session_id: str = Field(min_length=1, max_length=100)


class Retry(Input):
    confirm_duplicate_risk: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.check_schema()
        yield

    app = FastAPI(title='Data Agent', lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    feishu = FeishuModule(store, settings)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({'detail': 'Invalid request data'}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        return JSONResponse({'detail': 'Storage temporarily unavailable'}, status_code=503)

    @app.middleware('http')
    async def origin_guard(request: Request, call_next):
        if request.url.path.startswith('/api/v1/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('origin') != settings.public_origin:
                return JSONResponse({'detail': 'Untrusted request origin'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def current(request: Request) -> dict:
        identity = auth.authenticate(store, request.cookies.get(auth.COOKIE_NAME))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(supplied, identity['csrf_token']):
                raise ServiceError(403, 'CSRF token required')
        return identity

    @app.post('/api/v1/auth/login')
    def login(data: Login, request: Request, response: Response):
        token, identity = auth.login(store, data.username, data.password, settings.session_seconds,
                                     request.client.host if request.client else 'unknown')
        old = request.cookies.get(auth.COOKIE_NAME)
        if old:
            auth.logout(store, old)
        response.set_cookie(auth.COOKIE_NAME, token, httponly=True, secure=settings.cookie_secure,
                            samesite='strict', max_age=settings.session_seconds, path='/')
        return identity

    @app.get('/api/v1/auth/me')
    def me(identity: dict = Depends(current)):
        return identity

    @app.post('/api/v1/auth/logout')
    def logout(request: Request, response: Response, identity: dict = Depends(current)):
        auth.logout(store, request.cookies[auth.COOKIE_NAME])
        response.delete_cookie(auth.COOKIE_NAME, path='/')
        return {'ok': True}

    @app.get('/api/v1/sessions')
    def sessions(identity: dict = Depends(current)):
        return {'items': store.list_sessions(identity['user']['id'])}

    @app.post('/api/v1/sessions')
    def new_session(data: NewSession, identity: dict = Depends(current)):
        return store.create_session(identity['user']['id'], data.title)

    @app.get('/api/v1/sessions/{session_id}/messages')
    def messages(session_id: str, before_id: int | None = Query(default=None, ge=1),
                 identity: dict = Depends(current)):
        return store.messages(identity['user']['id'], session_id, before_id)

    @app.post('/api/v1/sessions/{session_id}/messages', status_code=202)
    def send(session_id: str, data: Message, identity: dict = Depends(current)):
        return submit_message(store, settings, identity['user']['id'], session_id, data.text, data.client_message_id)

    @app.get('/api/v1/runs/{run_id}')
    def run(run_id: str, identity: dict = Depends(current)):
        return store.get_run(identity['user']['id'], run_id)

    @app.post('/api/v1/runs/{run_id}/cancel')
    def cancel(run_id: str, identity: dict = Depends(current)):
        return store.cancel_run(identity['user']['id'], run_id)

    @app.get('/api/v1/memory')
    def memory(identity: dict = Depends(current)):
        return get_memory(store, identity['user']['id'])

    @app.put('/api/v1/memory')
    def edit_memory(data: MemoryEdit, identity: dict = Depends(current)):
        return replace_memory(store, identity['user']['id'], data.expected_revision,
                              [e.model_dump() for e in data.entries])

    @app.post('/api/v1/memory/forget')
    def forget(data: Forget, identity: dict = Depends(current)):
        return forget_memory(store, identity['user']['id'], data.expected_revision, data.entry_id)

    @app.get('/api/v1/skills')
    def skills(identity: dict = Depends(current)):
        enabled = store.enabled_skills(identity['user']['id'])
        return {'items': [{**{k: s[k] for k in ('name', 'description', 'version')},
                           'enabled': enabled.get(s['name'], True)} for s in installed_skills(settings)]}

    @app.put('/api/v1/skills/{name}')
    def enable_skill(name: str, data: Enable, identity: dict = Depends(current)):
        if name not in {s['name'] for s in installed_skills(settings)}:
            raise ServiceError(404, 'Skill not found')
        store.set_skill(identity['user']['id'], name, data.enabled)
        return {'name': name, 'enabled': data.enabled}

    @app.get('/api/v1/bindings/feishu')
    def binding(identity: dict = Depends(current)):
        bound = store.binding(identity['user']['id'])
        return {'enabled': bool(settings.feishu_app_id and settings.feishu_app_secret.get_secret_value()),
                'binding': {k: bound[k] for k in ('id', 'external_user_id', 'session_id')} if bound else None}

    @app.post('/api/v1/bindings/feishu/codes')
    def binding_code(identity: dict = Depends(current)):
        code, expires = generate_code(), time.time() + 600
        with store.transaction() as conn:
            conn.execute('DELETE FROM binding_codes WHERE user_id=?', (identity['user']['id'],))
            conn.execute('INSERT INTO binding_codes(code_hash,user_id,expires_at) VALUES(?,?,?)',
                         (sha256_hex(code), identity['user']['id'], expires))
        return {'code': code, 'expires_at': expires}

    @app.delete('/api/v1/bindings/feishu')
    def unbind(identity: dict = Depends(current)):
        with store.transaction() as conn:
            conn.execute('UPDATE channel_identities SET active=0 WHERE user_id=?', (identity['user']['id'],))
            conn.execute('DELETE FROM binding_codes WHERE user_id=?', (identity['user']['id'],))
        return {'ok': True}

    @app.put('/api/v1/bindings/feishu/session')
    def switch(data: SwitchSession, identity: dict = Depends(current)):
        with store.transaction() as conn:
            store.own_session(conn, identity['user']['id'], data.session_id)
            changed = conn.execute('UPDATE channel_identities SET session_id=? WHERE user_id=? AND active=1',
                                   (data.session_id, identity['user']['id'])).rowcount
            if not changed:
                raise ServiceError(404, 'Binding not found')
        return binding(identity)

    @app.post('/api/v1/deliveries/{delivery_id}/retry')
    def retry(delivery_id: str, data: Retry, identity: dict = Depends(current)):
        with store.transaction() as conn:
            row = conn.execute('SELECT d.* FROM deliveries d JOIN runs r ON r.id=d.run_id '
                               'WHERE d.id=? AND r.user_id=?', (delivery_id, identity['user']['id'])).fetchone()
            if row is None:
                raise ServiceError(404, 'Delivery not found')
            if row['status'] == 'unknown' and not data.confirm_duplicate_risk:
                raise ServiceError(409, 'Delivery may have succeeded; confirm duplicate risk')
            if row['status'] not in ('unknown', 'failed'):
                raise ServiceError(409, 'Delivery cannot be retried in its current state')
            if not conn.execute('SELECT 1 FROM channel_identities WHERE id=? AND active=1',
                                (row['binding_id'],)).fetchone():
                raise ServiceError(409, 'Channel binding is unavailable')
            conn.execute("UPDATE deliveries SET status='pending',retry_at=0,error_code=NULL,generation=generation+1 "
                         'WHERE id=?', (delivery_id,))
        return {'id': delivery_id, 'status': 'pending'}

    @app.post('/webhook/feishu')
    async def feishu_webhook(request: Request):
        # SDK verification/decryption is the only auth; browser CSRF does not apply.
        body = await request.body()
        result = feishu.handle_webhook(body, dict(request.headers))
        return Response(content=result.content, status_code=result.status_code,
                        media_type='application/json', headers=dict(result.headers))

    web = Path(settings.web_dist)
    if (web / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=web / 'assets'), name='assets')
    if (web / 'index.html').is_file():
        @app.get('/')
        def index():
            return FileResponse(web / 'index.html')

    return app
