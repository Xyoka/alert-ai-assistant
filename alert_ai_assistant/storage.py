from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from .models import AlertRecord, json_dumps


SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL,
    status_bucket TEXT NOT NULL,
    alarm_time TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    device_ip TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '',
    inserted_at TEXT NOT NULL,
    UNIQUE(content_hash, status_bucket, alarm_time)
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    ai_used INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


class AlertStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def insert_alerts(self, records: Iterable[AlertRecord]) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self.connect() as conn:
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO alert_records (
                        content_hash, status_bucket, alarm_time, source_type,
                        device_ip, hostname, title, content, severity,
                        external_id, raw_payload, inserted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.content_hash,
                        record.status_bucket,
                        record.alarm_time_text,
                        record.source_type,
                        record.device_ip,
                        record.hostname,
                        record.title,
                        record.content,
                        record.severity,
                        record.external_id,
                        record.raw_payload,
                        now,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def save_summary(
        self,
        window_start: datetime,
        window_end: datetime,
        summary_text: str,
        stats: dict,
        ai_used: bool,
        delivered: bool,
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO summaries (
                    window_start, window_end, summary_text, stats_json,
                    ai_used, delivered, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    summary_text,
                    json_dumps(stats),
                    int(ai_used),
                    int(delivered),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def latest_summary(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM summaries ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def cleanup(self, raw_alert_days: int, summary_days: int) -> tuple[int, int]:
        alert_before = (datetime.now() - timedelta(days=raw_alert_days)).strftime("%Y-%m-%d %H:%M:%S")
        summary_before = (datetime.now() - timedelta(days=summary_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            alert_deleted = conn.execute(
                "DELETE FROM alert_records WHERE inserted_at < ?",
                (alert_before,),
            ).rowcount
            summary_deleted = conn.execute(
                "DELETE FROM summaries WHERE created_at < ?",
                (summary_before,),
            ).rowcount
        return int(alert_deleted), int(summary_deleted)

