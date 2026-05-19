from pathlib import Path

import pytest

from alert_ai_assistant.sources import MockTextSource


SAMPLE_PATH = Path("/Users/zhangyanrui/Downloads/企业微信告警消息复制.txt")


@pytest.mark.skipif(not SAMPLE_PATH.exists(), reason="local sample file is not available")
def test_parse_local_wecom_copy_sample():
    records = MockTextSource.parse_text(SAMPLE_PATH.read_text(encoding="utf-8-sig"))

    assert len(records) == 46
    assert sum(1 for record in records if record.source_type == "log") == 20
    assert sum(1 for record in records if record.source_type == "snmp") == 26
    assert records[0].device_ip == "10.68.104.12"
    assert records[0].hostname == "SHJQD7-DMZ/BS-CE6881-G01-25U"
    assert records[0].alarm_time_text == "2026-05-08 17:39:24"


def test_parse_minimal_block():
    text = """
网络运维管理平台 5/8 17:42:34
[Alarm]服务器接入交换机端口连接状态告警
端口连接状态告警
当前采集值：Ethernet1/42 状态: up & Ethernet1/42 状态: down

主机名称：SW-A
IP地址：10.0.0.1
告警级别：严重
告警时间：2026-05-08 17:42:18
负责人：张三(zhangsan)
"""

    records = MockTextSource.parse_text(text)

    assert len(records) == 1
    assert records[0].source_type == "snmp"
    assert records[0].status_bucket == "unhandled"
    assert records[0].content == "Ethernet1/42 状态: up & Ethernet1/42 状态: down"

