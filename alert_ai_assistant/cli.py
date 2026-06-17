from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import importlib.util
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import sys

from .agent import AlertAgent
from .config import AppConfig, load_config
from .intelligent_bot import BOT_HEARTBEAT_KEY, BOT_STATUS_KEY, WeComAgentService, send_pending_summary_or_fallback
from .lock import FileLock, LockError
from .notifier import WeComSmartBotNotifier
from .sanitizer import sanitize_for_config
from .sources import MockTextSource, build_source
from .storage import AlertStore
from .summarizer import append_reference_section, build_reference_alerts, build_stats, generate_summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "summarize-sample":
        return summarize_sample(args)
    if args.command == "run-once":
        return run_once(args)
    if args.command == "cleanup":
        return cleanup(args)
    if args.command == "serve-wecom-bot":
        return serve_wecom_bot(args)
    if args.command == "ask":
        return ask(args)
    if args.command == "check-config":
        return check_config(args)
    if args.command == "status":
        return status(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alert-ai-assistant")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run-once", help="Fetch alarms, summarize, and notify once.")
    run_parser.add_argument("--config", required=True, help="Path to config.yaml.")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not deliver WeCom messages.")

    sample_parser = subparsers.add_parser("summarize-sample", help="Summarize a copied WeCom alarm text file.")
    sample_parser.add_argument("--input", required=True, help="Copied WeCom text file.")
    sample_parser.add_argument("--config", help="Optional config.yaml.")

    cleanup_parser = subparsers.add_parser("cleanup", help="Delete expired local records.")
    cleanup_parser.add_argument("--config", required=True, help="Path to config.yaml.")

    bot_parser = subparsers.add_parser("serve-wecom-bot", help="Run the WeCom intelligent bot Agent service.")
    bot_parser.add_argument("--config", required=True, help="Path to config.yaml.")
    bot_parser.add_argument("--dry-run", action="store_true", help="Run without sending messages to WeCom.")

    ask_parser = subparsers.add_parser("ask", help="Ask the local alert Agent from the command line.")
    ask_parser.add_argument("--config", required=True, help="Path to config.yaml.")
    ask_parser.add_argument("--question", required=True, help="Question to ask.")
    ask_parser.add_argument("--chat-id", default="cli", help="Conversation id for contextual follow-up.")
    ask_parser.add_argument("--user-id", default="cli", help="User id for contextual follow-up.")

    check_parser = subparsers.add_parser("check-config", help="Validate local configuration for deployment.")
    check_parser.add_argument("--config", required=True, help="Path to config.yaml.")

    status_parser = subparsers.add_parser("status", help="Show local runtime and delivery status.")
    status_parser.add_argument("--config", required=True, help="Path to config.yaml.")

    return parser


