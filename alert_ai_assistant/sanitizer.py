from __future__ import annotations

import re
from collections.abc import Iterable

from .config import AppConfig


RESPONSIBLE_LINE_RE = re.compile(r"负责人[：:][^\n\r]*")
ACCOUNT_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}\([A-Za-z0-9_.-]+\)")


def sanitize_text(text: str, mask_names: Iterable[str] | None = None) -> str:
    result = text or ""
    result = RESPONSIBLE_LINE_RE.sub("负责人：<已脱敏>", result)
    result = ACCOUNT_NAME_RE.sub("<负责人>", result)
    for name in mask_names or []:
        name = str(name).strip()
        if name:
            result = result.replace(name, "<已脱敏>")
    return result


def config_mask_names(config: AppConfig) -> list[str]:
    names = list(config.mask_names)
    owner = getattr(config.monitor_api, "owner_instance_name", "")
    if owner:
        names.append(owner)
    result: list[str] = []
    for name in names:
        value = str(name).strip()
        if value and value not in result:
            result.append(value)
    return result


def sanitize_for_config(text: str, config: AppConfig) -> str:
    return sanitize_text(text, config_mask_names(config))
