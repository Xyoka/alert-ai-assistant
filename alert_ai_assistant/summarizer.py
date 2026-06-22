from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re
from typing import Iterable

from .config import AppConfig
from .llm import OpenAICompatibleClient
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, SummaryStats

# Bandwidth-related keywords: alerts matching any of these are aggregated as counts only.
_BW_KEYWORDS = (
    "使用率告警",
    "入流量使用率",
    "出流量使用率",
    "流量统计超过阈值",
    "流量超过阈值",
    "流量超阈值",
    "Traffic statistics exceeded the threshold",
    "带宽利用率",
    "端口带宽使用率",
)


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
    return fallback_summary(stats, config.max_summary_items_per_section), False


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
    # Validate only non-bandwidth alerts; bandwidth alerts are aggregated as counts.
    required = [
        alert
        for alert in stats.all_alerts
        if alert.status_bucket in {STATUS_UNHANDLED, STATUS_ENDED}
        and not _is_bandwidth_alert(alert)
    ]
    # Check each unique (IP, interface) pair from non-bandwidth alerts is present.
    # This is a uniqueness-based check — if the same pair appears in multiple
    # buckets it only needs to appear once, but every distinct alert key must
    # be verifiable somewhere in the output.
    seen: set[tuple[str, str]] = set()
    for alert in required:
        iface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
        key = (alert.device_ip, iface)
        if key in seen:
            continue
        seen.add(key)
        if not alert.device_ip or alert.device_ip not in text:
            return False
        if iface and iface not in text:
            return False
    return True


def build_llm_prompt(stats: SummaryStats, config: AppConfig) -> str:
    unhandled = [a for a in stats.all_alerts if a.status_bucket == "unhandled"]
    processing = [a for a in stats.all_alerts if a.status_bucket == "processing"]
    # Filter ended alerts by alarm_time to match ended_count (exclude records
    # whose alarm_time was resolved to a different field outside the window).
    ended = [
        a for a in stats.all_alerts
        if a.status_bucket == "ended"
        and a.alarm_time
        and stats.window_start <= a.alarm_time <= stats.window_end
    ]

    # Separate bandwidth alerts from detailed ones – bandwidth only shown as counts.
    bw_unhandled, det_unhandled = _split_bw(unhandled)
    bw_processing, det_processing = _split_bw(processing)
    bw_ended, det_ended = _split_bw(ended)

    def brief(alert):
        interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
        person = _collect_person(alert)
        content_detail = (alert.content or alert.title).replace("<br>", " | ")
        return {
            "ip": alert.device_ip,
            "hostname": alert.hostname,
            "time": alert.alarm_time_text,
            "title": alert.title,
            "interface": interface or "",
            "content": single_line(content_detail, 500),
            "person": person,
        }

    payload = {
        "window": f"{stats.window_start:%Y-%m-%d %H:%M} - {stats.window_end:%Y-%m-%d %H:%M}",
        "stats": {
            "total": stats.total_new,
            "unhandled": stats.unhandled_count,
            "processing": stats.processing_count,
            "ended": stats.ended_count,
        },
        "alert_detail": {
            "未处理": [brief(a) for a in det_unhandled],
            "已结束": [brief(a) for a in det_ended],
        },
        "处理中汇总": [
            brief(a) for a in det_processing
        ],
        "带宽告警统计": {
            "未处理": len(bw_unhandled),
            "已结束": len(bw_ended),
            "处理中": len(bw_processing),
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
请根据下面的结构化网络告警数据生成企业微信摘要，要求精简明确。

格式和规则：
- 严格四段输出：**总体情况** / **未处理（重点）** / **已结束** / **处理中**
- **总体情况**：逐行列出窗口时间、未处理x条、已结束x条、处理中x条。
- **未处理（重点）**：按故障类型分类（端口Down、链路故障、配置变更等），不用“日志告警”等笼统分类。
  每条格式：IP / 主机 / 主要故障内容（主机名称已含负责人，不再单独展示）。
  不展示具体告警时间（窗口时间已说明发生时段）。
  同一IP设备的多条告警应合并为一条，列出所有涉及的接口及对应故障，如"Ethernet1/17链路故障、Ethernet1/35连接状态down"。不同IP的告警不可合并。仅发生1次不展示次数，发生多次的如"发生3次"。
- **已结束**：同上格式。同一IP设备合并为一条。
- 表述精简：中文已表意清楚的不要附加英文标签（如"Link failure"），避免冗余。
- **处理中**：只输出数量和类型归类，不逐条展开。`带宽告警统计.处理中`只输出数量。
- `带宽告警统计.未处理`和`带宽告警统计.已结束`仅输出"端口带宽利用率告警：N条"，不逐条展开。
- **IP、设备名、接口、阈值等关键信息必须完整展示，不得截断、缩写或用省略号**，这些是故障定位的关键依据。
- 精简原则：告警说明保留故障类型、设备、接口等关键信息，去掉冗余修饰词。不展示"受影响网段""影响范围"等信息。
- 不得输出"无需处理""可以忽略""已无风险"；可用"建议确认""建议关注"。
- 只根据输入数据总结，不编造原因或影响范围。
- 某类无告警写"无"，段名照常输出。

数据：
{payload_json}
""".strip()


def _split_bw(alerts: list[AlertRecord]) -> tuple[list[AlertRecord], list[AlertRecord]]:
    bw, detail = [], []
    for a in alerts:
        if _is_bandwidth_alert(a):
            bw.append(a)
        else:
            detail.append(a)
    return bw, detail


def _is_bandwidth_alert(alert: AlertRecord) -> bool:
    text = f"{alert.title}\n{alert.content or ''}"
    return any(kw in text for kw in _BW_KEYWORDS)


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
        bw_records = [a for a in records if _is_bandwidth_alert(a)]
        detail_records = [a for a in records if not _is_bandwidth_alert(a)]
        if bw_records:
            lines.append(f"端口带宽利用率告警：{len(bw_records)}条")
        if not detail_records:
            return lines
        # Apply per-section limit to avoid oversized fallback messages.
        limit = max_items_per_section
        shown = 0
        groups = Counter(a.title for a in detail_records)
        for group_title, count in groups.most_common():
            if limit and shown >= limit:
                break
            lines.append(f"{group_title or '未知告警'}：{count}条")
            for alert in [a for a in detail_records if a.title == group_title]:
                if limit and shown >= limit:
                    break
                lines.append(format_alert_detail(alert))
                shown += 1
        if limit and shown < len(detail_records):
            lines.append(f"另有{len(detail_records) - shown}条未展开，请登录网管平台查看完整列表。")
        return lines

    def _processing_section(records):
        lines = ["**处理中**"]
        if not records:
            lines.append("无")
            return lines
        bw_count = sum(1 for a in records if _is_bandwidth_alert(a))
        detail_records = [a for a in records if not _is_bandwidth_alert(a)]
        if bw_count:
            lines.append(f"端口带宽利用率告警：{bw_count}条")
        groups = Counter(a.title or "未知告警" for a in detail_records)
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
    interface = extract_interface(f"{alert.title}\n{alert.content}\n{alert.raw_payload}")
    content = single_line((alert.content or alert.title or "未知").replace("<br>", " | "), 0)
    # Merge interface into fault description when relevant.
    if interface:
        fault = f"接口{interface}：{content}"
    else:
        fault = content
    hostname = alert.hostname or "未知"
    ip = alert.device_ip or "未知"
    return f"- IP：{ip} / 主机：{hostname} / 故障：{fault}"


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
