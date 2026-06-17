from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import WeComConfig


@dataclass(slots=True)
class NotifyResult:
    delivered: bool
    parts: int
    dry_run: bool
    error: str = ""
    delivered_parts: int = 0


class WeComSmartBotNotifier:
    def __init__(self, config: WeComConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def send(self, text: str) -> NotifyResult:
        parts = add_part_headers(
            split_text(
                text,
                max_chars=self.config.max_message_chars,
                max_bytes=self.config.max_message_bytes,
                header_reserved_bytes=80,
            )
        )
        if self.config.dry_run or not self.config.enabled or not self.config.webhook_url:
            self.logger.info("WeCom dry-run summary parts=%s", len(parts))
            return NotifyResult(delivered=False, parts=len(parts), dry_run=True)

        delivered_parts = 0
        for index, part in enumerate(parts, start=1):
            error = self._post_text_with_retry(part)
            if error:
                return NotifyResult(
                    delivered=False,
                    parts=len(parts),
                    dry_run=False,
                    error=f"part {index}/{len(parts)} failed: {error}",
                    delivered_parts=delivered_parts,
                )
            delivered_parts += 1
        return NotifyResult(delivered=True, parts=len(parts), dry_run=False, delivered_parts=delivered_parts)

    def _post_text_with_retry(self, text: str) -> str:
        attempts = max(1, self.config.max_retries + 1)
        last_error = ""
        for attempt in range(1, attempts + 1):
            last_error = self._post_text(text)
            if not last_error:
                return ""
            if attempt < attempts:
                delay = max(0.0, self.config.retry_delay_seconds) * attempt
                self.logger.warning("WeCom send failed attempt=%s/%s error=%s", attempt, attempts, last_error)
                if delay:
                    time.sleep(delay)
        return last_error

    def _post_text(self, text: str) -> str:
        msg_type = self.config.msg_type
        content_key = "markdown" if msg_type == "markdown" else "text"
        payload = {
            "msgtype": msg_type,
            content_key: {
                "content": text,
            },
        }
        if self.config.target_user:
            payload["target_user"] = self.config.target_user
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        request = Request(self.config.webhook_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except (OSError, URLError) as exc:
            return str(exc)
        self.logger.info("WeCom response: %s", response_body)
        return parse_wecom_error(response_body)


def parse_wecom_error(response_body: str) -> str:
    try:
        data: Any = json.loads(response_body)
    except json.JSONDecodeError:
        return f"invalid JSON response: {response_body[:200]}"
    if not isinstance(data, dict):
        return f"unexpected response: {response_body[:200]}"
    errcode = data.get("errcode")
    if errcode in (0, "0", None):
        return ""
    errmsg = data.get("errmsg", "")
    return f"errcode={errcode} errmsg={errmsg}"


def add_part_headers(parts: list[str]) -> list[str]:
    if len(parts) <= 1:
        return parts
    total = len(parts)
    return [f"【告警摘要 {index}/{total}】\n{part}" for index, part in enumerate(parts, start=1)]


def split_text(
    text: str,
    max_chars: int,
    max_bytes: int | None = None,
    header_reserved_bytes: int = 0,
) -> list[str]:
    if not text:
        return [""]
    effective_max_bytes = None
    if max_bytes and max_bytes > 0:
        effective_max_bytes = max(1, max_bytes - max(0, header_reserved_bytes))
    if _fits(text, max_chars, effective_max_bytes):
        return [text]

    parts: list[str] = []
    remaining = text.strip()
    while remaining:
        split_at = _best_split_index(remaining, max_chars, effective_max_bytes)
        part = remaining[:split_at].strip()
        if not part:
            part = remaining[:1]
            split_at = 1
        parts.append(part)
        remaining = remaining[split_at:].strip()
    return parts


def _best_split_index(text: str, max_chars: int, max_bytes: int | None) -> int:
    hard_limit = _fit_prefix_length(text, max_chars, max_bytes)
    if hard_limit >= len(text):
        return len(text)

    newline_at = text.rfind("\n", 0, hard_limit + 1)
    if newline_at > 0 and newline_at >= hard_limit // 2:
        return newline_at
    return hard_limit


def _fit_prefix_length(text: str, max_chars: int, max_bytes: int | None) -> int:
    char_limit = len(text) if max_chars <= 0 else min(len(text), max_chars)
    if max_bytes is None:
        return max(1, char_limit)
    low, high = 1, char_limit
    best = 1
    while low <= high:
        mid = (low + high) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return max(1, best)


def _fits(text: str, max_chars: int, max_bytes: int | None) -> bool:
    char_ok = max_chars <= 0 or len(text) <= max_chars
    byte_ok = max_bytes is None or len(text.encode("utf-8")) <= max_bytes
    return char_ok and byte_ok
