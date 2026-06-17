from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Any
import uuid

from .agent import AlertAgent
from .config import AppConfig
from .notifier import WeComSmartBotNotifier, split_text
from .storage import AlertStore


BOT_HEARTBEAT_KEY = "wecom_intelligent_bot_heartbeat"
BOT_STATUS_KEY = "wecom_intelligent_bot_status"


class IntelligentBotUnavailable(RuntimeError):
    pass


class WeComAgentService:
    def __init__(
        self,
        config: AppConfig,
        store: AlertStore,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.logger = logger or logging.getLogger(__name__)
        self.agent = AlertAgent(config, store)
        self._stop = threading.Event()
        self._client: Any = None

    def serve_forever(self) -> None:
        if self.config.wecom_intelligent_bot.dry_run:
            self._serve_dry_run()
            return
        asyncio.run(self._serve_async())

    async def _serve_async(self) -> None:
        self.store.update_runtime_state(BOT_STATUS_KEY, "starting")
        self._client = self._build_sdk_client()
        self._register_handlers(self._client)
        outbox_task: asyncio.Task | None = None
        try:
            await self._client.connect()
            self.store.update_runtime_state(BOT_STATUS_KEY, "running")
            await self._send_recovery_notice_if_needed()
            outbox_task = asyncio.create_task(self._outbox_loop_async())
            while not self._stop.is_set():
                self.store.update_runtime_state(BOT_HEARTBEAT_KEY, "alive")
                await asyncio.sleep(max(1, self.config.agent.outbox_poll_seconds))
        finally:
            if outbox_task:
                outbox_task.cancel()
                await asyncio.gather(outbox_task, return_exceptions=True)
            if self._client:
                disconnect = getattr(self._client, "disconnect", None)
                if disconnect:
                    result = disconnect()
                    if inspect.isawaitable(result):
                        await result
            self.store.update_runtime_state(BOT_STATUS_KEY, "stopped")

    def _serve_dry_run(self) -> None:
        self.store.update_runtime_state(BOT_STATUS_KEY, "dry_run")
        self.logger.info("WeCom intelligent bot dry-run mode is enabled.")
        while not self._stop.is_set():
            self.store.update_runtime_state(BOT_HEARTBEAT_KEY, "alive")
            for message in self.store.fetch_pending_messages(limit=10):
                message_id = int(message["id"])
                summary_id = int(message["summary_id"]) if message.get("summary_id") else None
                self.logger.info("Intelligent bot dry-run send message_id=%s", message_id)
                self.store.mark_message_sent(message_id, summary_id)
            self._stop.wait(max(1, self.config.agent.outbox_poll_seconds))

    async def _outbox_loop_async(self) -> None:
        while not self._stop.is_set():
            self.store.update_runtime_state(BOT_HEARTBEAT_KEY, "alive")
            try:
                for message in self.store.fetch_pending_messages(limit=10):
                    await self._deliver_outbound_async(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive background loop.
                self.logger.exception("Outbox loop failed: %s", exc)
            await asyncio.sleep(max(1, self.config.agent.outbox_poll_seconds))

    async def _deliver_outbound_async(self, message: dict) -> None:
        message_id = int(message["id"])
        summary_id = int(message["summary_id"]) if message.get("summary_id") else None
        target_id = str(message.get("target_id") or self.config.wecom_intelligent_bot.summary_target_id)
        body = str(message.get("body") or "")
        if not target_id:
            self.store.mark_message_failed(message_id, "missing intelligent bot target id")
            return
        try:
            await self._send_text_async(target_id, body)
        except Exception as exc:
            self.logger.warning("Intelligent bot delivery failed, trying webhook fallback: %s", exc)
            if self.config.agent.fallback_webhook_enabled:
                self._send_webhook_fallback(message_id, summary_id, body, str(exc))
            else:
                self.store.mark_message_failed(message_id, str(exc))
            return
        self.store.mark_message_sent(message_id, summary_id)

    async def _send_text_async(self, chat_id: str, text: str) -> None:
        if not self._client:
            raise IntelligentBotUnavailable("intelligent bot client is not ready")
        for part in split_text(text, self.config.wecom_intelligent_bot.max_message_chars):
            body = {"msgtype": "markdown", "markdown": {"content": part}}
            result = self._client.send_message(chat_id, body)
            if inspect.isawaitable(result):
                await result

    async def _reply_text_async(self, frame: dict[str, Any], text: str) -> None:
        if not self._client:
            raise IntelligentBotUnavailable("intelligent bot client is not ready")
        for part in split_text(text, self.config.wecom_intelligent_bot.max_message_chars):
            stream_id = f"alert-agent-{uuid.uuid4().hex}"
            reply_stream = getattr(self._client, "reply_stream", None)
            if reply_stream:
                result = reply_stream(frame, stream_id, part, True)
            else:
                result = self._client.reply(frame, {"msgtype": "text", "text": {"content": part}})
            if inspect.isawaitable(result):
                await result

    def _send_webhook_fallback(
        self,
        message_id: int,
        summary_id: int | None,
        body: str,
        original_error: str,
    ) -> None:
        notifier = WeComSmartBotNotifier(self.config.wecom, self.logger)
        result = notifier.send(body)
        if result.delivered or (result.dry_run and self.config.wecom.dry_run):
            self.store.mark_message_fallback_sent(message_id, summary_id)
            return
        self.store.mark_message_failed(message_id, f"{original_error}; fallback failed: {result.error}")

    async def _handle_text_message_async(self, frame: dict[str, Any]) -> None:
        content = _frame_value(frame, "body.text.content") or _event_value(frame, "content")
        if not content:
            return
        chat_id = _event_value(frame, "chatid") or _event_value(frame, "chat_id") or _frame_value(frame, "headers.req_id")
        user_id = (
            _event_value(frame, "userid")
            or _event_value(frame, "user_id")
            or _event_value(frame, "from_user")
            or _event_value(frame, "from")
            or ""
        )
        answer = await asyncio.to_thread(
            self.agent.answer,
            str(content),
            str(chat_id or ""),
            str(user_id or ""),
        )
        await self._reply_text_async(frame, answer.text)

    async def _handle_enter_chat_async(self, frame: dict[str, Any]) -> None:
        welcome = {
            "msgtype": "text",
            "text": {
                "content": "我是网络告警 AI 助手，可以发送摘要编号、IP、主机名或告警关键字查询最近告警。"
            },
        }
        reply_welcome = getattr(self._client, "reply_welcome", None)
        if not reply_welcome:
            return
        result = reply_welcome(frame, welcome)
        if inspect.isawaitable(result):
            await result

    async def _send_recovery_notice_if_needed(self) -> None:
        if not self.config.agent.recovery_notice_enabled:
            return
        failed = self.store.fetch_failed_summary_messages(limit=50)
        if not failed:
            return
        target_id = self.config.wecom_intelligent_bot.summary_target_id
        if not target_id:
            self.logger.warning("Cannot send recovery notice because summary_target_id is empty.")
            return
        first_time = failed[0].get("created_at", "")
        last_time = failed[-1].get("created_at", "")
        latest_body = str(failed[-1].get("body") or "")
        if len(latest_body) > 1200:
            latest_body = f"{latest_body[:1200]}..."
        notice = (
            "【网络告警AI助手恢复通知】\n"
            "智能机器人服务已恢复。\n"
            f"异常期间有 {len(failed)} 条摘要未通过智能机器人和 webhook 兜底发送成功，"
            f"时间范围：{first_time} 至 {last_time}。\n"
            "为避免刷屏，本次不逐小时补发，仅发送这条聚合通知；请以网管平台状态为准。\n\n"
            "最近一条未送达摘要概览：\n"
            f"{latest_body}"
        )
        try:
            await self._send_text_async(target_id, notice)
        except Exception as exc:
            self.logger.warning("Recovery notice failed: %s", exc)
            return
        self.store.mark_messages_recovery_notified([int(item["id"]) for item in failed])

    def _build_sdk_client(self) -> Any:
        bot = self.config.wecom_intelligent_bot
        if not bot.enabled:
            raise IntelligentBotUnavailable("wecom_intelligent_bot.enabled is false")
        if not bot.bot_id or not bot.secret:
            raise IntelligentBotUnavailable("wecom_intelligent_bot.bot_id and secret are required")
        try:
            module = __import__("wecom_aibot_sdk", fromlist=["WSClient"])
        except ImportError as exc:
            raise IntelligentBotUnavailable(
                "wecom-aibot-sdk is not installed. Run: python -m pip install -e ."
            ) from exc
        client_cls = getattr(module, "WSClient", None)
        if client_cls is None:
            client_module = __import__("wecom_aibot_sdk.client", fromlist=["WSClient"])
            client_cls = getattr(client_module, "WSClient")
        return client_cls(bot.bot_id, bot.secret)

    def _register_handlers(self, client: Any) -> None:
        client.on("authenticated", lambda: self.store.update_runtime_state(BOT_STATUS_KEY, "authenticated"))
        client.on("disconnected", lambda reason: self.store.update_runtime_state(BOT_STATUS_KEY, f"disconnected:{reason}"))
        client.on("error", lambda error: self.logger.error("WeCom intelligent bot error: %s", error))
        client.on("message.text", self._handle_text_message_async)
        client.on("event.enter_chat", self._handle_enter_chat_async)


def send_pending_summary_or_fallback(
    config: AppConfig,
    store: AlertStore,
    summary_id: int,
    summary_text: str,
    logger: logging.Logger,
) -> tuple[bool, str, str]:
    target_id = config.wecom_intelligent_bot.summary_target_id
    message_id = store.enqueue_message(
        "summary",
        summary_text,
        target_id=target_id,
        summary_id=summary_id,
        channel="intelligent_bot",
    )
    recent = store.is_runtime_state_recent(
        BOT_HEARTBEAT_KEY,
        max(1, config.agent.summary_send_confirm_seconds),
    )
    if recent:
        logger.info("Summary queued for intelligent bot message_id=%s", message_id)
        return False, "intelligent_bot_pending", ""

    if not config.agent.fallback_webhook_enabled:
        logger.warning("Intelligent bot heartbeat is stale and webhook fallback is disabled.")
        return False, "intelligent_bot_pending", "intelligent bot heartbeat is stale"

    notifier = WeComSmartBotNotifier(config.wecom, logger)
    result = notifier.send(summary_text)
    if result.delivered or (result.dry_run and config.wecom.dry_run):
        store.mark_message_fallback_sent(message_id, summary_id)
        return bool(result.delivered), "webhook_fallback", ""
    store.mark_message_failed(message_id, f"intelligent bot stale; fallback failed: {result.error}")
    return False, "webhook_fallback_failed", result.error


def _frame_value(frame: dict[str, Any], path: str) -> Any:
    current: Any = frame
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        if key in event:
            return event[key]
        for value in event.values():
            found = _event_value(value, key)
            if found is not None:
                return found
        return None
    if hasattr(event, key):
        return getattr(event, key)
    return None
