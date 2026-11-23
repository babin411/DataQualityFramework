"""The report (app/streamlit_app.py) end to end with Streamlit's AppTest, reading made-up results
through a fake Snowflake connector: every tab renders, the cards add up, and clicks set the filters."""
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dqf.snowflake import ADDED_COLUMNS, RESULT_COLUMNS

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
# (check id, source, field, check type, rule, metric)
HOURLY = [("c1", "UW_A1_INPUT", "AVG_TICKET", "MISSING_VALUES", "", "bad_pct"),
          ("c2", "UW_A1_INPUT", "ZIP", "EXPECTED_FORMAT", "", "bad_pct"),
          ("c3", "UW_A1_INPUT", "MERCH_ID", "VALUE_LENGTH", "", "bad_pct"),
          ("c4", "UW_A1_INPUT", "", "VOLUME", "", "row_count"),
          ("c5", "UW_A1_SCORED", "", "BUSINESS_RULE", "DECISION_MATCHES_SCORE", "bad_pct"),
          ("c6", "UW_A1_SCORED", "MF_SCORE", "MISSING_VALUES", "", "bad_pct")]
DAILY = [("d1", "UW_A1_DAILY", "", "VOLUME", "", "row_count"),
         ("d2", "UW_A1_DAILY", "MF_SCORE", "PSI", "", "stability_index")]
# The latest hour: c1 fails and was alerted, c5 fails, c3 could not run.
LATEST = {"c1": "FAIL", "c5": "FAIL", "c3": "ERROR"}


def made_up_results(old_format: bool = False) -> pd.DataFrame:
    last = datetime.now().replace(minute=0, second=0, microsecond=0)
    rows = []
    for schedule, checks, step, periods in (("hourly", HOURLY, timedelta(hours=1), 3),
                                            ("daily", DAILY, timedelta(days=1), 2)):
        for k in range(periods):
            end = last - k * step
            for cid, source, field, ctype, rule, metric in checks:
                status = LATEST.get(cid, "PASS") if k == 0 and schedule == "hourly" else "PASS"
                if schedule == "daily" and k == 0 and cid == "d2":
                    status = "WARN"
                bad = status == "FAIL"
                rows.append({
                    "RUN_ID": f"{schedule}{k}", "RUN_TS": end + timedelta(minutes=5), "SCHEDULE": schedule,
                    "WINDOW_START": end - step, "WINDOW_END": end, "CHECK_ID": cid, "SOURCE": source,
                    "TABLE_NAME": "DQF_DB.DQF.UW_A1_RECORD", "FIELD_NAME": field, "CHECK_TYPE": ctype,
                    "RULE_NAME": rule, "CRITERIA": "at most 30% bad rows", "STATUS": status, "METRIC": metric,
                    "OBSERVED_VALUE": None if status == "ERROR" else 56.7 if bad else 0.15 if status == "WARN" else 0.0,
                    "ELEMENT_COUNT": 30, "UNEXPECTED_COUNT": 17 if bad else 0, "ALERT_FLAG": "Yes",
                    "DETAILS": '{"error": "boom"}' if status == "ERROR" else "{}",
                    "EXPLANATION": f"{field or rule} is wrong in 56.7% of rows" if status != "PASS" else "",
                    "INVESTIGATE_SQL": f"select * from DQF_DB.DQF.UW_A1_RECORD where {field or 'true'} limit 100;",
                    "ALERT_SENT": "Yes" if (k == 0 and cid == "c1" and schedule == "hourly") else "No",
                })
    frame = pd.DataFrame(rows)[list(RESULT_COLUMNS)]
    return frame.drop(columns=list(ADDED_COLUMNS)) if old_format else frame


class FakeCursor:
    def __init__(self, frame):
        self.frame = frame

    def execute(self, query):
        self.description = [(c,) for c in self.frame.columns]
        self._rows = list(self.frame.itertuples(index=False, name=None))
        return self

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, frame):
        self.frame = frame

    def cursor(self):
        return FakeCursor(self.frame)

    def close(self):
        pass


