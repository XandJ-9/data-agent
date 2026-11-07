"""Browser API routes. All mutation requests require an exact trusted Origin."""
import json
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response

from api import deps, schemas
from channels.feishu.webhook import generate_code, sha256_hex
from errors import ServiceError
from ids import identifier
from mcp_gateway.manager import McpGateway
from services import accounts
from services.chat import installed_skills, submit_message
from services.memory import forget_memory, get_memory, replace_memory
from skills.loader import parse_skill

router = APIRouter()


# ---------------------------------------------------------------- auth

@router.post('/api/v1/auth/login')
def login(data: schemas.Login, request: Request, response: Response):
    settings = request.app.state.settings
    token, identity = accounts.login(request.app.state.store, data.username, data.password,
                                     settings.session_seconds,
                                     request.client.host if request.client else 'unknown')
    old = request.cookies.get(accounts.COOKIE_NAME)
    if old:
        accounts.logout(request.app.state.store, old)
    response.set_cookie(accounts.COOKIE_NAME, token, httponly=True, secure=settings.cookie_secure,
                        samesite='strict', max_age=settings.session_seconds, path='/')
    return identity


@router.get('/api/v1/auth/me')
def me(identity: dict = Depends(deps.current)):
    return identity


@router.post('/api/v1/auth/logout')
def logout(request: Request, response: Response, identity: dict = Depends(deps.current)):
    accounts.logout(request.app.state.store, request.cookies[accounts.COOKIE_NAME])
    response.delete_cookie(accounts.COOKIE_NAME, path='/')
    return {'ok': True}


# ---------------------------------------------------------------- sessions & runs

