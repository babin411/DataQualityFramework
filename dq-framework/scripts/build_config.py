"""Convert the configuration between its formats: YAML (the main one), an Excel workbook, a CSV folder.

    python scripts/build_config.py SOURCE TARGET
        SOURCE, TARGET: a .yaml / .yml file, an .xlsx file, or a folder (one CSV per sheet)

    make config-xlsx         config/dq_config.yaml -> config/dq_config.xlsx and config/sheets/
    make config-from-xlsx    config/dq_config.xlsx -> config/dq_config.yaml (comments are not kept)

The source is validated first; a configuration with problems is not converted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from dqf.workbook import (SHEETS, ConfigError, _rows, is_yaml, load_config, parse_values,  # noqa: E402
                          read_sheets, read_yaml, yaml_rows)

CSV_NAMES = {name: f"{name.lower()}.csv" for name in SHEETS}
NUMERIC = {"MIN", "MAX", "MAX_BAD_PCT", "TOLERANCE", "BINS", "ACTIVE_DAYS", "WARN", "FAIL",
           "BENCHMARK_START", "BENCHMARK_END"}


def read_tables(location: str) -> dict[str, list[dict]]:
    """The configuration's rows, as written (not yet defaults-filled), after checking it is valid."""
    load_config(location)                                        # raises ConfigError with every problem
    problems: list[str] = []
    if is_yaml(location):
        tables = yaml_rows(read_yaml(location), problems)
    else:
        sheets = read_sheets(location)
        tables = {name: _rows(sheets.get(name), cols, name.capitalize(), problems) for name, cols in SHEETS.items()}
    return {name: [_tidy(row) for _, row in rows] for name, rows in tables.items()}


STRUCTURED = ("WHEN", "THEN", "WITH")                          # nested in YAML, YAML text in a sheet cell


def _tidy(row: dict) -> dict:
    out = {}
    for column, value in row.items():
        if column in STRUCTURED and isinstance(value, str):
            value = yaml.safe_load(value)
        elif column == "APPLIES_TO" and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if column == "ALERT":
            value = "Yes" if str(value).strip().lower() in ("yes", "y", "true", "1", "1.0") else "No"
        elif column in NUMERIC and value is not None:
            try:
                number = float(value)
                value = int(number) if number.is_integer() else number
            except (TypeError, ValueError):
                pass                                             # a date: keep as it is
        out[column] = value
    return out


# ---- writing ---------------------------------------------------------------------------------------
def _cell(column, value):
    if column in STRUCTURED and value is not None:
        return yaml.safe_dump(value, default_flow_style=True, sort_keys=False, width=10_000).strip()
    if column == "APPLIES_TO" and isinstance(value, list):
        return ", ".join(value)
    return value


def write_sheets(tables, target: Path) -> None:
    frames = {name: pd.DataFrame([{c: _cell(c, v) for c, v in row.items()} for row in tables[name]], columns=list(cols))
              for name, cols in SHEETS.items()}
    if target.suffix.lower() == ".xlsx":
        with pd.ExcelWriter(target, engine="openpyxl") as writer:
            for name, df in frames.items():
                df.to_excel(writer, sheet_name=name.capitalize(), index=False)
                ws = writer.sheets[name.capitalize()]
                for i, column in enumerate(df.columns, start=1):
                    width = max([len(str(column))] + [len(str(v)) for v in df[column] if v is not None]) + 2
                    ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 60)
    else:
        target.mkdir(parents=True, exist_ok=True)
        for name, df in frames.items():
            df.to_csv(target / CSV_NAMES[name], index=False)


def write_yaml(tables, target: Path, source: str) -> None:
    def plain(row, skip):
        out = {}
        for column, value in row.items():
            if column in skip or value is None:
                continue
            key = column.lower()
            if column == "ALERT":
                value = value == "Yes"
            elif column == "VALUES":
                value = [int(v) if isinstance(v, float) and v.is_integer() else v for v in parse_values(value)]
            elif column == "APPLIES_TO" and isinstance(value, list) and len(value) == 1:
                value = value[0]
            elif hasattr(value, "isoformat"):
                value = value.isoformat()[:10]
            out[key] = value
        return out

    sources = {}
    for row in tables["SOURCES"]:
        sources[str(row["SOURCE"]).upper()] = plain(row, {"SOURCE"})
    for section in ("CHECKS", "STABILITY"):
        for row in tables[section]:
            sources.setdefault(str(row["SOURCE"]).upper(), {}).setdefault(section.lower(), []).append(
                plain(row, {"SOURCE"}))
    doc = {"sources": sources}
    rules = {str(r["RULE_NAME"]).upper(): plain(r, {"RULE_NAME"}) for r in tables["RULES"]}
    if rules:
        doc["rules"] = rules

    class Dumper(yaml.SafeDumper):
        pass

    def text(dumper, value):                                     # multi-line SQL as a readable block
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    Dumper.add_representer(str, text)
    body = yaml.dump(doc, Dumper=Dumper, sort_keys=False, width=110, allow_unicode=True)
    target.write_text(f"# Data quality checks. Converted from {source}; see config/dq_config.yaml for the layout.\n\n"
                      + body)


def convert(source: str, target: str) -> None:
    tables = read_tables(source)
    out = Path(target)
    if is_yaml(target):
        write_yaml(tables, out, source)
    else:
        write_sheets(tables, out)
    counts = ", ".join(f"{len(rows)} {name.lower()}" for name, rows in tables.items())
    print(f"Wrote {target} from {source} ({counts})")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        sys.exit(__doc__)
    try:
        convert(*argv)
    except ConfigError as exc:
        sys.exit(f"Not converted, {argv[0]} has problems:\n{exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
