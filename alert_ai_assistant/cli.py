from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import sys

from .config import AppConfig, load_config
from .lock import FileLock, LockError
from .notifier import WeComSmartBotNotifier
from .sources import MockTextSource, build_source
from .storage import AlertStore
from .summarizer import build_stats, generate_summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "summarize-sample":
        return summarize_sample(args)
    if args.command == "run-once":
        return run_once(args)
    if args.command == "cleanup":
        return cleanup(args)
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

    check_parser = subparsers.add_parser("check-config", help="Validate config.yaml before rollout.")
    check_parser.add_argument("--config", required=True, help="Path to config.yaml.")

    status_parser = subparsers.add_parser("status", help="Show latest local summary delivery status.")
    status_parser.add_argument("--config", required=True, help="Path to config.yaml.")

    return parser


def run_once(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"failed to load config: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        config.wecom.dry_run = True
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
            notifier = WeComSmartBotNotifier(config.wecom, logger)
            notify_result = notifier.send(summary_text)
            store.save_summary(
                window_start,
                window_end,
                summary_text,
                stats.to_prompt_payload(),
                ai_used,
                notify_result.delivered,
            )
            logger.info(
                "Summary saved. ai_used=%s delivered=%s dry_run=%s parts=%s delivered_parts=%s error=%s",
                ai_used,
                notify_result.delivered,
                notify_result.dry_run,
                notify_result.parts,
                notify_result.delivered_parts,
                notify_result.error,
            )
            print(summary_text)
            if not notify_result.delivered and not notify_result.dry_run:
                print(f"WeCom notification failed: {notify_result.error}", file=sys.stderr)
                return 4
            return 0
    except LockError as exc:
        logger.warning("%s", exc)
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        logger.exception("run-once failed")
        _notify_runtime_failure(config, logger, exc)
        print(f"run-once failed: {exc}", file=sys.stderr)
        return 1


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
    print(f"parsed_records={len(records)} ai_used={ai_used}")
    print(summary_text)
    return 0


def cleanup(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"failed to load config: {exc}", file=sys.stderr)
        return 1
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


def check_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"failed to load config: {exc}", file=sys.stderr)
        return 1
    issues: list[str] = []

    if config.source.kind == "mock_text":
        if not config.source.mock_text_path:
            issues.append("source.mock_text_path is required when source.kind=mock_text")
    elif config.source.kind == "monitor_api":
        if not config.monitor_api.base_url:
            issues.append("monitor_api.base_url is required")
        if not config.monitor_api.sid:
            issues.append("monitor_api.sid is required or set ALERT_AI_MONITOR_SID")
        if not config.monitor_api.sid_param_name:
            issues.append("monitor_api.sid_param_name must not be empty")
        if not config.monitor_api.owner_instance_name:
            issues.append("monitor_api.owner_instance_name is required")
        if config.monitor_api.page_limit <= 0:
            issues.append("monitor_api.page_limit must be > 0")
        if config.monitor_api.max_pages <= 0:
            issues.append("monitor_api.max_pages must be > 0")
    else:
        issues.append("source.kind must be mock_text or monitor_api")

    if config.llm.enabled:
        if not config.llm.base_url:
            issues.append("llm.base_url is required when llm.enabled=true")
        if not config.llm.api_key:
            issues.append("llm.api_key is required when llm.enabled=true or set ALERT_AI_LLM_API_KEY")
        if not config.llm.model:
            issues.append("llm.model is required when llm.enabled=true")

    if not config.wecom.dry_run:
        if not config.wecom.enabled:
            issues.append("wecom.enabled must be true when wecom.dry_run=false")
        if not config.wecom.webhook_url:
            issues.append("wecom.webhook_url is required when wecom.dry_run=false")
    if config.wecom.max_message_bytes <= 0:
        issues.append("wecom.max_message_bytes must be > 0")
    if config.wecom.max_retries < 0:
        issues.append("wecom.max_retries must be >= 0")
    print(f"source.kind={config.source.kind}")
    print(f"database_path={config.database_path}")
    print(f"wecom.dry_run={config.wecom.dry_run}")
    if issues:
        print("\n配置检查发现问题：")
        for issue in issues:
            print(f"- {issue}")
        return 2
    print("\n配置检查通过。")
    return 0


def status(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"failed to load config: {exc}", file=sys.stderr)
        return 1
    store = AlertStore(config.database_path)
    store.init_schema()
    latest = store.latest_summary()
    print(f"database_path={config.database_path}")
    if not latest:
        print("latest_summary=none")
        return 0
    print(
        "latest_summary="
        f"id={latest['id']} window={latest['window_start']}~{latest['window_end']} "
        f"delivered={bool(latest['delivered'])} ai_used={bool(latest['ai_used'])} "
        f"created_at={latest['created_at']}"
    )
    return 0


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


def _notify_runtime_failure(config: AppConfig, logger: logging.Logger, exc: Exception) -> None:
    if config.wecom.dry_run or not config.wecom.enabled or not config.wecom.webhook_url:
        return
    text = (
        "**网络告警AI摘要助手运行失败**\n"
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
        "- 影响：本周期摘要可能未生成，请立即以网管平台和原始企业微信告警为准。\n"
        f"- 错误：{type(exc).__name__}: {str(exc)[:300]}"
    )
    try:
        result = WeComSmartBotNotifier(config.wecom, logger).send(text)
    except Exception as notify_exc:  # pragma: no cover - defensive failure path.
        logger.warning("Failed to notify runtime failure: %s", notify_exc)
        return
    if not result.delivered:
        logger.warning("Runtime failure notification was not delivered: %s", result.error)
