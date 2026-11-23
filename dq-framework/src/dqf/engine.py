"""One run: read the workbook -> GX checks on one window -> results table -> alerts."""
from __future__ import annotations

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("GX_ANALYTICS_ENABLED", "false")   # no usage statistics; set before importing GX

import great_expectations as gx  # noqa: E402
import pandas as pd  # noqa: E402

from dqf import alerts  # noqa: E402
from dqf.datadocs import DataDocs  # noqa: E402
from dqf.explain import explain, investigate_sql  # noqa: E402
from dqf.plan import check_type, criteria_text, metric_name, plan, windows  # noqa: E402
from dqf.snowflake import Snowflake  # noqa: E402
from dqf.sql import NO_ROWS, ts  # noqa: E402
from dqf.workbook import Config, load_config  # noqa: E402

log = logging.getLogger("dqf")
logging.getLogger("great_expectations.data_context.types.base").setLevel(logging.ERROR)

# Only these parts of a GX result are stored. GX results can carry example rows; those are dropped.
# SUMMARY gives the counts; partial_unexpected_count = 0 stops GX fetching example values at all.
RESULT_FORMAT = {"result_format": "SUMMARY", "partial_unexpected_count": 0}
SAFE_KEYS = ("element_count", "unexpected_count", "unexpected_percent", "missing_count",
             "missing_percent", "observed_value")
BAD = ("FAIL", "ERROR")


@dataclass
class RunSummary:
    run_id: str
    schedule: str
    window_start: datetime
    window_end: datetime
    results: pd.DataFrame
    new_failures: pd.DataFrame
    recovered: pd.DataFrame
    reminders: pd.DataFrame
    alert_sent_to: list[str] = field(default_factory=list)
    data_docs: str | None = None                     # where GX's HTML report for this run is
    report_url: str | None = None                    # the Streamlit report, opened on this run

    def counts(self) -> dict:
        return {
            "run_id": self.run_id, "schedule": self.schedule,
            "window_start": ts(self.window_start), "window_end": ts(self.window_end),
            "checks": len(self.results),
            **{s: int((self.results.STATUS == s).sum()) for s in ("PASS", "WARN", "FAIL", "ERROR")},
            "new_failures": len(self.new_failures), "recovered": len(self.recovered),
            "reminders": len(self.reminders), "alert_sent_to": self.alert_sent_to, "data_docs": self.data_docs,
        }


