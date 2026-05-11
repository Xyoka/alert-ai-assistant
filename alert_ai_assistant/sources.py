from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AppConfig, MonitorApiConfig
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, json_dumps


BLOCK_RE = re.compile(r"(?=^网络运维管理平台\s+\d+/\d+\s+\d+:\d+:\d+\s*$)", re.MULTILINE)


class MockTextSource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch_for_summary(self, window_start: datetime, window_end: datetime) -> list[AlertRecord]:
        return self.parse_text(self.path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def parse_text(text: str) -> list[AlertRecord]:
        blocks = [block.strip() for block in BLOCK_RE.split(text) if block.strip()]
        return [record for record in (MockTextSource._parse_block(block) for block in blocks) if record]

    @staticmethod
    def _parse_block(block: str) -> AlertRecord | None:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            return None

        body = "\n".join(lines[1:])
        is_recovery = "[Recovery]" in body or "日志告警恢复" in body
        is_alarm = "[Alarm]" in body or re.search(r"\n日志告警\n", f"\n{body}\n") is not None
        if not is_alarm and not is_recovery:
            return None

        source_type = "log" if "日志告警" in body else "snmp"
        status_bucket = STATUS_ENDED if is_recovery else STATUS_UNHANDLED
        hostname = _first_match(block, r"主机名称[：:]\s*(.+)") or _first_match(block, r"主机名[：:]\s*(.+)")
        device_ip = _first_match(block, r"IP地址[：:]\s*(.+)")
        alarm_time_text = _first_match(block, r"告警时间[：:]\s*(.+)") or _first_match(block, r"恢复时间[：:]\s*(.+)")
        alarm_time = parse_datetime(alarm_time_text)
        severity = _first_match(block, r"告警级别[：:]\s*(.+)")
        title = lines[1] if len(lines) > 1 else ""
        content = _extract_content(block, lines)
        external_id = _first_match(block, r"alarmID=([^;\s\)]+)") or _first_match(block, r"CID=([^;\s\)]+)")
        return AlertRecord(
            source_type=source_type,
            status_bucket=status_bucket,
            device_ip=device_ip,
            hostname=hostname,
            alarm_time=alarm_time,
            title=title,
            content=content,
            severity=severity,
            external_id=external_id,
            raw_payload=block,
        )


class MonitorApiSource:
    def __init__(self, config: MonitorApiConfig) -> None:
        self.config = config

    def fetch_for_summary(self, window_start: datetime, window_end: datetime) -> list[AlertRecord]:
        now = datetime.now()
        active_start = now - timedelta(days=self.config.active_lookback_days)
        records: list[AlertRecord] = []
        records.extend(self.fetch_bucket(STATUS_UNHANDLED, active_start, now))
        records.extend(self.fetch_bucket(STATUS_PROCESSING, active_start, now))
        records.extend(self.fetch_bucket(STATUS_ENDED, window_start, window_end))
        return records

    def fetch_bucket(self, bucket: str, start: datetime, end: datetime) -> list[AlertRecord]:
        payloads = self._request_bucket(bucket, start, end)
        return [self._payload_to_record(payload, bucket) for payload in payloads]

    def _request_bucket(self, bucket: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        search_units = self._search_units(bucket, start, end)
        params = {
            "offset": 0,
            "limit": 1000,
            "search_unit_list": json.dumps(search_units, ensure_ascii=False, separators=(",", ":")),
            "token": self.config.sid,
        }
        base = self.config.base_url.rstrip("/")
        path = self.config.search_path if self.config.search_path.startswith("/") else f"/{self.config.search_path}"
        url = f"{base}{path}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        return list(_extract_items(data))

    def _search_units(self, bucket: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        units = [dict(item) for item in self.config.bucket_search_units.get(bucket, [])]
        if not units:
            units = [
                {"attr": "is_ignore", "search": [0], "operator": "="},
                {"attr": "instance_name", "search": [self.config.owner_instance_name], "operator": "="},
            ]
        units.append({
            "attr": "create_time",
            "search": [start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")],
            "operator": "=",
        })
        return units

    def _payload_to_record(self, payload: dict[str, Any], bucket: str) -> AlertRecord:
        mapping = self.config.field_mapping
        alarm_time_text = str(_get_path(payload, mapping.get("alarm_time", "")) or "")
        title = str(_get_path(payload, mapping.get("title", "")) or "")
        content = str(_get_path(payload, mapping.get("content", "")) or "")
        if not content:
            content = title or json_dumps(payload)
        return AlertRecord(
            source_type=str(_get_path(payload, mapping.get("source_type", "")) or "unknown"),
            status_bucket=bucket,
            device_ip=str(_get_path(payload, mapping.get("device_ip", "")) or ""),
            hostname=str(_get_path(payload, mapping.get("hostname", "")) or ""),
            alarm_time=parse_datetime(alarm_time_text),
            title=title,
            content=content,
            severity=str(_get_path(payload, mapping.get("severity", "")) or ""),
            external_id=str(_get_path(payload, mapping.get("external_id", "")) or ""),
            raw_payload=json_dumps(payload),
        )


def build_source(config: AppConfig) -> MockTextSource | MonitorApiSource:
    if config.source.kind == "monitor_api":
        return MonitorApiSource(config.monitor_api)
    if not config.source.mock_text_path:
        raise ValueError("source.mock_text_path is required when source.kind is mock_text.")
    return MockTextSource(config.source.mock_text_path)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_content(block: str, lines: list[str]) -> str:
    for pattern in (
        r"当前采集值[：:]\s*([^\n\r]+)",
        r"恢复内容[：:]\s*([^\n\r]+)",
        r"(?:^|\n)(<\d+>[^\n\r]+)",
        r"(?:^|\n)(<\d+>:[^\n\r]+)",
    ):
        value = _first_match(block, pattern)
        if value:
            return value
    return lines[2] if len(lines) > 2 else (lines[1] if len(lines) > 1 else "")


def _extract_items(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "items", "list", "rows", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_items(value)
            nested_list = list(nested)
            if nested_list:
                return nested_list
    return [data]


def _get_path(payload: dict[str, Any], path: str) -> Any:
    if not path:
        return None
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current

