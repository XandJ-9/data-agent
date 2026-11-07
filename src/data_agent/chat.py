"""Atomic message ingestion and bounded, user-scoped runtime context."""
import hashlib
import json
import time

from data_agent.db import ServiceError, Store, identifier
from data_agent.settings import Settings

CONFIG_FIELDS = ('llm_model', 'llm_base_url', 'llm_timeout', 'llm_max_tokens', 'context_tokens', 'max_tool_rounds')


def installed_skills(settings: Settings) -> list[dict]:
    from data_agent.skills import load_skills
    if not settings.skills_path.exists():
        return []
    return load_skills(settings.skills_path)


def submit_message(store: Store, settings: Settings, user_id: str, session_id: str, text: str,
                   client_message_id: str, *, source: str = 'web', binding_id: str | None = None,
                   reply_target: str | None = None) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ServiceError(400, 'Message must contain 1 to 16000 characters')
    if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 200:
        raise ServiceError(400, 'Invalid client message ID')
    if source not in ('web', 'feishu'):
        raise ServiceError(400, 'Unsupported source')
    skills = installed_skills(settings)
    config = {k: getattr(settings, k) for k in CONFIG_FIELDS}
    with store.transaction() as conn:
        store.own_session(conn, user_id, session_id)
        if source == 'feishu':
            bound = conn.execute('SELECT 1 FROM channel_identities WHERE id=? AND user_id=? AND session_id=? '
                                 'AND active=1', (binding_id, user_id, session_id)).fetchone()
            if not bound or not reply_target:
                raise ServiceError(403, 'Channel binding is unavailable')
        old = conn.execute('SELECT * FROM runs WHERE user_id=? AND session_id=? AND client_message_id=?',
                           (user_id, session_id, client_message_id)).fetchone()
        if old:
            original = conn.execute('SELECT content FROM messages WHERE id=?', (old['input_message_id'],)).fetchone()
            if original['content'] != text:
                raise ServiceError(409, 'Client message ID already used for different text')
            return {'run_id': old['id'], 'message_id': old['input_message_id'], 'status': old['status']}
        enabled = {r['name']: bool(r['enabled']) for r in conn.execute(
            'SELECT name,enabled FROM user_skills WHERE user_id=?', (user_id,))}
        snapshot = [s for s in skills if enabled.get(s['name'], True)]
        definition = json.dumps({'version': 1, 'config': config, 'skills': snapshot}, sort_keys=True)
        run_id = identifier()
        conn.execute('INSERT INTO runs(id,user_id,session_id,client_message_id,created_at,definition_hash,'
                     'skills_snapshot,config_snapshot,source,binding_id,reply_target) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (run_id, user_id, session_id, client_message_id, time.time(),
                      hashlib.sha256(definition.encode()).hexdigest(), json.dumps(snapshot), json.dumps(config),
                      source, binding_id, reply_target))
        message_id = conn.execute(
            'INSERT INTO messages(user_id,session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?,?)',
            (user_id, session_id, run_id, 'user', text, time.time())).lastrowid
        conn.execute('UPDATE runs SET input_message_id=? WHERE id=?', (message_id, run_id))
        return {'run_id': run_id, 'message_id': message_id, 'status': 'queued'}


def prompt_messages(store: Store, run: dict) -> list[dict]:
    with store.transaction(immediate=False) as conn:
        # Select previous successful complete rounds by their INPUT sequence. Replies may
        # have IDs after this input because multiple questions can already be queued.
        prior = conn.execute("SELECT id FROM runs WHERE session_id=? AND status='succeeded' "
                             'AND input_message_id<? ORDER BY input_message_id DESC LIMIT 12',
                             (run['session_id'], run['input_message_id'])).fetchall()
        rounds = [[dict(r) for r in conn.execute('SELECT role,content FROM messages WHERE run_id=? '
                                               "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END", (p['id'],))]
                  for p in reversed(prior)]
        current = dict(conn.execute('SELECT role,content FROM messages WHERE id=?',
                                    (run['input_message_id'],)).fetchone())
    selected, remaining = [], 32000
    for pair in reversed(rounds):
        size = sum(len(m['content']) for m in pair)
        if size > remaining:
            break
        selected = pair + selected
        remaining -= size
    return selected + [current]


def search_history(store: Store, run: dict, args: dict) -> list[dict]:
    query = args.get('query')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or set(args) != {'query'}:
        raise ServiceError(400, 'History query must contain 1 to 200 characters')
    pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    with store.transaction(immediate=False) as conn:
        rows = conn.execute("SELECT m.id,m.role,substr(m.content,1,1500) AS content FROM messages m "
                            "JOIN runs r ON r.id=m.run_id WHERE m.user_id=? AND m.content LIKE ? ESCAPE '\\' "
                            'AND r.input_message_id<=? ORDER BY m.id DESC LIMIT 10',
                            (run['user_id'], pattern, run['input_message_id'])).fetchall()
        return [dict(r) for r in rows]
