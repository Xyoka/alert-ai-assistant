from datetime import datetime
import sqlite3

from alert_ai_assistant.models import AlertRecord
from alert_ai_assistant.storage import AlertStore


def test_insert_alerts_deduplicates(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    record = AlertRecord(
        source_type="snmp",
        status_bucket="unhandled",
        device_ip="10.0.0.1",
        hostname="SW-A",
        alarm_time=datetime(2026, 5, 8, 17, 42, 18),
        title="端口连接状态告警",
        content="Ethernet1/42 down",
    )

    assert store.insert_alerts([record]) == 1
    assert store.insert_alerts([record]) == 0


def test_cleanup_removes_expired_rows(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    store.save_summary(
        datetime(2026, 5, 8, 17),
        datetime(2026, 5, 8, 17, 59, 59),
        "summary",
        {},
        ai_used=False,
        delivered=False,
    )

    alert_deleted, summary_deleted = store.cleanup(raw_alert_days=5, summary_days=-1)

    assert alert_deleted == 0
    assert summary_deleted == 1


def test_summary_items_and_outbox_round_trip(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    alarm_time = datetime(2026, 5, 8, 17, 42, 18)
    record = AlertRecord(
        source_type="snmp",
        status_bucket="unhandled",
        device_ip="10.0.0.1",
        hostname="SW-A",
        alarm_time=alarm_time,
        title="端口连接状态告警",
        content="Ethernet1/42 down",
    )
    summary_id = store.save_summary(alarm_time, alarm_time, "summary", {}, False, False)

    items = store.save_summary_items(summary_id, [record])
    message_id = store.enqueue_message("summary", "summary", target_id="chat1", summary_id=summary_id)
    pending = store.fetch_pending_messages()

    assert items[0]["ref_code"] == "A1"
    assert store.find_summary_item("A1")["device_ip"] == "10.0.0.1"
    assert pending[0]["id"] == message_id

    store.mark_message_sent(message_id, summary_id)
    assert store.fetch_pending_messages() == []


def test_failed_summary_messages_can_be_recovery_notified(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    alarm_time = datetime(2026, 5, 8, 17, 42, 18)
    summary_id = store.save_summary(alarm_time, alarm_time, "summary", {}, False, False)
    message_id = store.enqueue_message("summary", "summary", target_id="chat1", summary_id=summary_id)

    store.mark_message_failed(message_id, "send failed")
    failed = store.fetch_failed_summary_messages()

    assert failed[0]["id"] == message_id

    store.mark_messages_recovery_notified([message_id])
    assert store.fetch_failed_summary_messages() == []


def test_init_schema_migrates_old_summary_only_database(tmp_path):
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE alert_records (
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
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            stats_json TEXT NOT NULL DEFAULT '{}',
            ai_used INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO summaries (
            window_start, window_end, summary_text, stats_json, ai_used, delivered, created_at
        ) VALUES (
            '2026-05-08 17:00:00', '2026-05-08 17:59:59', 'old summary', '{}', 0, 1,
            '2026-05-08 18:00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    store = AlertStore(db_path)
    store.init_schema()
    latest = store.latest_summary()
    with store.connect() as conn:
        summary_columns = {row["name"] for row in conn.execute("PRAGMA table_info(summaries)")}
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert latest["summary_text"] == "old summary"
    assert "delivery_channel" in summary_columns
    assert "delivery_error" in summary_columns
    assert "summary_items" in tables
    assert "outbound_messages" in tables
    assert "runtime_state" in tables