class Engine:
    """Holds the GX context and the Snowflake data source, so a backfill reuses them."""

    def __init__(self, settings, snowflake: Snowflake | None = None):
        self.settings = settings
        self.snowflake = snowflake or Snowflake(settings)
        self._source = None
        self._table_ready = False
        self.docs = DataDocs(settings.data_docs) if settings.data_docs else None

    @property
    def source(self):
        if self._source is None:
            context = gx.get_context(mode="ephemeral")       # nothing written to disk
            _quiet(context)
            self._source = self.snowflake.add_gx_source(context)
        return self._source

    def load_config(self) -> Config:
        return load_config(self.settings.config_uri)

    # ---- checks ----------------------------------------------------------------------------------
    def run_checks(self, config: Config, schedule: str, start: datetime, end: datetime, run_id: str) -> pd.DataFrame:
        outcomes: dict[str, tuple[str, dict]] = {}
        started = datetime.now(timezone.utc)
        for group in plan(config, start, end, self.settings):
            outcomes.update(self._validate(group, schedule, start, started))
        run_ts = ts(datetime.now(timezone.utc).replace(tzinfo=None))
        rows = []
        for row in [*config.checks, *config.stability]:
            rule = config.rules.get(row.get("RULE_NAME") or "")
            status, res = outcomes[row["CHECK_ID"]]
            observed = res.get("unexpected_percent", res.get("observed_value"))
            observed = float(observed) if isinstance(observed, (int, float, Decimal)) else None
            if status == "PASS" and check_type(row) in ("PSI", "CSI") and observed is not None and observed >= row["WARN"]:
                status = "WARN"                              # drifting, below the fail limit
            source = config.sources[row["SOURCE"]]
            rows.append({
                "RUN_ID": run_id, "RUN_TS": run_ts, "SCHEDULE": schedule,
                "WINDOW_START": ts(start), "WINDOW_END": ts(end),
                "CHECK_ID": row["CHECK_ID"], "SOURCE": source.name, "TABLE_NAME": source.table,
                "FIELD_NAME": row.get("FIELD") or "", "CHECK_TYPE": check_type(row),
                "RULE_NAME": row.get("RULE_NAME") or "", "CRITERIA": criteria_text(row, config.rules),
                "STATUS": status, "METRIC": metric_name(row), "OBSERVED_VALUE": observed,
                "ELEMENT_COUNT": _int(res.get("element_count")), "UNEXPECTED_COUNT": _int(res.get("unexpected_count")),
                "ALERT_FLAG": row["ALERT"], "ALERT_SENT": "No",
                "DETAILS": json.dumps({k: _short(res[k]) for k in (*SAFE_KEYS, "error", "parts") if k in res}, default=str),
            })
            # What it means in words, and a query to see the rows behind it (for the alert and the report).
            other = config.sources.get(row.get("COMPARE_SOURCE") or "")
            rows[-1]["EXPLANATION"] = explain(row, rows[-1], rule, res.get("parts"))
            rows[-1]["INVESTIGATE_SQL"] = investigate_sql(row, check_type(row), source, other, start, end, rule)
            rows[-1]["ERROR_TEXT"] = res.get("error", "")
        return pd.DataFrame(rows)

    def _validate(self, group, schedule: str = "", start: datetime | None = None,
                  started: datetime | None = None) -> dict[str, tuple[str, dict]]:
        """All checks of one group in a single GX call. One broken check never stops the others,
        and one broken group never stops the run."""
        # Named after the group, so GX's report reads "hourly.UW_A1_INPUT.checks". Assets are deleted
        # after use, so a name never clashes with an earlier run's.
        name = f"{schedule}.{group.name}" if group.name else f"q_{uuid.uuid4().hex[:8]}"
        expectations = []
        for row, expectation in group.items:
            expectation.meta = {**(expectation.meta or {}), "check_id": row["CHECK_ID"]}
            expectations.append(expectation)
        try:
            source = self.source
            suite = gx.ExpectationSuite(name=name, expectations=expectations)
            batch = source.add_query_asset(name=name, query=group.query).add_batch_definition_whole_table("all").get_batch()
            validation = batch.validate(suite, result_format=RESULT_FORMAT)
        except Exception as exc:
            error = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            return {row["CHECK_ID"]: ("ERROR", error) for row, _ in group.items}
        finally:
            try:
                self._source.delete_asset(name)
            except Exception:
                pass
        for vr in validation.results:                   # no example values anywhere: they may be personal data
            for key in [k for k in (vr.result or {}) if k.startswith("partial_unexpected")]:
                del vr.result[key]
        if self.docs is not None:                       # GX's own HTML report; never breaks the run
            try:
                self.docs.add(name, expectations, validation, schedule, start, started)
            except Exception as exc:
                log.warning("GX report: could not keep this result: %s", exc)
        parts: dict[str, list] = {}
        for vr in validation.results:
            res = dict(vr.result or {})
            if vr.success:
                outcome = ("PASS", res)
            elif not res:          # GX could not evaluate it (bad column, SQL error): broken, not a finding
                outcome = ("ERROR", {"error": _exception_text(vr)})
            else:
                outcome = ("FAIL", res)
            meta = vr.expectation_config.meta
            parts.setdefault(meta["check_id"], []).append((meta.get("part"), outcome))
        outcomes = {check: combine(found) for check, found in parts.items()}
        for row, _ in group.items:
            outcomes.setdefault(row["CHECK_ID"], ("ERROR", {"error": "No result returned"}))
        return outcomes

    # ---- runs ------------------------------------------------------------------------------------
    def run(self, schedule: str, end: datetime | None = None, *, send_alerts: bool = True,
            config: Config | None = None) -> RunSummary:
        """Check the window of `schedule` that ends at or before `end` (default: now, UTC)."""
        start, end = windows(schedule, end or datetime.now(timezone.utc).replace(tzinfo=None))
        config = (config or self.load_config()).for_schedule(schedule)
        run_id = uuid.uuid4().hex[:12]
        results = self.run_checks(config, schedule, start, end, run_id)
        if not self._table_ready:
            self.snowflake.ensure_result_table()
            self._table_ready = True
        previous, failing_since, previous_end = self.snowflake.record(results.to_dict("records"), schedule, start, end)
        new, recovered, reminders = decide(results, previous, failing_since, end, self.settings.remind_hours, previous_end)
        summary = RunSummary(run_id, schedule, start, end, results, new, recovered, reminders,
                             report_url=alerts.report_link(self.settings.report_url, schedule, end))
        if self.docs is not None:
            try:
                summary.data_docs = self.docs.build()
            except Exception as exc:
                log.warning("GX report: could not be built: %s", exc)
        if send_alerts and (len(new) or len(recovered) or len(reminders)):
            summary.alert_sent_to = alerts.send(self.settings, summary)
            if summary.alert_sent_to:                        # for the report: which checks were alerted
                alerted = [*new.CHECK_ID, *recovered.CHECK_ID, *reminders.CHECK_ID]
                try:
                    self.snowflake.mark_alerted(run_id, alerted)
                except Exception as exc:
                    log.warning("Could not mark the alerted checks in the results table: %s", exc)
        log.info(json.dumps(summary.counts()))
        return summary

    def backfill(self, schedule: str, periods: int, end: datetime | None = None, on_run=None) -> list[RunSummary]:
        """Run the last `periods` windows of `schedule`, oldest first, without alerts."""
        step = timedelta(days=1) if schedule == "daily" else timedelta(hours=1)
        _, last = windows(schedule, end or datetime.now(timezone.utc).replace(tzinfo=None))
        config = self.load_config()
        runs = []
        for k in range(periods - 1, -1, -1):
            runs.append(self.run(schedule, last - k * step, send_alerts=False, config=config))
            if on_run:
                on_run(runs[-1])
        return runs


