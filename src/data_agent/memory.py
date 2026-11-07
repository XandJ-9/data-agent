"""User memory CAS and recoverable extraction of bounded complete session rounds."""
import json
import logging
import re
import time

from data_agent.db import ServiceError, Store, identifier

logger = logging.getLogger(__name__)
SECRET = re.compile(
    r'(?:sk-[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY|(?:api[_ -]?key|password|密码|token)\s*[:=]\s*\S+)', re.I)


def _read(conn, user_id: str) -> dict:
    row = conn.execute('SELECT revision,entries FROM user_memories WHERE user_id=?', (user_id,)).fetchone()
    return {'revision': row['revision'], 'entries': json.loads(row['entries'])}


def get_memory(store: Store, user_id: str) -> dict:
    with store.transaction(immediate=False) as conn:
        return _read(conn, user_id)


def validate_entries(conn, user_id: str, entries: list[dict]) -> list[dict]:
    if len(entries) > 100:
        raise ServiceError(400, 'Memory is limited to 100 entries')
    clean = []
    for entry in entries:
        text, eid = entry.get('text'), entry.get('id')
        sources = entry.get('source_message_ids', [])
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000 or
                not isinstance(eid, str) or not 1 <= len(eid) <= 100 or not isinstance(sources, list)
                or len(sources) > 50 or any(type(s) is not int for s in sources)):
            raise ServiceError(400, 'Invalid memory entry')
        if SECRET.search(text):
            raise ServiceError(400, 'Memory must not contain credentials')
        for source in sources:
            if not conn.execute('SELECT 1 FROM messages WHERE id=? AND user_id=?', (source, user_id)).fetchone():
                raise ServiceError(400, 'Invalid memory source')
        clean.append({'id': eid, 'text': text.strip(), 'source_message_ids': sorted(set(sources)),
                      'updated_at': time.time()})
    if len({e['id'] for e in clean}) != len(clean):
        raise ServiceError(400, 'Duplicate memory entry IDs')
    return clean


def replace_memory(store: Store, user_id: str, expected_revision: int, entries: list[dict]) -> dict:
    with store.transaction() as conn:
        current = _read(conn, user_id)
        if current['revision'] != expected_revision:
            raise ServiceError(409, 'Memory changed; reload and try again')
        clean = validate_entries(conn, user_id, entries)
        old = {e['id']: e for e in current['entries']}
        new = {e['id']: e for e in clean}
        removed = [e for eid, e in old.items() if eid not in new or e['text'] != new[eid]['text']]
        if removed:
            # Conservative cutoff prevents paraphrased old evidence resurrecting forgotten facts.
            cutoff = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE user_id=?', (user_id,)).fetchone()[0]
            conn.execute('UPDATE user_memories SET excluded_before=MAX(excluded_before,?) WHERE user_id=?',
                         (cutoff, user_id))
            for entry in removed:
                for mid in entry['source_message_ids']:
                    conn.execute('INSERT OR IGNORE INTO memory_exclusions VALUES(?,?)', (user_id, mid))
        conn.execute('UPDATE user_memories SET revision=revision+1,entries=? WHERE user_id=?',
                     (json.dumps(clean), user_id))
        return {'revision': expected_revision + 1, 'entries': clean}


def forget_memory(store: Store, user_id: str, expected_revision: int, entry_id: str) -> dict:
    memory = get_memory(store, user_id)
    if not any(e['id'] == entry_id for e in memory['entries']):
        raise ServiceError(404, 'Memory entry not found')
    return replace_memory(store, user_id, expected_revision, [e for e in memory['entries'] if e['id'] != entry_id])


