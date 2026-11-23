from datetime import date

import pandas as pd
import pytest

from conftest import CONFIG
from dqf.workbook import ConfigError, load_config, parse, parse_values


def sheets(sources=None, checks=None, stability=None):
    src = sources or [{"SOURCE": "S", "TABLE": "DB.SC.T", "TS_COLUMN": "TS", "SCHEDULE": "hourly"}]
    out = {"SOURCES": pd.DataFrame(src)}
    if checks is not None:
        out["CHECKS"] = pd.DataFrame(checks)
    if stability is not None:
        out["STABILITY"] = pd.DataFrame(stability)
    return out


def check(**kw):
    return {"SOURCE": "S", "FIELD": "A", "CHECK_TYPE": "MISSING_VALUES", **kw}


def test_shipped_configuration_is_valid():
    config = load_config(str(CONFIG))
    assert set(config.sources) == {"UW_A1_INPUT", "UW_A1_SCORED", "UW_A1_DAILY"}
    assert len(config.checks) == 25 and len(config.stability) == 6 and len(config.rules) == 4
    assert config.sources["UW_A1_SCORED"].row_filter == "EXCLUSION_REASON is null"
    hourly, daily = config.for_schedule("hourly"), config.for_schedule("daily")
    assert len(hourly.checks) == 23 and hourly.stability == []
    assert len(daily.checks) == 2 and len(daily.stability) == 6
    mf = next(r for r in config.stability if r["FIELD"] == "MF_SCORE")
    assert (mf["BENCHMARK_START"], mf["BENCHMARK_END"], mf["ACTIVE_DAYS"]) == (-60, -30, 7)


def test_values_keep_quoted_text_and_convert_bare_numbers():
    assert parse_values("'01','02'") == ["01", "02"]
    assert parse_values("-999, -1") == [-999.0, -1.0]
    assert parse_values(-999) == [-999.0]


def test_comparison_defaults_and_ids():
    config = parse(sheets(
        sources=[{"SOURCE": "P", "TABLE": "DB.S.P", "TS_COLUMN": "TS", "SCHEDULE": "hourly"},
                 {"SOURCE": "RAW", "TABLE": "DB.S.R", "TS_COLUMN": "TS", "SCHEDULE": "daily"}],
        checks=[{"SOURCE": "P", "FIELD": "ZIP", "CHECK_TYPE": "MATCHES_SOURCE", "COMPARE_SOURCE": "raw", "KEY": "ID"}]))
    c = config.checks[0]
    assert (c["COMPARE_FIELD"], c["COMPARE_KEY"], c["COMPARE"], c["CHECK_ID"]) == ("ZIP", "ID", "EXACT", "P|ZIP|MATCHES_SOURCE:RAW")


def test_stability_dates_and_defaults():
    config = parse(sheets(stability=[{"SOURCE": "S", "FIELD": "SCORE", "BENCHMARK_START": "2026-01-01",
                                      "BENCHMARK_END": "2026-04-01"}]))
    r = config.stability[0]
    assert (r["INDEX"], r["FIELD_TYPE"], r["BINS"], r["ACTIVE_DAYS"], r["WARN"], r["FAIL"]) == ("PSI", "NUMERIC", 10, 7, 0.10, 0.25)
    assert r["BENCHMARK_START"] == date(2026, 1, 1)


def test_every_problem_is_reported_at_once():
    bad = sheets(
        sources=[{"SOURCE": "S", "TABLE": "DB.SC.T", "TS_COLUMN": "TS", "SCHEDULE": "weekly"},
                 {"SOURCE": "F", "TABLE": "DB.SC.T", "TS_COLUMN": "TS", "SCHEDULE": "hourly",
                  "ROW_FILTER": "1=1; drop table X"}],
        checks=[check(CHECK_TYPE="NOPE"), check(CHECK_TYPE="ACCEPTED_VALUES"), check(FIELD="B", MAX_BAD_PCT=150),
                check(SOURCE="MISSING"), check(CHECK_TYPE="BUSINESS_RULE", FIELD=None),
                check(CHECK_TYPE="MATCHES_SOURCE", FIELD="C", KEY="ID"), check(FIELD="D"), check(FIELD="D")],
        stability=[{"SOURCE": "S", "FIELD": "X", "INDEX": "KS", "BENCHMARK_START": -30, "BENCHMARK_END": -60,
                    "WARN": 0.3, "FAIL": 0.2}])
    with pytest.raises(ConfigError) as err:
        parse(bad)
    text = str(err.value)
    for expected in ("SCHEDULE must be one of", "ROW_FILTER may only be a condition", "unknown CHECK_TYPE 'NOPE'",
                     "needs VALUES", "between 0 and 100", "SOURCE 'MISSING' is not a defined source",
                     "business rules are written in the rules section", "MATCHES_SOURCE needs COMPARE_SOURCE", "duplicate check",
                     "INDEX must be PSI", "BENCHMARK_START before BENCHMARK_END", "WARN must not be above FAIL"):
        assert expected in text, expected
