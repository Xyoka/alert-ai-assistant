from datetime import datetime
import logging

from alert_ai_assistant.config import AppConfig
from alert_ai_assistant.intelligent_bot import WeComAgentService, send_pending_summary_or_fallback
from alert_ai_assistant.models import AlertRecord
from alert_ai_assistant.storage import AlertStore


def test_agent_summary_falls_back_to_webhook_dry_run_when_bot_stale(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    config = AppConfig(app_mode="agent")
    config.database_path = str(tmp_path / "alerts.db")
    config.wecom.dry_run = True
    config.wecom.enabled = False
    config.agent.fallback_webhook_enabled = True
    now = datetime.now()
    summary_id = store.save_summary(now, now, "summary", {}, ai_used=False, delivered=False)

    delivered, channel, error = send_pending_summary_or_fallback(
        config,
        store,
        summary_id,
        "summary",
        logging.getLogger(__name__),
    )
    messages = store.fetch_pending_messages()

    assert not delivered
    assert channel == "webhook_fallback"
    assert error == ""
    assert messages == []


def test_text_message_handler_replies_to_wecom_frame(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    alarm_time = datetime.now()
    record = AlertRecord(
        source_type="snmp",
        status_bucket="unhandled",
        device_ip="10.0.0.1",
        hostname="SW-A",
        alarm_time=alarm_time,
        title="端口连接状态告警",
        content="Ethernet1/42 down",
    )
    store.insert_alerts([record])
    summary_id = store.save_summary(alarm_time, alarm_time, "summary", {}, False, False)
    store.save_summary_items(summary_id, [record])
    service = WeComAgentService(AppConfig(), store, logging.getLogger(__name__))
    fake_client = FakeClient()
    service._client = fake_client

    frame = {
        "headers": {"req_id": "req-1"},
        "body": {
            "msgtype": "text",
            "text": {"content": "A1 这条告警报了几次？"},
            "chatid": "chat1",
            "userid": "user1",
        },
    }

    import asyncio

    asyncio.run(service._handle_text_message_async(frame))

    assert fake_client.replies
    assert "10.0.0.1" in fake_client.replies[0]["content"]
    assert "匹配次数" in fake_client.replies[0]["content"]


class FakeClient:
    def __init__(self):
        self.replies = []

    async def reply_stream(self, frame, stream_id, content, finish):
        self.replies.append(
            {
                "frame": frame,
                "stream_id": stream_id,
                "content": content,
                "finish": finish,
            }
        )
