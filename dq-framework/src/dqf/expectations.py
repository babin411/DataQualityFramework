"""Custom Great Expectations, for business logic that the rule vocabulary (when / then) cannot express.

Each one is a GX custom expectation written once by a developer and used from the configuration by
name:
    rules:
      SSN_TAX_ID_MATCH_DERIVED:
        applies_to: UW_A1_INPUT
        check: ssn_tax_id_match_follows_derivation

Built as GX 1.x describes: a metric that answers a yes/no question per row (with a SQLAlchemy version,
which is what runs on Snowflake, and a pandas version for tests), and an expectation that uses it. As
map expectations they get `mostly`, the share of bad rows, row conditions and Data Docs from GX for
free. Like every check, only counts come back from Snowflake.

To add one: write the metric and the expectation below, then register it in CHECKS with the words
used in alerts (`text`) and a condition that selects the breaking rows (`breaks_sql`), which goes into
the alert's "see the rows" query.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("GX_ANALYTICS_ENABLED", "false")

import sqlalchemy as sa  # noqa: E402
from great_expectations.execution_engine import PandasExecutionEngine, SqlAlchemyExecutionEngine  # noqa: E402
from great_expectations.expectations.expectation import MulticolumnMapExpectation  # noqa: E402
from great_expectations.expectations.metrics.map_metric_provider import (  # noqa: E402
    MulticolumnMapMetricProvider, multicolumn_condition_partial)


# ---- SSN_TAX_ID_MATCH derivation --------------------------------------------------------------------
# Implementation Support Document, section 1: '1' if the SSN and Tax ID (last 4) are both present and
# equal, '0' if both present and different, 'Z' if either is missing.
class SsnTaxIdMatchFollowsDerivation(MulticolumnMapMetricProvider):
    condition_metric_name = "multicolumn_values.dqf_ssn_tax_id_match_follows_derivation"
    condition_domain_keys = ("batch_id", "table", "column_list", "row_condition", "condition_parser", "ignore_row_if")

    @multicolumn_condition_partial(engine=SqlAlchemyExecutionEngine)
    def _sqlalchemy(cls, column_list, **kwargs):
        ssn, tax_id, match = column_list
        expected = sa.case((sa.or_(ssn.is_(None), tax_id.is_(None)), "Z"), (ssn == tax_id, "1"), else_="0")
        return sa.and_(match.isnot(None), match == expected)

    @multicolumn_condition_partial(engine=PandasExecutionEngine)
    def _pandas(cls, column_list, **kwargs):
        ssn, tax_id, match = (column_list.iloc[:, i] for i in range(3))
        both = ssn.notna() & tax_id.notna()
        expected = (ssn == tax_id).map({True: "1", False: "0"}).where(both, "Z")
        return match.notna() & (match == expected)


class ExpectSsnTaxIdMatchToFollowDerivation(MulticolumnMapExpectation):
    """Expect SSN_TAX_ID_MATCH to be '1' when SSN and Tax ID are equal, '0' when they differ, 'Z' when either is missing."""

    column_list: list = ["SSN_LAST4", "TAX_ID_LAST4", "SSN_TAX_ID_MATCH"]     # SSN, Tax ID, the match flag
    ignore_row_if: str = "never"                     # rows with empty values are judged too: they must be 'Z'
    map_metric = "multicolumn_values.dqf_ssn_tax_id_match_follows_derivation"
    success_keys = ("column_list", "mostly", "ignore_row_if")
    description: str = "SSN_TAX_ID_MATCH follows the documented derivation"


# ---- the register the configuration uses --------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    cls: type
    text: str            # what it requires, in words, for alerts and reports
    breaks_sql: str      # a condition true for a breaking row, for the alert's "see the rows" query


CHECKS: dict[str, Check] = {
    "ssn_tax_id_match_follows_derivation": Check(
        cls=ExpectSsnTaxIdMatchToFollowDerivation,
        text="SSN_TAX_ID_MATCH must be 1 when SSN_LAST4 equals TAX_ID_LAST4, 0 when they differ, Z when either is empty",
        breaks_sql=("SSN_TAX_ID_MATCH is distinct from case when SSN_LAST4 is null or TAX_ID_LAST4 is null then 'Z' "
                    "when SSN_LAST4 = TAX_ID_LAST4 then '1' else '0' end"),
    ),
}
CHECK_TEXTS = {name: c.text for name, c in CHECKS.items()}