def _batch(store: Store) -> dict | None:
    with store.transaction() as conn:
        row = conn.execute('SELECT s.*,m.revision,m.entries,m.excluded_before FROM sessions s JOIN user_memories m '
                           'ON s.user_id=m.user_id WHERE s.memory_dirty=1 AND s.memory_retry_at<=? '
                           'ORDER BY s.memory_retry_at,s.created_at LIMIT 1', (time.time(),)).fetchone()
        if row is None:
            return None
        rounds = conn.execute("SELECT r.id,r.input_message_id,MAX(m.id) AS end_id FROM runs r JOIN messages m "
                              "ON m.run_id=r.id WHERE r.session_id=? AND r.status='succeeded' "
                              'GROUP BY r.id HAVING end_id>? ORDER BY r.input_message_id LIMIT 8',
                              (row['id'], row['memory_cursor'])).fetchall()
        if not rounds:
            conn.execute('UPDATE sessions SET memory_dirty=0 WHERE id=?', (row['id'],))
            return None
        messages = []
        end = row['memory_cursor']
        size = 0
        for item in rounds:
            pair = [dict(m) for m in conn.execute('SELECT id,role,content FROM messages WHERE run_id=? '
                                                "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END", (item['id'],))]
            pair_size = sum(len(m['content']) for m in pair)
            if size + pair_size > 64000 and messages:
                break
            messages += pair
            size += pair_size
            end = max(end, item['end_id'])
        exclusions = {r[0] for r in conn.execute('SELECT message_id FROM memory_exclusions WHERE user_id=?',
                                                (row['user_id'],))}
        # Exclude entire rounds if either side predates the cutoff or was forgotten.
        excluded_runs = {m['id'] for m in messages if m['id'] <= row['excluded_before'] or m['id'] in exclusions}
        allowed = []
        for index in range(0, len(messages), 2):
            pair = messages[index:index + 2]
            if len(pair) == 2 and not any(m['id'] in excluded_runs for m in pair):
                allowed += pair
        claim = time.time() + 300
        conn.execute('UPDATE sessions SET memory_retry_at=? WHERE id=?', (claim, row['id']))
        return {**dict(row), 'messages': allowed, 'end': end, 'claim': claim}


def compress_one(store: Store, runtime) -> bool:
    batch = _batch(store)
    if batch is None:
        return False
    try:
        previous = json.loads(batch['entries'])
        candidates = runtime.extract_memory(previous=previous, messages=batch['messages']) if batch['messages'] else []
        valid_ids = {m['id'] for m in batch['messages']}
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ValueError('Invalid memory candidates')
        additions = []
        for entry in candidates:
            sources = entry.get('source_message_ids', [])
            if (not sources or any(type(s) is not int or s not in valid_ids for s in sources)):
                raise ValueError('Invalid candidate source')
            additions.append({**entry, 'id': identifier()})
        with store.transaction() as conn:
            session = conn.execute('SELECT * FROM sessions WHERE id=?', (batch['id'],)).fetchone()
            current = _read(conn, batch['user_id'])
            if (current['revision'] != batch['revision'] or session['memory_cursor'] != batch['memory_cursor']
                    or session['memory_retry_at'] != batch['claim']):
                conn.execute('UPDATE sessions SET memory_retry_at=0 WHERE id=? AND memory_retry_at=?',
                             (batch['id'], batch['claim']))
                return False
            clean = validate_entries(conn, batch['user_id'], additions)
            existing_texts = {e['text'] for e in current['entries']}
            merged = current['entries'] + [e for e in clean if e['text'] not in existing_texts]
            if len(merged) > 100:
                raise ValueError('Memory capacity reached')
            conn.execute('UPDATE user_memories SET entries=?,revision=revision+1 WHERE user_id=?',
                         (json.dumps(merged), batch['user_id']))
            dirty = conn.execute("SELECT 1 FROM messages m JOIN runs r ON r.id=m.run_id WHERE m.session_id=? "
                                 "AND m.id>? AND m.role='assistant' AND r.status='succeeded' LIMIT 1",
                                 (batch['id'], batch['end'])).fetchone() is not None
            conn.execute('UPDATE sessions SET memory_cursor=?,memory_dirty=?,memory_retry_at=0,memory_failures=0,'
                         'memory_error=NULL WHERE id=?', (batch['end'], int(dirty), batch['id']))
        return True
    except Exception:
        logger.warning('Memory extraction failed for session %s', batch['id'])
        backoff = time.time() + min(3600, 5 * 2 ** min(batch['memory_failures'], 9))
        with store.transaction() as conn:
            conn.execute('UPDATE sessions SET memory_failures=memory_failures+1,memory_error=?,memory_retry_at=? '
                         'WHERE id=? AND memory_retry_at=?',
                         ('memory_extraction_failed', backoff, batch['id'], batch['claim']))
        return False
