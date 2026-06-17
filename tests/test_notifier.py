import json

from alert_ai_assistant.config import WeComConfig
from alert_ai_assistant.notifier import WeComSmartBotNotifier, add_part_headers, parse_wecom_error, split_text


def test_split_text_respects_utf8_byte_limit_with_chinese():
    text = "告警内容：" + ("端口Down，" * 300)

    parts = split_text(text, max_chars=3500, max_bytes=300, header_reserved_bytes=80)
    headed = add_part_headers(parts)

    assert len(headed) > 1
    assert all(len(part.encode("utf-8")) <= 300 for part in headed)


def test_parse_wecom_error_detects_nonzero_errcode():
    assert parse_wecom_error('{"errcode":0,"errmsg":"ok"}') == ""

    error = parse_wecom_error('{"errcode":40058,"errmsg":"content too long"}')

    assert "40058" in error
    assert "content too long" in error


def test_send_reports_wecom_api_error(monkeypatch):
    responses = [
        {"errcode": 45009, "errmsg": "api freq out of limit"},
        {"errcode": 45009, "errmsg": "api freq out of limit"},
    ]

    def fake_urlopen(request, timeout=30):
        return FakeResponse(json.dumps(responses.pop(0)))

    monkeypatch.setattr("alert_ai_assistant.notifier.urlopen", fake_urlopen)
    monkeypatch.setattr("alert_ai_assistant.notifier.time.sleep", lambda seconds: None)
    config = WeComConfig(
        enabled=True,
        webhook_url="https://example.invalid/webhook",
        dry_run=False,
        max_retries=1,
    )

    result = WeComSmartBotNotifier(config).send("summary")

    assert not result.delivered
    assert result.delivered_parts == 0
    assert "45009" in result.error


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body.encode("utf-8")
