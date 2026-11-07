"""run_steps: per-run execution trace (assistant replies, tool calls, results).

Rows are appended incrementally by the worker as the runtime emits events,
so partial traces survive cancellation and failure. Append-only like messages.
"""
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository
from storage.schema import run_steps


class RunStepsRepository(BaseRepository):
    def append(self, conn: Connection, run_id: str, seq: int, event: dict, now: float) -> None:
        conn.execute(insert(run_steps), {
            'run_id': run_id, 'seq': seq, 'kind': event['kind'], 'name': event.get('name'),
            'content': event.get('content', ''), 'reasoning': event.get('reasoning'),
            'created_at': now})

    def list(self, conn: Connection, run_id: str) -> list[dict]:
        return self._rows(conn.execute(
            select(run_steps.c.seq, run_steps.c.kind, run_steps.c.name, run_steps.c.content,
                   run_steps.c.reasoning, run_steps.c.created_at)
            .where(run_steps.c.run_id == run_id).order_by(run_steps.c.seq)))
