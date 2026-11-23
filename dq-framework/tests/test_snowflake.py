"""Against REAL Snowflake, with the sample table from sql/02_sample_table.sql. Run: make test-snowflake

Reads DQF_DB.DQF.UW_A1_RECORD and writes only to DQF_TEST_RESULT_TABLE, never the real results table.
Each planted problem must be caught by the right check, and by nothing else.
"""
import dataclasses
import os
from datetime import timedelta

import pandas as pd
import pytest

from conftest import CONFIG
from dqf.engine import Engine
from dqf.settings import Settings, load_dotenv
from dqf.snowflake import Snowflake

pytestmark = pytest.mark.snowflake
I, SC, D = "UW_A1_INPUT", "UW_A1_SCORED", "UW_A1_DAILY"


@pytest.fixture(scope="module")
def engine():
    load_dotenv()
    table = os.environ.get("DQF_TEST_RESULT_TABLE")
    if not table:
        pytest.fail("Set DQF_TEST_RESULT_TABLE in .env")
    return Engine(dataclasses.replace(Settings.from_env(), result_table=table, alert_channels=(),
                                      config_uri=str(CONFIG)))


@pytest.fixture(scope="module")
def data_until(engine):
    with engine.snowflake.connect() as conn:
        value = conn.cursor().execute("select DATA_UNTIL from DQF_DB.DQF.DQF_SAMPLE_META").fetchone()[0]
    return pd.Timestamp(value).to_pydatetime()


def test_login(engine):
    assert Snowflake(engine.settings).whoami()["user"]


def not_passing(summary) -> dict:
    r = summary.results
    return dict(zip(r.CHECK_ID[r.STATUS != "PASS"], r.STATUS[r.STATUS != "PASS"]))


@pytest.mark.parametrize("hours_ago, expected", [
    (20, {}),
    (1, {f"{I}|AVG_TICKET|MISSING_VALUES": "FAIL"}),
    (2, {f"{I}||SSN_TAX_ID_MATCH_DERIVED": "FAIL"}),
    (3, {f"{I}|SSN_TAX_ID_MATCH|ACCEPTED_VALUES": "FAIL", f"{I}||SSN_TAX_ID_MATCH_DERIVED": "FAIL"}),
    (4, {f"{I}|BUREAU_INQUIRY_STATUS|ACCEPTED_VALUES": "FAIL"}),
    (5, {f"{I}|ZIP|EXPECTED_FORMAT": "FAIL"}),
    (6, {f"{I}|APPLICATION_ID|DUPLICATE_VALUES": "FAIL"}),
    (7, {f"{I}|MERCH_ID|VALUE_LENGTH": "FAIL"}),
    (10, {f"{I}||VOLUME": "FAIL"}),
    (9, {f"{I}||VOLUME": "FAIL", f"{I}||FRESHNESS": "FAIL"}),
    (11, {f"{SC}||SCORES_IN_RANGE": "FAIL"}),
    (12, {f"{SC}||DECISION_MATCHES_SCORE": "FAIL"}),
])
def test_hourly_planted_problems(engine, data_until, hours_ago, expected):
    end = data_until - timedelta(hours=hours_ago - 1)
    assert not_passing(engine.run("hourly", end, send_alerts=False)) == expected


def test_daily_volume_and_drift(engine, data_until):
    summary = engine.run("daily", data_until, send_alerts=False)
    assert not_passing(summary) == {
        f"{D}|MF_SCORE|PSI": "FAIL", f"{D}|AVG_TICKET|CSI": "FAIL", f"{D}|CHANNEL|CSI": "FAIL",
        f"{D}|TXN_PER_DAY_PERCENTILE|CSI": "WARN"}
