from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Iterable

from .config import AppConfig
from .llm import OpenAICompatibleClient
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, SummaryStats
from .sanitizer import sanitize_text


def build_stats(
    records: Iterable[AlertRecord],
    window_start: datetime,
    window_end: datetime,
    low_priority_keywords: list[str],
    max_focus_alerts: int,
) -> SummaryStats:
    record_list = sorted(
        list(records),
        key=lambda item: item.alarm_time or datetime.min,
        reverse=True,
    )
    window_records = [
        record
        for record in record_list
        if record.alarm_time and window_start <= record.alarm_time <= window_end
    ]
    low_priority = [record for record in record_list if is_low_priority(record, low_priority_keywords)]
    current = unresolved_current_records(record_list)
    focus_alerts = [
        record
        for record in current
        if not is_low_priority(record, low_priority_keywords)
    ][:max_focus_alerts]

    return SummaryStats(
        window_start=window_start,
        window_end=window_end,
        total_new=len(window_records),
        unhandled_count=sum(1 for record in current if record.status_bucket == STATUS_UNHANDLED),
        processing_count=sum(1 for record in current if record.status_bucket == STATUS_PROCESSING),
        ended_count=sum(1 for record in window_records if record.status_bucket == STATUS_ENDED),
        low_priority_count=len(low_priority),
        focus_alerts=focus_alerts,
        all_alerts=record_list,
    )


def generate_summary(
    stats: SummaryStats,
    config: AppConfig,
    llm_client: OpenAICompatibleClient | None = None,
) -> tuple[str, bool]:
    prompt = build_llm_prompt(stats, config)
    client = llm_client or OpenAICompatibleClient(config.llm)
    ai_text = client.complete(prompt) if client else None
    if ai_text:
        return sanitize_text(ai_text, config.mask_names), True
    return fallback_summary(stats, config.mask_names), False


def build_llm_prompt(stats: SummaryStats, config: AppConfig) -> str:
    payload = stats.to_prompt_payload(max_alerts=config.llm.max_focus_alerts)
    sanitized_payload = sanitize_text(json.dumps(payload, ensure_ascii=False, indent=2), config.mask_names)
    return f"""
请根据下面结构化网络告警数据生成企业微信摘要。

硬性要求：
- 固定使用“总体情况 / 建议优先关注 / 其他说明”三段。
- 每条重点告警只列 IP、主机、时间、内容、状态、建议。
- 使用“疑似”“建议确认”“当前仍在未处理/处理中列表”等谨慎措辞。
- 不要输出“无需处理”“可以忽略”“已经确认无风险”。
- 如果没有重点告警，明确写“当前无需要重点关注的未处理/处理中告警”。

数据：
{sanitized_payload}
""".strip()


def fallback_summary(stats: SummaryStats, mask_names: list[str] | None = None) -> str:
    window = f"{stats.window_start:%Y-%m-%d %H:%M}-{stats.window_end:%H:%M}"
    if stats.total_new == 0 and not stats.focus_alerts:
        return (
            f"【网络告警AI摘要】{window}\n"
            "本小时无新增告警。\n"
            "当前无需要重点关注的未处理/处理中告警。"
        )

    lines = [
        f"【网络告警AI摘要】{window}",
        "",
        "一、总体情况",
        f"本小时新增：{stats.total_new} 条",
        f"当前未处理：{stats.unhandled_count} 条",
        f"当前处理中：{stats.processing_count} 条",
        f"本小时已结束：{stats.ended_count} 条",
        f"下联接口类告警：{stats.low_priority_count} 条，已计入统计，未重点展开",
        "",
        "二、建议优先关注",
    ]
    if stats.focus_alerts:
        for index, alert in enumerate(stats.focus_alerts, start=1):
            content = single_line(alert.content or alert.title)
            status_text = "当前仍在未处理/处理中列表"
            lines.extend([
                f"{index}. IP：{alert.device_ip or 'unknown'}",
                f"   主机：{alert.hostname or 'unknown'}",
                f"   时间：{alert.alarm_time_text or 'unknown'}",
                f"   内容：{content}",
                f"   状态：{status_text}",
                "   建议：建议确认",
            ])
    else:
        lines.append("当前无需要重点关注的未处理/处理中告警。")

    lines.extend([
        "",
        "三、其他说明",
        "AI 仅作辅助摘要，最终以网管平台状态为准。",
    ])
    return sanitize_text("\n".join(lines), mask_names)


def is_low_priority(alert: AlertRecord, keywords: list[str]) -> bool:
    text = f"{alert.title}\n{alert.content}"
    return any(keyword and keyword in text for keyword in keywords)


def unresolved_current_records(records: list[AlertRecord]) -> list[AlertRecord]:
    latest_ended_by_key: dict[tuple[str, str], datetime] = {}
    for record in records:
        if record.status_bucket != STATUS_ENDED or not record.alarm_time:
            continue
        key = event_key(record)
        latest = latest_ended_by_key.get(key)
        if latest is None or record.alarm_time > latest:
            latest_ended_by_key[key] = record.alarm_time

    current: list[AlertRecord] = []
    for record in records:
        if record.status_bucket not in {STATUS_UNHANDLED, STATUS_PROCESSING}:
            continue
        ended_at = latest_ended_by_key.get(event_key(record))
        if ended_at and record.alarm_time and ended_at >= record.alarm_time:
            continue
        current.append(record)
    return current


def event_key(alert: AlertRecord) -> tuple[str, str]:
    text = f"{alert.title}\n{alert.content}\n{alert.raw_payload}"
    interface = extract_interface(text)
    if interface:
        return alert.device_ip, f"interface:{interface}"
    return alert.device_ip, f"{alert.title}:{alert.content_hash}"


def extract_interface(text: str) -> str:
    patterns = (
        r"ifName=([^,\)\s]+)",
        r"mainIfname=([^,\)\s]+)",
        r"Interface\s+([^\s]+)\s+is\s+down",
        r"(port-channel\d+)\s+状态\s*:",
        r"([0-9A-Za-z]+[A-Za-z0-9/-]*\d+(?:/\d+)*)\s+状态\s*:",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def single_line(text: str, max_chars: int = 160) -> str:
    value = " ".join((text or "").split())
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars - 3]}..."
