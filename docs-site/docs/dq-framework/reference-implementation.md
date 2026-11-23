# Reference implementation

This page shows how the Phase 1 design turns into code: the repository layout, the core modules, the
CI pipeline and the tests. Phases 2 and 3 add modules to the same structure.

!!! warning "Reference sketch, not production code"
The code shows the **shape** of the solution and the key patterns. It targets **GX Core 1.x** and
Python 3.12. Pin an exact GX version in `pyproject.toml`, and check every API call against that
version before relying on it. GX's API changed substantially between 0.x and 1.x, and some details
(for example the Snowflake key-pair connection arguments) vary between releases.

## Repository layout

```text
dq-framework/
├── .gitlab-ci.yml
├── pyproject.toml                  # pins great_expectations==<exact 1.x version>
├── config/
│   ├── check_types.yml             # catalog: check type -> builder, parameter schema
│   ├── environments/
│   │   ├── sandbox.yml             # database names, warehouse, role per environment
│   │   └── prod.yml
│   └── sources/
│       └── uw_a1.yml               # one file per monitored source
├── migrations/                     # versioned, forward-only DDL (never drop results)
│   ├── V001__dq_framework_schema.sql
│   └── V002__results_and_alerts.sql
├── views/
│   └── DQ_VW_UW_A1_INPUT.sql       # the data contract per source
├── src/dqf/
│   ├── cli.py                      # dqf run | backfill | validate-config | load-config | report
│   ├── config/
│   │   ├── models.py               # pydantic models for the YAML
│   │   └── registry.py             # read deployed config from Snowflake
│   ├── engine/
│   │   ├── builders.py             # check type -> GX expectation (+ derived columns)
│   │   ├── query.py                # builds the windowed SQL for a source
│   │   ├── runner.py               # orchestrates one run
│   │   └── classify.py             # PASS / WARN / FAIL / INSUFFICIENT_DATA / ERROR
│   ├── results/writer.py
│   ├── alerts/
│   │   ├── manager.py              # stateful alert lifecycle
│   │   └── notifiers.py            # email, Teams
│   ├── reports/weekly.py
│   └── metrics/                    # Phase 2-3: psi.py, performance.py
└── tests/
    ├── unit/                       # builders, classifier, config validation
    ├── faults/                     # fault-injection detection tests
    └── integration/                # end to end against sandbox
```

## 1. Config models

The YAML is validated by pydantic models, both in CI and at load time. Identifiers are restricted to
safe characters because they end up inside SQL.

```python
# src/dqf/config/models.py
from enum import Enum
from pydantic import BaseModel, Field, field_validator

IDENT = r"^[A-Za-z_][A-Za-z0-9_$.]*$"   # identifiers only, so config can't inject SQL


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CheckConfig(BaseModel):
    type: str
    severity: Severity = Severity.MEDIUM
    params: dict = Field(default_factory=dict)
    warn: dict | None = None
    fail: dict = Field(default_factory=dict)
    alert_enabled: bool = False          # new checks start in shadow mode


class FieldConfig(BaseModel):
    name: str = Field(pattern=IDENT)
    in_model: bool = False
    in_rules: bool = False
    importance_rank: int | None = None
    is_pii: bool = False
    placeholders: list = Field(default_factory=list)
    checks: list[CheckConfig] = Field(default_factory=list)

    @field_validator("checks")
    @classmethod
    def only_monitor_used_fields(cls, checks, info):
        data = info.data
        if checks and not (data.get("in_model") or data.get("in_rules")):
            raise ValueError(f"{data.get('name')}: only fields used in a model or rule are monitored")
        return checks


class SourceConfig(BaseModel):
    source_id: str = Field(pattern=r"^[a-z0-9_]+$")
    domain: str                           # UW | RM | CLOVER
    phase: str                            # INPUT | FEATURE_SCORE | PERFORMANCE
    snowflake_object: str = Field(pattern=IDENT)
    timestamp_col: str = Field(pattern=IDENT)
    window: str = "1h"
    arrival_lag: str = "15m"
    min_rows: int = 30
    owner_team: str
    alert_route: str
    table_checks: list[CheckConfig] = Field(default_factory=list)
    fields: list[FieldConfig] = Field(default_factory=list)
```

