"""The checks bring back counts, never the monitored rows."""
import sqlalchemy as sa
from great_expectations.expectations.metrics import util

from dqf import engine, snowflake


def test_gx_reads_column_names_without_rows(monkeypatch):
    seen = []
    monkeypatch.setattr(util, "column_reflection_fallback", lambda sel, *a, **k: seen.append(sel) or [])
    snowflake.column_names_without_rows()
    snowflake.column_names_without_rows()                    # applying twice wraps only once
    util.column_reflection_fallback(sa.text("select * from T where X > 1"), None, None)
    assert str(seen[-1]) == "select * from (select * from T where X > 1) as dqf_columns limit 0"
    table = sa.table("T")
    util.column_reflection_fallback(table, None, None)
    assert seen[-1] is table                                 # only custom queries are changed


def test_no_example_values_are_fetched():
    assert engine.RESULT_FORMAT == {"result_format": "SUMMARY", "partial_unexpected_count": 0}
