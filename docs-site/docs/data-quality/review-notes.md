# Review notes & open questions

The BRD is a **Version 1.0 draft** (it is still marked `@TEMPLATE`, and several matrix rows are
placeholders). Below are gaps and ambiguities worth raising with its author, roughly in order of
importance. Each one includes a suggested resolution, marked **(suggestion)**.

## 1. Blocking check or after-the-fact monitoring? { #1-blocking-check-or-after-the-fact-monitoring }

BO-01 says data should be checked "for availability and validity **before being consumed by models**".
But the parameters (a Snowflake path, hourly alert frequency, weekly reports) describe **batch
monitoring of stored data**. In Underwriting, Snowflake is
[never on the real-time path](../index.md). Data reaches Snowflake only **after** the decision, through
the transaction log (steps 5a–5c).

So there are two very different designs:

| Design                      | How it works                                                             | Implication                                                                 |
| --------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| **Blocking check**          | Checks run **inside** the decision flow (e.g. in the normalizer Lambda) and can stop or flag a decision | Real protection, but adds latency and new ways for live decisions to fail |
| **After-the-fact monitoring** | Checks run hourly on data already logged to Snowflake                  | No risk to live decisions; issues are caught within the hour, not prevented |

**(suggestion)** Make the BRD say which one it means. The parameters fit after-the-fact monitoring. If
so, reword BO-01 to something like "detected within one hour of arrival" rather than "before being
consumed by models".

## 2. Define every check type precisely { #2-define-every-check-type-precisely }

The check types are listed in a dropdown but never defined. Two of them overlap as drafted:

- **Missing Values > 30%** and **Field Population < 70%** describe the **same condition** if "populated"
  just means "not null": 30% missing is 70% populated.

**(suggestion)** Add a definitions table to the BRD. For example: Missing Values = null only; Field
Population = not null **and** not blank **and** not a placeholder. Then the two checks differ, and the
placeholder list for each field (e.g. `-999`) becomes part of its configuration.

## 3. Tighten the alert criteria { #3-tighten-the-alert-criteria }

- **The range notation is ambiguous.** Does `(<=$0 >=$10000)` describe the failing values or the allowed
  ones, and are the bounds inclusive? The draft relies on the reader guessing.
- **Some ranges look misplaced.** `(<=0 >=15)` on **Merch ID** is a numeric range on an identifier. It is
  probably meant to be a **length** check (Value Length) instead.
- **Hard-coded years will go stale.** `(<=2025 >=2026)` on **Enquiry Date** stops making sense once the
  calendar moves on. **(suggestion)** Use relative rules such as "not in the future and not older than
  N days".
- **One bad value or a percentage?** For range, format and accepted-value checks, say whether a single
  violating record makes the check FAIL, or only more than X% of records (GX calls this `mostly`).
- **Criteria with the check switched off.** Enquiry Date's Acceptable Range says **No** but still lists
  criteria. Clarify whether it is meant to be on.

## 4. Clarify what the FSI/CSI criteria measure { #4-clarify-what-the-fsicsi-criteria-measure }

The criteria `Dec > 10%` and `Inc > 5%` are directional percentages. But CSI and PSI are **never
negative** and go up whichever way the data moves (see the
[worked examples](feature-score-monitoring.md#how-csi-and-psi-are-calculated)). **(suggestion)** Decide
which of these is meant:

- **CSI/PSI index thresholds**, for example FAIL if CSI > 0.10, warn above 0.25; or
- **Directional change in a summary statistic**, for example FAIL if the **mean** FICO falls by more
  than 10% compared with the benchmark.

Also define the **binning** (how many bins, and are the edges set from the benchmark?), and how the
benchmark date range is chosen and refreshed.

## 5. Add availability checks { #5-add-availability-checks }

BO-01 asks for **availability**, but every check type measures **validity**. A vendor outage that sends
**zero** records can pass every percentage-based check. **(suggestion)** Add **Volume** (row count
against expected) and **Freshness** (time since the last record) check types. See
[Input monitoring](input-monitoring.md#coverage-gap-availability-isnt-checked-yet).

## 6. Small windows make noisy percentages { #6-small-windows-make-noisy-percentages }

Alerts run **hourly**. In a quiet hour with 12 applications, 4 missing values is already 33%, a FAIL.
**(suggestion)** Set a **minimum row count** below which a check reports "insufficient data" rather
than PASS/FAIL. Or evaluate over a rolling window, for example the last 24 hours, re-checked every hour.

## 7. Alerting details still open { #7-alerting-details-still-open }

The BRD itself flags these as undecided:

- **Channel:** email is proposed. Is there also Teams/Slack, or a ticket?
- **Recipients and ownership:** who receives an alert for each data source, and who fixes it?
- **Repeat suppression:** a problem that lasts 10 hours would send 10 identical emails. **(suggestion)**
  Send an alert on the first FAIL and a "resolved" note on the first PASS afterwards.
- **Sensitive fields:** the draft mentions a set of important fields with their own alert timing. Name
  them, perhaps as a `severity` in the config.
- **PASS/FAIL only:** consider adding a **WARN** level between them, so thresholds can be tuned without
  paging anyone.

## 8. Scope and data locations { #8-scope-and-data-locations }

- **Snowflake paths** ([what they are](input-monitoring.md#what-snowflake-path-means)) are still
  placeholder links. For each data source (applications, LN, TMX, Bureau), give the exact
  `DATABASE.SCHEMA.TABLE` in each environment, the **timestamp column** for windowing, and the
  **role** that can read it. For feature/score monitoring, say whether Benchmark and Active are two
  tables or one table with two date ranges.
- **"Alerts Sent? = No":** confirm this means "shown in the weekly report only", not "check disabled".
- **Clover Capital** is in scope for reuse, but none of its data sources are described yet.
- **Matrix rows marked `…`** need their variable names filled in before the matrix can be implemented.

## Summary for the author

| # | Topic                         | Ask                                                                    |
| - | ----------------------------- | ---------------------------------------------------------------------- |
| 1 | Blocking vs after-the-fact    | State which one; align BO-01 wording with the hourly Snowflake design. |
| 2 | Check definitions             | Define each check type; separate Missing Values from Field Population.  |
| 3 | Alert criteria                | Fix notation, relative dates, and the "one bad value vs %" rule.        |
| 4 | FSI/CSI criteria              | Index thresholds or directional change? Define bins and benchmark.      |
| 5 | Availability                  | Add Volume and Freshness checks.                                        |
| 6 | Noisy windows                 | Minimum row count or rolling window.                                    |
| 7 | Alerting                      | Channel, owners, repeat suppression, sensitive fields, WARN level.      |
| 8 | Scope                         | Real Snowflake paths, meaning of "No", Clover Capital sources.          |