The `only_monitor_used_fields` validator enforces the kickoff's scope rule in code: a field that isn't
used in a model or rule **cannot** be given checks.

## 2. Check builders

Each check type has one builder. A builder returns a **plan**: the GX expectation to run, any
**derived columns** it needs in the window query, and a **warn predicate** for the second threshold.

```python
# src/dqf/engine/builders.py
from dataclasses import dataclass, field
from typing import Callable
import great_expectations as gx


@dataclass
class CheckPlan:
    check_id: str
    expectation: object                       # a gx.expectations.* instance
    is_warn: Callable[[float], bool] = lambda metric: False
    derived_columns: dict[str, str] = field(default_factory=dict)   # name -> SQL expression
    probe: str = "window"                      # "window" or "freshness"
    table_level: bool = False                  # evaluated even when rows < min_rows


BUILDERS: dict[str, Callable] = {}


def builder(check_type: str):
    def register(fn):
        BUILDERS[check_type] = fn
        return fn
    return register


def _meta(spec) -> dict:
    return {"check_id": spec.check_id, "severity": spec.severity}


@builder("MISSING_VALUES")
def missing_values(spec, ctx) -> CheckPlan:
    # GX reports unexpected_percent = % null; FAIL via mostly, WARN checked by the framework
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectColumnValuesToNotBeNull(
            column=spec.field, mostly=1 - spec.fail["max_pct"], meta=_meta(spec)),
        is_warn=lambda pct_null: spec.warn is not None and pct_null > spec.warn["max_pct"],
    )


@builder("FIELD_POPULATION")
def field_population(spec, ctx) -> CheckPlan:
    # Derived 0/1 column: populated = not null, not blank, not a placeholder.
    # Its mean is the population rate, which GX reports as observed_value.
    col = f"DQ_POP_{spec.field}"
    conditions = [f"{spec.field} is not null", f"trim({spec.field}::varchar) <> ''"]
    if spec.placeholders:   # never emit "not in (NULL)": it is NULL for every row
        literals = ", ".join(ctx.sql_literal(v) for v in spec.placeholders)
        conditions.append(f"{spec.field} not in ({literals})")
    expr = f"iff({' and '.join(conditions)}, 1, 0)"
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectColumnMeanToBeBetween(
            column=col, min_value=spec.fail["min_pct"], meta=_meta(spec)),
        derived_columns={col: expr},
        is_warn=lambda rate: spec.warn is not None and rate < spec.warn["min_pct"],
    )


@builder("NO_HIT_RATE")
def no_hit_rate(spec, ctx) -> CheckPlan:
    # Relative to the trailing same-hour median from DQ_RESULT history
    col = f"DQ_NOHIT_{spec.field}"
    values = ", ".join(ctx.sql_literal(v) for v in spec.params["no_hit_values"])
    baseline = ctx.trailing_median(spec.check_id, weeks=spec.params.get("weeks", 4))
    if baseline is None:                              # cold start: never FAIL, only report
        fail_limit, warn_limit = 1.0, None
    else:
        fail_limit = baseline + spec.fail["max_increase_pp"] / 100
        warn_limit = baseline + spec.warn["max_increase_pp"] / 100 if spec.warn else None
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectColumnMeanToBeBetween(
            column=col, max_value=fail_limit, meta=_meta(spec)),
        derived_columns={col: f"iff({spec.field} in ({values}), 1, 0)"},
        is_warn=lambda rate: warn_limit is not None and rate > warn_limit,
    )


@builder("ACCEPTED_VALUES")
@builder("NEW_ENUMERATION")
def accepted_values(spec, ctx) -> CheckPlan:
    allowed = spec.params.get("allowed") or ctx.reference_values(spec.field)
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectColumnValuesToBeInSet(
            column=spec.field, value_set=allowed, mostly=1 - spec.fail.get("max_pct", 0.0),
            meta=_meta(spec)),
        is_warn=lambda pct_new: spec.warn is not None and pct_new > 0,   # any new value warns
    )


@builder("VOLUME")
def volume(spec, ctx) -> CheckPlan:
    baseline = ctx.trailing_median(spec.check_id, weeks=spec.params.get("weeks", 4))
    lo, hi = (0, None) if baseline is None else (
        baseline * spec.fail["min_ratio"], baseline * spec.fail["max_ratio"])
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectTableRowCountToBeBetween(
            min_value=lo, max_value=hi, meta=_meta(spec)),
        is_warn=lambda n: baseline is not None and spec.warn is not None
                          and n < baseline * spec.warn["min_ratio"],
        table_level=True,
    )


@builder("FRESHNESS")
def freshness(spec, ctx) -> CheckPlan:
    # Runs on a separate probe query that returns one row: minutes since the newest record
    return CheckPlan(
        check_id=spec.check_id,
        expectation=gx.expectations.ExpectColumnMaxToBeBetween(
            column="DQ_LAG_MINUTES", max_value=ctx.minutes(spec.fail["max_lag"]), meta=_meta(spec)),
        probe="freshness",
        table_level=True,
    )

# ACCEPTABLE_RANGE, EXPECTED_FORMAT, VALUE_LENGTH, VALUE_TYPE, PLACEHOLDER_VALUE,
# DUPLICATES, SCHEMA and TREND follow the same pattern with
# ExpectColumnValuesToBeBetween / ...ToMatchRegex / ExpectColumnValueLengthsToEqual /
# ...ToBeOfType / ...ToNotBeInSet / ExpectCompoundColumnsToBeUnique /
# ExpectTableColumnsToMatchSet / ExpectColumnMedianToBeBetween.
```

