from datetime import datetime

from alert_ai_assistant.config import AppConfig
from alert_ai_assistant.models import AlertRecord
from alert_ai_assistant.summarizer import build_llm_prompt, build_stats, fallback_summary, generate_summary


class FailingLLM:
    def complete(self, prompt: str):
        return None


class EchoLLM:
    def __init__(self):
        self.prompt = ""

    def complete(self, prompt: str):
        self.prompt = prompt
        return "负责人：张三(zhangsan)\n建议确认张三"


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


def test_summary_prompt_and_output_are_sanitized():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    config = AppConfig()
    config.mask_names = ["张三"]
    record = AlertRecord(
        source_type="snmp",
        status_bucket="unhandled",
        device_ip="10.0.0.1",
        hostname="SW-A_张三",
        alarm_time=start,
        title="端口连接状态告警",
        content="Ethernet1/42 down",
        raw_payload="负责人：张三(zhangsan)",
    )
    stats = build_stats([record], start, end, [], 10)
    llm = EchoLLM()

    text, ai_used = generate_summary(stats, config, llm_client=llm)

    assert ai_used
    assert "张三" not in llm.prompt
    assert "zhangsan" not in llm.prompt
    assert "张三" not in text
    assert "zhangsan" not in text
    assert "<已脱敏>" in text


def test_llm_prompt_does_not_include_responsible_person_field():
    start = datetime(2026, 5, 11, 10)
    end = datetime(2026, 5, 11, 10, 59, 59)
    config = AppConfig()
    record = AlertRecord(
        source_type="snmp",
        status_bucket="unhandled",
        device_ip="10.0.0.1",
        hostname="SW-A_张三",
        alarm_time=start,
        title="端口连接状态告警",
        content="Ethernet1/42 down",
    )

    prompt = build_llm_prompt(build_stats([record], start, end, [], 10), config)

    assert '"负责人"' not in prompt