@router.get('/api/v1/sessions')
def sessions(archived: bool = Query(default=False),
             identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return {'items': store.list_sessions(identity['user']['id'], archived=archived)}


@router.post('/api/v1/sessions')
def new_session(data: schemas.NewSession, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.create_session(identity['user']['id'], data.title)


@router.patch('/api/v1/sessions/{session_id}')
def rename_session(session_id: str, data: schemas.UpdateSession,
                   identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.rename_session(identity['user']['id'], session_id, data.title)


@router.post('/api/v1/sessions/{session_id}/archive')
def archive_session(session_id: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.archive_session(identity['user']['id'], session_id)


@router.post('/api/v1/sessions/{session_id}/restore')
def restore_session(session_id: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.restore_session(identity['user']['id'], session_id)


@router.get('/api/v1/sessions/{session_id}/messages')
def messages(session_id: str, before_id: int | None = Query(default=None, ge=1),
             identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.messages(identity['user']['id'], session_id, before_id)


@router.post('/api/v1/sessions/{session_id}/messages', status_code=202)
def send(session_id: str, data: schemas.Message, request: Request, identity: dict = Depends(deps.current)):
    return submit_message(request.app.state.store, request.app.state.settings,
                          identity['user']['id'], session_id, data.text, data.client_message_id)


@router.get('/api/v1/runs/{run_id}')
def run(run_id: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.get_run(identity['user']['id'], run_id)


@router.get('/api/v1/runs/{run_id}/steps')
def run_steps(run_id: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    store.get_run(identity['user']['id'], run_id)  # ownership check
    return store.list_steps(run_id)


@router.post('/api/v1/runs/{run_id}/cancel')
def cancel(run_id: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return store.cancel_run(identity['user']['id'], run_id)


# ---------------------------------------------------------------- memory

@router.get('/api/v1/memory')
def memory(identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return get_memory(store, identity['user']['id'])


@router.put('/api/v1/memory')
def edit_memory(data: schemas.MemoryEdit, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return replace_memory(store, identity['user']['id'], data.expected_revision,
                          [e.model_dump() for e in data.entries])


@router.post('/api/v1/memory/forget')
def forget(data: schemas.Forget, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    return forget_memory(store, identity['user']['id'], data.expected_revision, data.entry_id)


# ---------------------------------------------------------------- skills

@router.get('/api/v1/skills')
def skills(request: Request, identity: dict = Depends(deps.current)):
    store, settings = request.app.state.store, request.app.state.settings
    enabled = store.enabled_skills(identity['user']['id'])
    return {'items': [{**{k: s[k] for k in ('name', 'description', 'version')},
                       'enabled': enabled.get(s['name'], True)} for s in installed_skills(settings)]}


@router.put('/api/v1/skills/{name}')
def enable_skill(name: str, data: schemas.Enable, request: Request, identity: dict = Depends(deps.current)):
    store, settings = request.app.state.store, request.app.state.settings
    if name not in {s['name'] for s in installed_skills(settings)}:
        raise ServiceError(404, 'Skill not found')
    store.set_skill(identity['user']['id'], name, data.enabled)
    return {'name': name, 'enabled': data.enabled}


def _require_admin(identity: dict) -> None:
    if not identity.get('is_admin'):
        raise ServiceError(403, 'Administrator required')


def _skill_folder(settings, name: str) -> Path:
    root = Path(settings.skills_path).resolve()
    folder = (root / name).resolve()
    if folder == root or not folder.is_relative_to(root):
        raise ServiceError(404, 'Skill not found')
    return folder


@router.post('/api/v1/skills')
def upload_skill(data: schemas.SkillUpload, request: Request, identity: dict = Depends(deps.current)):
    """Admin-only: validate and write SKILL.md into the deployment directory
    (atomic replace, same validation the loader applies on read)."""
    _require_admin(identity)
    try:
        skill = parse_skill(data.content)
    except ValueError as exc:
        raise ServiceError(400, str(exc)) from None
    folder = _skill_folder(request.app.state.settings, skill['name'])
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / '.SKILL.md.tmp'
    tmp.write_text(skill['content'], encoding='utf-8')
    tmp.replace(folder / 'SKILL.md')
    return {k: skill[k] for k in ('name', 'description', 'version')}


@router.delete('/api/v1/skills/{name}')
def delete_skill(name: str, request: Request, identity: dict = Depends(deps.current)):
    _require_admin(identity)
    folder = _skill_folder(request.app.state.settings, name)
    if not folder.is_dir():
        raise ServiceError(404, 'Skill not found')
    shutil.rmtree(folder)
    return {'name': name, 'deleted': True}


# ---------------------------------------------------------------- mcp servers


def _mcp_fields(data: schemas.McpServerCreate | schemas.McpServerUpdate) -> dict:
    if data.transport == 'stdio' and not (data.command and data.command.strip()):
        raise ServiceError(400, 'stdio transport requires a command')
    if data.transport == 'http' and not (data.url and data.url.startswith(('http://', 'https://'))):
        raise ServiceError(400, 'http transport requires an http(s) url')
    return {
        'transport': data.transport,
        'command': (data.command or '').strip() or None,
        'args_json': json.dumps(data.args),
        'env_json': json.dumps(data.env),
        'url': data.url,
        'headers_json': json.dumps(data.headers),
        'enabled': int(data.enabled),
    }


def _public_mcp(row: dict) -> dict:
    return {'name': row['name'], 'transport': row['transport'], 'command': row['command'],
            'args': json.loads(row['args_json']), 'env': json.loads(row['env_json']),
            'url': row['url'], 'headers': json.loads(row['headers_json']),
            'enabled': bool(row['enabled'])}


@router.get('/api/v1/mcp/servers')
def mcp_servers(identity: dict = Depends(deps.current), store=Depends(deps.store)):
    _require_admin(identity)
    return {'items': [_public_mcp(row) for row in store.list_mcp_servers()]}


@router.post('/api/v1/mcp/servers')
def create_mcp_server(data: schemas.McpServerCreate, identity: dict = Depends(deps.current),
                      store=Depends(deps.store)):
    _require_admin(identity)
    fields = {'id': identifier(), 'name': data.name, 'created_at': time.time(), **_mcp_fields(data)}
    store.upsert_mcp_server(fields)
    return _public_mcp({'created_at': fields['created_at'], **fields})


@router.put('/api/v1/mcp/servers/{name}')
def update_mcp_server(name: str, data: schemas.McpServerUpdate,
                      identity: dict = Depends(deps.current), store=Depends(deps.store)):
    _require_admin(identity)
    if not any(row['name'] == name for row in store.list_mcp_servers()):
        raise ServiceError(404, 'Server not found')
    store.upsert_mcp_server({'id': identifier(), 'name': name,
                             'created_at': time.time(), **_mcp_fields(data)})
    return _public_mcp({'name': name, 'created_at': time.time(), **_mcp_fields(data)})


@router.delete('/api/v1/mcp/servers/{name}')
def delete_mcp_server(name: str, identity: dict = Depends(deps.current), store=Depends(deps.store)):
    _require_admin(identity)
    if not store.delete_mcp_server(name):
        raise ServiceError(404, 'Server not found')
    return {'name': name, 'deleted': True}


@router.post('/api/v1/mcp/servers/{name}/test')
async def test_mcp_server(name: str, identity: dict = Depends(deps.current),
                          store=Depends(deps.store)):
    """Short-lived connection check: list the tools this server exposes."""
    _require_admin(identity)
    row = next((r for r in store.list_mcp_servers() if r['name'] == name), None)
    if row is None:
        raise ServiceError(404, 'Server not found')
    gateway = McpGateway(store)
    try:
        definitions = gateway.tool_definitions(refresh=True)
    except Exception:
        raise ServiceError(502, 'Connection failed') from None
    finally:
        gateway.close()
    return {'name': name, 'tools': [
        {'name': d.name, 'description': d.description} for d in definitions
        if d.name.startswith(f'mcp_{name}_')]}


# ---------------------------------------------------------------- feishu binding

@router.get('/api/v1/bindings/feishu')
def binding(request: Request, identity: dict = Depends(deps.current)):
    store, settings = request.app.state.store, request.app.state.settings
    bound = store.binding(identity['user']['id'])
    return {'enabled': bool(settings.feishu_app_id and settings.feishu_app_secret.get_secret_value()),
            'binding': {k: bound[k] for k in ('id', 'external_user_id', 'session_id')} if bound else None}


@router.post('/api/v1/bindings/feishu/codes')
def binding_code(identity: dict = Depends(deps.current), store=Depends(deps.store)):
    code, expires = generate_code(), time.time() + 600
    store.create_binding_code(identity['user']['id'], sha256_hex(code), expires)
    return {'code': code, 'expires_at': expires}


@router.delete('/api/v1/bindings/feishu')
def unbind(identity: dict = Depends(deps.current), store=Depends(deps.store)):
    store.unbind_user(identity['user']['id'])
    return {'ok': True}


@router.put('/api/v1/bindings/feishu/session')
def switch(data: schemas.SwitchSession, request: Request, identity: dict = Depends(deps.current),
           store=Depends(deps.store)):
    store.switch_binding_session(identity['user']['id'], data.session_id)
    return binding(request, identity)


@router.post('/api/v1/deliveries/{delivery_id}/retry')
def retry(delivery_id: str, data: schemas.Retry, identity: dict = Depends(deps.current),
          store=Depends(deps.store)):
    return store.retry_delivery(identity['user']['id'], delivery_id,
                                confirm_duplicate_risk=data.confirm_duplicate_risk)


# ---------------------------------------------------------------- feishu webhook

@router.post('/webhook/feishu')
async def feishu_webhook(request: Request):
    # SDK verification/decryption is the only auth; browser CSRF does not apply.
    body = await request.body()
    result = request.app.state.feishu.handle_webhook(body, dict(request.headers))
    return Response(content=result.content, status_code=result.status_code,
                    media_type='application/json', headers=dict(result.headers))