!!! tip "The derived-column pattern"
Rate-type checks (no-hit rate, field population) add a 0/1 **indicator column** to the window query
and use `ExpectColumnMeanToBeBetween`. The mean of a 0/1 column **is** the rate, and GX returns it as
`observed_value`. Every check therefore records a real measured value in `DQ_RESULT`, which the WARN
thresholds and the trailing comparisons depend on. A custom SQL expectation would only report
pass or fail.

## 3. Building the window query

```python
# src/dqf/engine/query.py
from datetime import datetime


def window_query(source, plans, start: datetime, end: datetime) -> str:
    """Select only monitored columns + derived indicators, for one closed window."""
    columns = sorted({p.expectation.column for p in plans
                      if getattr(p.expectation, "column", None) and not p.derived_columns})
    derived = [f"{expr} as {name}" for p in plans for name, expr in p.derived_columns.items()]
    select_list = ",\n       ".join(columns + derived) or "1 as DQ_ROW"
    return (
        f"select {select_list}\n"
        f"from {source.snowflake_object}\n"
        f"where {source.timestamp_col} >= '{start:%Y-%m-%d %H:%M:%S}'\n"
        f"  and {source.timestamp_col} <  '{end:%Y-%m-%d %H:%M:%S}'"
    )


def freshness_query(source) -> str:
    return (f"select datediff('minute', max({source.timestamp_col}), current_timestamp()) "
            f"as DQ_LAG_MINUTES from {source.snowflake_object}")
```

## 4. The runner

One run covers one source and one closed window. GX runs in an **ephemeral** context built from the
registry, so no GX state is kept on disk.

