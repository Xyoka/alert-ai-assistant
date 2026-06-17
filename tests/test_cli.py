from alert_ai_assistant.cli import main
from alert_ai_assistant.cli import previous_hour_window
from alert_ai_assistant.storage import AlertStore


def test_check_config_summary_only_dry_run_passes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app_mode: summary_only
database_path: data/alerts.db
source:
  kind: mock_text
  mock_text_path: sample.txt
wecom:
  dry_run: true
""".strip(),
        encoding="utf-8",
    )

    code = main(["check-config", "--config", str(config_path)])
    output = capsys.readouterr().out

    assert code == 0
    assert "配置检查通过" in output


def test_check_config_agent_requires_summary_target(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app_mode: agent
database_path: data/alerts.db
source:
  kind: mock_text
  mock_text_path: sample.txt
wecom_intelligent_bot:
  enabled: true
  dry_run: true
agent:
  fallback_webhook_enabled: false
""".strip(),
        encoding="utf-8",
    )

    code = main(["check-config", "--config", str(config_path)])
    output = capsys.readouterr().out

    assert code == 2
    assert "summary_target_id" in output


def test_check_config_agent_real_bot_requires_sdk_and_credentials(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app_mode: agent
database_path: data/alerts.db
source:
  kind: mock_text
  mock_text_path: sample.txt
wecom:
  dry_run: true
wecom_intelligent_bot:
  enabled: true
  summary_target_id: chat1
  dry_run: false
agent:
  fallback_webhook_enabled: false
""".strip(),
        encoding="utf-8",
    )

    code = main(["check-config", "--config", str(config_path)])
    output = capsys.readouterr().out

    assert code == 2
    assert "bot_id" in output
    assert "secret" in output


def test_run_once_summary_only_dry_run_keeps_existing_summary_shape(tmp_path, capsys):
    sample_path = tmp_path / "sample.txt"
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "alerts.db"
    lock_path = tmp_path / "run.lock"
    log_path = tmp_path / "app.log"
    sample_path.write_text(_sample_alarm_text(), encoding="utf-8")
    config_path.write_text(
        f"""
app_mode: summary_only
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

    code = main(["run-once", "--config", str(config_path), "--dry-run"])
    output = capsys.readouterr().out

    assert code == 0
    assert "**总体情况**" in output
    assert "**可追问告警**" not in output


def test_run_once_agent_dry_run_saves_reference_items(tmp_path, capsys):
    sample_path = tmp_path / "sample.txt"
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "alerts.db"
    lock_path = tmp_path / "run.lock"
    log_path = tmp_path / "app.log"
    sample_path.write_text(_sample_alarm_text(), encoding="utf-8")
    config_path.write_text(
        f"""
app_mode: agent
database_path: {db_path.as_posix()}
lock_file: {lock_path.as_posix()}
log_file: {log_path.as_posix()}
source:
  kind: mock_text
  mock_text_path: {sample_path.as_posix()}
wecom:
  dry_run: true
wecom_intelligent_bot:
  enabled: true
  summary_target_id: chat1
  dry_run: true
agent:
  fallback_webhook_enabled: true
""".strip(),
        encoding="utf-8",
    )

    code = main(["run-once", "--config", str(config_path), "--dry-run"])
    output = capsys.readouterr().out

    assert code == 0
    assert "**可追问告警**" in output
    assert "A1：" in output
    items = AlertStore(db_path).get_summary_items(limit=5)
    assert len(items) == 1
    assert items[0]["ref_code"] == "A1"

    status_code = main(["status", "--config", str(config_path)])
    status_output = capsys.readouterr().out

    assert status_code == 0
    assert "latest_summary=id=" in status_output
    assert "bot_status=none" in status_output


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
