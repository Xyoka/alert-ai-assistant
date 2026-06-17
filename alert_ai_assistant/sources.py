from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
import ssl
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AppConfig, MonitorApiConfig
from .models import AlertRecord, STATUS_ENDED, STATUS_PROCESSING, STATUS_UNHANDLED, json_dumps


_logger = logging.getLogger(__name__)


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
        # Unhandled/processing alarms intentionally use only the target hour.
        records.extend(self.fetch_bucket(STATUS_UNHANDLED, window_start, window_end))
        records.extend(self._fetch_processing_window(STATUS_PROCESSING, window_start, window_end))
        # Ended: broader create_time range, filter by recovery/update time within the target hour.
        ended_payloads = self._request_bucket(STATUS_ENDED, active_start, now)
        for payload in ended_payloads:
            end_time = self._payload_end_time(payload)
            if end_time and window_start <= end_time <= window_end:
                records.append(self._payload_to_record(payload, STATUS_ENDED))
        return records

    def _fetch_processing_window(self, bucket: str, start: datetime, end: datetime) -> list[AlertRecord]:
        """Fetch processing alarms created in the current summary window.

        Uses create_time to avoid pulling ancient records whose
        last_alarm_time may be NULL/empty in the API response.
        """
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
        payloads = self._request_with_units(bucket, units)
        return [self._payload_to_record(p, bucket) for p in payloads]

    def fetch_bucket(self, bucket: str, start: datetime, end: datetime) -> list[AlertRecord]:
        payloads = self._request_bucket(bucket, start, end)
        return [self._payload_to_record(payload, bucket) for payload in payloads]

    def _request_bucket(self, bucket: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        units = self._search_units(bucket, start, end)
        return self._request_with_units(bucket, units)

    def _request_with_units(self, bucket: str, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Make API request with pre-built search units."""
        page_limit = max(1, int(self.config.page_limit))
        max_pages = max(1, int(self.config.max_pages))
        all_items: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        seen_pages: set[str] = set()

        for page_index in range(max_pages):
            offset = page_index * page_limit
            items = self._request_page(bucket, units, offset, page_limit)
            if not items:
                break
            page_fingerprint = json_dumps(items)
            if page_fingerprint in seen_pages:
                break
            seen_pages.add(page_fingerprint)

            for item in items:
                item_key = json_dumps(item)
                if item_key in seen_items:
                    continue
                seen_items.add(item_key)
                all_items.append(item)

            if len(items) < page_limit:
                break
        return all_items

    def _request_page(
        self,
        bucket: str,
        units: list[dict[str, Any]],
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        params = {
            "offset": offset,
            "limit": limit,
            "search_unit_list": json.dumps(units, ensure_ascii=False, separators=(",", ":")),
            self.config.sid_param_name or "secret": self.config.sid,
        }
        url = self._build_search_url(params)
        req = Request(url, headers={"Accept": "application/json"})
        ssl_ctx = ssl._create_unverified_context()

        # Retry on transient network errors (up to 2 retries with backoff).
        max_attempts = 3
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                with urlopen(req, timeout=self.config.timeout_seconds, context=ssl_ctx) as response:
                    body = response.read().decode("utf-8")
                break
            except OSError as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    delay = (attempt + 1) * 2.0
                    _logger.warning(
                        "Monitor API network error (bucket=%s offset=%s attempt=%s/%s): %s, retrying in %.1fs",
                        bucket, offset, attempt + 1, max_attempts, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    _logger.warning(
                        "Monitor API request failed after %s attempts (bucket=%s offset=%s): %s",
                        max_attempts, bucket, offset, exc,
                    )
                    return []
        else:
            _logger.warning(
                "Monitor API request exhausted retries (bucket=%s offset=%s): %s",
                bucket, offset, last_error,
            )
            return []

        data = json.loads(body)
        # Detect API-level errors (e.g. SID expiration) and surface them.
        if isinstance(data, dict):
            code = data.get("code")
            if code is not None and code != 0:
                msg = data.get("message", "")
                _logger.warning("Monitor API returned error code=%s message=%s (bucket=%s offset=%s)", code, msg, bucket, offset)
                return []
        return list(_extract_items(data))

    def _build_search_url(self, params: dict[str, Any]) -> str:
        base = self.config.base_url.rstrip("/")
        path = self.config.search_path if self.config.search_path.startswith("/") else f"/{self.config.search_path}"
        return f"{base}{path}?{urlencode(params)}"

    def _search_units(self, bucket: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        units = [dict(item) for item in self.config.bucket_search_units.get(bucket, [])]
        if not units:
            units = [
                {"attr": "is_ignore", "search": [0], "operator": "="},
                {"attr": "instance_name", "search": [self.config.owner_instance_name], "operator": "="},
            ]
        # Unhandled: last_alarm_time (catch recurring). Ended: create_time (broad lookback).
        time_attr = "last_alarm_time" if bucket != STATUS_ENDED else "create_time"
        units.append({
            "attr": time_attr,
            "search": [start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")],
            "operator": "=",
        })
        return units

    def _payload_to_record(self, payload: dict[str, Any], bucket: str) -> AlertRecord:
        mapping = self.config.field_mapping
        # Ended uses recovery/end time; active alarms prefer last_alarm_time
        # but fall back to configured alarm_time/create_time because some platform
        # payloads do not include last_alarm_time.
        if bucket == STATUS_ENDED:
            time_candidates = self._ended_time_candidates()
        elif bucket == STATUS_UNHANDLED:
            time_candidates = ("last_alarm_time", mapping.get("alarm_time", ""), "create_time")
        else:
            time_candidates = (mapping.get("alarm_time", ""), "create_time", "last_alarm_time")
        alarm_time_text = str(_first_payload_value(payload, time_candidates) or "")
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

    def _payload_end_time(self, payload: dict[str, Any]) -> datetime | None:
        value = _first_payload_value(payload, self._ended_time_candidates())
        return parse_datetime(str(value or ""))

    def _ended_time_candidates(self) -> tuple[str, ...]:
        mapping = self.config.field_mapping
        return (
            "update_time",
            "recover_time",
            "recovery_time",
            "end_time",
            "finish_time",
            mapping.get("alarm_time", ""),
            "create_time",
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
        return [item for item in data if isinstance(item, dict) and item]
    if not isinstance(data, dict):
        return []
    for key in ("data", "items", "list", "rows", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict) and item]
        if isinstance(value, dict):
            return list(_extract_items(value))
    # 过滤掉空对象 {} — API 某些无数据窗口返回占位空对象
    if not data:
        return []
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


def _first_payload_value(payload: dict[str, Any], paths: Iterable[str]) -> Any:
    for path in paths:
        value = _get_path(payload, path)
        if value not in (None, ""):
            return value
    return None