```python
# src/dqf/engine/runner.py
import great_expectations as gx

from dqf.engine.builders import BUILDERS
from dqf.engine.classify import classify, observed_metric
from dqf.engine.query import freshness_query, window_query


def run_source(source_id: str, start, end, *, registry, results, settings):
    source = registry.load_source(source_id)
    checks = registry.load_checks(source_id)                  # active checks + config_version
    ctx = registry.run_context(source, start, end, history=results)
    plans = [BUILDERS[c.type](c, ctx) for c in checks]

    run = results.start_run(source_id, start, end, registry.config_version,
                            gx_version=gx.__version__)
    try:
        context = gx.get_context(mode="ephemeral")
        ds = context.data_sources.add_snowflake(
            name="snowflake",
            connection_string=settings.snowflake_url,          # user/account/db/schema/warehouse/role
            kwargs={"connect_args": {"private_key": settings.private_key_der}},  # verify per GX version
        )

        window_plans = [p for p in plans if p.probe == "window"]
        window = ds.add_query_asset(name=f"{source_id}_window",
                                    query=window_query(source, window_plans, start, end))
        batch = window.add_batch_definition_whole_table("window").get_batch()

        row_count = batch.validate(
            gx.expectations.ExpectTableRowCountToBeBetween(min_value=0)
        ).result["observed_value"]

        validations = [(p, batch.validate(p.expectation, result_format="SUMMARY"))
                       for p in window_plans]

        fresh_plans = [p for p in plans if p.probe == "freshness"]
        if fresh_plans:
            probe = ds.add_query_asset(name=f"{source_id}_freshness",
                                       query=freshness_query(source))
            fresh_batch = probe.add_batch_definition_whole_table("probe").get_batch()
            validations += [(p, fresh_batch.validate(p.expectation)) for p in fresh_plans]

        rows = [
            {
                "check_id": plan.check_id,
                "status": classify(plan, vr, row_count, source.min_rows),
                "metric_value": observed_metric(vr),
                "element_count": vr.result.get("element_count"),
                "unexpected_count": vr.result.get("unexpected_count"),
                "details": registry.pii_safe_details(plan.check_id, vr.result),
            }
            for plan, vr in validations
        ]
        results.write_results(run.run_id, rows)
        results.finish_run(run.run_id, status="SUCCEEDED", row_count=row_count)
        return rows
    except Exception as exc:                                    # a run that can't execute is an alert
        results.finish_run(run.run_id, status="ERROR", error=str(exc)[:2000])
        raise
```

!!! note "One `validate` call per expectation"
Running expectations one at a time keeps each result tied to its check and isolates failures: one bad
check can't hide the others. Running them as a single `ExpectationSuite` is also fine and cuts round
trips. In that case, map results back through the `meta.check_id` that every expectation carries.

## 5. Classifying results

```python
# src/dqf/engine/classify.py
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


def observed_metric(vr) -> float | None:
    r = vr.result or {}
    if "unexpected_percent" in r and r["unexpected_percent"] is not None:
        return r["unexpected_percent"] / 100          # column-map checks: share of bad rows
    value = r.get("observed_value")
    return float(value) if isinstance(value, (int, float)) else None


def classify(plan, vr, row_count: int, min_rows: int) -> Status:
    if (vr.exception_info or {}).get("raised_exception"):
        return Status.ERROR
    if row_count < min_rows and not plan.table_level:
        return Status.INSUFFICIENT_DATA               # VOLUME / FRESHNESS still evaluated
    if not vr.success:
        return Status.FAIL
    metric = observed_metric(vr)
    if metric is not None and plan.is_warn(metric):
        return Status.WARN
    return Status.PASS
```

## 6. Alert manager

