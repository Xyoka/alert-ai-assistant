from datetime import datetime, timedelta

from alert_ai_assistant.agent import AlertAgent
from alert_ai_assistant.config import AppConfig
from alert_ai_assistant.models import AlertRecord
from alert_ai_assistant.storage import AlertStore


class NamedLLM:
    def __init__(self):
        self.prompt = ""

    def complete(self, prompt: str):
        self.prompt = prompt
        return "建议联系张三(zhangsan)确认。"


def test_agent_answers_by_summary_reference(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    first = datetime.now() - timedelta(minutes=30)
    second = datetime.now() - timedelta(minutes=5)
    records = [
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip="10.0.0.1",
            hostname="SW-A",
            alarm_time=first,
            title="端口连接状态告警",
            content="Ethernet1/42 状态: down",
        ),
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip="10.0.0.1",
            hostname="SW-A",
            alarm_time=second,
            title="端口连接状态告警",
            content="Ethernet1/42 状态: down",
        ),
        AlertRecord(
            source_type="snmp",
            status_bucket="unhandled",
            device_ip="10.0.0.1",
            hostname="SW-A",
            alarm_time=second,
            title="端口连接状态告警",
            content="Ethernet1/43 状态: down",
        ),
    ]
    store.insert_alerts(records)
    summary_id = store.save_summary(first, second, "summary", {}, ai_used=False, delivered=False)
    store.save_summary_items(summary_id, [records[1]])

    answer = AlertAgent(AppConfig(), store).answer("A1 这条告警是什么时间报出来的，报了几次？")

    assert "10.0.0.1" in answer.text
    assert "首次时间" in answer.text
    assert "最近 7 天匹配次数：2 次" in answer.text
    assert "Ethernet1/42 状态: down" in answer.text


def test_agent_contextual_question_uses_last_answer(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    alarm_time = datetime.now() - timedelta(minutes=10)
    record = AlertRecord(
        source_type="log",
        status_bucket="processing",
        device_ip="10.0.0.2",
        hostname="FW-A",
        alarm_time=alarm_time,
        title="CPU告警",
        content="CPU high",
    )
    store.insert_alerts([record])
    summary_id = store.save_summary(alarm_time, alarm_time, "summary", {}, ai_used=False, delivered=False)
    store.save_summary_items(summary_id, [record])
    agent = AlertAgent(AppConfig(), store)

    first = agent.answer("A1 是什么？", chat_id="chat1", user_id="user1")
    second = agent.answer("这条告警如何处理？", chat_id="chat1", user_id="user1")

    assert "10.0.0.2" in first.text
    assert "10.0.0.2" in second.text
    assert "处理建议" in second.text


def test_agent_answer_and_llm_prompt_are_sanitized(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.init_schema()
    alarm_time = datetime.now() - timedelta(minutes=10)
    record = AlertRecord(
        source_type="log",
        status_bucket="processing",
        device_ip="10.0.0.3",
        hostname="FW-A_张三",
        alarm_time=alarm_time,
        title="CPU告警",
        content="CPU high",
        raw_payload="负责人：张三(zhangsan)",
    )
    store.insert_alerts([record])
    summary_id = store.save_summary(alarm_time, alarm_time, "summary", {}, ai_used=False, delivered=False)
    store.save_summary_items(summary_id, [record])
    config = AppConfig()
    config.mask_names = ["张三"]
    llm = NamedLLM()

    answer = AlertAgent(config, store, llm_client=llm).answer("A1 如何处理？")

    assert "张三" not in llm.prompt
    assert "zhangsan" not in llm.prompt
    assert "张三" not in answer.text
    assert "zhangsan" not in answer.text
    assert "<已脱敏>" in answer.text
