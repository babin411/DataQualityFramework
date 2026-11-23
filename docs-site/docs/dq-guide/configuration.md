# Configuration

There are three things to configure:

| What | Where | Who changes it, and how often |
| --- | --- | --- |
| **What to check**, including **business rules** | The configuration file `config/dq_config.yaml` | Analysts and stakeholders' rules, often. Plain field conditions, no SQL, no code deploy |
| Custom checks for logic field conditions cannot express (rare) | `src/dqf/expectations.py` | A developer, once per check |
| **How it runs** | Environment variables (`.env` locally; the Lambda's environment in AWS) | Once per environment |

## The configuration file

**`config/dq_config.yaml` is the main configuration.** It is one text file, reviewed in Git like code,
with comments explaining each check. It has two sections:

- **`sources`**: one block per table (or view) to watch, holding that table's **checks** and **drift
  checks** (`stability`).
- **`rules`**: the **business rules**, written as field conditions: *when* a row is in scope, *then*
  what must be true of it.

```yaml
sources:
  UW_A1_INPUT:                             # a short name for this table, seen this way
    table: DQF_DB.DQF.UW_A1_RECORD
    ts_column: REQUEST_TS
    schedule: hourly                       # checks the previous full hour
    checks:
      - {check_type: VOLUME, min: 1, alert: yes}
      - {field: AVG_TICKET, check_type: MISSING_VALUES, max_bad_pct: 30, alert: yes}
      - {field: SSN_TAX_ID_MATCH, check_type: ACCEPTED_VALUES, values: ['1', '0', 'Z'], max_bad_pct: 0, alert: yes}

  UW_A1_DAILY:
    table: DQF_DB.DQF.UW_A1_RECORD
    ts_column: REQUEST_TS
    schedule: daily
    row_filter: EXCLUSION_REASON is null
    stability:
      - {field: MF_SCORE, index: PSI, field_type: NUMERIC, benchmark_start: -60, benchmark_end: -30,
         active_days: 7, warn: 0.10, fail: 0.25, alert: yes}

rules:
  DECISION_MATCHES_SCORE:
    description: No auto-approval with a merchant fraud score below 400
    applies_to: UW_A1_SCORED
    when: {DECISION: AUTO_APPROVE}
    then: {MF_SCORE: {at_least: 400}}
    alert: yes
```

The keys are the column names in the tables below, in lower case. Writing tips:

| Tip | Why |
| --- | --- |
| Quote text that looks like a number or a date: `values: ['1', '0', 'Z']`, `['1900-01-01']` | Unquoted, YAML reads `1` as a number, which would not match a text column |
| Quote regular expressions: `regex: '^[0-9]{5}$'` | Braces and `$` have meanings in YAML |
| `alert: yes` / `no` (or `true` / `false`) | Either works |
| One check per line, `- {...}` | Each change is one line in a Git diff |

### Checking it

```bash
make validate
```

This checks every entry before anything runs, and lists **all** problems at once, each with where it
is (for example `sources.UW_A1_INPUT.checks[3]: unknown key(s) max_bad`). It catches unknown sources,
keys or check types, missing limits, regexes that do not compile, unknown rules, and row filters or
rules that try to do anything but read. The Lambda runs the same validation and stops with the full
list if the file is broken.

### Excel and CSV: the extra options

The same configuration can be kept as an **Excel workbook** (sheets **Sources**, **Checks**,
**Stability**, **Rules**) or as a **folder of CSVs** (`sources.csv`, `checks.csv`, `stability.csv`,
`rules.csv`). Every format is read into the same rows and validated the same way, and the tests check
that all three give an identical configuration.

| Use | Set `DQF_CONFIG_URI` to |
| --- | --- |
| YAML (the default) | `config/dq_config.yaml` (or `s3://bucket/dq_config.yaml`) |
| Excel | `config/dq_config.xlsx` (or `s3://bucket/dq_config.xlsx`) |
| CSV folder | `config/sheets` (local only) |

Converting between them:

```bash
make config-xlsx         # dq_config.yaml -> dq_config.xlsx and config/sheets/*.csv
make config-from-xlsx    # an edited dq_config.xlsx -> dq_config.yaml (YAML comments are not kept)
.venv/bin/python scripts/build_config.py SOURCE TARGET     # any format to any other
```

The converter validates the source first and refuses to convert a broken configuration. Keep **one**
format as the one people edit, which is the YAML file, so the copies cannot drift apart. The Excel and CSV
files in `config/` are generated from it.

In the sheets, each source's checks are rows with a `SOURCE` column instead of being nested under the
source. Everything else is the same.

### Sources: what to watch

One block per table or view (`sources:` in YAML; one row per source on the Sources sheet).

| Column | Meaning | Example |
| --- | --- | --- |
| `SOURCE` | A short name the checks refer to: the block's key in YAML | `UW_A1_INPUT` |
| `TABLE` | `DB.SCHEMA.TABLE` | `DQF_DB.DQF.UW_A1_RECORD` |
| `TS_COLUMN` | The timestamp or date column that places a row in time | `REQUEST_TS` |
| `SCHEDULE` | `hourly`: checks the previous full hour. `daily`: the previous full day (UTC) | `hourly` |
| `ROW_FILTER` | Optional SQL condition; only rows matching it are checked | `EXCLUSION_REASON is null` |
| `DESCRIPTION` | Free text | |

**One table can be several sources.** The sample configuration has three sources, all on the single table
`UW_A1_RECORD`:

| Source | Table | Schedule | Filter | Why |
| --- | --- | --- | --- | --- |
| `UW_A1_INPUT` | `UW_A1_RECORD` | hourly | none | Input checks apply to **every** application |
| `UW_A1_SCORED` | `UW_A1_RECORD` | hourly | `EXCLUSION_REASON is null` | Score checks apply only to applications the models scored |
| `UW_A1_DAILY` | `UW_A1_RECORD` | daily | `EXCLUSION_REASON is null` | Daily volume and PSI / CSI drift |

A source is "this table, seen this way, this often", not a separate table.

### Checks: what to test on each window

One entry per check, under its source's `checks:` (or one row on the Checks sheet). Each check type maps to one GX expectation.

| `CHECK_TYPE` | Fails when | Needs | GX expectation |
| --- | --- | --- | --- |
| `VOLUME` | Fewer than `MIN` (or more than `MAX`) rows in the window | `MIN` or `MAX` | `ExpectTableRowCountToBeBetween` |
| `FRESHNESS` | The newest row is more than `MAX` hours old | `MAX` | `ExpectColumnMaxToBeBetween` on a computed age |
| `MISSING_VALUES`, `FIELD_POPULATION` | More than `MAX_BAD_PCT`% of `FIELD` is empty | | `ExpectColumnValuesToNotBeNull` |
| `ACCEPTABLE_RANGE` | Values outside `MIN`–`MAX` | `MIN` or `MAX` | `ExpectColumnValuesToBeBetween` |
| `ACCEPTED_VALUES` | Values not in `VALUES`. With `'CY'` this is a source **hit rate** | `VALUES` | `ExpectColumnValuesToBeInSet` |
| `PLACEHOLDER_VALUE` | Values in `VALUES` (e.g. `-999`) | `VALUES` | `ExpectColumnValuesToNotBeInSet` |
| `EXPECTED_FORMAT` | Values not matching `REGEX` | `REGEX` | `ExpectColumnValuesToMatchRegex` |
| `VALUE_LENGTH` | Length outside `MIN`–`MAX` | `MIN` or `MAX` | `ExpectColumnValueLengthsToBeBetween` |
| `DUPLICATE_VALUES` | Repeated values | | `ExpectColumnValuesToBeUnique` |
| `SOURCE_COVERAGE` | Rows with no partner in `COMPARE_SOURCE` (joined on `KEY` = `COMPARE_KEY`) | `COMPARE_SOURCE`, `KEY` | `ExpectColumnValuesToBeInSet` on a join |
| `MATCHES_SOURCE` | Both sides have a value and they differ (`COMPARE`: `EXACT`, `TEXT`, `NUMERIC` with `TOLERANCE`) | `COMPARE_SOURCE`, `KEY` | same |
| `LOST_IN_MAPPING` | The other source has a value and this one is empty | `COMPARE_SOURCE`, `KEY` | same |

Other columns:

- **`MAX_BAD_PCT`**: the share of rows allowed to break the check, in percent. Empty means 0. It becomes
  GX's `mostly` (`mostly = 1 − MAX_BAD_PCT / 100`).
- **`ALERT`**: `Yes` sends alerts for this check. `No` still runs and stores the check, so it shows in
  the reports, but sends no email.

The comparison types (`SOURCE_COVERAGE`, `MATCHES_SOURCE`, `LOST_IN_MAPPING`) compare the payload with
the raw data (or any two sources) to catch mapping errors.

### Stability: drift against a benchmark

One entry per score (PSI) or feature (CSI), under a `daily` source's `stability:` (or one row on the Stability sheet).

| Column | Meaning | Default |
| --- | --- | --- |
| `SOURCE`, `FIELD` | What to compare | |
| `INDEX` | `PSI` (a score) or `CSI` (a feature). The calculation is the same | |
| `FIELD_TYPE` | `NUMERIC`: binned at the benchmark's quantiles. `CATEGORICAL`: the benchmark's most common values, plus `OTHER` | |
| `BINS` | Number of bins. Missing values always get their own bin | 10 |
| `BENCHMARK_START`, `BENCHMARK_END` | Fixed dates (`2026-01-01`), or days relative to the run (`-60`, `-30`) | |
| `ACTIVE_DAYS` | The recent period compared with the benchmark | 7 |
| `WARN`, `FAIL` | Limits. `WARN` shows in the reports; `FAIL` alerts | 0.10, 0.25 |

The index is `Σ (recent share − benchmark share) × ln(recent share ÷ benchmark share)`. It is computed
inside Snowflake, so no rows are downloaded.

### Pointing it at the real tables

| Use | Sources | Typical checks |
| --- | --- | --- |
| Underwriting input | The A1 record table, `hourly` | Missing values, source hit rates (`'CY'`), `SSN_TAX_ID_MATCH`, volume, freshness |
| Underwriting output | The same table, `hourly`, `ROW_FILTER` = scored population | Scores 300–900, decision consistent with score |
| Payload against raw data | Both tables, `hourly` | `SOURCE_COVERAGE`, `MATCHES_SOURCE`, `LOST_IN_MAPPING` using the input mapping spreadsheet |
| Risk Monitoring feature store | One `daily` source per model's feature view (`TS_COLUMN` = `LOADDT`) | `VOLUME` (merchants per day), `FRESHNESS`, `MISSING_VALUES` per feature, and a `stability` entry per top feature and per score |

Remember to grant `SELECT` on each new table to `DQF_ROLE`.

## Business rules

Business rules are **written as field conditions, not SQL**, so they can be stated by the people who
own them. Each rule says **when** a row is in scope, and **then** what must be true of it:

```yaml
rules:
  DECISION_MATCHES_SCORE:
    description: No auto-approval with a merchant fraud score below 400
    applies_to: UW_A1_SCORED
    when: {DECISION: AUTO_APPROVE}
    then: {MF_SCORE: {at_least: 400}}
    alert: yes

  MINIMUM_DATA_FOR_MODEL:
    description: A model with the Experian component is used only when Experian returned data
    applies_to: UW_A1_SCORED
    when: {MF_MODEL_USED: [SS_MF_100, SS_MF_101]}
    then:
      BUREAU_INQUIRY_STATUS: CY
      BIZID_INQUIRY_STATUS: CY
    alert: yes

  SCORES_IN_RANGE:
    description: Scores lie between 300 and 900
    applies_to: UW_A1_SCORED
    then:
      MF_SCORE: {between: [300, 900]}
      IT_SCORE: {between: [300, 900]}
      CR_SCORE: {between: [300, 900]}
    alert: yes
```

### The keys of a rule

| Key | Meaning |
| --- | --- |
| `description` | The rule in a sentence. Shown in the alert email and in GX's Data Docs |
| `applies_to` | The source, or a list of sources, the rule checks. It runs on each source's schedule, window and `row_filter` |
| `when` | Which rows the rule is about. Leave it out for every row. Several fields: all must match. A list of field groups: any group |
| `then` | What must be true of those rows. Several fields: each must hold |
| `check` | Instead of `then`: a named custom check written by a developer (see below) |
| `max_bad_pct` | The share of in-scope rows allowed to break the rule, in percent (default 0) |
| `alert` | `yes` sends an email when the rule starts failing, recovers, or keeps failing |

### The words

| In `when` (which rows) | In `then` (what must hold) | Meaning |
| --- | --- | --- |
| `FIELD: value` | `FIELD: value` | is / must be that value |
| `FIELD: [a, b]` | `FIELD: [a, b]` | is / must be one of them |
| `FIELD: present` / `missing` | `FIELD: present` / `missing` | has a value / is empty |
| `{not: v}`, `{not_in: [a, b]}` | the same | is not / must not be |
| `{at_least: n}`, `{at_most: n}`, `{above: n}`, `{below: n}`, `{between: [lo, hi]}` | the same | number comparisons (`above` / `below` exclude the limit) |
| | `{matches: regex}`, `{not_matches: regex}` | must (not) match a pattern |
| | `{length_between: [lo, hi]}` | must be lo to hi characters long |
| | `{equals_field: OTHER}` | must equal another field of the same row |

Several words can be combined on one field: `MF_SCORE: {at_least: 300, at_most: 900}`. Empty values
are not judged by value requirements, as in GX; add `FIELD: present` to require a value. Quote values
YAML would read as something else: `'1'`, `'yes'`, `'Y'`.

For example, "either field empty" is a list of `when` groups:

```yaml
    when:
      - {SSN_LAST4: missing}
      - {TAX_ID_LAST4: missing}
    then: {SSN_TAX_ID_MATCH: Z}
```

### How a rule runs

Nothing is written in SQL. Each `then` field becomes a **built-in Great Expectations expectation**,
and `when` becomes its **row condition** (GX 1.x, "Restrict an Expectation to specific rows"). GX
then generates the SQL and runs it inside Snowflake:

| Rule | Great Expectations |
| --- | --- |
| `when: {DECISION: AUTO_APPROVE}` | `row_condition = Column("DECISION") == "AUTO_APPROVE"` |
| `then: {MF_SCORE: {at_least: 400}}` | `ExpectColumnValuesToBeBetween(column="MF_SCORE", min_value=400, row_condition=…)` |
| `then: {BUREAU_INQUIRY_STATUS: CY}` | `ExpectColumnValuesToBeInSet(column="BUREAU_INQUIRY_STATUS", value_set=["CY"], …)` |
| `then: {CODE: {matches: …}}` | `ExpectColumnValuesToMatchRegex(…)` |
| `then: {A: {equals_field: B}}` | `ExpectColumnPairValuesToBeEqual(column_A="A", column_B="B", …)` |

A rule with several `then` fields is still **one result**: it fails if any field fails, and the alert
says which. It reports the share of the in-scope rows that break it, for example: *"No auto-approval
with a merchant fraud score below 400: broken in 66.7% of rows where DECISION is AUTO_APPROVE (2 of 3
rows). Required: MF_SCORE must be at least 400."* The alert also gives a query to list those rows in
Snowsight, generated from the rule.

`make validate` checks every rule and says where any problem is (`rules.DECISION_MATCHES_SCORE.when.DECISION:
unknown condition 'abov'`).

### Custom checks (developers)

A few rules compare values in ways field conditions cannot. For example, `SSN_TAX_ID_MATCH` must be
`1` when SSN and Tax ID are equal, `0` when they differ, and `Z` when either is empty. A developer
writes such a check once, as a **custom GX expectation** in `src/dqf/expectations.py`, and the rule
uses it by name:

```yaml
  SSN_TAX_ID_MATCH_DERIVED:
    description: SSN_TAX_ID_MATCH follows the documented derivation
    applies_to: UW_A1_INPUT
    check: ssn_tax_id_match_follows_derivation
    alert: yes
```

A custom check is a GX multicolumn map expectation: a per-row yes/no metric with a SQLAlchemy version
(what runs on Snowflake) and a pandas version (for tests), as GX's custom expectation guides describe.
It therefore gets `mostly`, percentages, row conditions (`when`) and Data Docs like a built-in. To add
one, write the metric and the expectation and register it in `CHECKS` with a sentence for the alerts
and a condition for the "see the rows" query. It is code, so it needs a code deploy; using it is
configuration.

## Settings

All settings are environment variables. Locally they come from `.env`; in AWS, from the Lambda's
environment, which the CloudFormation template fills in.

### Snowflake login

| Setting | Meaning |
| --- | --- |
| `SNOWFLAKE_ACCOUNT`, `_USER`, `_ROLE`, `_WAREHOUSE`, `_DATABASE`, `_SCHEMA` | The login |
| `SNOWFLAKE_AUTH` | `keypair` (what the Lambda uses), `password`, or `externalbrowser` (your own SSO, laptop only) |
| `SNOWFLAKE_PRIVATE_KEY_PATH`, `_PASSPHRASE` | The key file, and its passphrase if encrypted |
| `DQF_SNOWFLAKE_SECRET_ID` | **AWS and LocalStack.** When set, the whole login is read from this Secrets Manager secret instead |

The secret is JSON:

```json
{"account": "ORG-ACCOUNT", "user": "DQF_SVC", "role": "DQF_ROLE", "warehouse": "DQF_WH",
 "database": "DQF_DB", "schema": "DQF", "auth": "keypair",
 "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"}
```

### The framework

| Setting | Meaning | Default |
| --- | --- | --- |
| `DQF_CONFIG_URI` | The configuration: a `.yaml` or `.xlsx` path or `s3://bucket/key`, or a CSV folder | `config/dq_config.yaml` |
| `DQF_RESULT_TABLE` | The results table, `DB.SCHEMA.TABLE` | `DQ_RESULT` |
| `DQF_REMIND_HOURS` | Repeat an alert for a check still failing after this many hours. `0` = never | 24 |
| `DQF_DATA_DOCS` | GX's HTML report: a folder, `s3://bucket/prefix`, or empty for none | empty |
| `DQF_REPORT_URL` | The Streamlit report's address. Every alert links to it. Empty = no link | empty |

### Alert settings

| Setting | Meaning |
| --- | --- |
| `DQF_ALERT_CHANNELS` | Any of `sns`, `email`, `log`, comma separated. AWS: `sns,log`. Laptop: `log,email` |
| `DQF_SNS_TOPIC_ARN` | The SNS topic (set by the template) |
| `DQF_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `DQF_EMAIL_FROM`, `DQF_EMAIL_TO` | Direct email. Local inbox: `localhost`, `1025`. Gmail: `smtp.gmail.com`, `587`, your address and an **app password** |
| `DQF_LOCAL_ALERT_CHANNELS`, `DQF_LOCAL_SCHEDULES` | Only for the Lambda in LocalStack |

In AWS, email goes through **SNS**: the template subscribes an address to the topic, and SNS sends the
email. SMTP is only for running on a laptop or in LocalStack.