This component carries state, and that state is what separates useful alerting from alert storms. The full lifecycle is on the
[Operations](operations.md#alert-lifecycle) page.

```python
# src/dqf/alerts/manager.py
ESCALATE_AFTER_HOURS = {"HIGH": 4, "MEDIUM": 24, "LOW": None}
RESOLVE_AFTER_PASSES = 2                     # avoids flapping open/resolve/open


def evaluate(run_results, *, store, notifier):
    for r in run_results:
        alert = store.open_alert_for(r.check_id)          # OPEN or ESCALATED, if any
        if store.is_suppressed(r.check_id):               # maintenance window / known incident
            continue

        if r.status in ("FAIL", "ERROR") and r.alert_enabled:
            if alert is None:
                alert = store.open(r)                     # first failure -> one notification
                notifier.send("OPENED", alert, r)
            else:
                store.touch(alert, r)                     # still failing -> no new notification
                limit = ESCALATE_AFTER_HOURS[r.severity]
                if alert.state == "OPEN" and limit and alert.age_hours >= limit:
                    store.escalate(alert)
                    notifier.send("ESCALATED", alert, r)

        elif r.status == "PASS" and alert is not None:
            if store.consecutive_passes(r.check_id) >= RESOLVE_AFTER_PASSES:
                store.resolve(alert, r)
                notifier.send("RESOLVED", alert, r)

        # WARN and INSUFFICIENT_DATA never notify; they appear in reports and digests
```

## 7. Writing results

```python
# src/dqf/results/writer.py
import json


def write_results(conn, run_id: str, rows: list[dict]) -> None:
    """Idempotent per run: re-running a window replaces its rows."""
    with conn.cursor() as cur:
        cur.execute("delete from DQ_FRAMEWORK.DQ_RESULT where run_id = %s", (run_id,))
        cur.executemany(
            """insert into DQ_FRAMEWORK.DQ_RESULT
                 (run_id, check_id, status, metric_value, element_count, unexpected_count, details)
               select %s, %s, %s, %s, %s, %s, parse_json(%s)""",
            [(run_id, r["check_id"], r["status"], r["metric_value"], r["element_count"],
              r["unexpected_count"], json.dumps(r["details"])) for r in rows],
        )
```

## 8. CI pipeline

```yaml
# .gitlab-ci.yml (sketch)
stages: [test, validate, deploy, run]

unit_and_fault_tests:
  stage: test
  script:
    - pip install -e ".[dev]"
    - pytest tests/unit tests/faults

validate_config:
  stage: validate
  script:
    - dqf validate-config --env sandbox        # schema, check params, fields exist in views

deploy_sandbox:
  stage: deploy
  rules: [{if: '$CI_COMMIT_BRANCH == "main"'}]
  script:
    - schemachange deploy --config-folder migrations   # forward-only migrations
    - dqf deploy-views --env sandbox
    - dqf load-config --env sandbox                     # stamps config_version = commit SHA

deploy_prod:
  stage: deploy
  rules: [{if: '$CI_COMMIT_BRANCH == "main"', when: manual}]
  script:
    - schemachange deploy --config-folder migrations
    - dqf deploy-views --env prod
    - dqf load-config --env prod

hourly_shadow_run:                               # MVP scheduler (pipeline schedule)
  stage: run
  rules: [{if: '$CI_PIPELINE_SOURCE == "schedule"'}]
  script:
    - dqf run --all-due --env sandbox
```

## 9. Tests

Unit tests use GX's **pandas** datasource, so they run in milliseconds without Snowflake. Fault tests
make sure each check catches the failure it is meant to catch.

```python
# tests/faults/test_missing_values.py
import pandas as pd
import great_expectations as gx

from dqf.engine.builders import BUILDERS
from dqf.engine.classify import Status, classify


def _batch(df):
    ctx = gx.get_context(mode="ephemeral")
    asset = ctx.data_sources.add_pandas("t").add_dataframe_asset("a")
    return asset.add_batch_definition_whole_dataframe("b").get_batch(
        batch_parameters={"dataframe": df})


def test_40pct_nulls_fail(spec_factory, ctx_stub):
    df = pd.DataFrame({"AVG_TICKET": [None] * 40 + [125.0] * 60})
    spec = spec_factory("MISSING_VALUES", field="AVG_TICKET",
                        warn={"max_pct": 0.10}, fail={"max_pct": 0.30})
    plan = BUILDERS["MISSING_VALUES"](spec, ctx_stub)
    vr = _batch(df).validate(plan.expectation)
    assert classify(plan, vr, row_count=100, min_rows=30) == Status.FAIL


def test_15pct_nulls_warn(spec_factory, ctx_stub):
    df = pd.DataFrame({"AVG_TICKET": [None] * 15 + [125.0] * 85})
    spec = spec_factory("MISSING_VALUES", field="AVG_TICKET",
                        warn={"max_pct": 0.10}, fail={"max_pct": 0.30})
    plan = BUILDERS["MISSING_VALUES"](spec, ctx_stub)
    vr = _batch(df).validate(plan.expectation)
    assert classify(plan, vr, row_count=100, min_rows=30) == Status.WARN


def test_quiet_hour_is_insufficient(spec_factory, ctx_stub):
    df = pd.DataFrame({"AVG_TICKET": [None] * 5 + [125.0] * 5})     # 50% null, but only 10 rows
    spec = spec_factory("MISSING_VALUES", field="AVG_TICKET", fail={"max_pct": 0.30})
    plan = BUILDERS["MISSING_VALUES"](spec, ctx_stub)
    vr = _batch(df).validate(plan.expectation)
    assert classify(plan, vr, row_count=10, min_rows=30) == Status.INSUFFICIENT_DATA
```