def run_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.dry_run:
        config.wecom.dry_run = True
        config.wecom_intelligent_bot.dry_run = True
    setup_logging(config)
    logger = logging.getLogger(__name__)

    try:
        with FileLock(config.lock_file):
            store = AlertStore(config.database_path)
            store.init_schema()
            window_start, window_end = previous_hour_window()
            source = build_source(config)
            records = source.fetch_for_summary(window_start, window_end)
            inserted = store.insert_alerts(records)
            logger.info("Fetched %s records, inserted %s new records.", len(records), inserted)

            stats = build_stats(
                records,
                window_start,
                window_end,
                config.low_priority_keywords,
                config.llm.max_focus_alerts,
            )
            summary_text, ai_used = generate_summary(stats, config)
            reference_alerts = []
            if config.app_mode == "agent":
                reference_alerts = build_reference_alerts(stats, config.agent.summary_reference_limit)
                if config.agent.append_reference_section:
                    summary_text = append_reference_section(summary_text, reference_alerts)
            summary_text = sanitize_for_config(summary_text, config)

            if config.app_mode == "agent":
                summary_id = store.save_summary(
                    window_start,
                    window_end,
                    summary_text,
                    stats.to_prompt_payload(),
                    ai_used,
                    False,
                    delivery_channel="intelligent_bot_pending",
                )
                store.save_summary_items(summary_id, reference_alerts)
                if args.dry_run:
                    notify_result = _DryRunResult(delivered=False, dry_run=True, error="")
                    delivery_channel = "dry_run"
                    store.mark_summary_delivered(summary_id, False, delivery_channel, "")
                    logger.info("Agent dry-run summary generated, not queued.")
                else:
                    delivered, delivery_channel, error = send_pending_summary_or_fallback(
                        config,
                        store,
                        summary_id,
                        summary_text,
                        logger,
                    )
                    notify_result = _DryRunResult(delivered=delivered, dry_run=False, error=error)
                    store.mark_summary_delivered(summary_id, delivered, delivery_channel, error)
            else:
                notifier = WeComSmartBotNotifier(config.wecom, logger)
                notify_result = notifier.send(summary_text)
                store.save_summary(
                    window_start,
                    window_end,
                    summary_text,
                    stats.to_prompt_payload(),
                    ai_used,
                    notify_result.delivered,
                    delivery_channel="webhook",
                    delivery_error=notify_result.error,
                )
            logger.info(
                "Summary saved. ai_used=%s delivered=%s dry_run=%s error=%s",
                ai_used,
                notify_result.delivered,
                notify_result.dry_run,
                notify_result.error,
            )
            print(summary_text)
            return 0
    except LockError as exc:
        logger.warning("%s", exc)
        print(str(exc), file=sys.stderr)
        return 3


