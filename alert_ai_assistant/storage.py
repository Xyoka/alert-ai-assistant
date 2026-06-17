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
    delivery_channel TEXT NOT NULL DEFAULT '',
    delivery_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id INTEGER NOT NULL,
    ref_code TEXT NOT NULL,
    alert_content_hash TEXT NOT NULL DEFAULT '',
    status_bucket TEXT NOT NULL DEFAULT '',
    alarm_time TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    device_ip TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(summary_id, ref_code)
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_type TEXT NOT NULL,
    summary_id INTEGER,
    channel TEXT NOT NULL DEFAULT 'intelligent_bot',
    target_id TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT '',
    fallback_sent_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_state (
    chat_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id, key)
);

CREATE TABLE IF NOT EXISTS qa_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    matched_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_records_inserted_at
    ON alert_records(inserted_at);
CREATE INDEX IF NOT EXISTS idx_alert_records_device_time
    ON alert_records(device_ip, alarm_time);
CREATE INDEX IF NOT EXISTS idx_summary_items_summary_ref
    ON summary_items(summary_id, ref_code);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_status_created
    ON outbound_messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_qa_interactions_created
    ON qa_interactions(created_at);
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
            _ensure_column(conn, "summaries", "delivery_channel", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "summaries", "delivery_error", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "outbound_messages", "sent_at", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "outbound_messages", "fallback_sent_at", "TEXT NOT NULL DEFAULT ''")

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
        delivery_channel: str = "",
        delivery_error: str = "",
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO summaries (
                    window_start, window_end, summary_text, stats_json,
                    ai_used, delivered, delivery_channel, delivery_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    summary_text,
                    json_dumps(stats),
                    int(ai_used),
                    int(delivered),
                    delivery_channel,
                    delivery_error,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def save_summary_items(self, summary_id: int, records: Iterable[AlertRecord]) -> list[dict]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        items: list[dict] = []
        with self.connect() as conn:
            for index, record in enumerate(records, start=1):
                ref_code = f"A{index}"
                cursor = conn.execute(
                    """
                    INSERT OR REPLACE INTO summary_items (
                        summary_id, ref_code, alert_content_hash, status_bucket,
                        alarm_time, source_type, device_ip, hostname, title,
                        content, severity, external_id, raw_payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        ref_code,
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
                items.append(
                    {
                        "id": int(cursor.lastrowid),
                        "summary_id": summary_id,
                        "ref_code": ref_code,
                        "record": record,
                    }
                )
        return items

    def mark_summary_delivered(
        self,
        summary_id: int,
        delivered: bool,
        delivery_channel: str = "",
        delivery_error: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE summaries
                SET delivered = ?, delivery_channel = ?, delivery_error = ?
                WHERE id = ?
                """,
                (int(delivered), delivery_channel, delivery_error, summary_id),
            )

    def enqueue_message(
        self,
        message_type: str,
        body: str,
        target_id: str = "",
        summary_id: int | None = None,
        channel: str = "intelligent_bot",
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO outbound_messages (
                    message_type, summary_id, channel, target_id, body,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (message_type, summary_id, channel, target_id, body, now, now),
            )
            return int(cursor.lastrowid)

    def fetch_pending_messages(self, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbound_messages
                WHERE status = 'pending'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_message_sent(self, message_id: int, summary_id: int | None = None) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'sent', attempts = attempts + 1, error = '',
                    updated_at = ?, sent_at = ?
                WHERE id = ?
                """,
                (now, now, message_id),
            )
            if summary_id:
                conn.execute(
                    """
                    UPDATE summaries
                    SET delivered = 1, delivery_channel = 'intelligent_bot', delivery_error = ''
                    WHERE id = ?
                    """,
                    (summary_id,),
                )

    def mark_message_failed(self, message_id: int, error: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'failed', attempts = attempts + 1, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, now, message_id),
            )

    def mark_message_fallback_sent(self, message_id: int, summary_id: int | None = None) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'fallback_sent', attempts = attempts + 1, error = '',
                    updated_at = ?, fallback_sent_at = ?
                WHERE id = ?
                """,
                (now, now, message_id),
            )
            if summary_id:
                conn.execute(
                    """
                    UPDATE summaries
                    SET delivered = 1, delivery_channel = 'webhook_fallback', delivery_error = ''
                    WHERE id = ?
                    """,
                    (summary_id,),
                )

    def fetch_failed_summary_messages(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbound_messages
                WHERE status = 'failed' AND message_type = 'summary'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_messages_recovery_notified(self, message_ids: Iterable[int]) -> None:
        ids = [int(message_id) for message_id in message_ids]
        if not ids:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE outbound_messages
                SET status = 'recovery_notified', updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [now, *ids],
            )

    def update_runtime_state(self, key: str, value: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_runtime_state(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runtime_state WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def is_runtime_state_recent(self, key: str, max_age_seconds: int) -> bool:
        state = self.get_runtime_state(key)
        if not state:
            return False
        updated_at = _parse_db_datetime(state.get("updated_at", ""))
        if not updated_at:
            return False
        return (datetime.now() - updated_at).total_seconds() <= max_age_seconds

    def latest_summary_id(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM summaries ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def latest_summary(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM summaries ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def outbound_status_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM outbound_messages
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def get_summary_items(self, summary_id: int | None = None, limit: int = 20) -> list[dict]:
        if summary_id is None:
            summary_id = self.latest_summary_id()
        if summary_id is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM summary_items
                WHERE summary_id = ?
                ORDER BY CAST(SUBSTR(ref_code, 2) AS INTEGER), id
                LIMIT ?
                """,
                (summary_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_summary_item(self, ref_code: str, summary_id: int | None = None) -> dict | None:
        ref = ref_code.upper()
        params: tuple
        if summary_id is None:
            sql = """
                SELECT * FROM summary_items
                WHERE UPPER(ref_code) = ?
                ORDER BY summary_id DESC, id DESC
                LIMIT 1
            """
            params = (ref,)
        else:
            sql = """
                SELECT * FROM summary_items
                WHERE summary_id = ? AND UPPER(ref_code) = ?
                ORDER BY id DESC
                LIMIT 1
            """
            params = (summary_id, ref)
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def search_alerts(self, query: str, history_days: int, limit: int = 5) -> list[AlertRecord]:
        cutoff = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d %H:%M:%S")
        tokens = [token for token in query.strip().split() if token]
        with self.connect() as conn:
            if not tokens:
                rows = conn.execute(
                    """
                    SELECT * FROM alert_records
                    WHERE inserted_at >= ?
                    ORDER BY alarm_time DESC, inserted_at DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
            else:
                clauses = []
                params: list[str | int] = [cutoff]
                for token in tokens:
                    like = f"%{token}%"
                    clauses.append(
                        "(device_ip LIKE ? OR hostname LIKE ? OR title LIKE ? OR content LIKE ? OR raw_payload LIKE ?)"
                    )
                    params.extend([like, like, like, like, like])
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT * FROM alert_records
                    WHERE inserted_at >= ? AND {' AND '.join(clauses)}
                    ORDER BY alarm_time DESC, inserted_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        return [_row_to_alert(row) for row in rows]

    def related_alerts(self, alert: AlertRecord, history_days: int, limit: int = 100) -> list[AlertRecord]:
        cutoff = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert_records
                WHERE inserted_at >= ?
                  AND device_ip = ?
                  AND (
                      content_hash = ?
                      OR title = ?
                      OR (? != '' AND external_id = ?)
                  )
                ORDER BY alarm_time ASC, inserted_at ASC
                LIMIT ?
                """,
                (
                    cutoff,
                    alert.device_ip,
                    alert.content_hash,
                    alert.title,
                    alert.external_id,
                    alert.external_id,
                    limit,
                ),
            ).fetchall()
        return [_row_to_alert(row) for row in rows]

    def set_conversation_state(self, chat_id: str, user_id: str, key: str, value: dict | str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        value_text = value if isinstance(value, str) else json_dumps(value)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_state (chat_id, user_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id, key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (chat_id, user_id, key, value_text, now),
            )

    def get_conversation_state(self, chat_id: str, user_id: str, key: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT value FROM conversation_state
                WHERE chat_id = ? AND user_id = ? AND key = ?
                """,
                (chat_id, user_id, key),
            ).fetchone()
        return str(row["value"]) if row else ""

    def save_qa_interaction(
        self,
        chat_id: str,
        user_id: str,
        question: str,
        answer: str,
        matched: dict | None = None,
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO qa_interactions (chat_id, user_id, question, answer, matched_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, question, answer, json_dumps(matched or {}), now),
            )

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
            conn.execute(
                "DELETE FROM summary_items WHERE summary_id NOT IN (SELECT id FROM summaries)"
            )
            conn.execute(
                "DELETE FROM outbound_messages WHERE created_at < ? AND status != 'pending'",
                (summary_before,),
            )
            conn.execute(
                "DELETE FROM qa_interactions WHERE created_at < ?",
                (summary_before,),
            )
        return int(alert_deleted), int(summary_deleted)


def _row_to_alert(row: sqlite3.Row | dict) -> AlertRecord:
    return AlertRecord(
        source_type=str(row["source_type"] or "unknown"),
        status_bucket=str(row["status_bucket"] or ""),
        device_ip=str(row["device_ip"] or ""),
        hostname=str(row["hostname"] or ""),
        alarm_time=_parse_db_datetime(str(row["alarm_time"] or "")),
        title=str(row["title"] or ""),
        content=str(row["content"] or ""),
        severity=str(row["severity"] or ""),
        external_id=str(row["external_id"] or ""),
        raw_payload=str(row["raw_payload"] or ""),
        content_hash=str(row["content_hash"] if "content_hash" in row.keys() else row["alert_content_hash"]),
    )


def _parse_db_datetime(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
