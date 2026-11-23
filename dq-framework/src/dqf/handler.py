"""AWS Lambda entry point. EventBridge Scheduler calls it with one of:

    {"schedule": "hourly"}        the previous full hour, for sources scheduled hourly (Underwriting input)
    {"schedule": "daily"}         the previous full day, for daily sources and the PSI / CSI checks

For a manual test the event may also carry:

    "window_end": "2026-10-17 10:00"   check the window ending then instead of now (UTC)
    "send_alerts": false               store results but send no alert
    "action": "test_alert"             send a test message through the alert channels, and nothing else
    "action": "check"                  after a deployment: log in to Snowflake and read the configuration.
                                       Runs no checks, writes nothing, sends nothing

Settings come from environment variables; the Snowflake login from Secrets Manager.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

# Only /tmp is writable in Lambda, and some libraries write a cache under HOME.
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("GX_ANALYTICS_ENABLED", "false")

logging.getLogger().setLevel(logging.WARNING)          # GX is chatty at INFO
logging.getLogger("dqf").setLevel(logging.INFO)        # one JSON line per run
_engine = None                                         # kept while the container is warm


def handler(event, context):
    global _engine
    event = event or {}
    from dqf.settings import Settings

    if event.get("action") == "test_alert":
        from dqf import alerts

        settings = Settings.from_env()
        sent = alerts.send_message(settings, "[DQ] Test alert",
                                   "This is a test of the data quality alert channels. Nothing is wrong.")
        return {"action": "test_alert", "sent_to": sent}

    if event.get("action") == "check":
        return check(Settings.from_env())

    if _engine is None:
        # Imported here: Great Expectations takes seconds to import, and Lambda allows only 10 seconds
        # for the start-up phase.
        from dqf.engine import Engine

        _engine = Engine(Settings.from_env())
    schedule = event.get("schedule", "hourly")
    if schedule not in ("hourly", "daily"):
        raise ValueError(f"schedule must be hourly or daily, not {schedule!r}")
    end = datetime.strptime(event["window_end"], "%Y-%m-%d %H:%M") if event.get("window_end") else None
    return _engine.run(schedule, end, send_alerts=_flag(event.get("send_alerts", True))).counts()


def check(settings) -> dict:
    """Is everything a run needs reachable? Each part is reported separately, so one failure does not
    hide the others: the Snowflake login (secret, network, key), and the configuration (S3, validation)."""
    from dqf.snowflake import Snowflake
    from dqf.workbook import load_config

    out = {"action": "check"}
    try:
        out["snowflake"] = Snowflake(settings).whoami()
    except Exception as exc:
        out["snowflake"] = f"FAILED: {type(exc).__name__}: {exc}"[:500]
    try:
        config = load_config(settings.config_uri)
        out["config"] = {"location": settings.config_uri, "sources": len(config.sources),
                           "checks": len(config.checks), "stability": len(config.stability)}
    except Exception as exc:
        out["config"] = f"FAILED: {type(exc).__name__}: {exc}"[:500]
    out["ok"] = not any(isinstance(out[k], str) for k in ("snowflake", "config"))
    return out


def _flag(value) -> bool:
    """A test event may type the flag as text: "false" must mean false."""
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)
