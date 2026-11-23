"""The YAML configuration (the main format), rules written in it, and conversion between formats."""
import dataclasses
import subprocess
import sys

import pytest
import yaml

from conftest import CONFIG, ROOT, WORKBOOK
from dqf.workbook import ConfigError, load_config, parse_yaml


def summary(config):
    return (sorted((k, dataclasses.asdict(v)) for k, v in config.sources.items()),
            sorted(config.checks, key=lambda r: r["CHECK_ID"]),
            sorted(config.stability, key=lambda r: r["CHECK_ID"]), sorted(config.rules.items()))


def doc(checks=None, rules=None, **source):
    body = {"table": "DB.S.T", "ts_column": "TS", "schedule": "hourly", **source}
    if checks is not None:
        body["checks"] = checks
    out = {"sources": {"S": body}}
    if rules is not None:
        out["rules"] = rules
    return out


def problems(document) -> str:
    with pytest.raises(ConfigError) as err:
        parse_yaml(document)
    return str(err.value)


def test_every_format_gives_the_same_configuration():
    main = summary(load_config(str(CONFIG)))
    assert summary(load_config(str(WORKBOOK))) == main
    assert summary(load_config(str(ROOT / "config" / "sheets"))) == main


def test_yaml_values_and_alerts():
    config = parse_yaml(doc(checks=[{"field": "C", "check_type": "ACCEPTED_VALUES", "values": ["1", "Z", -999], "alert": True},
                                    {"field": "D", "check_type": "MISSING_VALUES", "alert": False}]))
    values, missing = config.checks
    assert (values["VALUES"], values["ALERT"], missing["ALERT"]) == ("'1','Z',-999", "Yes", "No")


def test_a_rule_becomes_one_check_per_source_it_applies_to():
    d = doc(checks=[{"check_type": "VOLUME", "min": 1}])
    d["sources"]["T"] = {"table": "DB.S.U", "ts_column": "TS", "schedule": "daily"}
    d["rules"] = {"low": {"description": "No low scores", "applies_to": ["S", "T"], "then": {"SCORE": {"at_least": 300}},
                          "alert": "yes"}}
    config = parse_yaml(d)
    rule_rows = [c for c in config.checks if c["CHECK_TYPE"] == "BUSINESS_RULE"]
    assert [r["CHECK_ID"] for r in rule_rows] == ["S||LOW", "T||LOW"] and {r["ALERT"] for r in rule_rows} == {"Yes"}
    assert config.rules["LOW"].then == [("SCORE", "range", {"min": 300, "strict_min": False})]


def test_yaml_problems_say_where_they_are():
    text = problems({"sources": {"S": {"table": "DB.S.T", "ts_column": "TS", "schedule": "hourly", "colour": "red",
                                       "checks": [{"field": "A", "check_type": "MISSING_VALUES", "max_bad": 5}]}},
                     "rules": {"R": {"applies_to": "S", "when": {"A": {"abov": 3}}, "then": {"B": "x"}},
                               "Q": {"applies_to": "NOPE", "then": {"B": "x"}}},
                     "extra": 1})
    for expected in ("unknown top-level key 'extra'", "sources.S: unknown key(s) colour",
                     "sources.S.checks[1]: unknown key(s) max_bad", "rules.R.when.A: unknown condition 'abov'",
                     "rules.Q: applies_to 'NOPE' is not a defined source"):
        assert expected in text, expected


@pytest.mark.parametrize("rule, message", [
    ({"then": {"B": "x"}}, "needs applies_to"),
    ({"applies_to": "S"}, "needs either then"),
    ({"applies_to": "S", "then": {"B": "x"}, "check": "ssn_tax_id_match_follows_derivation"}, "needs either then"),
    ({"applies_to": "S", "check": "no_such_check"}, "unknown check 'no_such_check'"),
    ({"applies_to": "S", "then": {"B": True}}, "write yes/no/true/false values in quotes"),
    ({"applies_to": "S", "then": {"B": {"between": [9, 1]}}}, "the low value is above the high value"),
    ({"applies_to": "S", "then": {"B": {"at_least": "ten"}}}, "'ten' is not a number"),
    ({"applies_to": "S", "then": {"B": {"matches": "([0-9"}}}, "the pattern does not compile"),
    ({"applies_to": "S", "then": {"B-1": "x"}}, "'B-1' is not a valid column name"),
    ({"applies_to": "S", "then": {"B": "x"}, "max_bad_pct": 150}, "max_bad_pct must be a number between 0 and 100"),
    ({"applies_to": "S", "then": {"B": "x"}, "sql": "select 1"}, "unknown key(s) sql"),
])
def test_rules_that_are_refused(rule, message):
    assert message in problems(doc(checks=[{"check_type": "VOLUME", "min": 1}], rules={"R": rule}))


def test_conversion_round_trip(tmp_path):
    def convert(a, b):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_config.py"), str(a), str(b)], check=True,
                       capture_output=True)
    convert(CONFIG, tmp_path / "c.xlsx")
    convert(tmp_path / "c.xlsx", tmp_path / "c.yaml")
    convert(tmp_path / "c.yaml", tmp_path / "sheets")
    sheet_rules = (tmp_path / "sheets" / "rules.csv").read_text()
    assert "{DECISION: AUTO_APPROVE}" in sheet_rules                 # WHEN / THEN as YAML text in a cell
    main = summary(load_config(str(CONFIG)))
    for converted in ("c.xlsx", "c.yaml", "sheets"):
        assert summary(load_config(str(tmp_path / converted))) == main, converted
    assert "rules" in yaml.safe_load((tmp_path / "c.yaml").read_text())


def test_a_broken_configuration_is_not_converted(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("sources:\n  S: {table: DB.S.T, ts_column: TS, schedule: weekly, checks: [{check_type: VOLUME, min: 1}]}\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_config.py"), str(bad), str(tmp_path / "o.xlsx")],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "SCHEDULE must be one of" in result.stderr and not (tmp_path / "o.xlsx").exists()
