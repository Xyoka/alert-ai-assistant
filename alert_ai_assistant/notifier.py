from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import WeComConfig


@dataclass(slots=True)
class NotifyResult:
    delivered: bool
    parts: int
    dry_run: bool
    error: str = ""


class WeComSmartBotNotifier:
    def __init__(self, config: WeComConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def send(self, text: str) -> NotifyResult:
        parts = split_text(text, self.config.max_message_chars)
        if self.config.dry_run or not self.config.enabled or not self.config.webhook_url:
            self.logger.info("WeCom dry-run summary parts=%s", len(parts))
            return NotifyResult(delivered=False, parts=len(parts), dry_run=True)

        for part in parts:
            error = self._post_text(part)
            if error:
                return NotifyResult(delivered=False, parts=len(parts), dry_run=False, error=error)
        return NotifyResult(delivered=True, parts=len(parts), dry_run=False)

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
        return ""


def split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at <= 0:
            split_at = max_chars
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts

