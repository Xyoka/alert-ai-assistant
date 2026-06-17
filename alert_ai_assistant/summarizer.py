from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re
from typing import Iterable

from .config import AppConfig
from .llm import OpenAICompatibleClient
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, SummaryStats
from .sanitizer import sanitize_for_config


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
    # Focus alerts: exclude already-resolved alarms to reduce noise
    current = unresolved_current_records(record_list)
    focus_alerts = [
        record
        for record in current
        if not is_low_priority(record, low_priority_keywords)
    ][:max_focus_alerts]

    # Counts directly from raw API results (same as platform view)
    unhandled_count = sum(1 for r in record_list if r.status_bucket == STATUS_UNHANDLED)
    processing_count = sum(1 for r in record_list if r.status_bucket == STATUS_PROCESSING)
    ended_count = sum(1 for r in window_records if r.status_bucket == STATUS_ENDED)

    return SummaryStats(
        window_start=window_start,
        window_end=window_end,
        total_new=len(window_records),
        unhandled_count=unhandled_count,
        processing_count=processing_count,
        ended_count=ended_count,
        low_priority_count=len(low_priority),
        focus_alerts=focus_alerts,
        all_alerts=record_list,
    )


def generate_summary(
    stats: SummaryStats,
    config: AppConfig,
    llm_client: OpenAICompatibleClient | None = None,
) -> tuple[str, bool]:
    prompt = sanitize_for_config(build_llm_prompt(stats, config), config)
    client = llm_client or OpenAICompatibleClient(config.llm)
    ai_text = client.complete(prompt) if client else None
    if ai_text:
        return sanitize_for_config(ai_text, config), True
    return sanitize_for_config(fallback_summary(stats), config), False


def build_reference_alerts(stats: SummaryStats, max_items: int) -> list[AlertRecord]:
    candidates = list(stats.focus_alerts)
    if len(candidates) < max_items:
        existing = {alert.content_hash for alert in candidates}
        for alert in stats.all_alerts:
            if alert.status_bucket not in {STATUS_UNHANDLED, STATUS_PROCESSING}:
                continue
            if alert.content_hash in existing:
                continue
            candidates.append(alert)
            existing.add(alert.content_hash)
            if len(candidates) >= max_items:
                break
    return candidates[:max_items]


def append_reference_section(summary_text: str, reference_alerts: list[AlertRecord]) -> str:
    if not reference_alerts:
        return summary_text
    lines = [
        "",
        "**可追问告警**",
    ]
    for index, alert in enumerate(reference_alerts, start=1):
        title = single_line(alert.title or alert.content, 70)
        lines.append(
            f"A{index}：{alert.device_ip or '-'} / {alert.hostname or '-'} / "
            f"{alert.alarm_time_text or '-'} / {title}"
        )
    return f"{summary_text.rstrip()}\n" + "\n".join(lines)


