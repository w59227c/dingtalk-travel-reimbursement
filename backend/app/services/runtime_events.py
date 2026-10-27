from __future__ import annotations

import json
import queue
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.core.runtime_events import EVENTS, MODULES, RESULTS, RuntimeEvent, safe_code, safe_details
from app.models.runtime_event import RuntimeEventRecord

RETENTION_DAYS = 7
MAX_RECORDS = 10_000
MAX_BYTES = 16 * 1024 * 1024


class RuntimeEventStore:
    """Loss-tolerant bounded collector, independent of business transactions.

    A bounded queue keeps SQLite contention away from employee requests. Gaps
    and write failures are observable; this is not a business audit ledger.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_records: int = MAX_RECORDS,
        max_bytes: int = MAX_BYTES,
        queue_size: int = 256,
    ) -> None:
        self.factory = session_factory
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._queue: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._reported_dropped = 0
        self._write_error = False
        self.started_at = datetime.now(UTC).isoformat()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="runtime-events", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=7)

    def record(self, event: RuntimeEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "startedAt": self.started_at,
                "pending": self._queue.unfinished_tasks,
                "droppedSinceStart": self._dropped,
                "writeError": self._write_error,
            }

    def flush(self, timeout: float = 1) -> bool:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            self._stop.wait(0.01) if not self._stop.is_set() else time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def _run(self) -> None:
        last_prune = 0.0
        while not self._stop.is_set() or self._queue.unfinished_tasks:
            batch = []
            try:
                batch.append(self._queue.get(timeout=0.1))
                while len(batch) < 50:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if not batch and time.monotonic() - last_prune < 60:
                continue
            with self._lock:
                dropped = self._dropped
            gap = dropped - self._reported_dropped
            events = batch + (
                [
                    RuntimeEvent(
                        datetime.now(UTC).replace(tzinfo=None),
                        "system",
                        "collection_gap",
                        "incomplete",
                        None,
                        {"dropped": gap},
                    )
                ]
                if gap
                else []
            )
            try:
                self._write(events)
            except Exception:
                # Do not recursively log through the collector or propagate into requests.
                with self._lock:
                    self._write_error = True
                    self._dropped += len(batch)
            else:
                with self._lock:
                    self._write_error = False
                self._reported_dropped = dropped
            finally:
                last_prune = time.monotonic()
                for _ in batch:
                    self._queue.task_done()

    def _write(self, events: list[RuntimeEvent]) -> None:
        with self.factory() as database:
            for event in events:
                if (
                    event.module not in MODULES
                    or event.event not in EVENTS
                    or event.result not in RESULTS
                ):
                    continue
                encoded = json.dumps(
                    safe_details(event.details), ensure_ascii=True, separators=(",", ":")
                )
                size = len(encoded.encode("utf-8")) + 512
                if size > 8192:
                    raise ValueError("operational event exceeds bounded size")
                database.add(
                    RuntimeEventRecord(
                        occurred_at=event.occurred_at,
                        module=event.module,
                        event=event.event,
                        result=event.result,
                        request_id=safe_code(event.request_id),
                        details_json=encoded,
                        byte_size=size,
                    )
                )
            database.flush()
            database.execute(
                delete(RuntimeEventRecord).where(
                    RuntimeEventRecord.occurred_at
                    < datetime.now(UTC).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
                )
            )
            total = 0
            cutoff = None
            for index, (event_id, size) in enumerate(
                database.execute(
                    select(RuntimeEventRecord.id, RuntimeEventRecord.byte_size).order_by(
                        RuntimeEventRecord.id.desc()
                    )
                )
            ):
                total += size
                if index >= self.max_records or total > self.max_bytes:
                    cutoff = event_id
                    break
            if cutoff is not None:
                database.execute(delete(RuntimeEventRecord).where(RuntimeEventRecord.id <= cutoff))
            database.commit()

    def search(
        self,
        *,
        start: datetime,
        end: datetime,
        module: str | None = None,
        result: str | None = None,
        request_id: str | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        try:
            with self.factory() as database:
                retained_since = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                    days=RETENTION_DAYS
                )
                query = select(RuntimeEventRecord).where(
                    RuntimeEventRecord.occurred_at >= max(start, retained_since),
                    RuntimeEventRecord.occurred_at <= end,
                )
                if module:
                    query = query.where(RuntimeEventRecord.module == module)
                if result:
                    query = query.where(RuntimeEventRecord.result == result)
                if request_id:
                    query = query.where(RuntimeEventRecord.request_id == request_id)
                if before:
                    query = query.where(RuntimeEventRecord.id < before)
                rows = list(
                    database.scalars(query.order_by(RuntimeEventRecord.id.desc()).limit(limit + 1))
                )
                has_more = len(rows) > limit
                items = [self._public(row) for row in rows[:limit]]
                oldest, newest, count, size = database.execute(
                    select(
                        func.min(RuntimeEventRecord.occurred_at),
                        func.max(RuntimeEventRecord.occurred_at),
                        func.count(),
                        func.coalesce(func.sum(RuntimeEventRecord.byte_size), 0),
                    ).where(RuntimeEventRecord.occurred_at >= retained_since)
                ).one()
                return {
                    "items": items,
                    "nextCursor": rows[limit - 1].id if has_more else None,
                    "coverage": {
                        "oldestAt": _iso(oldest),
                        "newestAt": _iso(newest),
                        "records": count,
                        "bytes": size,
                    },
                    "limits": {
                        "retentionDays": RETENTION_DAYS,
                        "maxRecords": self.max_records,
                        "maxBytes": self.max_bytes,
                    },
                    "collector": self.status(),
                }
        except Exception:
            raise ApiError(
                "RUNTIME_EVENTS_UNAVAILABLE", "运行记录暂时无法读取，请稍后重试", 503
            ) from None

    @staticmethod
    def _public(row: RuntimeEventRecord) -> dict[str, object]:
        if row.module not in MODULES or row.event not in EVENTS or row.result not in RESULTS:
            raise ValueError("invalid operational record")
        return {
            "id": row.id,
            "occurredAt": _iso(row.occurred_at),
            "module": row.module,
            "event": row.event,
            "result": row.result,
            "requestId": safe_code(row.request_id),
            "details": safe_details(json.loads(row.details_json)),
        }


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value else None
