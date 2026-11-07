"""Outbox rows for channel delivery (deliveries, feishu_replies): same state
machine, parameterized by table. The pump claims with a conditional UPDATE
(status guard + rowcount) so concurrent workers never double-claim."""
import time

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from storage.repositories.base import BaseRepository


class OutboxRepository(BaseRepository):
    def __init__(self, table, order_column):
        self.table = table
        self.order_column = order_column

    def claim_batch(self, conn: Connection, owner: str, lease_seconds: int, now: float,
                    limit: int) -> list[dict]:
        conn.execute(update(self.table)
                     .where(self.table.c.status == 'sending', self.table.c.lease_until <= now)
                     .values(status='unknown', generation=self.table.c.generation + 1))
        rows = conn.execute(select(self.table.c.id)
                            .where(self.table.c.status == 'pending', self.table.c.retry_at <= now)
                            .order_by(self.order_column, self.table.c.id).limit(limit)).scalars()
        claimed = []
        for row_id in rows:
            cursor = conn.execute(update(self.table)
                                  .where(self.table.c.id == row_id, self.table.c.status == 'pending')
                                  .values(status='sending', owner=owner,
                                          generation=self.table.c.generation + 1,
                                          lease_until=now + lease_seconds))
            if cursor.rowcount == 1:
                claimed.append(self._row(conn.execute(select(self.table).where(self.table.c.id == row_id))))
        return claimed

    def finish(self, conn: Connection, row: dict, status: str, *, receipt: str | None = None,
               error_code: str | None = None, retry_at: float = 0, now: float | None = None) -> bool:
        """Fenced transition out of 'sending'; a lost lease means another owner moved on."""
        cursor = conn.execute(update(self.table)
                              .where(self.table.c.id == row['id'], self.table.c.status == 'sending',
                                     self.table.c.owner == row['owner'],
                                     self.table.c.generation == row['generation'],
                                     self.table.c.lease_until > (time.time() if now is None else now))
                              .values(status=status, receipt=receipt, error_code=error_code,
                                      attempts=self.table.c.attempts + 1, retry_at=retry_at))
        return cursor.rowcount == 1

    def enqueue(self, conn: Connection, fields: dict) -> str:
        conn.execute(self.table.insert(), fields)
        return fields['id']

    def list_for_run(self, conn: Connection, run_id: str) -> list[dict]:
        return self._rows(conn.execute(select(self.table.c.id, self.table.c.status)
                                       .where(self.table.c.run_id == run_id)
                                       .order_by(self.table.c.part)))

    def get_for_owner(self, conn: Connection, delivery_id: str, user_id: str) -> dict | None:
        from storage.schema import runs
        return self._row(conn.execute(
            select(self.table).join(runs, runs.c.id == self.table.c.run_id)
            .where(self.table.c.id == delivery_id, runs.c.user_id == user_id)
            .with_for_update(of=self.table)))

    def reset_pending(self, conn: Connection, delivery_id: str) -> None:
        conn.execute(update(self.table).where(self.table.c.id == delivery_id)
                     .values(status='pending', retry_at=0, error_code=None,
                             generation=self.table.c.generation + 1))
