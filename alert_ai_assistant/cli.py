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

    return parser


def run_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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