def decide(results: pd.DataFrame, previous: dict[str, str], failing_since: dict[str, datetime],
           end: datetime, remind_hours: int, previous_end: datetime | None = None):
    """Which checks to alert on: (new failures, recovered, reminders). Only checks with ALERT = Yes.
    WARN never alerts by itself; it shows in the results and the report."""
    alerting = results.loc[results.ALERT_FLAG == "Yes"]
    bad = alerting.loc[alerting.STATUS.isin(BAD)]
    ok = alerting.loc[alerting.STATUS.isin(("PASS", "WARN"))]
    # A change between FAIL and ERROR is news too: a failing check that can no longer run, or back.
    same = [previous.get(c) == status for c, status in zip(bad.CHECK_ID, bad.STATUS)]
    new = bad.loc[[not s for s in same]]
    recovered = ok.loc[[previous.get(c) in BAD for c in ok.CHECK_ID]]

    # Repeat a check that keeps failing each time it crosses another `remind_hours`. Comparing with
    # the previous run, rather than testing for an exact multiple, means a skipped run loses nothing.
    still = bad.loc[same].copy()
    hours = lambda since, at: (at - since).total_seconds() / 3600  # noqa: E731
    still["FAILING_HOURS"] = [int(hours(failing_since[c], end)) if c in failing_since else 0 for c in still.CHECK_ID]
    due = []
    for c in still.CHECK_ID:
        if remind_hours <= 0 or c not in failing_since:
            due.append(False)
            continue
        now = hours(failing_since[c], end)
        before = hours(failing_since[c], previous_end) if previous_end else now - 1
        due.append(now >= remind_hours and math.floor(now / remind_hours) > math.floor(before / remind_hours))
    return new, recovered, still.loc[due]


def combine(parts: list) -> tuple[str, dict]:
    """One result for a check made of several expectations (a business rule with several `then`
    fields): it fails if any part fails, cannot run if any part cannot, and reports its worst part.
    "parts" lists each part's outcome, for the explanation and DETAILS (counts only, no data)."""
    if len(parts) == 1 and parts[0][0] is None:
        return parts[0][1]
    statuses = [status for _, (status, _) in parts]
    status = "FAIL" if "FAIL" in statuses else "ERROR" if "ERROR" in statuses else "PASS"
    pick = [o for _, o in parts if o[0] == status]
    worst = max(pick, key=lambda o: o[1].get("unexpected_percent") or 0)[1]
    res = dict(worst)
    res["parts"] = [{"part": label, "status": s, **{k: r[k] for k in ("element_count", "unexpected_count",
                     "unexpected_percent", "error") if k in r}} for label, (s, r) in parts]
    return status, res


def _int(value):
    return None if value is None else int(value)


def _short(value):
    return value[:500] if isinstance(value, str) else value


def _exception_text(vr) -> str:
    info = vr.exception_info or {}
    messages = [info.get("exception_message")] if "exception_message" in info else [
        v.get("exception_message") for v in info.values() if isinstance(v, dict)]
    return ("; ".join(m for m in messages if m) or "The check could not be evaluated")[:500]


def _quiet(context) -> None:
    try:
        from great_expectations.data_context.types.base import ProgressBarsConfig

        context.variables.progress_bars = ProgressBarsConfig(globally=False)
    except Exception:
        pass


__all__ = ["Engine", "RunSummary", "decide", "NO_ROWS"]
