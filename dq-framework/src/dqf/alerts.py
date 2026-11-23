"""Alerts: one message per run, through any of these channels (DQF_ALERT_CHANNELS, comma separated):

  sns    publish to the SNS topic DQF_SNS_TOPIC_ARN; its email subscribers receive it. What AWS uses
  email  send through an SMTP server (DQF_SMTP_*), as HTML with a plain-text copy. For local runs
  log    print it (CloudWatch, in Lambda)

A message goes out when a check started failing (or could not run), recovered, or has been failing for
another DQF_REMIND_HOURS. It then also lists everything else not passing, so it gives the full picture.
Each problem says what is wrong in plain words, how much, and gives a query to see the rows.
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dqf.sql import NO_ROWS

log = logging.getLogger("dqf")
TITLE = {"FAIL": "FAILED", "ERROR": "COULD NOT RUN", "WARN": "WARNING", "PASS": "PASSED"}
COLOUR = {"FAIL": "#c0392b", "ERROR": "#8e44ad", "WARN": "#d68910", "PASS": "#1e8449"}


# ---- content ----------------------------------------------------------------------------------------
def report_link(base: str, schedule: str, end) -> str | None:
    """The Streamlit report's address, opened on this run's schedule and window when the host allows it.

    A Snowsight address (Streamlit in Snowflake) keeps its route after '#', where extra parameters would
    be lost, so it is linked as it is: the report then opens on the latest run.
    """
    if not base:
        return None
    parts = urlsplit(base)
    if parts.fragment:
        return base
    query = parse_qsl(parts.query) + [("schedule", schedule), ("window", f"{end:%Y-%m-%d %H:%M}")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def observed_text(status: str, metric: str, value) -> str:
    """The measured number in a few words. Also used by the command line."""
    if status == "ERROR":
        return "could not run"
    if value is None or value != value:
        return "n/a"
    if value >= NO_ROWS:
        return "no rows at all"
    if metric == "bad_pct":
        return f"{value:.1f}% bad rows"
    if metric == "row_count":
        return f"{value:,.0f} rows"
    if metric == "hours_since_last":
        return f"{value:.1f} hours since the newest row"
    if metric == "stability_index":
        return f"index {value:.3f}"
    return "rule holds" if status in ("PASS", "WARN") else "rule broken"


def _window(summary) -> str:
    s, e = summary.window_start, summary.window_end
    if summary.schedule == "daily":
        return f"{s:%Y-%m-%d} (UTC)"
    return f"{s:%Y-%m-%d %H:%M} to {e:%H:%M} UTC"


def _name(r) -> str:
    what = r.RULE_NAME or r.CHECK_TYPE
    return f"{r.SOURCE} · {r.FIELD_NAME or 'whole table'} · {what}"


def _others(summary):
    """Everything not passing that is not already a new failure, a reminder or a recovery."""
    listed = set(summary.new_failures.CHECK_ID) | set(summary.reminders.CHECK_ID) | set(summary.recovered.CHECK_ID)
    r = summary.results
    return r[(r.STATUS != "PASS") & ~r.CHECK_ID.isin(listed)]


def subject(summary) -> str:
    parts = []
    if len(summary.new_failures):
        parts.append(f"{len(summary.new_failures)} new problem{'s' if len(summary.new_failures) > 1 else ''}")
    if len(summary.reminders):
        parts.append(f"{len(summary.reminders)} still failing")
    if len(summary.recovered):
        parts.append(f"{len(summary.recovered)} recovered")
    sources = sorted(set(summary.new_failures.SOURCE) | set(summary.reminders.SOURCE)) or sorted(set(summary.recovered.SOURCE))
    where = f" in {', '.join(sources)}" if sources else ""
    return f"[DQ {summary.schedule}] {', '.join(parts)}{where}"[:100]


def _counts(summary) -> str:
    c = summary.counts()
    return (f"{c['checks']} checks ran: {c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed, "
            f"{c['ERROR']} could not run.")


def body(summary) -> str:
    """The plain-text message (SNS, log, and the email's text part)."""
    out = [f"DATA QUALITY ALERT - {summary.schedule} check of {_window(summary)}", "", _counts(summary), ""]

    def block(title, frame, extra=None):
        if not len(frame):
            return
        out.extend([f"=== {title} ({len(frame)}) ===", ""])
        for i, r in enumerate(frame.itertuples(), 1):
            out.append(f"{i}. [{TITLE[r.STATUS]}] {_name(r)}")
            out.append(f"   What is wrong: {r.EXPLANATION}")
            if extra:
                out.append(f"   {extra(r)}")
            out.append(f"   Measured: {observed_text(r.STATUS, r.METRIC, r.OBSERVED_VALUE)}. Limit: {r.CRITERIA}.")
            if r.STATUS == "ERROR" and r.ERROR_TEXT:
                out.append(f"   Error: {r.ERROR_TEXT}")
            out.append(f"   Table: {r.TABLE_NAME}")
            out.append("   See the rows (Snowflake):")
            out.extend(f"      {line}" for line in r.INVESTIGATE_SQL.splitlines())
            out.append("")

    block("NEW PROBLEMS", summary.new_failures)
    block("STILL FAILING (reminder)", summary.reminders, lambda r: f"Failing for {r.FAILING_HOURS} hours.")
    if len(summary.recovered):
        out.extend([f"=== RECOVERED ({len(summary.recovered)}) ===", ""])
        out.extend(f"- {_name(r)}: passing again" for r in summary.recovered.itertuples())
        out.append("")
    others = _others(summary)
    if len(others):
        out.extend([f"=== ALSO NOT PASSING ({len(others)}) ===", ""])
        out.extend(f"- [{TITLE[r.STATUS]}] {_name(r)}: {r.EXPLANATION}" for r in others.itertuples())
        out.append("")
    if summary.report_url:
        out += ["Open the data quality report: " + summary.report_url, ""]
    if summary.data_docs:
        out += ["Full Great Expectations report for this run: " + summary.data_docs, ""]
    out += ["---", "A problem is emailed when it starts, when it recovers, and as a reminder while it lasts;",
            "not every run. Run id: " + summary.run_id]
    return "\n".join(out)


def body_html(summary) -> str:
    """The same content as an HTML email."""
    e = html.escape

    def card(r, extra=""):
        colour = COLOUR[r.STATUS]
        error = f"<p style='margin:4px 0;color:#555'><b>Error:</b> {e(r.ERROR_TEXT)}</p>" if r.STATUS == "ERROR" and r.ERROR_TEXT else ""
        return (
            f"<div style='border-left:5px solid {colour};background:#fafafa;padding:10px 14px;margin:10px 0'>"
            f"<div style='font-weight:bold;color:{colour}'>{TITLE[r.STATUS]} · {e(_name(r))}</div>"
            f"<p style='margin:6px 0'>{e(r.EXPLANATION)}</p>{extra}{error}"
            f"<table style='font-size:13px;color:#333;border-collapse:collapse'>"
            f"<tr><td style='padding:2px 12px 2px 0'><b>Measured</b></td><td>{e(observed_text(r.STATUS, r.METRIC, r.OBSERVED_VALUE))}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0'><b>Limit</b></td><td>{e(r.CRITERIA)}</td></tr>"
            f"<tr><td style='padding:2px 12px 2px 0'><b>Table</b></td><td>{e(r.TABLE_NAME)}</td></tr></table>"
            f"<div style='margin-top:6px;font-size:12px;color:#555'>See the rows (run in Snowflake):</div>"
            f"<pre style='background:#f0f0f0;padding:8px;font-size:12px;white-space:pre-wrap'>{e(r.INVESTIGATE_SQL)}</pre></div>")

    c = summary.counts()
    tiles = "".join(
        f"<td style='padding:8px 14px;text-align:center;background:#f4f4f4'><div style='font-size:22px;"
        f"font-weight:bold;color:{COLOUR.get(k, '#333')}'>{c[k] if k in c else c['checks']}</div>"
        f"<div style='font-size:12px'>{label}</div></td>"
        for k, label in (("checks", "checks"), ("PASS", "passed"), ("WARN", "warnings"), ("FAIL", "failed"),
                         ("ERROR", "could not run")))
    parts = [
        "<div style='font-family:Arial,sans-serif;max-width:820px;color:#222'>",
        f"<h2 style='margin:0 0 4px'>Data quality alert</h2>",
        f"<div style='color:#555;margin-bottom:12px'>{e(summary.schedule.capitalize())} check of {e(_window(summary))}</div>",
        f"<table style='border-spacing:6px'><tr>{tiles}</tr></table>",
    ]
    if summary.report_url:
        parts.append(f"<p><a href='{e(summary.report_url)}' style='display:inline-block;background:#127EA6;color:#fff;"
                     f"padding:8px 16px;border-radius:4px;text-decoration:none;font-weight:bold'>"
                     f"Open the data quality report</a></p>")
    if len(summary.new_failures):
        parts.append(f"<h3>New problems ({len(summary.new_failures)})</h3>")
        parts += [card(r) for r in summary.new_failures.itertuples()]
    if len(summary.reminders):
        parts.append(f"<h3>Still failing ({len(summary.reminders)})</h3>")
        parts += [card(r, f"<p style='margin:4px 0'><b>Failing for {r.FAILING_HOURS} hours.</b></p>")
                  for r in summary.reminders.itertuples()]
    if len(summary.recovered):
        parts.append(f"<h3>Recovered ({len(summary.recovered)})</h3><ul>")
        parts += [f"<li style='color:{COLOUR['PASS']}'>{e(_name(r))}: passing again</li>" for r in summary.recovered.itertuples()]
        parts.append("</ul>")
    others = _others(summary)
    if len(others):
        parts.append(f"<h3>Also not passing ({len(others)})</h3><ul>")
        parts += [f"<li><b style='color:{COLOUR[r.STATUS]}'>{TITLE[r.STATUS]}</b> {e(_name(r))}: {e(r.EXPLANATION)}</li>"
                  for r in others.itertuples()]
        parts.append("</ul>")
    if summary.data_docs:
        parts.append(f"<p>Full Great Expectations report for this run: <code>{e(summary.data_docs)}</code></p>")
    parts.append(f"<p style='color:#888;font-size:12px;margin-top:20px'>A problem is emailed when it starts, when it "
                 f"recovers, and as a reminder while it lasts; not every run. Run id {e(summary.run_id)}.</p></div>")
    return "".join(parts)


# ---- sending ----------------------------------------------------------------------------------------
def send(settings, summary) -> list[str]:
    return send_message(settings, subject(summary), body(summary), body_html(summary))


def send_message(settings, subject_line: str, text: str, html_text: str | None = None) -> list[str]:
    """Send through every configured channel. A broken channel is logged and does not stop the rest."""
    sent = []
    for channel in settings.alert_channels:
        try:
            CHANNELS[channel](settings, subject_line, text, html_text)
            sent.append(channel)
        except KeyError:
            log.error("Unknown alert channel '%s'", channel)
        except Exception as exc:
            log.error("Alert channel '%s' failed: %s", channel, exc)
    return sent


def _log(settings, subject_line, text, html_text) -> None:
    print(f"\n===== ALERT: {subject_line} =====\n{text}\n=====\n")


def _sns(settings, subject_line, text, html_text) -> None:
    import boto3

    if not settings.sns_topic_arn:
        raise RuntimeError("DQF_SNS_TOPIC_ARN is not set")
    # SNS email is plain text, at most 256 KB.
    boto3.client("sns").publish(TopicArn=settings.sns_topic_arn, Subject=subject_line, Message=text[:250_000])


def _email(settings, subject_line, text, html_text) -> None:
    env = os.environ.get
    host, to = env("DQF_SMTP_HOST"), [a.strip() for a in env("DQF_EMAIL_TO", "").split(",") if a.strip()]
    if not host or not to:
        raise RuntimeError("email alerts need DQF_SMTP_HOST and DQF_EMAIL_TO")
    port = int(env("DQF_SMTP_PORT", "587"))
    user, password = env("DQF_SMTP_USER"), env("DQF_SMTP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject_line, ", ".join(to)
    msg["From"] = env("DQF_EMAIL_FROM") or user or "dqf@localhost"
    msg.set_content(text)
    if html_text:
        msg.add_alternative(html_text, subtype="html")
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.ehlo()
        if env("DQF_SMTP_STARTTLS", "true").lower() != "false" and server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
    with server:
        if user and password:
            server.login(user, password)
        server.send_message(msg)


CHANNELS = {"log": _log, "sns": _sns, "email": _email}