def summarize_sample(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else AppConfig()
    setup_logging(config, console_only=True)
    source = MockTextSource(args.input)
    records = source.fetch_for_summary(*previous_hour_window())
    if records:
        times = [record.alarm_time for record in records if record.alarm_time]
        window_start = min(times) if times else previous_hour_window()[0]
        window_end = max(times) if times else previous_hour_window()[1]
    else:
        window_start, window_end = previous_hour_window()
    stats = build_stats(
        records,
        window_start,
        window_end,
        config.low_priority_keywords,
        config.llm.max_focus_alerts,
    )
    summary_text, ai_used = generate_summary(stats, config)
    summary_text = sanitize_for_config(summary_text, config)
    print(f"parsed_records={len(records)} ai_used={ai_used}")
    print(summary_text)
    return 0


def cleanup(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config)
    store = AlertStore(config.database_path)
    store.init_schema()
    alert_deleted, summary_deleted = store.cleanup(
        config.retention.raw_alert_days,
        config.retention.summary_days,
    )
    logging.getLogger(__name__).info(
        "Cleanup complete alert_deleted=%s summary_deleted=%s",
        alert_deleted,
        summary_deleted,
    )
    print(f"alert_deleted={alert_deleted} summary_deleted={summary_deleted}")
    return 0


def serve_wecom_bot(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.dry_run:
        config.wecom_intelligent_bot.dry_run = True
        config.wecom.dry_run = True
    setup_logging(config)
    logger = logging.getLogger(__name__)
    store = AlertStore(config.database_path)
    store.init_schema()
    service = WeComAgentService(config, store, logger)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        logger.info("WeCom bot service stopped by user.")
        return 0
    return 0


def ask(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config, console_only=True)
    store = AlertStore(config.database_path)
    store.init_schema()
    answer = AlertAgent(config, store).answer(args.question, args.chat_id, args.user_id)
    print(answer.text)
    return 0


def check_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    issues: list[str] = []
    lines = [
        f"app_mode={config.app_mode}",
        f"source.kind={config.source.kind}",
        f"database_path={config.database_path}",
    ]

    if config.app_mode not in {"summary_only", "agent"}:
        issues.append("app_mode must be summary_only or agent")

    if config.source.kind == "monitor_api":
        if not config.monitor_api.base_url:
            issues.append("monitor_api.base_url is required")
        if not config.monitor_api.sid:
            issues.append("monitor_api.sid is required or set ALERT_AI_MONITOR_SID")
        if not config.monitor_api.owner_instance_name:
            issues.append("monitor_api.owner_instance_name is recommended")
    elif config.source.kind == "mock_text":
        if not config.source.mock_text_path:
            issues.append("source.mock_text_path is required when source.kind=mock_text")
    else:
        issues.append("source.kind must be monitor_api or mock_text")

    if config.app_mode == "summary_only":
        if not config.wecom.dry_run and (not config.wecom.enabled or not config.wecom.webhook_url):
            issues.append("summary_only mode requires wecom.enabled=true and wecom.webhook_url, unless dry_run=true")
    elif config.app_mode == "agent":
        sdk_available = importlib.util.find_spec("wecom_aibot_sdk") is not None
        lines.append(f"wecom_aibot_sdk_installed={sdk_available}")
        bot_dry_run = config.wecom_intelligent_bot.dry_run
        if not sdk_available and not bot_dry_run:
            issues.append("wecom-aibot-sdk is not installed; run python -m pip install -e .[test]")
        if not config.wecom_intelligent_bot.enabled:
            issues.append("agent mode requires wecom_intelligent_bot.enabled=true")
        if not config.wecom_intelligent_bot.bot_id and not bot_dry_run:
            issues.append("wecom_intelligent_bot.bot_id is required or set ALERT_AI_WECOM_BOT_ID")
        if not config.wecom_intelligent_bot.secret and not bot_dry_run:
            issues.append("wecom_intelligent_bot.secret is required or set ALERT_AI_WECOM_BOT_SECRET")
        if not config.wecom_intelligent_bot.summary_target_id:
            issues.append("wecom_intelligent_bot.summary_target_id is required for hourly summary push")
        if config.agent.fallback_webhook_enabled and not config.wecom.dry_run:
            if not config.wecom.enabled or not config.wecom.webhook_url:
                issues.append("webhook fallback is enabled, so wecom.enabled=true and wecom.webhook_url are required")

    for line in lines:
        print(line)
    if issues:
        print("\n配置检查发现问题：")
        for issue in issues:
            print(f"- {issue}")
        return 2
    print("\n配置检查通过。")
    return 0


def status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = AlertStore(config.database_path)
    store.init_schema()

    print(f"app_mode={config.app_mode}")
    print(f"database_path={config.database_path}")
    latest = store.latest_summary()
    if latest:
        print(
            "latest_summary="
            f"id={latest['id']} window={latest['window_start']}~{latest['window_end']} "
            f"delivered={bool(latest['delivered'])} channel={latest['delivery_channel'] or '-'} "
            f"created_at={latest['created_at']}"
        )
    else:
        print("latest_summary=none")

    counts = store.outbound_status_counts()
    if counts:
        count_text = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
        print(f"outbound_messages={count_text}")
    else:
        print("outbound_messages=none")

    bot_status = store.get_runtime_state(BOT_STATUS_KEY)
    heartbeat = store.get_runtime_state(BOT_HEARTBEAT_KEY)
    if bot_status:
        print(f"bot_status={bot_status['value']} updated_at={bot_status['updated_at']}")
    else:
        print("bot_status=none")
    if heartbeat:
        print(f"bot_heartbeat={heartbeat['value']} updated_at={heartbeat['updated_at']}")
    else:
        print("bot_heartbeat=none")
    return 0


class _DryRunResult:
    def __init__(self, delivered: bool, dry_run: bool, error: str) -> None:
        self.delivered = delivered
        self.dry_run = dry_run
        self.error = error


def previous_hour_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now()
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    return start, end - timedelta(seconds=1)


def setup_logging(config: AppConfig, console_only: bool = False) -> None:
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not console_only:
        log_path = Path(config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            TimedRotatingFileHandler(
                log_path,
                when="D",
                interval=1,
                backupCount=config.retention.log_days,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
