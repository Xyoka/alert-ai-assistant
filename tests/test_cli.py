import json

from alert_ai_assistant.cli import main, previous_hour_window


def test_run_once_returns_nonzero_when_wecom_rejects_message(tmp_path, monkeypatch, capsys):
    sample_path = tmp_path / "sample.txt"
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "alerts.db"
    lock_path = tmp_path / "run.lock"
    log_path = tmp_path / "app.log"
    sample_path.write_text(_sample_alarm_text(), encoding="utf-8")
    config_path.write_text(
        f"""
database_path: {db_path.as_posix()}
lock_file: {lock_path.as_posix()}
log_file: {log_path.as_posix()}
source:
  kind: mock_text
  mock_text_path: {sample_path.as_posix()}
wecom:
  enabled: true
  webhook_url: https://example.invalid/webhook
  dry_run: false
  max_retries: 0
""".strip(),
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout=30):
        return FakeResponse(json.dumps({"errcode": 40058, "errmsg": "content too long"}))

    monkeypatch.setattr("alert_ai_assistant.notifier.urlopen", fake_urlopen)

    code = main(["run-once", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 4
    assert "WeCom notification failed" in captured.err


def test_check_config_reports_missing_wecom_for_real_send(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
source:
  kind: mock_text
  mock_text_path: sample.txt
wecom:
  dry_run: false
""".strip(),
        encoding="utf-8",
    )

    code = main(["check-config", "--config", str(config_path)])
    output = capsys.readouterr().out

    assert code == 2
    assert "wecom.enabled" in output
    assert "wecom.webhook_url" in output


def test_check_config_handles_invalid_yaml(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("source: [", encoding="utf-8")

    code = main(["check-config", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "failed to load config" in captured.err


def test_status_prints_latest_summary(tmp_path, capsys):
    sample_path = tmp_path / "sample.txt"
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "alerts.db"
    lock_path = tmp_path / "run.lock"
    log_path = tmp_path / "app.log"
    sample_path.write_text(_sample_alarm_text(), encoding="utf-8")
    config_path.write_text(
        f"""
database_path: {db_path.as_posix()}
lock_file: {lock_path.as_posix()}
log_file: {log_path.as_posix()}
source:
  kind: mock_text
  mock_text_path: {sample_path.as_posix()}
wecom:
  dry_run: true
""".strip(),
        encoding="utf-8",
    )

    assert main(["run-once", "--config", str(config_path), "--dry-run"]) == 0
    code = main(["status", "--config", str(config_path)])
    output = capsys.readouterr().out

    assert code == 0
    assert "latest_summary=id=" in output
    assert "delivered=False" in output


def _sample_alarm_text() -> str:
    window_start, _ = previous_hour_window()
    alarm_time = window_start.replace(minute=5, second=0, microsecond=0)
    return f"""
网络运维管理平台 {alarm_time.month}/{alarm_time.day} {alarm_time:%H:%M:%S}
[Alarm]服务器接入交换机端口连接状态告警
端口连接状态告警
当前采集值：Ethernet1/42 状态: down

主机名称：SW-A
IP地址：10.0.0.1
告警级别：严重
告警时间：{alarm_time:%Y-%m-%d %H:%M:%S}
负责人：张三(zhangsan)
"""


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body.encode("utf-8")
