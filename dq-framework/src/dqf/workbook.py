"""Read and validate the configuration: YAML (the main format), an Excel workbook, or a folder of CSVs.

    config/dq_config.yaml        the main format: one block per source with its checks, plus rules
    config/dq_config.xlsx        the same content as sheets: Sources, Checks, Stability, Rules
    config/sheets/               the same content as CSVs: sources.csv, checks.csv, stability.csv, rules.csv

A YAML or Excel file can be local or in S3 (s3://bucket/key). Every format is read into the same rows
and validated the same way, so the rest of the framework does not know which one was used. The YAML
keys are the column names below in lower case. scripts/build_config.py converts between formats.

The sheets (or YAML sections). Column names are case-insensitive; empty cells mean "not set".

Sources    one row per table to monitor
    SOURCE        short name used by the other sheets
    TABLE         DB.SCHEMA.TABLE (or a view)
    TS_COLUMN     timestamp or date column that places a row in time
    SCHEDULE      hourly | daily: how often its checks run, and the window each run looks at
    ROW_FILTER    optional SQL condition, e.g. the scorable population: EXCLUSION_REASON is null
    DESCRIPTION   free text

Checks     one row per data quality check on one window of data
    SOURCE, FIELD, CHECK_TYPE, MIN, MAX, VALUES, REGEX, MAX_BAD_PCT, RULE_NAME,
    COMPARE_SOURCE, COMPARE_FIELD, KEY, COMPARE_KEY, COMPARE, TOLERANCE, ALERT

Stability  one row per feature or score whose distribution is compared with a benchmark period
    SOURCE, FIELD, INDEX (PSI | CSI), FIELD_TYPE (NUMERIC | CATEGORICAL), BINS,
    BENCHMARK_START, BENCHMARK_END, ACTIVE_DAYS, WARN, FAIL, ALERT
    The benchmark is a fixed date range (2026-01-01, 2026-04-01), or a range in days relative to the
    run (-60, -30 = from 60 to 30 days before).

Rules      one row per business rule, written as field conditions (no SQL); see dqf/rules.py
    RULE_NAME, DESCRIPTION, APPLIES_TO (sources), WHEN (which rows), THEN (what must hold),
    CHECK (a named custom check instead of THEN), WITH (its arguments), MAX_BAD_PCT, ALERT
    In the sheets, WHEN / THEN / WITH hold the same YAML text, e.g. {DECISION: AUTO_APPROVE}.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

SCHEDULES = ("hourly", "daily")
_TABLE_RE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){0,2}$")
_NAME_RE = re.compile(r"^[A-Za-z_][\w$]*$")
# A row filter is a WHERE condition written by a person. It runs with a read-only role, and these are
# refused anyway so it cannot become anything but a condition.
_FILTER_FORBIDDEN = re.compile(
    r";|--|/\*|\b(insert|update|delete|merge|drop|create|alter|grant|revoke|truncate|call|copy|put|get|"
    r"use|execute|undrop|remove|list)\b", re.I)

# What each check type needs. "A|B" means either.
CHECK_NEEDS = {
    "VOLUME": ("MIN|MAX",),
    "FRESHNESS": ("MAX",),                    # MAX = hours allowed since the newest row
    "MISSING_VALUES": (),
    "FIELD_POPULATION": (),
    "ACCEPTABLE_RANGE": ("MIN|MAX",),
    "ACCEPTED_VALUES": ("VALUES",),
    "EXPECTED_FORMAT": ("REGEX",),
    "DUPLICATE_VALUES": (),
    "VALUE_LENGTH": ("MIN|MAX",),
    "PLACEHOLDER_VALUE": ("VALUES",),
    "SOURCE_COVERAGE": ("COMPARE_SOURCE", "KEY"),
    "MATCHES_SOURCE": ("COMPARE_SOURCE", "KEY"),
    "LOST_IN_MAPPING": ("COMPARE_SOURCE", "KEY"),
}
TABLE_LEVEL = {"VOLUME", "FRESHNESS", "BUSINESS_RULE", "SOURCE_COVERAGE"}
# The type of the check rows made from rules (one per rule and source it applies to).
RULE_CHECK = "BUSINESS_RULE"
COMPARISONS = {"SOURCE_COVERAGE", "MATCHES_SOURCE", "LOST_IN_MAPPING"}
COMPARE_MODES = {"EXACT", "TEXT", "NUMERIC"}

SOURCE_COLUMNS = ("SOURCE", "TABLE", "TS_COLUMN", "SCHEDULE", "ROW_FILTER", "DESCRIPTION")
CHECK_COLUMNS = ("SOURCE", "FIELD", "CHECK_TYPE", "MIN", "MAX", "VALUES", "REGEX", "MAX_BAD_PCT",
                 "COMPARE_SOURCE", "COMPARE_FIELD", "KEY", "COMPARE_KEY", "COMPARE", "TOLERANCE", "ALERT")
STABILITY_COLUMNS = ("SOURCE", "FIELD", "INDEX", "FIELD_TYPE", "BINS", "BENCHMARK_START", "BENCHMARK_END",
                     "ACTIVE_DAYS", "WARN", "FAIL", "ALERT")
RULE_COLUMNS = ("RULE_NAME", "DESCRIPTION", "APPLIES_TO", "WHEN", "THEN", "CHECK", "WITH", "MAX_BAD_PCT", "ALERT")
SHEETS = {"SOURCES": SOURCE_COLUMNS, "CHECKS": CHECK_COLUMNS, "STABILITY": STABILITY_COLUMNS, "RULES": RULE_COLUMNS}
SOURCE_KEYS = {c.lower() for c in SOURCE_COLUMNS if c != "SOURCE"} | {"checks", "stability"}


class ConfigError(ValueError):
    """The workbook has problems. The message lists every one of them."""


@dataclass
class Source:
    name: str
    table: str
    ts_column: str
    schedule: str
    row_filter: str | None = None
    description: str | None = None


@dataclass
class Config:
    sources: dict[str, Source]
    checks: list[dict] = field(default_factory=list)       # one dict per Checks row, with CHECK_ID
    stability: list[dict] = field(default_factory=list)    # one dict per Stability row, with CHECK_ID
    rules: dict = field(default_factory=dict)              # business rules: name -> rules.Rule

    def for_schedule(self, schedule: str) -> "Config":
        """Only the sources run on this schedule, with their checks."""
        names = {n for n, s in self.sources.items() if s.schedule == schedule}
        return Config(
            sources=dict(self.sources),                      # comparisons may read any source
            checks=[c for c in self.checks if c["SOURCE"] in names],
            stability=[s for s in self.stability if s["SOURCE"] in names],
            rules=dict(self.rules),
        )


# ---- reading ---------------------------------------------------------------------------------------
def is_yaml(location: str) -> bool:
    return str(location).lower().endswith((".yaml", ".yml"))


def _read_bytes(location: str) -> bytes:
    if location.startswith("s3://"):
        import boto3

        bucket, key = location[5:].split("/", 1)
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return Path(location).read_bytes()


def read_sheets(location: str) -> dict[str, pd.DataFrame]:
    """All sheets of an Excel workbook, or all CSVs of a folder (sources.csv, …), by upper-case name."""
    if not location.startswith("s3://") and Path(location).is_dir():
        return {p.stem.strip().upper(): pd.read_csv(p, dtype=object)
                for p in sorted(Path(location).glob("*.csv")) if p.stem.strip().upper() in SHEETS}
    sheets = pd.read_excel(io.BytesIO(_read_bytes(location)), sheet_name=None, dtype=object)
    return {name.strip().upper(): df for name, df in sheets.items()}


def read_yaml(location: str):
    import yaml

    try:
        return yaml.safe_load(_read_bytes(location)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{location} is not valid YAML: {exc}") from exc


def load_config(location: str, known_checks: set[str] | None = None) -> Config:
    """The configuration at a path or s3:// URI: .yaml / .yml, .xlsx, or a folder of CSVs.
    known_checks: the named custom checks rules may use (default: those in dqf/expectations.py)."""
    if is_yaml(location):
        return parse_yaml(read_yaml(location), known_checks)
    return parse(read_sheets(location), known_checks)


def _registered_checks() -> set[str]:
    from dqf.expectations import CHECKS

    return set(CHECKS)


def parse(sheets: dict[str, pd.DataFrame], known_checks: set[str] | None = None) -> Config:
    """Sheets (Excel or CSV) -> a validated Config."""
    problems: list[str] = []
    if "SOURCES" not in sheets:
        raise ConfigError("The workbook needs a 'Sources' sheet")
    tables = {name: _rows(sheets.get(name), columns, name.capitalize(), problems) for name, columns in SHEETS.items()}
    return _build(tables, known_checks, problems)


def parse_yaml(doc, known_checks: set[str] | None = None) -> Config:
    """A YAML document (already loaded) -> a validated Config."""
    problems: list[str] = []
    return _build(yaml_rows(doc, problems), known_checks, problems)


def _build(tables: dict[str, list], known_checks: set[str] | None, problems: list[str]) -> Config:
    sources = _sources(tables["SOURCES"], problems)
    checks = _checks(tables["CHECKS"], sources, problems)
    rules, rule_checks = _rules(tables["RULES"], _registered_checks() if known_checks is None else known_checks,
                                sources, {c["CHECK_ID"] for c in checks}, problems)
    checks += rule_checks
    stability = _stability(tables["STABILITY"], sources, problems)
    if problems:
        raise ConfigError("The configuration has problems:\n  - " + "\n  - ".join(problems))
    if not checks and not stability:
        raise ConfigError("The configuration has no checks")
    return Config(sources, checks, stability, rules)


def yaml_rows(doc, problems: list[str]) -> dict[str, list]:
    """The YAML layout -> the same (where, row) lists the sheets give, with upper-case column names.

        sources:
          NAME: {table: ..., ts_column: ..., schedule: ..., checks: [...], stability: [...]}
        rules:
          NAME: {description: ..., applies_to: ..., when: {...}, then: {...}}   (or check: NAME)
    """
    tables: dict[str, list] = {name: [] for name in SHEETS}
    if not isinstance(doc, dict):
        problems.append("The YAML file must be a mapping with 'sources' (and optionally 'rules')")
        return tables
    for key in set(doc) - {"sources", "rules"}:
        problems.append(f"unknown top-level key '{key}' (expected sources, rules)")
    sources = doc.get("sources") or {}
    if not isinstance(sources, dict):
        problems.append("sources: must be a mapping of source name -> settings")
        sources = {}
    for name, body in sources.items():
        where = f"sources.{name}"
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping (table, ts_column, schedule, checks, ...)")
            continue
        _unknown_keys(body, SOURCE_KEYS, where, problems)
        tables["SOURCES"].append((where, _yaml_row({**body, "source": name}, SOURCE_COLUMNS)))
        for section, columns in (("checks", CHECK_COLUMNS), ("stability", STABILITY_COLUMNS)):
            items = body.get(section) or []
            if not isinstance(items, list):
                problems.append(f"{where}.{section}: must be a list")
                continue
            allowed = {c.lower() for c in columns if c != "SOURCE"}
            for i, item in enumerate(items, start=1):
                at = f"{where}.{section}[{i}]"
                if not isinstance(item, dict):
                    problems.append(f"{at}: must be a mapping")
                    continue
                _unknown_keys(item, allowed, at, problems)
                tables[section.upper()].append((at, _yaml_row({**item, "source": name}, columns)))
    rules = doc.get("rules") or {}
    if not isinstance(rules, dict):
        problems.append("rules: must be a mapping of rule name -> description, applies_to, when, then")
        rules = {}
    for name, body in rules.items():
        where = f"rules.{name}"
        if not isinstance(body, dict):
            problems.append(f"{where}: must be a mapping (description, applies_to, when, then)")
            continue
        _unknown_keys(body, {c.lower() for c in RULE_COLUMNS if c != "RULE_NAME"}, where, problems)
        tables["RULES"].append((where, _yaml_row({**body, "rule_name": name}, RULE_COLUMNS)))
    return tables


def _unknown_keys(body: dict, allowed: set[str], where: str, problems: list[str]) -> None:
    unknown = sorted(str(k) for k in body if str(k).lower() not in allowed)
    if unknown:
        problems.append(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _yaml_row(body: dict, columns: tuple[str, ...]) -> dict:
    lower = {str(k).lower(): v for k, v in body.items()}
    row = {c: _clean(lower.get(c.lower())) for c in columns}
    if "VALUES" in row and isinstance(row["VALUES"], list):
        row["VALUES"] = values_text(row["VALUES"])
    return row


def values_text(items: list) -> str:
    """A YAML list of values in the sheet's text form: text quoted, numbers bare ("'A','B'", "-999")."""
    def one(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "'" + str(v) + "'"
        return str(int(v)) if float(v).is_integer() else str(v)
    return ",".join(one(v) for v in items)


def _rows(df: pd.DataFrame | None, columns: tuple[str, ...], sheet: str, problems: list[str]) -> list[tuple[str, dict]]:
    """Plain Python rows: one kind of empty (None), text trimmed. Fully empty rows are skipped."""
    if df is None:
        return []
    df = df.copy()
    df.columns = [str(c).strip().upper() for c in df.columns]
    unknown = [c for c in df.columns if c not in columns and not c.startswith("UNNAMED")]
    if unknown:
        problems.append(f"{sheet}: unknown column(s) {', '.join(unknown)}")
    rows = []
    for i, raw in enumerate(df.to_dict("records")):
        row = {c: _clean(raw.get(c)) for c in columns}
        if all(v is None for v in row.values()):
            continue
        rows.append((f"{sheet} row {i + 2}", row))           # +2: header row, 1-based
    return rows


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _number(row: dict, column: str, where: str, problems: list[str]):
    value = row[column]
    if value is None:
        return None
    try:
        row[column] = float(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: {column} '{value}' is not a number")
        row[column] = None
    return row[column]


def _date(row: dict, column: str, where: str, problems: list[str]):
    """A date (YYYY-MM-DD), or a whole number of days relative to the run: -60 = 60 days before."""
    value = row[column]
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) or re.fullmatch(r"-?\d+", str(value)):
        row[column] = int(float(value))
    elif isinstance(value, datetime):
        row[column] = value.date()
    elif isinstance(value, date):
        row[column] = value
    else:
        try:
            row[column] = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            problems.append(f"{where}: {column} '{value}' is not a date (YYYY-MM-DD)")
            row[column] = None
    return row[column]


def _alert(row: dict) -> None:
    row["ALERT"] = "Yes" if str(row["ALERT"]).strip().lower() in ("yes", "y", "true", "1", "1.0") else "No"


# ---- the four sections ----------------------------------------------------------------------------
def _sources(rows, problems) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for where, r in rows:
        name = str(r["SOURCE"] or "").strip()
        if not _NAME_RE.match(name):
            problems.append(f"{where}: SOURCE '{name}' must be a plain name (letters, digits, _)")
            continue
        if name.upper() in sources:
            problems.append(f"{where}: SOURCE '{name}' appears twice")
        table, ts_col = str(r["TABLE"] or ""), str(r["TS_COLUMN"] or "")
        schedule = str(r["SCHEDULE"] or "").lower()
        if not _TABLE_RE.match(table):
            problems.append(f"{where}: TABLE '{table}' is not a valid table name")
        if not _NAME_RE.match(ts_col):
            problems.append(f"{where}: TS_COLUMN '{ts_col}' is not a valid column name")
        if schedule not in SCHEDULES:
            problems.append(f"{where}: SCHEDULE must be one of {', '.join(SCHEDULES)}")
        row_filter = r["ROW_FILTER"]
        if row_filter and _FILTER_FORBIDDEN.search(str(row_filter)):
            problems.append(f"{where}: ROW_FILTER may only be a condition (no ;, comments or statements)")
        sources[name.upper()] = Source(name.upper(), table, ts_col, schedule,
                                       str(row_filter) if row_filter else None, r["DESCRIPTION"])
    if not sources:
        problems.append("Sources: no sources")
    return sources


def _checks(rows, sources, problems) -> list[dict]:
    checks, seen = [], set()
    for where, r in rows:
        r["SOURCE"] = str(r["SOURCE"] or "").upper()
        r["CHECK_TYPE"] = str(r["CHECK_TYPE"] or "").strip().upper().replace(" ", "_")
        for col in ("FIELD", "COMPARE_FIELD", "KEY", "COMPARE_KEY", "COMPARE", "REGEX", "VALUES"):
            r[col] = None if r[col] is None else str(r[col]).strip()
        for col in ("COMPARE_SOURCE", "COMPARE"):
            r[col] = r[col].upper() if r[col] else r[col]
        for col in ("MIN", "MAX", "MAX_BAD_PCT", "TOLERANCE"):
            _number(r, col, where, problems)
        _alert(r)
        ctype = r["CHECK_TYPE"]
        if r["SOURCE"] not in sources:
            problems.append(f"{where}: SOURCE '{r['SOURCE']}' is not a defined source")
        if ctype == RULE_CHECK:
            problems.append(f"{where}: business rules are written in the rules section, with applies_to")
            continue
        if ctype not in CHECK_NEEDS:
            problems.append(f"{where}: unknown CHECK_TYPE '{ctype}'")
            continue
        if ctype not in TABLE_LEVEL and not (r["FIELD"] and _NAME_RE.match(r["FIELD"])):
            problems.append(f"{where}: {ctype} needs a valid FIELD")
        for need in CHECK_NEEDS[ctype]:
            if all(r[c] is None for c in need.split("|")):
                problems.append(f"{where}: {ctype} needs {need.replace('|', ' or ')}")
        if r["MAX_BAD_PCT"] is not None and not 0 <= r["MAX_BAD_PCT"] <= 100:
            problems.append(f"{where}: MAX_BAD_PCT must be between 0 and 100")
        if r["REGEX"] is not None:
            try:
                re.compile(r["REGEX"])
            except re.error as exc:
                problems.append(f"{where}: REGEX does not compile ({exc})")
        if ctype in COMPARISONS:
            r["COMPARE_KEY"] = r["COMPARE_KEY"] or r["KEY"]
            if ctype != "SOURCE_COVERAGE":
                r["COMPARE_FIELD"] = r["COMPARE_FIELD"] or r["FIELD"]
            if ctype == "MATCHES_SOURCE":
                r["COMPARE"] = r["COMPARE"] or "EXACT"
                if r["COMPARE"] not in COMPARE_MODES:
                    problems.append(f"{where}: COMPARE must be one of {', '.join(sorted(COMPARE_MODES))}")
            if r["COMPARE_SOURCE"] and r["COMPARE_SOURCE"] not in sources:
                problems.append(f"{where}: COMPARE_SOURCE '{r['COMPARE_SOURCE']}' is not a defined source")
            for col in ("KEY", "COMPARE_KEY", "COMPARE_FIELD"):
                if r[col] and not _NAME_RE.match(r[col]):
                    problems.append(f"{where}: {col} '{r[col]}' is not a valid column name")
        r["CHECK_ID"] = check_id(r)
        if r["CHECK_ID"] in seen:
            problems.append(f"{where}: duplicate check {r['CHECK_ID']}")
        seen.add(r["CHECK_ID"])
        checks.append(r)
    return checks


def _stability(rows, sources, problems) -> list[dict]:
    out, seen = [], set()
    for where, r in rows:
        r["SOURCE"] = str(r["SOURCE"] or "").upper()
        r["INDEX"] = str(r["INDEX"] or "PSI").upper()
        r["FIELD_TYPE"] = str(r["FIELD_TYPE"] or "NUMERIC").upper()
        for col in ("BINS", "ACTIVE_DAYS", "WARN", "FAIL"):
            _number(r, col, where, problems)
        for col in ("BENCHMARK_START", "BENCHMARK_END"):
            _date(r, col, where, problems)
        r["BINS"] = int(r["BINS"] or 10)
        r["ACTIVE_DAYS"] = int(r["ACTIVE_DAYS"] or 7)
        r["WARN"] = 0.10 if r["WARN"] is None else r["WARN"]
        r["FAIL"] = 0.25 if r["FAIL"] is None else r["FAIL"]
        _alert(r)
        if r["SOURCE"] not in sources:
            problems.append(f"{where}: SOURCE '{r['SOURCE']}' is not a defined source")
        if not (r["FIELD"] and _NAME_RE.match(str(r["FIELD"]))):
            problems.append(f"{where}: needs a valid FIELD")
        if r["INDEX"] not in ("PSI", "CSI"):
            problems.append(f"{where}: INDEX must be PSI (a score) or CSI (a feature)")
        if r["FIELD_TYPE"] not in ("NUMERIC", "CATEGORICAL"):
            problems.append(f"{where}: FIELD_TYPE must be NUMERIC or CATEGORICAL")
        if not 2 <= r["BINS"] <= 50:
            problems.append(f"{where}: BINS must be between 2 and 50")
        bs, be = r["BENCHMARK_START"], r["BENCHMARK_END"]
        if bs is None or be is None or type(bs) is not type(be) or bs >= be:
            problems.append(f"{where}: needs BENCHMARK_START before BENCHMARK_END, both dates or both day offsets")
        if r["WARN"] > r["FAIL"]:
            problems.append(f"{where}: WARN must not be above FAIL")
        r["FIELD"] = str(r["FIELD"] or "")
        r["CHECK_ID"] = f"{r['SOURCE']}|{r['FIELD']}|{r['INDEX']}"
        if r["CHECK_ID"] in seen:
            problems.append(f"{where}: duplicate {r['CHECK_ID']}")
        seen.add(r["CHECK_ID"])
        out.append(r)
    return out


def _rules(rows, known_checks: set[str], sources, taken: set[str], problems) -> tuple[dict, list[dict]]:
    """Business rules -> (name -> rules.Rule, one check row per rule and source it applies to).

    The check rows join the other checks, so a rule is planned, stored, alerted on and reported like
    any check. Its CHECK_ID is SOURCE||RULE_NAME."""
    import yaml

    from dqf import rules as vocabulary

    out, checks = {}, []
    for where, r in rows:
        name = str(r["RULE_NAME"] or "").strip().upper()
        if not _NAME_RE.match(name):
            problems.append(f"{where}: rule name '{name}' must be a plain name (letters, digits, _)")
            continue
        if name in out:
            problems.append(f"{where}: rule {name} is defined twice")
            continue
        body = {}
        for column in RULE_COLUMNS[1:]:
            value = r[column]
            if column in ("WHEN", "THEN", "WITH") and isinstance(value, str):    # a sheet cell: YAML text
                try:
                    value = yaml.safe_load(value)
                except yaml.YAMLError as exc:
                    problems.append(f"{where}: {column} is not valid YAML text ({exc})")
                    value = None
            if column == "MAX_BAD_PCT" and isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            if value is not None:
                body[column.lower()] = value
        try:
            rule = vocabulary.parse_rule(name, body, where, known_checks)
        except vocabulary.RuleError as exc:
            problems.append(str(exc))
            continue
        alert = {"ALERT": body.get("alert")}
        _alert(alert)
        for source in rule.applies_to:
            if source not in sources:
                problems.append(f"{where}: applies_to '{source}' is not a defined source")
                continue
            row = {c: None for c in CHECK_COLUMNS}
            row.update(SOURCE=source, CHECK_TYPE=RULE_CHECK, RULE_NAME=name, MAX_BAD_PCT=rule.max_bad_pct,
                       ALERT=alert["ALERT"])
            row["CHECK_ID"] = check_id(row)
            if row["CHECK_ID"] in taken:
                problems.append(f"{where}: duplicate check {row['CHECK_ID']}")
            taken.add(row["CHECK_ID"])
            checks.append(row)
        out[name] = rule
    return out, checks


def check_id(row: dict) -> str:
    """A stable name for one check. Alerts use it to tell 'still failing' from 'new failure'."""
    name = row["RULE_NAME"] if row["CHECK_TYPE"] == RULE_CHECK else row["CHECK_TYPE"]
    if row["CHECK_TYPE"] in COMPARISONS:
        name = f"{name}:{row['COMPARE_SOURCE']}"
    return f"{row['SOURCE']}|{row['FIELD'] or ''}|{name}"


def parse_values(value) -> list:
    """Turn "'A','B'" or "-999,-1" into a list. Quoted items stay text; bare numbers become numbers."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    items = []
    for raw in str(value).split(","):
        item = raw.strip()
        if not item:
            continue
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            items.append(item[1:-1])
            continue
        try:
            items.append(float(item))
        except ValueError:
            items.append(item)
    return items
