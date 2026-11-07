"""Chat use cases: atomic message ingestion and bounded, user-scoped runtime context."""
import hashlib
import json
import logging
import time

from errors import ServiceError
from ids import identifier
from settings import Settings
from storage import Store

logger = logging.getLogger(__name__)

CONFIG_FIELDS = ('llm_model', 'llm_base_url', 'llm_timeout', 'llm_max_tokens', 'context_tokens', 'max_tool_rounds')


def installed_skills(settings: Settings) -> list[dict]:
    from skills.loader import load_skills
    if not settings.skills_path.exists():
        return []
    return load_skills(settings.skills_path)


def submit_message(store: Store, settings: Settings, user_id: str, session_id: str, text: str,
                   client_message_id: str, *, source: str = 'web', binding_id: str | None = None,
                   reply_target: str | None = None) -> dict:
    with store.transaction() as conn:
        return submit_message_conn(conn, store, settings, user_id, session_id, text, client_message_id,
                                   source=source, binding_id=binding_id, reply_target=reply_target)


def submit_message_conn(conn, store: Store, settings: Settings, user_id: str, session_id: str, text: str,
                        client_message_id: str, *, source: str = 'web', binding_id: str | None = None,
                        reply_target: str | None = None) -> dict:
    """Shared ingestion in the caller's transaction, including webhook dedupe."""
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ServiceError(400, 'Message must contain 1 to 16000 characters')
    if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 200:
        raise ServiceError(400, 'Invalid client message ID')
    if source not in ('web', 'feishu'):
        raise ServiceError(400, 'Unsupported source')
    skills = installed_skills(settings)
    config = {k: getattr(settings, k) for k in CONFIG_FIELDS}
    session = store.sessions.lock(conn, user_id, session_id)
    if session.get('archived_at') is not None:
        logger.info('Rejected message to archived session %s (user=%s)', session_id, user_id)
        raise ServiceError(409, 'Cannot send messages to an archived session')
    if source == 'feishu':
        if not binding_id or not reply_target or not store.channels.identity_of_binding(
                conn, binding_id, user_id, session_id):
            raise ServiceError(403, 'Channel binding is unavailable')
    old = store.runs.by_client_message_id(conn, user_id, session_id, client_message_id)
    if old:
        original = store.messages_repo.by_id(conn, old['input_message_id'])
        if original['content'] != text:
            logger.info('Duplicate client_message_id %s with different text (session=%s user=%s)',
                       client_message_id, session_id, user_id)
            raise ServiceError(409, 'Client message ID already used for different text')
        return {'run_id': old['id'], 'message_id': old['input_message_id'], 'status': old['status']}
    enabled = store.skills.enabled_map(conn, user_id)
    snapshot = [s for s in skills if enabled.get(s['name'], True)]
    definition = json.dumps({'version': 1, 'config': config, 'skills': snapshot}, sort_keys=True)
    run_id = identifier()
    store.runs.insert(conn, {
        'id': run_id, 'user_id': user_id, 'session_id': session_id,
        'client_message_id': client_message_id, 'created_at': time.time(),
        'definition_hash': hashlib.sha256(definition.encode()).hexdigest(),
        'skills_snapshot': json.dumps(snapshot), 'config_snapshot': json.dumps(config),
        'source': source, 'binding_id': binding_id, 'reply_target': reply_target})
    message_id = store.messages_repo.insert(conn, user_id, session_id, run_id, 'user', text, time.time())
    store.runs.set_input_message(conn, run_id, message_id)
    logger.info('Message submitted (run=%s session=%s user=%s len=%s source=%s)',
               run_id, session_id, user_id, len(text), source)
    return {'run_id': run_id, 'message_id': message_id, 'status': 'queued'}


def prompt_messages(store: Store, run: dict) -> list[dict]:
    with store.transaction(immediate=False) as conn:
        # Select previous successful complete rounds by their INPUT sequence. Replies may
        # have IDs after this input because multiple questions can already be queued.
        prior = store.runs.succeeded_inputs_before(conn, run['session_id'], run['input_message_id'])
        rounds = [store.messages_repo.by_run_ordered(conn, run_id) for run_id in reversed(prior)]
        current = store.messages_repo.by_id(conn, run['input_message_id'])
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
        return store.messages_repo.search(conn, run['user_id'], pattern, run['input_message_id'])
