from datetime import datetime

import pandas as pd

from dqf import handler
from dqf.engine import RunSummary


def test_scheduled_and_manual_events(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings): pass

        def run(self, schedule, end, send_alerts):
            calls.append((schedule, end, send_alerts))
            empty = pd.DataFrame(columns=["STATUS"])
            return RunSummary("r", schedule, datetime(2026, 10, 17, 9), datetime(2026, 10, 17, 10), empty, empty, empty, empty)

    import dqf.engine
    monkeypatch.setattr(dqf.engine, "Engine", FakeEngine)
    monkeypatch.setattr(handler, "_engine", None)
    handler.handler({"schedule": "daily"}, None)
    handler.handler({"schedule": "hourly", "window_end": "2026-10-17 10:00", "send_alerts": "false"}, None)
    assert calls == [("daily", None, True), ("hourly", datetime(2026, 10, 17, 10), False)]


def test_test_alert_action(monkeypatch):
    from dqf import alerts

    monkeypatch.setattr(alerts, "send_message", lambda settings, subject, text, html_text=None: ["log"])
    assert handler.handler({"action": "test_alert"}, None) == {"action": "test_alert", "sent_to": ["log"]}


def test_check_action_reports_each_part(monkeypatch):
    from dqf import snowflake

    monkeypatch.setattr(snowflake.Snowflake, "whoami", lambda self: (_ for _ in ()).throw(TimeoutError("no route")))
    monkeypatch.setenv("DQF_CONFIG_URI", "config/dq_config.yaml")
    monkeypatch.delenv("DQF_SNOWFLAKE_SECRET_ID", raising=False)
    out = handler.handler({"action": "check"}, None)
    assert out["snowflake"].startswith("FAILED: TimeoutError")
    assert out["config"]["sources"] == 3 and out["ok"] is False
