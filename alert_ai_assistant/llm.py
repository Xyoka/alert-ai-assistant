from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import LLMConfig


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.enabled and self.config.base_url and self.config.api_key and self.config.model)

    def complete(self, prompt: str) -> str | None:
        if not self.available:
            return None

        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是网络运维告警摘要助手。只根据输入事实总结，措辞谨慎，"
                        "不要说无需处理、可以忽略或已确认无风险。"
                        "输出要求：\n"
                        "1. 简短精炼：跳过详细信息，只精选少量最紧急的告警。\n"
                        "2. 分类聚合：同一IP同类告警合并描述。\n"
                        "3. 标出负责人：每条告警括号标明负责人姓名。\n"
                        "4. 用 markdown 格式（**加粗**、- 列表），不加多余评论。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self._chat_completions_url(),
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError):
            return None

        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            return None

    def _chat_completions_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"


