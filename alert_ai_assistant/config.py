from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without optional dependency.
    yaml = None


@dataclass(slots=True)
class RetentionConfig:
    raw_alert_days: int = 5
    summary_days: int = 15
    log_days: int = 15


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 60
    max_focus_alerts: int = 10
    max_prompt_alerts_per_bucket: int = 80  # deprecated: no longer used after truncation removed


@dataclass(slots=True)
class WeComConfig:
    enabled: bool = False
    webhook_url: str = ""
    token: str = ""
    target_user: str = ""
    max_message_chars: int = 3500
    max_message_bytes: int = 3000
    max_retries: int = 2
    retry_delay_seconds: float = 1.0
    msg_type: str = "markdown"
    dry_run: bool = True


@dataclass(slots=True)
class MonitorApiConfig:
    enabled: bool = False
    base_url: str = ""
    search_path: str = "/api/monitor/alarm/search"
    sid: str = ""
    sid_param_name: str = "secret"
    owner_instance_name: str = ""
    timeout_seconds: int = 30
    active_lookback_days: int = 5
    page_limit: int = 1000
    max_pages: int = 20
    field_mapping: dict[str, str] = field(default_factory=lambda: {
        "device_ip": "ip",
        "hostname": "hostname",
        "alarm_time": "create_time",
        "title": "title",
        "content": "content",
        "severity": "severity",
        "external_id": "id",
    })
    bucket_search_units: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(slots=True)
class DataSourceConfig:
    kind: str = "mock_text"
    mock_text_path: str = ""


@dataclass(slots=True)
class AppConfig:
    database_path: str = "data/alerts.db"
    lock_file: str = "data/run.lock"
    log_file: str = "logs/alert_ai_assistant.log"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    source: DataSourceConfig = field(default_factory=DataSourceConfig)
    monitor_api: MonitorApiConfig = field(default_factory=MonitorApiConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    wecom: WeComConfig = field(default_factory=WeComConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    max_summary_items_per_section: int = 80
    low_priority_keywords: list[str] = field(default_factory=lambda: [
        "服务器接入交换机端口连接状态告警",
        "端口连接状态告警",
    ]) 


def load_config(path: str | Path | None) -> AppConfig:
    data: dict[str, Any] = {}
    if path:
        config_path = Path(path)
        if config_path.exists():
            if yaml is None:
                raise RuntimeError("PyYAML is required to load YAML config files.")
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("Config file must contain a YAML mapping.")
            data = loaded
    config = _build_config(data)
    _apply_env_overrides(config)
    return config


def _build_config(data: dict[str, Any]) -> AppConfig:
    return AppConfig(
        database_path=str(data.get("database_path", "data/alerts.db")),
        lock_file=str(data.get("lock_file", "data/run.lock")),
        log_file=str(data.get("log_file", "logs/alert_ai_assistant.log")),
        timezone=str(data.get("timezone", "Asia/Shanghai")),
        log_level=str(data.get("log_level", "INFO")),
        source=DataSourceConfig(**_section(data, "source")),
        monitor_api=MonitorApiConfig(**_section(data, "monitor_api")),
        llm=LLMConfig(**_section(data, "llm")),
        wecom=WeComConfig(**_section(data, "wecom")),
        retention=RetentionConfig(**_section(data, "retention")),
        max_summary_items_per_section=int(data.get("max_summary_items_per_section", 80)),
        low_priority_keywords=list(data.get("low_priority_keywords", [
            "服务器接入交换机端口连接状态告警",
            "端口连接状态告警",
        ])),
    )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Config section {name!r} must be a mapping.")
    return value


def _apply_env_overrides(config: AppConfig) -> None:
    config.monitor_api.sid = os.getenv("ALERT_AI_MONITOR_SID", config.monitor_api.sid)
    config.llm.api_key = os.getenv("ALERT_AI_LLM_API_KEY", config.llm.api_key)
    config.wecom.token = os.getenv("ALERT_AI_WECOM_TOKEN", config.wecom.token)
    config.wecom.webhook_url = os.getenv("ALERT_AI_WECOM_WEBHOOK_URL", config.wecom.webhook_url)
