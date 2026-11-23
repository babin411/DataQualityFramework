"""The Snowflake SQL behind the checks. Every query here is read-only.

Names come from the workbook, which only accepts plain identifiers. A source's ROW_FILTER is a
person-written condition, checked by the workbook reader and run with a read-only role.
"""
from __future__ import annotations

from datetime import datetime, timedelta

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
NO_ROWS = 999999          # "hours since the newest row" when there are no rows at all


def ts(value: datetime) -> str:
    return value.strftime(TS_FORMAT)


def _where(source, start: datetime | None, end: datetime) -> str:
    parts = []
    if start is not None:
        parts.append(f"{source.ts_column} >= '{ts(start)}'::timestamp_ntz")
    parts.append(f"{source.ts_column} < '{ts(end)}'::timestamp_ntz")
    if source.row_filter:
        parts.append(f"({source.row_filter})")
    return " and ".join(parts)


def window_query(source, start: datetime, end: datetime) -> str:
    """The rows of one source in one window."""
    return f"select * from {source.table} where {_where(source, start, end)}"


def freshness_query(source, end: datetime) -> str:
    """Hours between the newest row and the end of the window, over the whole table."""
    return (
        f"select coalesce(datediff('minute', max({source.ts_column})::timestamp_ntz, '{ts(end)}'::timestamp_ntz)"
        f" / 60.0, {NO_ROWS}) as HOURS_SINCE_LAST from {source.table} where {_where(source, None, end)}"
    )


def _text(expr: str) -> str:
    return f"upper(trim({expr}::varchar))"


def _differs(mode: str, left: str, right: str, tolerance: float) -> str:
    if mode == "NUMERIC":
        return f"abs(try_to_double({left}::varchar) - try_to_double({right}::varchar)) > {tolerance}"
    if mode == "TEXT":
        return f"{_text(left)} <> {_text(right)}"
    return f"{left}::varchar <> {right}::varchar"


def compare_query(source, other, key: str, other_key: str, checks: list[dict],
                  start: datetime, end: datetime) -> tuple[str, list[str]]:
    """Join one window of `source` to all of `other` on the key, with a 0/1 column per check.

    The three kinds never count the same row twice:
      SOURCE_COVERAGE   the row has no partner in the other table
      MATCHES_SOURCE    both sides have a value and the values differ
      LOST_IN_MAPPING   the other side has a value and this side is empty
    """
    other_fields = sorted({c["COMPARE_FIELD"] for c in checks if c["COMPARE_FIELD"]})
    flags, columns = [], []
    for i, c in enumerate(checks):
        mine, theirs = f"p.{c['FIELD']}", f"s.{c['COMPARE_FIELD']}"
        if c["CHECK_TYPE"] == "SOURCE_COVERAGE":
            condition = "s.DQF_KEY is null"
        elif c["CHECK_TYPE"] == "LOST_IN_MAPPING":
            condition = f"s.DQF_KEY is not null and {mine} is null and {theirs} is not null"
        else:
            differs = _differs(c["COMPARE"], mine, theirs, c["TOLERANCE"] or 0)
            condition = f"s.DQF_KEY is not null and {mine} is not null and {theirs} is not null and {differs}"
        flags.append(f"case when {condition} then 1 else 0 end as C{i}")
        columns.append(f"C{i}")
    picked = "".join(f", max({f}) as {f}" for f in other_fields)
    other_filter = f" where {other.row_filter}" if other.row_filter else ""
    query = (
        f"select {', '.join(flags)} from ({window_query(source, start, end)}) p "
        f"left join (select {other_key} as DQF_KEY{picked} from {other.table}{other_filter} group by {other_key}) s "
        f"on p.{key} = s.DQF_KEY"
    )
    return query, columns


def _day(value, end: datetime) -> str:
    """A benchmark boundary: a fixed date, or a number of days relative to the end of the window."""
    if isinstance(value, int):
        value = (end + timedelta(days=value)).date()
    return f"'{value:%Y-%m-%d}'::timestamp_ntz"


def stability_query(source, row: dict, active_start: datetime, active_end: datetime) -> str:
    """One row, one column STABILITY_INDEX: the PSI / CSI of one field, active period against benchmark.

    Numeric fields are cut into BINS bins at the benchmark's quantiles; categorical fields use the
    benchmark's most common values plus OTHER. Missing values are a bin of their own. A share of zero
    is floored at 0.0001 so the logarithm is defined.

        index = sum over bins of (active_share - benchmark_share) * ln(active_share / benchmark_share)
    """
    f, t, n = row["FIELD"], source.ts_column, row["BINS"]
    filt = f" and ({source.row_filter})" if source.row_filter else ""
    bench = (f"select {f} as V from {source.table} where {t} >= {_day(row['BENCHMARK_START'], active_end)} "
             f"and {t} < {_day(row['BENCHMARK_END'], active_end)}{filt}")
    active = (f"select {f} as V from {source.table} where {t} >= '{ts(active_start)}'::timestamp_ntz "
              f"and {t} < '{ts(active_end)}'::timestamp_ntz{filt}")
    if row["FIELD_TYPE"] == "NUMERIC":
        quantiles = ", ".join(f"percentile_cont({k / n:.6f}) within group (order by V) as E{k}" for k in range(1, n))
        bucket = " + ".join(f"iff(V > e.E{k}, 1, 0)" for k in range(1, n))
        edges = f"edges as (select {quantiles} from bench)"
        binned = (f"binned as (select 'B' as SIDE, iff(V is null, -1, {bucket}) as BIN from bench, edges e "
                  f"union all select 'A', iff(V is null, -1, {bucket}) from active, edges e)")
    else:
        edges = (f"edges as (select V as CAT from bench where V is not null group by V "
                 f"order by count(*) desc limit {n - 1})")
        binned = ("binned as (select 'B' as SIDE, case when V is null then '~MISSING' "
                  "when V::varchar in (select CAT::varchar from edges) then V::varchar else '~OTHER' end as BIN from bench "
                  "union all select 'A', case when V is null then '~MISSING' "
                  "when V::varchar in (select CAT::varchar from edges) then V::varchar else '~OTHER' end from active)")
    # Wrapped in an outer SELECT: Great Expectations accepts only queries that start with SELECT.
    return (
        f"select STABILITY_INDEX from (with bench as ({bench}), active as ({active}), {edges}, {binned}, "
        "shares as (select BIN, "
        "greatest(coalesce(count_if(SIDE = 'B') / nullif(sum(count_if(SIDE = 'B')) over (), 0), 0), 0.0001) as B_SHARE, "
        "greatest(coalesce(count_if(SIDE = 'A') / nullif(sum(count_if(SIDE = 'A')) over (), 0), 0), 0.0001) as A_SHARE "
        "from binned group by BIN) "
        # No rows on either side cannot be judged: report the NO_ROWS marker, which fails the check.
        f"select iff((select count(*) from bench) = 0 or (select count(*) from active) = 0, {NO_ROWS}, "
        "sum((A_SHARE - B_SHARE) * ln(A_SHARE / B_SHARE))) as STABILITY_INDEX from shares)"
    )
