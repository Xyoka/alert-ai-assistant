from datetime import datetime

from alert_ai_assistant.config import AppConfig
from alert_ai_assistant.models import AlertRecord
from alert_ai_assistant.summarizer import (
    ai_summary_covers_required_alerts,
    build_llm_prompt,
    build_stats,
    fallback_summary,
    generate_summary,
)


class FailingLLM:
    def complete(self, prompt: str):
        return None


class StaticLLM:
    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, prompt: str):
        return self.text


def test_no_alarm_summary():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)

    stats = build_stats([], start, end, [], 10)
    text = fallback_summary(stats)

    assert "**总体情况**" in text
    assert "未处理：0条" in text
    assert "**未处理（重点）**" in text


def test_low_priority_counted_but_not_focused():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip="10.0.0.1",
            hostname="SW-A",
            alarm_time=start,
            title="服务器接入交换机端口连接状态告警",
            content="Ethernet1/1 down",
        ),
        AlertRecord(
            source_type="log",
            status_bucket="processing",
            device_ip="10.0.0.2",
            hostname="FW-A",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        ),
    ]

    stats = build_stats(records, start, end, ["服务器接入交换机端口连接状态告警"], 10)

    assert stats.total_new == 2
    assert stats.low_priority_count == 1
    assert len(stats.focus_alerts) == 1
    assert stats.focus_alerts[0].device_ip == "10.0.0.2"


def test_generate_summary_falls_back_when_llm_fails():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    config = AppConfig()
    stats = build_stats([], start, end, [], 10)

    text, ai_used = generate_summary(stats, config, llm_client=FailingLLM())

    assert not ai_used
    assert "**总体情况**" in text
    assert "0条" in text


def test_generate_summary_falls_back_when_ai_omits_required_alerts():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    config = AppConfig()
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip="10.0.0.1",
            hostname="SW-A",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        )
    ]
    stats = build_stats(records, start, end, [], 10)

    text, ai_used = generate_summary(stats, config, llm_client=StaticLLM("**总体情况**\n- 未处理：1条"))

    assert not ai_used
    assert "IP：10.0.0.1" in text


def test_ai_summary_coverage_ignores_processing_detail():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="processing",
            device_ip="10.0.2.1",
            hostname="SW-P",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        )
    ]
    stats = build_stats(records, start, end, [], 10)

    assert ai_summary_covers_required_alerts("**处理中**\n- CPU告警：1条", stats)


def test_ai_summary_coverage_rejects_disallowed_phrases():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    stats = build_stats([], start, end, [], 10)

    assert not ai_summary_covers_required_alerts("另有3条未展开，请登录网管平台查看完整列表", stats)
    assert not ai_summary_covers_required_alerts("当前无需处理", stats)


def test_fallback_summary_expands_all_unhandled_and_ended_alerts():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip=f"10.0.0.{index}",
            hostname=f"SW-{index}",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        )
        for index in range(5)
    ]
    records.append(
        AlertRecord(
            source_type="snmp",
            status_bucket="ended",
            device_ip="10.0.1.1",
            hostname="SW-ended",
            alarm_time=start,
            title="端口恢复",
            content="Ethernet1/1 up",
        )
    )
    stats = build_stats(records, start, end, [], 10)

    text = fallback_summary(stats, max_items_per_section=2)

    assert text.count("IP：10.0.0.") == 5
    assert "IP：10.0.1.1" in text
    assert "另有" not in text
    assert text.index("**未处理（重点）**") < text.index("**已结束**") < text.index("**处理中**")


def test_fallback_summary_keeps_processing_as_grouped_statistics():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="processing",
            device_ip=f"10.0.2.{index}",
            hostname=f"SW-P-{index}",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        )
        for index in range(3)
    ]
    stats = build_stats(records, start, end, [], 10)

    text = fallback_summary(stats)

    assert "共3条，管理员已确认，按类型归类如下：" in text
    assert "- CPU告警：3条" in text
    assert "IP：10.0.2." not in text


def test_llm_prompt_includes_all_alert_details_without_omitted_bucket():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    config = AppConfig()
    config.llm.max_prompt_alerts_per_bucket = 10  # high enough to avoid truncation
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip=f"10.0.0.{index}",
            hostname=f"SW-{index}",
            alarm_time=start,
            title="CPU告警",
            content="CPU high",
        )
        for index in range(5)
    ]
    stats = build_stats(records, start, end, [], 10)

    prompt = build_llm_prompt(stats, config)

    for index in range(5):
        assert f"10.0.0.{index}" in prompt
    assert '"未展开明细数"' not in prompt
    # Prompt now uses concise format without priority hint line.
    assert "**总体情况**" in prompt
