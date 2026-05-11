from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any


STATUS_UNHANDLED = "unhandled"
STATUS_PROCESSING = "processing"
STATUS_ENDED = "ended"


@dataclass(slots=True)
class AlertRecord:
    source_type: str
    status_bucket: str
    device_ip: str
    hostname: str
    alarm_time: datetime | None
    title: str
    content: str
    severity: str = ""
    external_id: str = ""
    raw_payload: str = ""
    content_hash: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = build_content_hash(
                self.external_id,
                self.device_ip,
                self.hostname,
                self.title,
                self.content,
            )

    @property
    def alarm_time_text(self) -> str:
        if not self.alarm_time:
            return ""
        return self.alarm_time.strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data = {
            "source_type": self.source_type,
            "status_bucket": self.status_bucket,
            "device_ip": self.device_ip,
            "hostname": self.hostname,
            "alarm_time": self.alarm_time_text,
            "title": self.title,
            "content": self.content,
            "severity": self.severity,
            "external_id": self.external_id,
            "content_hash": self.content_hash,
        }
        if include_raw:
            data["raw_payload"] = self.raw_payload
        return data


@dataclass(slots=True)
class SummaryStats:
    window_start: datetime
    window_end: datetime
    total_new: int
    unhandled_count: int
    processing_count: int
    ended_count: int
    low_priority_count: int
    focus_alerts: list[AlertRecord]
    all_alerts: list[AlertRecord]

    def to_prompt_payload(self, max_alerts: int = 30) -> dict[str, Any]:
        return {
            "window": f"{self.window_start:%Y-%m-%d %H:%M} - {self.window_end:%Y-%m-%d %H:%M}",
            "stats": {
                "total_new": self.total_new,
                "unhandled_count": self.unhandled_count,
                "processing_count": self.processing_count,
                "ended_count": self.ended_count,
                "low_priority_count": self.low_priority_count,
            },
            "focus_alerts": [a.to_dict() for a in self.focus_alerts[:max_alerts]],
        }


def build_content_hash(*parts: str) -> str:
    normalized = "\n".join((part or "").strip() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)

