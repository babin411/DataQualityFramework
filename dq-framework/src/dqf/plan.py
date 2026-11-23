"""Turn the workbook's rows into Great Expectations expectations, grouped by the query they run on.

    window      one window of one source             -> the standard checks and the business rules
    freshness   the newest row of the whole source   -> FRESHNESS
    compare     one window joined to another source  -> SOURCE_COVERAGE, MATCHES_SOURCE, LOST_IN_MAPPING
    stability   active period against benchmark      -> PSI / CSI

Each group is one query and one GX validation, so one Snowflake login. Every expectation is a
standard one from the GX Expectation Gallery. A business rule becomes one gallery expectation per
`then` field, restricted by its `when` (a GX row condition), or a named custom expectation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import great_expectations as gx

from dqf import sql
from dqf import rules
from dqf.workbook import COMPARISONS, Config, parse_values

E = gx.expectations


def mostly(row) -> float:
    """MAX_BAD_PCT is the share of rows allowed to break the rule; GX wants the share that must pass."""
    return 1 - (row["MAX_BAD_PCT"] or 0) / 100


BUILDERS = {
    "VOLUME": lambda r, s: E.ExpectTableRowCountToBeBetween(min_value=r["MIN"], max_value=r["MAX"]),
    "MISSING_VALUES": lambda r, s: E.ExpectColumnValuesToNotBeNull(column=r["FIELD"], mostly=mostly(r)),
    "FIELD_POPULATION": lambda r, s: E.ExpectColumnValuesToNotBeNull(column=r["FIELD"], mostly=mostly(r)),
    "ACCEPTABLE_RANGE": lambda r, s: E.ExpectColumnValuesToBeBetween(
        column=r["FIELD"], min_value=r["MIN"], max_value=r["MAX"], mostly=mostly(r)),
    "ACCEPTED_VALUES": lambda r, s: E.ExpectColumnValuesToBeInSet(
        column=r["FIELD"], value_set=parse_values(r["VALUES"]), mostly=mostly(r)),
    "EXPECTED_FORMAT": lambda r, s: E.ExpectColumnValuesToMatchRegex(
        column=r["FIELD"], regex=r["REGEX"], mostly=mostly(r)),
    "DUPLICATE_VALUES": lambda r, s: E.ExpectColumnValuesToBeUnique(column=r["FIELD"], mostly=mostly(r)),
    "VALUE_LENGTH": lambda r, s: E.ExpectColumnValueLengthsToBeBetween(
        column=r["FIELD"], min_value=r["MIN"], max_value=r["MAX"], mostly=mostly(r)),
    "PLACEHOLDER_VALUE": lambda r, s: E.ExpectColumnValuesToNotBeInSet(
        column=r["FIELD"], value_set=parse_values(r["VALUES"]), mostly=mostly(r)),
}

# What the stored number means. Everything else is "bad_pct".
METRIC = {"VOLUME": "row_count", "FRESHNESS": "hours_since_last",
          "PSI": "stability_index", "CSI": "stability_index"}


@dataclass
class Group:
    """One query and the checks evaluated on its result."""
    query: str
    items: list[tuple[dict, object]] = field(default_factory=list)    # (row, expectation)
    name: str = ""                                                     # for GX's report: the suite name


def windows(schedule: str, end: datetime) -> tuple[datetime, datetime]:
    """The window a scheduled run checks: the previous full hour, or the previous full day."""
    if schedule == "daily":
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return end - timedelta(days=1), end
    end = end.replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=1), end


def plan(config: Config, start: datetime, end: datetime, settings) -> list[Group]:
    window_groups: dict[str, Group] = {}
    compare: dict[tuple, list[dict]] = {}
    groups: list[Group] = []

    for check in config.checks:
        source = config.sources[check["SOURCE"]]
        ctype = check["CHECK_TYPE"]
        if ctype == "FRESHNESS":
            groups.append(Group(sql.freshness_query(source, end), [
                (check, E.ExpectColumnMaxToBeBetween(column="HOURS_SINCE_LAST", max_value=check["MAX"]))],
                f"{source.name}.freshness"))
        elif ctype in COMPARISONS:
            key = (check["SOURCE"], check["COMPARE_SOURCE"], check["KEY"], check["COMPARE_KEY"])
            compare.setdefault(key, []).append(check)
        else:
            group = window_groups.setdefault(source.name, Group(sql.window_query(source, start, end), name=f"{source.name}.checks"))
            if ctype == "BUSINESS_RULE":                    # one expectation per part, one result per rule
                for part, expectation in rules.build(config.rules[check["RULE_NAME"]]):
                    expectation.meta = {"part": part}
                    group.items.append((check, expectation))
            else:
                group.items.append((check, BUILDERS[ctype](check, settings)))

    for (name, other, key, other_key), rows in compare.items():
        query, columns = sql.compare_query(config.sources[name], config.sources[other], key, other_key,
                                           rows, start, end)
        groups.append(Group(query, [
            (row, E.ExpectColumnValuesToBeInSet(column=column, value_set=[0], mostly=mostly(row)))
            for row, column in zip(rows, columns)], f"{name}.compared_with_{other}"))

    for row in config.stability:
        source = config.sources[row["SOURCE"]]
        active_start = end - timedelta(days=row["ACTIVE_DAYS"])
        groups.append(Group(sql.stability_query(source, row, active_start, end), [
            (row, E.ExpectColumnMaxToBeBetween(column="STABILITY_INDEX", max_value=row["FAIL"]))],
            f"{source.name}.drift_{row['FIELD']}"))

    return [*window_groups.values(), *groups]


def check_type(row: dict) -> str:
    return row.get("CHECK_TYPE") or row["INDEX"]


def metric_name(row: dict) -> str:
    return METRIC.get(check_type(row), "bad_pct")


def criteria_text(row: dict, config_rules: dict | None = None) -> str:
    """The limit as short text, for the report and the alert."""
    ctype = check_type(row)
    if ctype == "BUSINESS_RULE" and config_rules and row["RULE_NAME"] in config_rules:
        from dqf.expectations import CHECK_TEXTS

        allowed = f"; at most {_num(row['MAX_BAD_PCT'])}% bad rows" if row.get("MAX_BAD_PCT") else ""
        return rules.text(config_rules[row["RULE_NAME"]], CHECK_TEXTS) + allowed
    if ctype in ("PSI", "CSI"):
        bench = (f"days {row['BENCHMARK_START']} to {row['BENCHMARK_END']}" if isinstance(row["BENCHMARK_START"], int)
                 else f"{row['BENCHMARK_START']} to {row['BENCHMARK_END']}")
        return (f"{ctype} below {_num(row['WARN'])} (warn) / {_num(row['FAIL'])} (fail); "
                f"last {row['ACTIVE_DAYS']} days vs {bench}")
    if ctype == "FRESHNESS":
        return f"newest row at most {_num(row['MAX'])} hours old"
    parts = []
    if ctype == "SOURCE_COVERAGE":
        parts.append(f"has a row in {row['COMPARE_SOURCE']} (key {row['KEY']})")
    elif ctype == "MATCHES_SOURCE":
        mode = row["COMPARE"].lower() + (f" ±{_num(row['TOLERANCE'])}" if row["TOLERANCE"] else "")
        parts.append(f"same as {row['COMPARE_SOURCE']}.{row['COMPARE_FIELD']} ({mode})")
    elif ctype == "LOST_IN_MAPPING":
        parts.append(f"not empty when {row['COMPARE_SOURCE']}.{row['COMPARE_FIELD']} has a value")
    if row.get("RULE_NAME"):
        parts.append(f"rule {row['RULE_NAME']}")
    for col, label in (("MIN", "min"), ("MAX", "max")):
        if row.get(col) is not None:
            parts.append(f"{label} {_num(row[col])}")
    if row.get("VALUES") is not None:
        parts.append(f"values {row['VALUES']}")
    if row.get("REGEX") is not None:
        parts.append(f"format {row['REGEX']}")
    if row.get("MAX_BAD_PCT") is not None:
        parts.append(f"at most {_num(row['MAX_BAD_PCT'])}% bad rows")
    return ", ".join(parts)


def _num(value) -> str:
    value = float(value)
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
