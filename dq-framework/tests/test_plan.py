"""How workbook rows become GX expectations and Snowflake SQL. No database needed."""
from datetime import datetime

import great_expectations as gx
import pandas as pd
import pytest

from conftest import CONFIG
from dqf import sql
from dqf.plan import BUILDERS, criteria_text, plan, windows
from dqf.settings import Settings
from dqf.workbook import Source, load_config, parse

END = datetime(2026, 10, 17, 11, 0)


def test_windows_hourly_and_daily():
    assert windows("hourly", datetime(2026, 10, 17, 11, 25)) == (datetime(2026, 10, 17, 10), datetime(2026, 10, 17, 11))
    assert windows("daily", datetime(2026, 10, 17, 1, 30)) == (datetime(2026, 10, 16), datetime(2026, 10, 17))


def test_shipped_configuration_plans_one_query_per_kind():
    config = load_config(str(CONFIG))
    hourly = plan(config.for_schedule("hourly"), datetime(2026, 10, 17, 10), END, Settings())
    # input window (16 checks + the SSN rule), scored window (2 checks + rule parts 1 + 2 + 3), freshness
    assert [len(g.items) for g in hourly] == [17, 8, 1]
    assert "(EXCLUSION_REASON is null)" in hourly[1].query and "EXCLUSION_REASON" not in hourly[0].query
    daily = plan(config.for_schedule("daily"), datetime(2026, 10, 16), datetime(2026, 10, 17), Settings())
    assert len(daily) == 1 + 1 + 6                               # volume window, freshness, one per stability row


def test_sql_window_freshness_and_filter():
    s = Source("S", "DB.SC.T", "TS", "hourly", "EXCLUSION_REASON is null")
    assert sql.window_query(s, datetime(2026, 10, 17, 10), END) == (
        "select * from DB.SC.T where TS >= '2026-10-17 10:00:00'::timestamp_ntz and TS < '2026-10-17 11:00:00'::timestamp_ntz"
        " and (EXCLUSION_REASON is null)")
    q = sql.freshness_query(s, END)
    assert "datediff('minute', max(TS)::timestamp_ntz, '2026-10-17 11:00:00'::timestamp_ntz)" in q and "999999" in q


def test_sql_stability_numeric_and_categorical():
    s = Source("S", "DB.SC.T", "TS", "daily", None)
    row = {"FIELD": "SCORE", "BINS": 4, "FIELD_TYPE": "NUMERIC", "BENCHMARK_START": -60, "BENCHMARK_END": -30}
    q = sql.stability_query(s, row, datetime(2026, 10, 10), datetime(2026, 10, 17))
    assert "TS >= '2026-08-18'::timestamp_ntz and TS < '2026-09-17'::timestamp_ntz" in q     # relative benchmark
    assert "percentile_cont(0.250000) within group (order by V) as E1" in q and "as E3" in q and "E4" not in q
    assert "iff(V > e.E1, 1, 0) + iff(V > e.E2, 1, 0) + iff(V > e.E3, 1, 0)" in q
    assert "ln(A_SHARE / B_SHARE)" in q and "999999" in q
    assert q.lower().startswith("select ")                    # GX accepts only queries starting with SELECT
    q = sql.stability_query(s, {**row, "FIELD_TYPE": "CATEGORICAL"}, datetime(2026, 10, 10), datetime(2026, 10, 17))
    assert "order by count(*) desc limit 3" in q and "'~OTHER'" in q and "'~MISSING'" in q


def test_sql_comparison_counts_each_row_once():
    p, r = Source("P", "DB.S.P", "TS", "hourly"), Source("R", "DB.S.R", "TS", "hourly", "OK = 1")
    rows = [{"CHECK_TYPE": "SOURCE_COVERAGE", "FIELD": None, "COMPARE_FIELD": None},
            {"CHECK_TYPE": "MATCHES_SOURCE", "FIELD": "AMT", "COMPARE_FIELD": "AMOUNT", "COMPARE": "NUMERIC", "TOLERANCE": 0.01},
            {"CHECK_TYPE": "LOST_IN_MAPPING", "FIELD": "SCORE", "COMPARE_FIELD": "SCORE"}]
    q, columns = sql.compare_query(p, r, "ID", "APP_ID", rows, datetime(2026, 10, 17, 10), END)
    assert columns == ["C0", "C1", "C2"]
    assert "case when s.DQF_KEY is null then 1 else 0 end as C0" in q
    assert "abs(try_to_double(p.AMT::varchar) - try_to_double(s.AMOUNT::varchar)) > 0.01" in q
    assert "from DB.S.R where OK = 1 group by APP_ID) s on p.ID = s.DQF_KEY" in q


@pytest.fixture(scope="module")
def validate():
    context = gx.get_context(mode="ephemeral")
    definition = context.data_sources.add_pandas("p").add_dataframe_asset("a").add_batch_definition_whole_dataframe("b")

    def run(frame, **row):
        config = parse({"SOURCES": pd.DataFrame([{"SOURCE": "S", "TABLE": "T", "TS_COLUMN": "TS", "SCHEDULE": "hourly"}]),
                        "CHECKS": pd.DataFrame([{"SOURCE": "S", **row}])})
        check = config.checks[0]
        return definition.get_batch(batch_parameters={"dataframe": frame}).validate(BUILDERS[check["CHECK_TYPE"]](check, Settings()))
    return run


def test_limits_mean_what_the_workbook_says(validate):
    """The mapping from a workbook row to a gallery expectation, run on in-memory data."""
    frame = pd.DataFrame({"A": [1, 2, None, None, 5, 6, 7, 8, 9, 10], "C": ["01", "02", "01", "Y", "1", "0", "Z", "1", "0", "Z"]})
    assert validate(frame, FIELD="A", CHECK_TYPE="MISSING_VALUES", MAX_BAD_PCT=20).success      # 20% missing
    assert not validate(frame, FIELD="A", CHECK_TYPE="MISSING_VALUES", MAX_BAD_PCT=19).success
    assert not validate(frame, FIELD="C", CHECK_TYPE="ACCEPTED_VALUES", VALUES="'1','0','Z'").success
    assert validate(frame, FIELD="C", CHECK_TYPE="ACCEPTED_VALUES", VALUES="'1','0','Z','01','02','Y'").success
    assert validate(frame, CHECK_TYPE="VOLUME", MIN=10).success and not validate(frame, CHECK_TYPE="VOLUME", MIN=11).success


def test_criteria_text():
    config = load_config(str(CONFIG))
    mf = next(r for r in config.stability if r["FIELD"] == "MF_SCORE")
    assert criteria_text(mf) == "PSI below 0.10 (warn) / 0.25 (fail); last 7 days vs days -60 to -30"
    fresh = next(c for c in config.checks if c["CHECK_TYPE"] == "FRESHNESS" and c["SOURCE"] == "UW_A1_INPUT")
    assert criteria_text(fresh) == "newest row at most 2 hours old"
