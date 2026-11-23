from datetime import datetime, timedelta

import pandas as pd

from dqf import alerts
from dqf.engine import RunSummary, decide
from dqf.settings import Settings

END = datetime(2026, 10, 17, 12)
COLUMNS = ["CHECK_ID", "STATUS", "ALERT_FLAG", "SOURCE", "FIELD_NAME", "CHECK_TYPE", "RULE_NAME", "METRIC",
           "OBSERVED_VALUE", "CRITERIA", "TABLE_NAME", "EXPLANATION", "INVESTIGATE_SQL", "ERROR_TEXT"]


def results(**statuses) -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, data=[{
        "CHECK_ID": n, "STATUS": s, "ALERT_FLAG": "No" if n.startswith("quiet") else "Yes", "SOURCE": "S",
        "FIELD_NAME": n, "CHECK_TYPE": "MISSING_VALUES", "RULE_NAME": "", "METRIC": "bad_pct",
        "OBSERVED_VALUE": 12.5, "CRITERIA": "at most 5% bad rows", "TABLE_NAME": "DB.S.T",
        "EXPLANATION": f"{n} is empty in 12.5% of rows; at most 5% allowed.",
        "INVESTIGATE_SQL": f"select * from DB.S.T where {n} is null limit 100;", "ERROR_TEXT": ""} for n, s in statuses.items()])


def test_new_recovered_and_warn_does_not_alert():
    now = results(a="FAIL", b="FAIL", c="PASS", d="ERROR", e="ERROR", w="WARN", quiet="FAIL")
    new, recovered, reminders = decide(now, {"a": "PASS", "b": "FAIL", "c": "FAIL", "e": "FAIL", "w": "FAIL"},
                                       {"b": END - timedelta(hours=1)}, END, 24)
    assert set(new.CHECK_ID) == {"a", "d", "e"} and set(recovered.CHECK_ID) == {"c", "w"} and reminders.empty


def test_reminders_survive_a_skipped_run():
    now, hour = results(b="FAIL"), timedelta(hours=1)
    for failing, gap, due in ((23, 1, False), (24, 1, True), (25, 1, False), (48, 1, True), (25, 2, True)):
        _, _, reminders = decide(now, {"b": "FAIL"}, {"b": END - failing * hour}, END, 24, END - gap * hour)
        assert (not reminders.empty) == due, (failing, gap)


def test_channels(monkeypatch):
    import boto3
    import smtplib

    now = results(a="FAIL")
    new, rec, rem = decide(now, {}, {}, END, 24)
    summary = RunSummary("r", "hourly", END - timedelta(hours=1), END, now, new, rec, rem)
    assert alerts.subject(summary) == "[DQ hourly] 1 new problem in S"
    text, page = alerts.body(summary), alerts.body_html(summary)
    assert "1. [FAILED] S · a · MISSING_VALUES" in text and "What is wrong: a is empty in 12.5% of rows" in text
    assert "select * from DB.S.T where a is null limit 100;" in text
    assert "New problems (1)" in page and "a is empty in 12.5% of rows" in page

    published, mailed = [], []
    monkeypatch.setattr(boto3, "client", lambda name: type("SNS", (), {"publish": lambda self, **kw: published.append(kw)})())

    class FakeSMTP:
        def __init__(self, host, port, timeout): mailed.append((host, port))
        def ehlo(self): pass
        def has_extn(self, name): return False
        def send_message(self, msg): mailed.append(msg["To"]); mailed.append(msg.get_content_type())
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("DQF_SMTP_HOST", "localhost")
    monkeypatch.setenv("DQF_SMTP_PORT", "1025")
    monkeypatch.setenv("DQF_EMAIL_TO", "a@example.com")
    sent = alerts.send(Settings(alert_channels=("sns", "email", "nope"), sns_topic_arn="arn:aws:sns:x:1:t"), summary)
    assert sent == ["sns", "email"]                      # an unknown channel is logged, not fatal
    assert published[0]["TopicArn"] == "arn:aws:sns:x:1:t"
    assert mailed == [("localhost", 1025), "a@example.com", "multipart/alternative"]   # HTML + plain text


def test_report_link():
    end = datetime(2026, 10, 17, 12)
    assert alerts.report_link("", "hourly", end) is None
    assert alerts.report_link("http://localhost:8501", "hourly", end) == \
        "http://localhost:8501?schedule=hourly&window=2026-10-17+12%3A00"
    snowsight = "https://app.snowflake.com/org/acct/#/streamlit-apps/DQF_DB.DQF.DQ_REPORT"
    assert alerts.report_link(snowsight, "daily", end) == snowsight          # the route after '#' is kept
    now = results(a="FAIL")
    new, rec, rem = decide(now, {}, {}, END, 24)
    summary = RunSummary("r", "hourly", END - timedelta(hours=1), END, now, new, rec, rem,
                         report_url=alerts.report_link("http://localhost:8501", "hourly", END))
    assert "Open the data quality report: http://localhost:8501?schedule=hourly" in alerts.body(summary)
    assert "href='http://localhost:8501?schedule=hourly&amp;window=" in alerts.body_html(summary)
