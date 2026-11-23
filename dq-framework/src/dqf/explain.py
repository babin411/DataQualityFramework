"""Plain-language explanations of check results, and a SQL query to look at the rows behind them.

Used by the alerts, so a person reading the email knows what is wrong, how bad it is, and where to
look, without knowing how the framework works.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from dqf import sql
from dqf.sql import NO_ROWS, ts


def _n(value) -> str:
    value = float(value)
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"


def _pct(row) -> str:
    return f"{row['OBSERVED_VALUE']:.1f}%" if row.get("OBSERVED_VALUE") is not None else "an unknown share"


def _of(row) -> str:
    bad, total = row.get("UNEXPECTED_COUNT"), row.get("ELEMENT_COUNT")
    return f" ({int(bad):,} of {int(total):,} rows)" if bad is not None and total else ""


def explain(check: dict, result: dict, rule=None, parts: list | None = None) -> str:
    """One or two sentences: what is wrong, how much, and the limit."""
    ctype, field = result["CHECK_TYPE"], check.get("FIELD")
    status, value = result["STATUS"], result.get("OBSERVED_VALUE")
    allowed = f"at most {_n(check['MAX_BAD_PCT'])}% allowed" if check.get("MAX_BAD_PCT") is not None else "none allowed"
    if status == "ERROR":
        return "The check could not run, so this data was not checked. Usually a table or column name is wrong, or access is missing."
    if value is not None and value >= NO_ROWS:
        return "There are no rows at all to judge, so this check fails."
    if ctype == "VOLUME":
        expected = f"at least {_n(check['MIN'])}" if check.get("MIN") is not None else f"at most {_n(check['MAX'])}"
        return f"{_n(value or 0)} rows arrived in this window; {expected} expected. Data may have stopped arriving."
    if ctype == "FRESHNESS":
        return (f"The newest row is {value:.1f} hours old; the limit is {_n(check['MAX'])} hours. "
                "Data has probably stopped arriving or loading.")
    if ctype in ("MISSING_VALUES", "FIELD_POPULATION"):
        return f"{field} is empty in {_pct(result)} of rows{_of(result)}; {allowed}."
    if ctype == "ACCEPTABLE_RANGE":
        bounds = " to ".join(_n(b) for b in (check.get("MIN"), check.get("MAX")) if b is not None)
        return f"{field} is outside the range {bounds} in {_pct(result)} of rows{_of(result)}; {allowed}."
    if ctype == "ACCEPTED_VALUES":
        return (f"{field} has a value other than {check['VALUES']} in {_pct(result)} of rows{_of(result)}; {allowed}. "
                "For a source status field this means the source did not return data for those applications.")
    if ctype == "PLACEHOLDER_VALUE":
        return f"{field} holds a placeholder ({check['VALUES']}) instead of real data in {_pct(result)} of rows{_of(result)}; {allowed}."
    if ctype == "EXPECTED_FORMAT":
        return f"{field} does not match the format {check['REGEX']} in {_pct(result)} of rows{_of(result)}; {allowed}."
    if ctype == "VALUE_LENGTH":
        return (f"{field} has a length outside {_n(check['MIN'])} to {_n(check['MAX'])} characters in "
                f"{_pct(result)} of rows{_of(result)}; {allowed}.")
    if ctype == "DUPLICATE_VALUES":
        return f"{_pct(result)} of rows{_of(result)} share their {field} with another row; {allowed}. The key should be unique."
    if ctype == "BUSINESS_RULE":
        return _rule_sentence(check, result, rule, parts or [], allowed)
    if ctype == "SOURCE_COVERAGE":
        return (f"{_pct(result)} of rows{_of(result)} have no matching row in {check['COMPARE_SOURCE']} "
                f"(matched on {check['KEY']}); {allowed}. The data did not reach both places.")
    if ctype == "MATCHES_SOURCE":
        return (f"{field} differs from {check['COMPARE_SOURCE']}.{check['COMPARE_FIELD']} in {_pct(result)} of rows"
                f"{_of(result)}; {allowed}. Likely a mapping error.")
    if ctype == "LOST_IN_MAPPING":
        return (f"{field} is empty although {check['COMPARE_SOURCE']}.{check['COMPARE_FIELD']} has a value, in "
                f"{_pct(result)} of rows{_of(result)}; {allowed}. The value was lost on the way.")
    if ctype in ("PSI", "CSI"):
        kind = "score" if ctype == "PSI" else "feature"
        level = "a large shift" if value is not None and value >= check["FAIL"] else "a moderate shift"
        return (f"The distribution of the {kind} {field} over the last {check['ACTIVE_DAYS']} days has {level} from "
                f"the benchmark: {ctype} {value:.3f} (warn at {_n(check['WARN'])}, fail at {_n(check['FAIL'])}). "
                "The population or the data feeding the model has changed.")
    return f"{ctype} on {field or 'the table'}: {result['STATUS']}."


def _rule_sentence(check: dict, result: dict, rule, parts: list, allowed: str) -> str:
    from dqf import rules
    from dqf.expectations import CHECK_TEXTS

    if rule is None:
        return f"The rule {check['RULE_NAME']} is broken."
    scope = f"rows where {rules.when_text(rule)}" if rule.when else "rows"
    total = result.get("ELEMENT_COUNT")
    if result["STATUS"] in ("PASS", "WARN"):
        if rule.when and not total:
            return f"{rule.description}: no {scope} in this window, so nothing to check."
        return f"{rule.description}: holds."
    failing = [p["part"] for p in parts if p.get("status") == "FAIL"] or (
        [CHECK_TEXTS.get(rule.check, rule.check)] if rule.check else [rules.part_text(p) for p in rule.then])
    failing = [CHECK_TEXTS.get(p, p) for p in failing]
    how_many = f"{_pct(result)} of {scope}{_of(result)}"
    return f"{rule.description}: broken in {how_many}. Required: {'; '.join(failing)}. {allowed.capitalize()}."


def _window_where(source, start: datetime, end: datetime) -> str:
    where = f"{source.ts_column} >= '{ts(start)}' and {source.ts_column} < '{ts(end)}'"
    return where + (f" and ({source.row_filter})" if source.row_filter else "")


def _quote(text: str) -> str:
    return str(text).replace("'", "''")


def investigate_sql(check: dict, ctype: str, source, other, start: datetime, end: datetime,
                    rule=None) -> str:
    """A query to run in Snowsight that shows the rows (or numbers) behind the result."""
    t, f, where = source.table, check.get("FIELD"), _window_where(source, start, end)
    base = f"select * from {t}\nwhere {where}"
    if ctype == "VOLUME":
        since = ts(start - timedelta(days=1))
        return (f"select date_trunc('hour', {source.ts_column}) as HOUR, count(*) as ROWS_ARRIVED\nfrom {t}\n"
                f"where {source.ts_column} >= '{since}'" + (f" and ({source.row_filter})" if source.row_filter else "")
                + "\ngroup by 1 order by 1;")
    if ctype == "FRESHNESS":
        return f"select max({source.ts_column}) as NEWEST_ROW, current_timestamp() as NOW\nfrom {t};"
    if ctype in ("MISSING_VALUES", "FIELD_POPULATION"):
        return f"{base}\n  and {f} is null\nlimit 100;"
    if ctype == "ACCEPTABLE_RANGE":
        parts = [f"{f} < {check['MIN']}" if check.get("MIN") is not None else None,
                 f"{f} > {check['MAX']}" if check.get("MAX") is not None else None]
        return f"{base}\n  and ({' or '.join(p for p in parts if p)})\nlimit 100;"
    if ctype == "ACCEPTED_VALUES":
        return (f"select {f}, count(*) as ROWS_\nfrom {t}\nwhere {where}\n  and ({f} not in ({check['VALUES']}) or {f} is null)\n"
                "group by 1 order by 2 desc;")
    if ctype == "PLACEHOLDER_VALUE":
        return f"{base}\n  and {f} in ({check['VALUES']})\nlimit 100;"
    if ctype == "EXPECTED_FORMAT":
        return f"{base}\n  and not regexp_like({f}::varchar, '{_quote(check['REGEX'])}')\nlimit 100;"
    if ctype == "VALUE_LENGTH":
        return f"{base}\n  and length({f}::varchar) not between {_n(check['MIN'])} and {_n(check['MAX'])}\nlimit 100;"
    if ctype == "DUPLICATE_VALUES":
        return f"{base}\nqualify count(*) over (partition by {f}) > 1\norder by {f}\nlimit 100;"
    if ctype == "BUSINESS_RULE" and rule is not None:
        from dqf import rules

        return f"{base}\n  and {rules.breaks_sql(rule)}\nlimit 100;"
    if ctype in ("SOURCE_COVERAGE", "MATCHES_SOURCE", "LOST_IN_MAPPING") and other is not None:
        key, okey = check["KEY"], check["COMPARE_KEY"]
        join = f"select p.*{', s.' + check['COMPARE_FIELD'] + ' as SOURCE_VALUE' if check.get('COMPARE_FIELD') else ''}\n" \
               f"from ({sql.window_query(source, start, end)}) p\nleft join {other.table} s on p.{key} = s.{okey}\n"
        if ctype == "SOURCE_COVERAGE":
            return join + f"where s.{okey} is null\nlimit 100;"
        if ctype == "LOST_IN_MAPPING":
            return join + f"where p.{f} is null and s.{check['COMPARE_FIELD']} is not null\nlimit 100;"
        return join + f"where p.{f}::varchar <> s.{check['COMPARE_FIELD']}::varchar\nlimit 100;"
    if ctype in ("PSI", "CSI"):
        bench_start = check["BENCHMARK_START"]
        b0 = ts(end + timedelta(days=bench_start)) if isinstance(bench_start, int) else f"{bench_start} 00:00:00"
        b1 = ts(end + timedelta(days=check["BENCHMARK_END"])) if isinstance(check["BENCHMARK_END"], int) \
            else f"{check['BENCHMARK_END']} 00:00:00"
        a0 = ts(end - timedelta(days=check["ACTIVE_DAYS"]))
        filt = f" and ({source.row_filter})" if source.row_filter else ""
        period = (f"case when {source.ts_column} >= '{b0}' and {source.ts_column} < '{b1}' then '1 benchmark' "
                  f"when {source.ts_column} >= '{a0}' and {source.ts_column} < '{ts(end)}' then '2 recent' end")
        if check["FIELD_TYPE"] == "CATEGORICAL":
            return (f"select {period} as PERIOD, {f} as VALUE, count(*) as ROWS_,\n"
                    f"       round(100 * ratio_to_report(count(*)) over (partition by {period}), 1) as PCT\n"
                    f"from {t}\nwhere PERIOD is not null{filt}\ngroup by 1, 2 order by 2, 1;")
        return (f"select {period} as PERIOD, count(*) as ROWS_, round(avg({f}), 2) as MEAN,\n"
                f"       percentile_cont(0.1) within group (order by {f}) as P10, median({f}) as MEDIAN,\n"
                f"       percentile_cont(0.9) within group (order by {f}) as P90, count_if({f} is null) as MISSING\n"
                f"from {t}\nwhere PERIOD is not null{filt}\ngroup by 1 order by 1;")
    return base + "\nlimit 100;"
