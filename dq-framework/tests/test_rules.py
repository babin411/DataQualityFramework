"""Business rules written as when / then: what each word means, run on in-memory data with GX's pandas
engine (the same expectations run on Snowflake), plus the words and the "see the rows" query."""
from datetime import datetime

import great_expectations as gx
import pandas as pd
import pytest

from dqf import rules
from dqf.engine import combine
from dqf.expectations import CHECKS

FRAME = pd.DataFrame({
    "DECISION": ["AUTO_APPROVE", "AUTO_APPROVE", "AUTO_APPROVE", "MANUAL_REVIEW", "AUTO_DECLINE"],
    "SCORE":    [650,            390,            450,            350,             310],
    "STATUS":   ["CY",           "CY",           None,           "NA",            "CY"],
    "CODE":     ["AB12",         "AB1",          "AB12",         "ZZ99",          None],
    "SSN":      ["1111",         "2222",         None,           "4444",          "5555"],
    "TAX":      ["1111",         "9999",         "3333",         "4444",          "5555"],
    "MATCH":    ["1",            "1",            "Z",            "0",             "1"],
})


@pytest.fixture(scope="module")
def batch():
    context = gx.get_context(mode="ephemeral")
    asset = context.data_sources.add_pandas("p").add_dataframe_asset("a")
    return asset.add_batch_definition_whole_dataframe("b").get_batch(batch_parameters={"dataframe": FRAME})


def run(batch, **body):
    rule = rules.parse_rule("R", {"applies_to": "S", **body}, "rules.R", set(CHECKS))
    found = []
    for part, expectation in rules.build(rule):
        r = batch.validate(expectation, result_format={"result_format": "SUMMARY", "partial_unexpected_count": 0})
        found.append((part, ("PASS" if r.success else "FAIL", dict(r.result))))
    status, res = combine(found)
    return status, res.get("element_count"), res.get("unexpected_count")


@pytest.mark.parametrize("body, expected", [
    # when DECISION is AUTO_APPROVE (3 rows), SCORE at least 400: 390 breaks it
    ({"when": {"DECISION": "AUTO_APPROVE"}, "then": {"SCORE": {"at_least": 400}}}, ("FAIL", 3, 1)),
    ({"when": {"DECISION": "AUTO_APPROVE"}, "then": {"SCORE": {"at_least": 390}}}, ("PASS", 3, 0)),
    ({"when": {"DECISION": "AUTO_APPROVE"}, "then": {"SCORE": {"above": 390}}}, ("FAIL", 3, 1)),
    ({"when": {"DECISION": "AUTO_APPROVE"}, "then": {"SCORE": {"at_least": 400}}, "max_bad_pct": 34}, ("PASS", 3, 1)),
    # one of / not one of
    ({"when": {"DECISION": ["MANUAL_REVIEW", "AUTO_DECLINE"]}, "then": {"SCORE": {"below": 400}}}, ("PASS", 2, 0)),
    ({"when": {"DECISION": {"not": "AUTO_APPROVE"}}, "then": {"SCORE": {"between": [300, 340]}}}, ("FAIL", 2, 1)),
    ({"when": {"DECISION": {"not_in": ["AUTO_APPROVE"]}}, "then": {"SCORE": {"between": [300, 400]}}}, ("PASS", 2, 0)),
    # number conditions in `when`, and several fields (all must match)
    ({"when": {"SCORE": {"at_least": 400}, "DECISION": "AUTO_APPROVE"}, "then": {"STATUS": "CY"}}, ("PASS", 2, 0)),
    ({"when": {"SCORE": {"between": [300, 400]}}, "then": {"STATUS": "CY"}}, ("FAIL", 3, 1)),
    # empty values are not judged by value requirements; `present` requires a value
    ({"then": {"STATUS": "CY"}}, ("FAIL", 5, 1)),
    ({"then": {"STATUS": "present"}}, ("FAIL", 5, 1)),
    ({"when": {"STATUS": "missing"}, "then": {"DECISION": "AUTO_APPROVE"}}, ("PASS", 1, 0)),
    ({"when": {"CODE": "present"}, "then": {"CODE": {"matches": "^[A-Z]{2}[0-9]{2}$"}}}, ("FAIL", 4, 1)),
    ({"then": {"CODE": {"length_between": [4, 4]}}}, ("FAIL", 5, 1)),
    ({"then": {"CODE": {"not_matches": "^ZZ"}}}, ("FAIL", 5, 1)),
    # a list of when-blocks: any block
    ({"when": [{"DECISION": "AUTO_DECLINE"}, {"SCORE": {"below": 360}}], "then": {"STATUS": "CY"}}, ("FAIL", 2, 1)),
    # comparing two fields
    ({"when": {"MATCH": "1"}, "then": {"SSN": {"equals_field": "TAX"}}}, ("FAIL", 3, 1)),
    # several then fields: one result, the worst part
    ({"when": {"DECISION": "AUTO_APPROVE"}, "then": {"STATUS": "CY", "SCORE": {"at_least": 400}}}, ("FAIL", 3, 1)),
    # the custom check: MATCH must be '1' equal, '0' different, 'Z' either empty. Rows 2 (1 but differ), 4 (0 but equal)
    ({"check": "ssn_tax_id_match_follows_derivation", "with": {"column_list": ["SSN", "TAX", "MATCH"]}}, ("FAIL", 5, 2)),
])
def test_rule_words(batch, body, expected):
    assert run(batch, **body) == expected


def test_words_and_query():
    rule = rules.parse_rule("R", {"applies_to": "S", "when": [{"DECISION": "AUTO_APPROVE", "SCORE": {"at_least": 1}},
                                                               {"STATUS": "missing"}],
                                  "then": {"SCORE": {"at_least": 400}, "CODE": "present"}}, "rules.R", set())
    assert rules.text(rule) == ("when (DECISION is AUTO_APPROVE and SCORE is at least 1) or STATUS is empty: "
                                "SCORE must be at least 400; CODE must have a value")
    assert rules.breaks_sql(rule) == ("((DECISION = 'AUTO_APPROVE' and SCORE >= 1) or (STATUS is null)) and "
                                      "((SCORE < 400) or CODE is null)")


def test_quotes_in_values_are_escaped():
    rule = rules.parse_rule("R", {"applies_to": "S", "when": {"NAME": "O'Brien"}, "then": {"A": "x"}}, "rules.R", set())
    assert "NAME = 'O''Brien'" in rules.breaks_sql(rule)


def test_dates_in_when():
    rule = rules.parse_rule("R", {"applies_to": "S", "when": {"D": {"at_least": 5}}, "then": {"A": "x"}}, "rules.R", set())
    assert rules.when_sql(rule) == "D >= 5"
    rule = rules.parse_rule("R", {"applies_to": "S", "when": {"D": datetime(2026, 1, 1).date()}, "then": {"A": "x"}},
                            "rules.R", set())
    assert rules.when_sql(rule) == "D = '2026-01-01'"