def start(monkeypatch, frame) -> AppTest:
    import snowflake.connector
    import streamlit as st

    monkeypatch.setattr(snowflake.connector, "connect", lambda **kw: FakeConnection(frame))
    st.cache_data.clear()                               # the cache outlives an AppTest: start each test fresh
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


@pytest.fixture
def app(monkeypatch):
    return start(monkeypatch, made_up_results())


def cards(app) -> str:
    return " ".join(m.value for m in app.markdown)


def has_card(app, label: str, value: int) -> bool:
    return f"<div class='label'>{label}</div><div class='value'>{value:,}</div>" in cards(app)


def test_every_tab_renders_and_the_cards_add_up(app):
    assert [t.label for t in app.tabs] == ["Executive Summary", "Types of Checks", "All Check Results", "Fail Details"]
    for label, value in [("Total no. of checks", 6), ("Total passed", 3), ("Warnings", 0), ("Total failed", 2),
                         ("Could not run", 1), ("Total alerts sent", 1)]:
        assert has_card(app, label, value), label
    assert "5 record · 1 file level" in cards(app)
    assert len(app.expander) == 3                       # one per check not passing
    assert any("DECISION_MATCHES_SCORE" in e.label for e in app.expander)   # a rule shows under its name
    assert any("select * from DQF_DB.DQF.UW_A1_RECORD where AVG_TICKET" in c.value for c in app.code)


def test_an_older_window_is_clean(app):
    app.sidebar.selectbox[0].select_index(2).run()      # the oldest of the three hours
    assert not app.exception
    assert any("Nothing failed in this window." in s.value for s in app.success)


def test_the_daily_schedule_shows_a_warning(app):
    app.sidebar.radio[0].set_value("daily").run()
    assert not app.exception, app.exception
    assert has_card(app, "Total no. of checks", 2) and has_card(app, "Warnings", 1)
    assert "0 record · 2 file level" in cards(app)


def test_filters_narrow_every_tab(app):
    """What a click on a chart does: it sets a sidebar filter (here set directly), and the tabs follow."""
    app.session_state["f_field"] = ["AVG_TICKET"]
    app.run()
    assert not app.exception, app.exception
    assert len(app.expander) == 1 and " · AVG_TICKET · " in app.expander[0].label
    assert any("Filtered by Field: AVG_TICKET" in i.value for i in app.info)
    app.sidebar.button[0].click().run()                 # Clear filters
    assert len(app.expander) == 3 and not any("Filtered by" in i.value for i in app.info)


def click(app, chart: str, point: dict):
    """What Streamlit keeps under a chart's key after a click on a mark."""
    key = f"{chart}_{app.session_state['gen_' + chart] if 'gen_' + chart in app.session_state else 0}"
    app.session_state[key] = {"selection": {"pick": [point]}}
    app.run()
    assert not app.exception, app.exception


def test_clicking_a_slice_adds_and_removes_it(app):
    click(app, "click_status", {"STATUS_LABEL": "Passed"})
    assert app.session_state["f_status"] == ["Passed"]
    click(app, "click_status", {"STATUS_LABEL": "Failed"})          # a second result: both are shown
    assert app.session_state["f_status"] == ["Passed", "Failed"]
    click(app, "click_status", {"STATUS_LABEL": "Passed"})          # the same slice again: taken out
    assert app.session_state["f_status"] == ["Failed"]


def test_a_heatmap_click_opens_the_window_and_the_field(app):
    oldest = app.sidebar.selectbox[0].options[-1]
    click(app, "click_heat", {"FIELD_NAME": "ZIP", "WINDOW_LABEL": oldest})
    assert app.sidebar.selectbox[0].value == oldest
    assert app.session_state["f_field"] == ["ZIP"]


def test_results_stored_before_the_new_columns_still_show(monkeypatch):
    app = start(monkeypatch, made_up_results(old_format=True))
    assert has_card(app, "Total failed", 2) and has_card(app, "Total alerts sent", 0)
    assert "not recorded for this run" in cards(app)
    assert any("No explanation stored" in m.value for m in app.markdown)
