from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re
from typing import Iterable

from .config import AppConfig
from .llm import OpenAICompatibleClient
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, SummaryStats


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
    prompt = build_llm_prompt(stats, config)
    client = llm_client or OpenAICompatibleClient(config.llm)
    ai_text = client.complete(prompt) if client else None
    if ai_text and ai_summary_covers_required_alerts(ai_text, stats):
        return ai_text, True
    return fallback_summary(stats), False


def ai_summary_covers_required_alerts(text: str, stats: SummaryStats) -> bool:
    disallowed_phrases = (
        "未展开",
        "登录网管平台查看",
        "无需处理",
        "可以忽略",
        "已无风险",
    )
    if any(phrase in text for phrase in disallowed_phrases):
        return False
    required = [
        alert
        for alert in stats.all_alerts
        if alert.status_bucket in {STATUS_UNHANDLED, STATUS_ENDED}
    ]
    for alert in required:
        if alert.device_ip and alert.device_ip not in text:
            return False
        if alert.alarm_time_text and alert.alarm_time_text not in text:
            return False
    return True


def build_llm_prompt(stats: SummaryStats, config: AppConfig) -> str:
    unhandled = [a for a in stats.all_alerts if a.status_bucket == "unhandled"]
    processing = [a for a in stats.all_alerts if a.status_bucket == "processing"]
    ended = [a for a in stats.all_alerts if a.status_bucket == "ended"]

    def brief(alert):
        interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
        person = _collect_person(alert)
        content_detail = (alert.content or alert.title).replace("<br>", " | ")
        return {
            "source_type": alert.source_type,
            "ip": alert.device_ip,
            "hostname": alert.hostname,
            "time": alert.alarm_time_text,
            "severity": alert.severity,
            "title": alert.title,
            "interface": interface or "",
            "external_id": alert.external_id,
            "responsible_person": person,
            "content_detail": single_line(content_detail, 0),
        }

    payload = {
        "window": f"{stats.window_start:%Y-%m-%d %H:%M} - {stats.window_end:%Y-%m-%d %H:%M}",
        "stats": {
            "total": stats.total_new,
            "unhandled": stats.unhandled_count,
            "processing": stats.processing_count,
            "ended": stats.ended_count,
        },
        "alert_list": {
            "未处理": [brief(a) for a in unhandled],
            "已结束": [brief(a) for a in ended],
            "处理中": [brief(a) for a in processing],
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
请根据下面的结构化网络告警数据生成企业微信摘要。

要求和规矩：
- 严格按以下四段格式输出，不允许改变段名称和顺序：
  **总体情况**
  **未处理（重点）**
  **已结束**
  **处理中**
- **总体情况**：逐行列出，格式：
  - 窗口时间：
  - 未处理：x条
  - 已结束：x条
  - 处理中：x条
- 展示优先级：未处理 > 已结束 > 处理中。未处理是新故障，已结束是平台自动归档且管理员可能未知悉，处理中是管理员已手动确认。
- **未处理（重点）**：必须覆盖 `alert_list.未处理` 中的全部告警，按实际故障类型分类（如端口Down、端口Up、链路故障、配置变更、CPU/内存告警、端口错误、带宽利用率告警等），不要用"日志告警类""网络设备日志告警"这种笼统分类。每条格式：IP / 主机 / 时间 / 接口 / 内容 / 负责人。字段为空写"未知"。
- **已结束**：必须覆盖 `alert_list.已结束` 中的全部告警，按实际恢复/结束类型分类。每条格式同未处理，用于帮助管理员了解本小时自动归档的告警闭环。
- **处理中**：只做数量统计和类型归类，不逐条展开，不抢占未处理和已结束版面。
- 不得省略未处理和已结束告警；不得输出"另有N条未展开"或"请登录网管平台查看完整列表"。
- 不得输出"无需处理""可以忽略""已无风险"等结论；只能使用"建议确认""建议关注""建议结合网管平台状态核对"等谨慎表述。
- 只根据输入数据总结，不得补充输入中不存在的原因、影响范围或处理结果。
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


def fallback_summary(stats: SummaryStats, max_items_per_section: int | None = None) -> str:
    window = f"{stats.window_start:%Y-%m-%d %H:%M}-{stats.window_end:%H:%M}"
    if stats.total_new == 0 and not stats.unhandled_count and not stats.processing_count:
        return (
            f"**总体情况**\n"
            f"- 窗口时间：{window}\n"
            f"- 未处理：0条\n"
            f"- 已结束：0条\n"
            f"- 处理中：0条\n\n"
            "**未处理（重点）**\n无\n\n"
            "**已结束**\n无\n\n"
            "**处理中**\n无"
        )

    unhandled = [a for a in stats.all_alerts if a.status_bucket == "unhandled"]
    processing = [a for a in stats.all_alerts if a.status_bucket == "processing"]
    ended = [a for a in stats.all_alerts if a.status_bucket == "ended"]

    def _detail_section(records, title):
        lines = [f"**{title}**"]
        if not records:
            lines.append("无")
            return lines
        groups = Counter(a.title for a in records)
        for group_title, count in groups.most_common():
            lines.append(f"{group_title or '未知告警'}：{count}条")
            for alert in [a for a in records if a.title == group_title]:
                lines.append(format_alert_detail(alert))
        return lines

    def _processing_section(records):
        lines = ["**处理中**"]
        if not records:
            lines.append("无")
            return lines
        groups = Counter(a.title or "未知告警" for a in records)
        lines.append(f"共{len(records)}条，管理员已确认，按类型归类如下：")
        for group_title, count in groups.most_common():
            lines.append(f"- {group_title}：{count}条")
        return lines

    result = [
        "**总体情况**",
        f"- 窗口时间：{window}",
        f"- 未处理：{stats.unhandled_count}条",
        f"- 已结束：{stats.ended_count}条",
        f"- 处理中：{stats.processing_count}条",
        "",
    ]
    result.extend(_detail_section(unhandled, "未处理（重点）"))
    result.append("")
    result.extend(_detail_section(ended, "已结束"))
    result.append("")
    result.extend(_processing_section(processing))

    return "\n".join(result)


def format_alert_detail(alert: AlertRecord) -> str:
    interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}") or "未知"
    person = "、".join(_collect_person(alert)) or "未知"
    hostname = alert.hostname or "未知"
    ip = alert.device_ip or "未知"
    alarm_time = alert.alarm_time_text or "未知"
    content = single_line((alert.content or alert.title or "未知").replace("<br>", " | "), 0)
    return f"- IP：{ip} / 主机：{hostname} / 时间：{alarm_time} / 接口：{interface} / 内容：{content} / 负责人：{person}"


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
    if max_chars <= 0:
        return value
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars - 3]}..."
