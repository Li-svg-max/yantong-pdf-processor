from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredTask:
    job_id: str
    payload: dict
    attempts: int


class QueueStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "UPDATE tasks SET status = 'queued' WHERE status = 'processing'"
            )

    def enqueue(self, job_id: str, payload: dict) -> bool:
        now = time.time()
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM tasks WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing and existing["status"] in {"queued", "processing", "complete"}:
                return False
            connection.execute(
                """
                INSERT INTO tasks(job_id, payload, status, attempts, error, created_at, updated_at)
                VALUES(?, ?, 'queued', 0, '', ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload = excluded.payload,
                    status = 'queued',
                    error = '',
                    updated_at = excluded.updated_at
                """,
                (job_id, serialized, now, now),
            )
        return True

    def claim_next(self, max_attempts: int) -> StoredTask | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id, payload, attempts
                FROM tasks
                WHERE status = 'queued' AND attempts < ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (max_attempts,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE tasks
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (time.time(), row["job_id"]),
            )
            return StoredTask(
                job_id=row["job_id"],
                payload=json.loads(row["payload"]),
                attempts=int(row["attempts"]) + 1,
            )

    def mark_complete(self, job_id: str) -> None:
        self._set_status(job_id, "complete", "")

    def mark_failed(self, job_id: str, error: str, retry: bool) -> None:
        self._set_status(job_id, "queued" if retry else "failed", error[:2000])

    def _set_status(self, job_id: str, status: str, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (status, error, time.time(), job_id),
            )

    def status(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, status, attempts, error, created_at, updated_at FROM tasks WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM tasks GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

