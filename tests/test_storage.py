from datetime import datetime

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

