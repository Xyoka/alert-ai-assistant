from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
import re

from .config import AppConfig
from .llm import OpenAICompatibleClient
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED
from .sanitizer import sanitize_for_config
from .storage import AlertStore
from .summarizer import extract_interface, single_line


REF_RE = re.compile(r"\bA\s*(\d{1,3})\b", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*条")


@dataclass(slots=True)
class AgentAnswer:
    text: str
    matched: dict


class AlertAgent:
    def __init__(
        self,
        config: AppConfig,
        store: AlertStore,
        llm_client: OpenAICompatibleClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.llm_client = llm_client or OpenAICompatibleClient(config.llm)

    def answer(self, question: str, chat_id: str = "", user_id: str = "") -> AgentAnswer:
        question = (question or "").strip()
        if not question:
            return AgentAnswer("请直接发送告警编号、IP、主机名或想追问的问题。", {})

        matches = self._find_matches(question, chat_id, user_id)
        if not matches:
            text = (
                f"未在最近 {self.config.agent.history_days} 天的本地告警记录中找到匹配项。\n"
                "可以尝试发送摘要里的编号（如 A1）、设备 IP、主机名或告警关键字。"
            )
            self.store.save_qa_interaction(chat_id, user_id, question, text, {"matches": 0})
            return AgentAnswer(text, {"matches": 0})

        if len(matches) > 1 and not _has_direct_reference(question):
            text = sanitize_for_config(self._format_candidates(matches), self.config)
            self.store.save_qa_interaction(chat_id, user_id, question, text, {"matches": len(matches)})
            return AgentAnswer(text, {"matches": len(matches), "ambiguous": True})

        target = matches[0]
        related = self.store.related_alerts(target, self.config.agent.history_days)
        related = _filter_related_by_interface(target, related)
        if not related:
            related = [target]
        answer = sanitize_for_config(self._format_alert_answer(target, related, question), self.config)
        matched = {
            "matches": len(matches),
            "device_ip": target.device_ip,
            "hostname": target.hostname,
            "title": target.title,
            "content_hash": target.content_hash,
        }
        self._remember_target(chat_id, user_id, target)
        self.store.save_qa_interaction(chat_id, user_id, question, answer, matched)
        return AgentAnswer(answer, matched)

    def _find_matches(self, question: str, chat_id: str, user_id: str) -> list[AlertRecord]:
        ref = extract_reference(question)
        if ref:
            item = self.store.find_summary_item(ref)
            return [_summary_item_to_record(item)] if item else []

        if _is_contextual_question(question):
            remembered = self._remembered_target(chat_id, user_id)
            if remembered:
                return [remembered]
            latest_items = self.store.get_summary_items(limit=1)
            if latest_items:
                return [_summary_item_to_record(latest_items[0])]

        ip_match = IP_RE.search(question)
        if ip_match:
            matches = self.store.search_alerts(
                ip_match.group(0),
                self.config.agent.history_days,
                self.config.agent.max_candidates,
            )
            return _dedupe_alerts(matches)

        tokens = _query_tokens(question)
        if not tokens:
            return []
        matches = self.store.search_alerts(
            " ".join(tokens),
            self.config.agent.history_days,
            self.config.agent.max_candidates,
        )
        return _dedupe_alerts(matches)

    def _format_candidates(self, matches: list[AlertRecord]) -> str:
        lines = [
            "找到多条可能相关的告警，请补充编号、IP 或更具体的关键字：",
        ]
        for idx, alert in enumerate(matches[: self.config.agent.max_candidates], start=1):
            lines.append(
                f"{idx}. {alert.device_ip or '-'} / {alert.hostname or '-'} / "
                f"{alert.alarm_time_text or '-'} / {single_line(alert.title or alert.content, 70)}"
            )
        return "\n".join(lines)

    def _format_alert_answer(
        self,
        target: AlertRecord,
        related: list[AlertRecord],
        question: str,
    ) -> str:
        ordered = sorted(
            related,
            key=lambda item: item.alarm_time or datetime.min,
        )
        first = next((item.alarm_time for item in ordered if item.alarm_time), None)
        last = next((item.alarm_time for item in reversed(ordered) if item.alarm_time), None)
        status_counts = Counter(item.status_bucket for item in ordered)
        latest = ordered[-1] if ordered else target
        interface = extract_interface(f"{target.title}\n{target.content}\n{target.raw_payload}")
        duration = _format_duration(first, last, latest.status_bucket)
        content = single_line(target.content or target.title, 450)

        lines = [
            "告警查询结果：",
            f"- IP：{target.device_ip or '-'}",
            f"- 主机：{target.hostname or '-'}",
            f"- 首次时间：{_time_text(first)}",
            f"- 最近时间：{_time_text(last)}",
            f"- 最近状态：{_status_text(latest.status_bucket)}",
            f"- 最近 {self.config.agent.history_days} 天匹配次数：{len(ordered)} 次",
            f"- 状态分布：未处理 {status_counts.get(STATUS_UNHANDLED, 0)}，处理中 {status_counts.get(STATUS_PROCESSING, 0)}，已结束 {status_counts.get(STATUS_ENDED, 0)}",
            f"- 持续情况：{duration}",
        ]
        if interface:
            lines.append(f"- 接口/对象：{interface}")
        lines.extend(
            [
                f"- 告警标题：{target.title or '-'}",
                f"- 告警内容：{content or '-'}",
                "",
                "处理建议：",
                self._advice(target, related, question),
                "",
                "说明：以上结果来自本地保存的网管告警数据，最终状态以网管平台为准。",
            ]
        )
        return "\n".join(lines)

    def _advice(self, target: AlertRecord, related: list[AlertRecord], question: str) -> str:
        prompt = build_advice_prompt(target, related, question, self.config)
        ai_text = self.llm_client.complete(prompt) if self.llm_client else None
        if ai_text:
            return sanitize_for_config(ai_text.strip(), self.config)
        return (
            "建议先在网管平台确认当前状态和最近恢复记录；再核对设备、接口或链路的实时状态，"
            "结合近期变更、对端设备日志和链路质量判断是否仍在影响业务。若仍未恢复，按值班流程升级处理。"
        )

    def _remember_target(self, chat_id: str, user_id: str, target: AlertRecord) -> None:
        if not chat_id and not user_id:
            return
        self.store.set_conversation_state(
            chat_id,
            user_id,
            "last_alert",
            target.to_dict(include_raw=True),
        )

    def _remembered_target(self, chat_id: str, user_id: str) -> AlertRecord | None:
        if not chat_id and not user_id:
            return None
        value = self.store.get_conversation_state(chat_id, user_id, "last_alert")
        if not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _dict_to_record(data)


def build_advice_prompt(
    target: AlertRecord,
    related: list[AlertRecord],
    question: str,
    config: AppConfig,
) -> str:
    payload = {
        "question": question,
        "target_alert": target.to_dict(),
        "related_count": len(related),
        "related_statuses": Counter(item.status_bucket for item in related),
        "related_times": [item.alarm_time_text for item in related[:20]],
    }
    payload_json = sanitize_for_config(
        json.dumps(payload, ensure_ascii=False, default=str),
        config,
    )
    return (
        "请基于下面的网络告警事实，给出谨慎的排查建议。"
        "不要断言无需处理，不要编造不存在的恢复状态。"
        "输出 2-4 条短建议即可。\n"
        f"{payload_json}"
    )


def extract_reference(question: str) -> str:
    match = REF_RE.search(question)
    if match:
        return f"A{int(match.group(1))}"
    ordinal = ORDINAL_RE.search(question)
    if ordinal:
        number = _cn_number_to_int(ordinal.group(1))
        if number:
            return f"A{number}"
    return ""


def _has_direct_reference(question: str) -> bool:
    return bool(extract_reference(question) or IP_RE.search(question))


def _is_contextual_question(question: str) -> bool:
    return any(keyword in question for keyword in ("这条", "该告警", "上面", "刚才", "它", "这个告警"))


def _query_tokens(question: str) -> list[str]:
    ignored = {
        "告警",
        "是什么",
        "什么",
        "时间",
        "报了",
        "几次",
        "多久",
        "持续",
        "如何",
        "处理",
        "原因",
        "建议",
    }
    rough = re.findall(r"[A-Za-z0-9_.:/-]{3,}|[\u4e00-\u9fff]{3,}", question)
    return [token for token in rough if token not in ignored]


def _summary_item_to_record(item: dict | None) -> AlertRecord:
    if not item:
        raise ValueError("summary item is empty")
    return AlertRecord(
        source_type=str(item.get("source_type") or "unknown"),
        status_bucket=str(item.get("status_bucket") or ""),
        device_ip=str(item.get("device_ip") or ""),
        hostname=str(item.get("hostname") or ""),
        alarm_time=_parse_time(str(item.get("alarm_time") or "")),
        title=str(item.get("title") or ""),
        content=str(item.get("content") or ""),
        severity=str(item.get("severity") or ""),
        external_id=str(item.get("external_id") or ""),
        raw_payload=str(item.get("raw_payload") or ""),
        content_hash=str(item.get("alert_content_hash") or ""),
    )


def _dict_to_record(data: dict) -> AlertRecord:
    return AlertRecord(
        source_type=str(data.get("source_type") or "unknown"),
        status_bucket=str(data.get("status_bucket") or ""),
        device_ip=str(data.get("device_ip") or ""),
        hostname=str(data.get("hostname") or ""),
        alarm_time=_parse_time(str(data.get("alarm_time") or "")),
        title=str(data.get("title") or ""),
        content=str(data.get("content") or ""),
        severity=str(data.get("severity") or ""),
        external_id=str(data.get("external_id") or ""),
        raw_payload=str(data.get("raw_payload") or ""),
        content_hash=str(data.get("content_hash") or ""),
    )


def _dedupe_alerts(records: list[AlertRecord]) -> list[AlertRecord]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[AlertRecord] = []
    for record in records:
        key = (record.device_ip, record.title, record.content_hash, record.alarm_time_text)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _filter_related_by_interface(target: AlertRecord, related: list[AlertRecord]) -> list[AlertRecord]:
    target_interface = extract_interface(f"{target.title}\n{target.content}\n{target.raw_payload}")
    if not target_interface:
        return related
    filtered = [
        record
        for record in related
        if extract_interface(f"{record.title}\n{record.content}\n{record.raw_payload}") == target_interface
    ]
    return filtered or related


def _format_duration(first: datetime | None, last: datetime | None, latest_status: str) -> str:
    if not first:
        return "未能从本地记录判断"
    if not last:
        return "未能从本地记录判断"
    end = last
    if latest_status != STATUS_ENDED:
        end = datetime.now()
    seconds = max(0, int((end - first).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    suffix = "，当前仍需以网管平台确认是否恢复" if latest_status != STATUS_ENDED else "，本地记录显示已有结束记录"
    if hours:
        return f"约 {hours} 小时 {minutes} 分钟{suffix}"
    return f"约 {minutes} 分钟{suffix}"


def _status_text(status: str) -> str:
    return {
        STATUS_UNHANDLED: "未处理",
        STATUS_PROCESSING: "处理中",
        STATUS_ENDED: "已结束",
    }.get(status, status or "未知")


def _time_text(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _cn_number_to_int(text: str) -> int:
    if text.isdigit():
        return int(text)
    mapping = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if text in mapping:
        return mapping[text]
    if text.startswith("十") and len(text) == 2:
        return 10 + mapping.get(text[1], 0)
    if text.endswith("十") and len(text) == 2:
        return mapping.get(text[0], 0) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return mapping.get(left, 1) * 10 + mapping.get(right, 0)
    return 0
