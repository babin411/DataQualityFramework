"""Command line: `dqf <command>`. It reads the same settings as the Lambda (environment, or .env)."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime

from dqf.settings import Settings, load_dotenv


def print_results(summary) -> None:
    from dqf.alerts import observed_text

    icon = {"PASS": "ok  ", "WARN": "warn", "FAIL": "FAIL", "ERROR": "ERR "}
    print(f"\n{summary.schedule} window {summary.window_start:%Y-%m-%d %H:%M} to {summary.window_end:%Y-%m-%d %H:%M} UTC"
          f"   run {summary.run_id}")
    print("-" * 140)
    for r in summary.results.itertuples():
        print(f"{icon[r.STATUS]}  {r.SOURCE:<16} {r.FIELD_NAME or '(table)':<24} {r.RULE_NAME or r.CHECK_TYPE:<26} "
              f"{observed_text(r.STATUS, r.METRIC, r.OBSERVED_VALUE):<32} {r.CRITERIA}")
        if r.STATUS == "ERROR":
            print(f"      {r.DETAILS}")
    print("-" * 140)
    c = summary.counts()
    if summary.data_docs:
        print(f"Great Expectations report: {summary.data_docs}")
    print(f"{c['checks']} checks: {c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed, {c['ERROR']} errors. "
          f"New failures: {c['new_failures']}, reminders: {c['reminders']}, recovered: {c['recovered']}. "
          f"Alert sent to: {', '.join(c['alert_sent_to']) or 'nobody'}")


def cmd_check_connection(args, settings):
    from dqf.snowflake import Snowflake

    started = time.time()
    info = Snowflake(settings).whoami()
    print(f"Connected to Snowflake in {time.time() - started:.1f}s")
    for key, value in info.items():
        print(f"  {key:<10} {value}")


def cmd_describe(args, settings):
    from dqf.snowflake import Snowflake

    columns, count = Snowflake(settings).describe(args.table)
    print(f"{args.table}: {count:,} rows, {len(columns)} columns")
    for name, kind in columns:
        print(f"  {name:<36} {kind}")


def cmd_validate(args, settings):
    from dqf.workbook import load_config

    config = load_config(settings.config_uri)
    print(f"Configuration OK: {len(config.sources)} source(s), {len(config.checks)} checks, "
          f"{len(config.stability)} stability checks")
    for name, s in config.sources.items():
        n = Counter(c["CHECK_TYPE"] for c in config.checks if c["SOURCE"] == name)
        st = sum(1 for r in config.stability if r["SOURCE"] == name)
        print(f"  {name:<16} {s.schedule:<7} {s.table:<40} {sum(n.values())} checks, {st} stability")


def cmd_init(args, settings):
    from dqf.snowflake import Snowflake

    Snowflake(settings).ensure_result_table()
    print(f"Results table ready: {settings.result_table}")


def cmd_run(args, settings):
    from dqf.engine import Engine

    end = datetime.strptime(args.end, "%Y-%m-%d %H:%M") if args.end else None
    summary = Engine(settings).run(args.schedule, end, send_alerts=not args.no_alerts)
    print_results(summary)


def cmd_backfill(args, settings):
    from dqf.engine import Engine

    started = time.time()
    progress = lambda run: print(f"  {run.schedule} {run.window_end:%Y-%m-%d %H:%M}: {run.counts()}", flush=True)  # noqa: E731
    end = datetime.strptime(args.end, "%Y-%m-%d %H:%M") if args.end else None
    runs = Engine(settings).backfill(args.schedule, args.periods, end=end, on_run=progress if args.verbose else None)
    bad = sum(int((r.results.STATUS != "PASS").sum()) for r in runs)
    print(f"Backfilled {len(runs)} {args.schedule} windows in {time.time() - started:.0f}s; "
          f"{bad} result(s) were not PASS. No alerts were sent.")


def cmd_test_alert(args, settings):
    from dqf import alerts

    sent = alerts.send_message(settings, "[DQ] Test alert", "A test of the data quality alert channels. Nothing is wrong.")
    print(f"Sent to: {', '.join(sent) or 'nobody'}")


def cmd_show(args, settings):
    from dqf.snowflake import Snowflake

    df = Snowflake(settings).read_results()
    if df.empty:
        sys.exit("No results yet.")
    for schedule, part in df.groupby("SCHEDULE"):
        latest = part[part.WINDOW_END == part.WINDOW_END.max()]
        print(f"{schedule}: {part.WINDOW_END.nunique()} windows, latest ends {latest.WINDOW_END.max()}: "
              f"{latest.STATUS.value_counts().to_dict()}")


def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(prog="dqf", description="Data quality checks on Snowflake")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check-connection", help="log in to Snowflake (read-only)").set_defaults(func=cmd_check_connection)
    d = sub.add_parser("describe-table", help="a table's columns and row count (read-only)")
    d.add_argument("table")
    d.set_defaults(func=cmd_describe)
    sub.add_parser("validate", help="read the configuration workbook and list any problems").set_defaults(func=cmd_validate)
    sub.add_parser("init", help="create the results table").set_defaults(func=cmd_init)
    r = sub.add_parser("run", help="run one schedule's checks for its latest window")
    r.add_argument("schedule", choices=["hourly", "daily"])
    r.add_argument("--end", help='window end, UTC, "YYYY-MM-DD HH:MM"')
    r.add_argument("--no-alerts", action="store_true")
    r.set_defaults(func=cmd_run)
    b = sub.add_parser("backfill", help="run past windows, without alerts")
    b.add_argument("schedule", choices=["hourly", "daily"])
    b.add_argument("periods", type=int, help="how many hours (hourly) or days (daily)")
    b.add_argument("--end", help='last window ends at, UTC, "YYYY-MM-DD HH:MM" (default: now)')
    b.set_defaults(func=cmd_backfill)
    sub.add_parser("test-alert", help="send a test message through the alert channels").set_defaults(func=cmd_test_alert)
    sub.add_parser("show", help="summarise the results table").set_defaults(func=cmd_show)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    try:
        args.func(args, Settings.from_env())
    except Exception as exc:
        from dqf.workbook import ConfigError

        if isinstance(exc, (ConfigError, RuntimeError)):
            sys.exit(str(exc))
        raise


if __name__ == "__main__":
    main()