def build_llm_prompt(stats: SummaryStats, config: AppConfig) -> str:
    unhandled = [a for a in stats.all_alerts if a.status_bucket == "unhandled"]
    processing = [a for a in stats.all_alerts if a.status_bucket == "processing"]
    ended = [a for a in stats.all_alerts if a.status_bucket == "ended"]

    _BW_KEYWORDS = ["使用率告警", "入流量使用率", "出流量使用率"]
    def _is_bw(a):
        text = f"{a.title}\n{a.content}"
        return any(kw in text for kw in _BW_KEYWORDS)

    def brief(alert):
        interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
        content_detail = (alert.content or alert.title).replace("<br>", " | ")
        content_snippet = single_line(content_detail, 250)
        return {
            "ip": alert.device_ip,
            "time": alert.alarm_time_text,
            "title": alert.title,
            "interface": interface or "",
            "content_detail": content_snippet,
        }

    # Separate bandwidth alarms from detailed ones
    def split_bw(items):
        bw = [a for a in items if _is_bw(a)]
        detail = [a for a in items if not _is_bw(a)]
        return bw, detail

    bw_unhandled, det_unhandled = split_bw(unhandled)
    bw_processing, det_processing = split_bw(processing)
    bw_ended, det_ended = split_bw(ended)

    payload = {
        "window": f"{stats.window_start:%Y-%m-%d %H:%M} - {stats.window_end:%Y-%m-%d %H:%M}",
        "stats": {
            "total": stats.total_new,
            "unhandled": stats.unhandled_count,
            "processing": stats.processing_count,
            "ended": stats.ended_count,
        },
        "alert_list": {
            "未处理": [brief(a) for a in det_unhandled],
            "处理中": [brief(a) for a in det_processing],
            "已结束": [brief(a) for a in det_ended],
        },
        "带宽利用率告警": {
            "未处理": len(bw_unhandled),
            "处理中": len(bw_processing),
            "已结束": len(bw_ended),
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
请根据下面的结构化网络告警数据生成企业微信摘要。

要求和规矩：
- 严格按以下三段格式输出，不允许改变段名称和顺序：
  **总体情况**
  **未处理（重点）**
  **处理中**
  **已结束**
- **总体情况**：逐行列出，格式：
  - 窗口时间：
  - 未处理：x条
  - 处理中：x条
  - 已结束：x条
- **未处理（重点）**：观察每条告警的 content_detail，按**实际故障类型**分类（如端口Down、端口Up、链路故障、配置变更、CPU/内存告警、端口错误等），不要用"日志告警类""网络设备日志告警"这种笼统分类。子类名为纯文本不加粗。每条格式：IP / 时间 / 简要内容。不要输出负责人姓名或账号。`带宽利用率告警`字段按桶统计了数量，属于哪个桶就加到哪个段末尾（格式："端口带宽利用率告警：N条"），不逐条展开。
- **处理中**：格式同上。带宽利用率告警同样处理。
- **已结束**：格式同上。带宽利用率告警同样处理。
- 某类无告警则子内容写"无"，段名照常输出。

数据：
{payload_json}
""".strip()


def _collect_person(alert: AlertRecord) -> list[str]:
    """Extract responsible person names from hostname (instance_name), matching platform order."""
    person_str = _extract_person(alert.hostname)
    if person_str:
        return [n.strip() for n in person_str.replace("、", ",").split(",") if n.strip()]
    return []


def fallback_summary(stats: SummaryStats) -> str:
    window = f"{stats.window_start:%Y-%m-%d %H:%M}-{stats.window_end:%H:%M}"
    if stats.total_new == 0 and not stats.unhandled_count and not stats.processing_count:
        return (
            f"**总体情况**\n"
            f"- 窗口时间：{window}\n"
            f"- 未处理：0条\n"
            f"- 处理中：0条\n"
            f"- 已结束：0条\n\n"
            "**未处理（重点）**\n无\n\n"
            "**处理中**\n无\n\n"
            "**已结束**\n无"
        )

    unhandled = [a for a in stats.all_alerts if a.status_bucket == "unhandled"]
    processing = [a for a in stats.all_alerts if a.status_bucket == "processing"]
    ended = [a for a in stats.all_alerts if a.status_bucket == "ended"]

    _BW_KEYWORDS = ["使用率告警", "入流量使用率", "出流量使用率"]

    def _is_bandwidth(title: str) -> bool:
        return any(kw in title for kw in _BW_KEYWORDS)

    def _section(records, title):
        lines = [f"**{title}**"]
        if not records:
            lines.append("无")
            return lines
        groups = Counter(a.title for a in records)
        for group_title, count in groups.most_common():
            if _is_bandwidth(group_title):
                lines.append(f"端口带宽利用率告警：{count}条")
            else:
                lines.append(f"{group_title}：")
                for alert in [a for a in records if a.title == group_title]:
                    interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
                    intf_text = f" ({interface})" if interface else ""
                    lines.append(f"- {alert.device_ip} / {alert.alarm_time_text}{intf_text}")
        return lines

    result = [
        "**总体情况**",
        f"- 窗口时间：{window}",
        f"- 未处理：{stats.unhandled_count}条",
        f"- 处理中：{stats.processing_count}条",
        f"- 已结束：{stats.ended_count}条",
        "",
    ]
    result.extend(_section(unhandled, "未处理（重点）"))
    result.append("")
    result.extend(_section(processing, "处理中"))
    result.append("")
    result.extend(_section(ended, "已结束"))

    return "\n".join(result)


def _extract_person(hostname: str) -> str:
    """从 hostname 中提取负责人姓名。支持下划线（_）和横线（-）分隔。"""
    for sep in ("_", "-"):
        idx = hostname.rfind(sep)
        if idx == -1 or idx + 1 >= len(hostname):
            continue
        suffix = hostname[idx + 1:]
        if any("\u4e00" <= c <= "\u9fff" for c in suffix):
            return suffix
    return ""


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
