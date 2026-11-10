"""Memory use cases: user memory CAS and recoverable extraction of bounded complete session rounds."""
import json
import logging
import re
from clock import now_iso, now_plus

from errors import ServiceError
from ids import identifier
from storage import Store

logger = logging.getLogger(__name__)
SECRET = re.compile(
    r'(?:sk-[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY|(?:api[_ -]?key|password|密码|token)\s*[:=]\s*\S+)', re.I)


def get_memory(store: Store, user_id: str) -> dict:
    with store.transaction(immediate=False) as conn:
        row = store.memory.read(conn, user_id)
        return {'revision': row['revision'], 'entries': json.loads(row['entries'])}


def validate_entries(store: Store, conn, user_id: str, entries: list[dict]) -> list[dict]:
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
            if not store.messages_repo.exists_for_user(conn, source, user_id):
                raise ServiceError(400, 'Invalid memory source')
        clean.append({'id': eid, 'text': text.strip(), 'source_message_ids': sorted(set(sources)),
                      'updated_at': now_iso()})
    if len({e['id'] for e in clean}) != len(clean):
        raise ServiceError(400, 'Duplicate memory entry IDs')
    return clean


def replace_memory(store: Store, user_id: str, expected_revision: int, entries: list[dict]) -> dict:
    with store.transaction() as conn:
        current = store.memory.read(conn, user_id)
        if current['revision'] != expected_revision:
            raise ServiceError(409, 'Memory changed; reload and try again')
        current_entries = json.loads(current['entries'])
        clean = validate_entries(store, conn, user_id, entries)
        old = {e['id']: e for e in current_entries}
        new = {e['id']: e for e in clean}
        removed = [e for eid, e in old.items() if eid not in new or e['text'] != new[eid]['text']]
        if not store.memory.bump_entries(conn, user_id, json.dumps(clean), expected_revision):
            raise ServiceError(409, 'Memory changed; reload and try again')
        if removed:
            # Conservative cutoff prevents paraphrased old evidence resurrecting forgotten facts.
            store.memory.advance_cutoff(conn, user_id, store.messages_repo.max_id_for_user(conn, user_id))
            for entry in removed:
                for mid in entry['source_message_ids']:
                    store.memory.add_exclusion(conn, user_id, mid)
        return {'revision': expected_revision + 1, 'entries': clean}


def forget_memory(store: Store, user_id: str, expected_revision: int, entry_id: str) -> dict:
    memory = get_memory(store, user_id)
    if not any(e['id'] == entry_id for e in memory['entries']):
        raise ServiceError(404, 'Memory entry not found')
    return replace_memory(store, user_id, expected_revision, [e for e in memory['entries'] if e['id'] != entry_id])


def _batch(store: Store) -> dict | None:
    with store.transaction() as conn:
        row = store.memory.claim_batch(conn, now_iso())
        if row is None:
            return None
        rounds = store.runs.memory_rounds(conn, row['id'], row['memory_cursor'])
        if not rounds:
            store.memory.clear_dirty(conn, row['id'])
            return None
        messages = []
        end = row['memory_cursor']
        size = 0
        for item in rounds:
            pair = store.messages_repo.by_run_ordered(conn, item['id'])
            pair_size = sum(len(m['content']) for m in pair)
            if size + pair_size > 64000 and messages:
                break
            messages += pair
            size += pair_size
            end = max(end, item['end_id'])
        exclusions = store.memory.exclusion_ids(conn, row['user_id'])
        # Exclude entire rounds if either side predates the cutoff or was forgotten.
        excluded = {m['id'] for m in messages if m['id'] <= row['excluded_before'] or m['id'] in exclusions}
        allowed = []
        for index in range(0, len(messages), 2):
            pair = messages[index:index + 2]
            if len(pair) == 2 and not any(m['id'] in excluded for m in pair):
                allowed += pair
        claim = now_plus(300)
        if not store.memory.set_claim(conn, row['id'], claim, row['memory_retry_at']):
            return None
        return {**row, 'messages': allowed, 'end': end, 'claim': claim}


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
        if additions:
            logger.info('Memory: %d new entries from session %s batch (cursor=%d, messages=%d)',
                       len(additions), batch['id'][:12], batch['memory_cursor'], len(batch['messages']))
        else:
            logger.info('Memory: no new entries from session %s (cursor=%d, messages=%d)',
                       batch['id'][:12], batch['memory_cursor'], len(batch['messages']))
        with store.transaction() as conn:
            session = store.sessions.lock(conn, batch['user_id'], batch['id'])
            current = store.memory.read(conn, batch['user_id'])
            if (current['revision'] != batch['revision'] or session['memory_cursor'] != batch['memory_cursor']
                    or session['memory_retry_at'] != batch['claim']):
                store.memory.reset_claim(conn, batch['id'], batch['claim'])
                return False
            clean = validate_entries(store, conn, batch['user_id'], additions)
            existing_texts = {e['text'] for e in json.loads(current['entries'])}
            new_entries = [e for e in clean if e['text'] not in existing_texts]
            merged = json.loads(current['entries']) + new_entries
            if len(merged) > 100:
                raise ValueError('Memory capacity reached')
            if not new_entries:
                # Nothing extracted; still advance cursor so we don't re-scan this batch.
                store.memory.advance_cursor(conn, batch['id'], batch['end'], batch['claim'])
                return False
            dirty = store.messages_repo.has_succeeded_after(conn, batch['id'], batch['end'])
            if not store.memory.apply_extraction(
                    conn, batch['id'], batch['user_id'], json.dumps(merged), batch['end'], dirty,
                    revision=batch['revision'], cursor=batch['memory_cursor'], claim=batch['claim']):
                store.memory.reset_claim(conn, batch['id'], batch['claim'])
                return False
        return True
    except Exception:
        logger.warning('Memory extraction failed for session %s', batch['id'])
        backoff = now_plus(min(3600, 5 * 2 ** min(batch['memory_failures'], 9)))
        with store.transaction() as conn:
            store.memory.register_failure(conn, batch['id'], 'memory_extraction_failed', backoff, batch['claim'])
        return False
