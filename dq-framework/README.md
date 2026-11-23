# Data Quality Framework (dqf)

Checks data in **Snowflake** against checks and business rules kept in a **YAML file**
(`config/dq_config.yaml`; Excel and CSV also work), using **Great Expectations
(GX)**. It runs on a schedule as an **AWS Lambda**, stores every result in a **Snowflake table**, sends
**alerts through SNS** (email), and has a **Streamlit report**.

It covers two kinds of monitoring:

| Kind | Question | Schedule | Used for |
| --- | --- | --- | --- |
| **Data checks** | Is the data valid, complete, and still arriving? | Hourly (or daily) | Underwriting input and output (the A1 record) |
| **Drift checks** (PSI / CSI) | Does recent data still look like the benchmark the models were built on? | Daily | Model scores and features: Underwriting, and the Risk Monitoring feature store |

```text
EventBridge (hourly, daily) --> Lambda: dqf --GX, Snowflake connector--> Snowflake tables (read only)
                                   |-- reads    the checks and rules, dq_config.yaml (S3)
                                   |-- reads    the Snowflake login (Secrets Manager)
                                   |-- writes   one row per check per run (Snowflake results table)
                                   `-- sends    alerts (SNS topic -> email)
```

For development, everything AWS runs on your machine in **LocalStack**; Snowflake is always the real one.

## Status

| Part | Status |
| --- | --- |
| Configuration reader (YAML, Excel, CSV), business rules, check planning, SQL generation, alert logic, alert channels, Lambda handler | **Tested**: unit tests (`make test`) |
| The configuration-to-expectation mapping | **Tested** on in-memory data with GX's pandas engine |
| PSI / CSI SQL | **Checked** against an independent numpy calculation (same results to 4 decimals) |
| The CloudFormation template on **LocalStack** | **Tested**: deployed; the Lambda ran; a test alert reached SNS and the email inbox |
| The Lambda **container image** for AWS | **Built** and invoked locally |
| Against **your Snowflake account** (2026-10-17) | **Run.** Key-pair login as `DQF_SVC`; hourly run: 23 checks in ~18 s; daily run with PSI / CSI: 8 checks. `make test-snowflake`: 12 of 14 passed; the 2 misses were an off-by-one hour in the sample SQL (fixed; re-run `02_sample_table.sql`) |
| The Lambda **in LocalStack against your Snowflake** | **Run.** Login from Secrets Manager, configuration from S3, results written to Snowflake, alert delivered through SNS and email |
| A real **AWS** deployment | **Not done**: no AWS account access yet. Same template, `Target=aws` |

## Layout

```text
dq-framework/
├── src/dqf/                  the code: the only code that goes to AWS
│   ├── handler.py            Lambda entry point
│   ├── engine.py             one run: configuration -> checks -> results -> alerts
│   ├── workbook.py           read and validate the configuration (YAML, Excel or CSV)
│   ├── plan.py, sql.py       configuration rows -> GX expectations and Snowflake SQL
│   ├── rules.py              business rules (when / then) -> GX expectations with row conditions
│   ├── expectations.py       custom GX expectations, for logic field conditions cannot express
│   ├── snowflake.py          logins, GX data source, results table
│   ├── alerts.py             SNS, email (SMTP), log: the alert message
│   ├── explain.py            plain-language explanation and a "see the rows" query per result
│   ├── datadocs.py           Great Expectations' own HTML report (Data Docs)
│   ├── settings.py, cli.py   settings; the `dqf` command
├── config/dq_config.yaml     the checks and business rules: the main configuration
├── config/dq_config.xlsx     the same as Excel, and config/sheets/*.csv as CSVs (generated: make config-xlsx)
├── sql/01_setup.sql          Snowflake: warehouse, schema, role, service user
├── sql/02_sample_table.sql   Snowflake: the sample table, with planted problems
├── Dockerfile                the Lambda image for AWS
├── requirements.txt          what the Lambda installs (pinned)
├── infra/cloudformation.yaml one template: AWS (Target=aws) and LocalStack (Target=localstack)
├── infra/deploy.sh           deploy to AWS
├── local/docker-compose.yml  LocalStack and a local mail inbox
├── scripts/local.py          build, deploy, invoke, read alerts on LocalStack
├── scripts/make_snowflake_key.sh   the service user's key pair
├── app/streamlit_app.py      the report
└── tests/                    unit tests; opt-in tests for Snowflake and LocalStack
```

## 1. Snowflake set-up (once)

```bash
make setup                       # .venv with the package, the report and the tests
scripts/make_snowflake_key.sh    # a key pair for the service user; prints the public key
```

1. Open `sql/01_setup.sql` in Snowsight, paste the public key into the `alter user DQF_SVC` line, and
   run it all as ACCOUNTADMIN. It creates warehouse `DQF_WH`, schema `DQF_DB.DQF`, role `DQF_ROLE` and the
   service user `DQF_SVC`, which logs in with the key (no password).
2. Run `sql/02_sample_table.sql`. It creates `DQF_DB.DQF.UW_A1_RECORD` (below).
3. `cp .env.example .env` and fill in `SNOWFLAKE_ACCOUNT` (Snowsight: your name, bottom left > Account
   > copy the account identifier) and `SNOWFLAKE_PRIVATE_KEY_PATH` (the script prints it).
4. `make check` should print the account, `DQF_SVC`, `DQF_ROLE`, `DQF_WH`.

Credentials stay in `.env` and `keys/`, both git-ignored, and never go into the image. In AWS and
LocalStack the login lives in Secrets Manager.

### The sample table

`DQF_DB.DQF.UW_A1_RECORD`: one row per merchant application, shaped like the Underwriting A1 record
(Implementation Support Document): application fields, the status of each data source, model
features, and the model output. 60 days, 30 applications an hour, ending at the current hour.

| Columns | What they hold |
| --- | --- |
| `APPLICATION_ID`, `REQUEST_TS` | The application and when it arrived |
| `CLIENT_ID`, `CHANNEL`, `MERCH_ID`, `MCC`, `STATE`, `ZIP`, `ENQUIRY_DATE`, `AVG_TICKET`, `ANNUAL_VOLUME` | Application data |
| `SSN_LAST4`, `TAX_ID_LAST4`, `SSN_TAX_ID_MATCH` | The documented derivation: `1` equal, `0` different, `Z` either missing |
| `BUREAU_INQUIRY_STATUS`, `BIZID_INQUIRY_STATUS`, `CLN_INQUIRY_STATUS`, `NEU_INQUIRY_STATUS` | Did each source return data (`CY`) |
| `TMX_REQUEST_ID`, `TMX_REQUEST_RESULT` | ThreatMetrix request |
| `AAPBIZ_SCORE_PRIN`, `TXN_PER_DAY_PERCENTILE`, `HOMEBASED_PHONE_FLAG`, `JAVASCRIPT_FONT_COUNT` | Model features |
| `EXCLUSION_REASON` | `ADDITIONAL_OUTLET` / `WISO`: not scored by the A1 models, so no output |
| `MF_MODEL_USED`, `MF_SCORE`, `IT_SCORE`, `CR_SCORE`, `DECISION` | Model output (scores 300–900) |

| Planted | What | Caught by |
| --- | --- | --- |
| hour 1 | `AVG_TICKET` missing for 60% | `MISSING_VALUES` (alerts) |
| hour 2 | `SSN_TAX_ID_MATCH` derived wrongly | rule `SSN_TAX_ID_MATCH_DERIVED` |
| hour 3 | `SSN_TAX_ID_MATCH = 'Y'` | `ACCEPTED_VALUES`, and the rule |
| hour 4 | Experian bureau returns no data for 75% | `ACCEPTED_VALUES` on `BUREAU_INQUIRY_STATUS` (hit rate) |
| hour 5 | Malformed ZIP codes | `EXPECTED_FORMAT` |
| hour 6 | Duplicated application IDs | `DUPLICATE_VALUES` |
| hour 7 | `MERCH_ID` too short | `VALUE_LENGTH` |
| hours 8–10 | No applications at all | `VOLUME`; `FRESHNESS` after 2 hours |
| hour 11 | `MF_SCORE` 950 | rule `SCORES_IN_RANGE` |
| hour 12 | Auto-approved with `MF_SCORE` below 400 | rule `DECISION_MATCHES_SCORE` |
| last 7 days | `MF_SCORE` 60 points lower; `AVG_TICKET` 60% higher; 60% `API` channel | PSI / CSI **fail** |
| last 7 days | `TXN_PER_DAY_PERCENTILE` slightly shifted | CSI **warns** |

"Hour N" counts back from the start of the current hour when the SQL ran. Re-run the SQL shortly
before a demo or a test.

## 2. Run from your machine

```bash
make validate      # read config/dq_config.yaml, list any problems (no Snowflake)
make init          # create the results table DQF_DB.DQF.DQ_RESULT
make run-hourly    # check the last full hour: AVG_TICKET missing fails, an alert is sent
make run-daily     # check yesterday: volume, and PSI / CSI drift against the benchmark
make backfill      # 24 hourly and 7 daily runs of history, no alerts
make app           # the report at http://localhost:8501
make report-publish   # host the report in Streamlit in Snowflake (after sql/03_report_app.sql once)
make test-snowflake   # every planted problem caught by the right check, and nothing else
```

Alerts from your machine go to `DQF_ALERT_CHANNELS` (default `log,email`). Email goes to the local
mail inbox (`make local-up`, then <http://localhost:8025>). For real email, set the `DQF_SMTP_*`
settings in `.env` (Gmail: `smtp.gmail.com`, port 587, an app password).

## 3. Run the Lambda in LocalStack (AWS on your machine)

```bash
make local-up           # LocalStack + the mail inbox
make local-deploy       # build the code, store the Snowflake login, deploy the CloudFormation stack
make local-check        # the Lambda logs in to Snowflake and reads the configuration (runs nothing)
make local-test-alert   # the Lambda sends a test alert: through SNS and by email
make local-alerts       # what reached the SNS topic
make local-invoke       # the hourly checks, run by the Lambda
make local-daily        # the daily checks
make local-logs         # the Lambda's log
make local-down
```

`make local-deploy` deploys **the same `infra/cloudformation.yaml`** that AWS uses, with
`Target=localstack`. The differences are only these:

| | AWS | LocalStack |
| --- | --- | --- |
| Lambda code | Container image from ECR | The folder `build/lambda`, mounted (LocalStack's free edition has no ECR) |
| Alerts | SNS, with email subscribers | SNS (read with `make local-alerts`) and email to the local inbox |
| Schedules | On | Off by default; invoke by hand (`DQF_LOCAL_SCHEDULES=ENABLED` to turn on) |
| Error alarm | CloudWatch alarm to the topic | Not created |

After a code change: `make local-build` (the folder is mounted, so no redeploy). After a configuration
change: `make local-deploy` (rebuilds the folder and re-uploads `dq_config.yaml`).

LocalStack is pinned to **4.9**, the last open-source community release: it needs no account or
token, and its licence (Apache 2.0) allows work use. Newer LocalStack releases need an account, and
their free plan is for non-commercial use only.

## 4. Deploy to AWS

You need: the Snowflake service user (step 1), an AWS account with ECR, and a network path from the
Lambda to Snowflake (in a VPC: NAT or PrivateLink; the AWS2SF Lambda already has one).

1. Store the login in Secrets Manager (once):

   ```json
   {"account": "...", "user": "DQF_SVC", "role": "DQF_ROLE", "warehouse": "DQF_WH", "database": "DQF_DB",
    "schema": "DQF", "auth": "keypair", "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"}
   ```

   `aws secretsmanager create-secret --name dqf/snowflake --secret-string file://secret.json`, then delete
   `secret.json`.
2. Deploy:

   ```bash
   STACK=dqf SNOWFLAKE_SECRET_ARN=arn:aws:secretsmanager:...:secret:dqf/snowflake-AbCdEf \
   RESULT_TABLE=DQF_DB.DQF.DQ_RESULT ALERT_EMAIL=me@example.com infra/deploy.sh
   ```

   It builds and pushes the image, deploys the stack (Lambda, role, hourly and daily schedules, SNS
   topic with the email subscription, an error alarm, the configuration bucket) and uploads `dq_config.yaml`.
3. Check the login, network and configuration without running anything:
   `aws lambda invoke --function-name dqf-run --payload '{"action": "check"}' --cli-binary-format raw-in-base64-out /dev/stdout`
4. Confirm the subscription from the email AWS sends, then test:
   `aws lambda invoke --function-name dqf-run --payload '{"action": "test_alert"}' --cli-binary-format raw-in-base64-out /dev/stdout`

Optional `deploy.sh` settings for company accounts: `PERMISSIONS_BOUNDARY_ARN`, `SECRET_KMS_KEY_ARN`,
`TAGS`, `SCHEDULES=DISABLED` (deploy with the schedules off), and `SKIP_BUILD=1 IMAGE_URI=…` (deploy an
image a CI pipeline built). The full plan, with what to ask the platform, network and Snowflake teams
for, is in the docs site: *DQ Framework: Lift & Shift to AWS*.

If your team deploys through its own pipeline, keep the `Dockerfile` and the template's settings and
use that pipeline.

## The configuration

`config/dq_config.yaml` holds everything that is checked: one block per **source** (a table, seen a
certain way, on a schedule) with its **checks** and **stability** (drift) entries, and the **business
rules**. Run `make validate` after every change. The keys are the column names below, in lower case.

```yaml
sources:
  UW_A1_SCORED:
    table: DQF_DB.DQF.UW_A1_RECORD
    ts_column: REQUEST_TS
    schedule: hourly
    row_filter: EXCLUSION_REASON is null
    checks:
      - {field: MF_SCORE, check_type: MISSING_VALUES, max_bad_pct: 0, alert: yes}
rules:
  DECISION_MATCHES_SCORE:
    description: No auto-approval with a merchant fraud score below 400
    applies_to: UW_A1_SCORED
    when: {DECISION: AUTO_APPROVE}            # which rows
    then: {MF_SCORE: {at_least: 400}}         # what must hold
    alert: yes
```

**Business rules** are written as field conditions, with no SQL: `when` (which rows; leave out for all)
and `then` (what must hold), using words such as `at_least`, `between`, `[a, b]`, `present`, `matches`
and `equals_field` (the full list is at the top of `src/dqf/rules.py`). Each `then` field becomes a
built-in GX expectation restricted by a GX row condition, so GX writes the SQL. Logic that field
conditions cannot express is a custom GX expectation in `src/dqf/expectations.py`, used by name:
`check: ssn_tax_id_match_follows_derivation`.

**Excel and CSV** are extra formats with the same content (sheets or CSVs Sources, Checks, Stability,
Rules). Set `DQF_CONFIG_URI` to `config/dq_config.xlsx` or `config/sheets` to use one. Converting:
`make config-xlsx` (YAML to Excel and CSV), `make config-from-xlsx` (Excel to YAML), or
`scripts/build_config.py SOURCE TARGET`. The tables below give each column (or YAML key):

**Sources**: one per table (or view) to monitor.

| Column | Meaning |
| --- | --- |
| `SOURCE` | A short name (the block's key in YAML) |
| `TABLE` | `DB.SCHEMA.TABLE` |
| `TS_COLUMN` | The timestamp or date column that places a row in time |
| `SCHEDULE` | `hourly` (checks the previous full hour) or `daily` (the previous full day, UTC) |
| `ROW_FILTER` | Optional condition, e.g. the scorable population: `EXCLUSION_REASON is null` |

**Checks**: one per check on one window.

| `CHECK_TYPE` | What fails | Needs |
| --- | --- | --- |
| `VOLUME` | Fewer than `MIN` (or more than `MAX`) rows in the window | `MIN` or `MAX` |
| `FRESHNESS` | The newest row is older than `MAX` hours | `MAX` |
| `MISSING_VALUES`, `FIELD_POPULATION` | More than `MAX_BAD_PCT`% of `FIELD` empty | |
| `ACCEPTABLE_RANGE` | Values outside `MIN`–`MAX` | `MIN` or `MAX` |
| `ACCEPTED_VALUES` | Values not in `VALUES` (`'A','B'`); also a hit rate: `'CY'` | `VALUES` |
| `PLACEHOLDER_VALUE` | Values in `VALUES` (`-999`) | `VALUES` |
| `EXPECTED_FORMAT` | Values not matching `REGEX` | `REGEX` |
| `VALUE_LENGTH` | Length outside `MIN`–`MAX` | `MIN` or `MAX` |
| `DUPLICATE_VALUES` | Repeated values | |
| `SOURCE_COVERAGE` | Rows with no partner in `COMPARE_SOURCE` (joined on `KEY` / `COMPARE_KEY`) | `COMPARE_SOURCE`, `KEY` |
| `MATCHES_SOURCE` | Both sides have a value and they differ (`COMPARE`: `EXACT`, `TEXT`, `NUMERIC` with `TOLERANCE`) | `COMPARE_SOURCE`, `KEY` |
| `LOST_IN_MAPPING` | The compared source has a value, this one is empty | `COMPARE_SOURCE`, `KEY` |

`MAX_BAD_PCT` is the share of rows allowed to break the rule (empty = 0). `ALERT = Yes` sends an alert.
The three comparison types compare the payload with the raw data (or any two sources), for mapping
errors.

**Stability**: one per score (PSI) or feature (CSI).

| Column | Meaning |
| --- | --- |
| `SOURCE`, `FIELD` | What to compare. Runs on the source's schedule (use a `daily` source) |
| `INDEX` | `PSI` (a score) or `CSI` (a feature). Same calculation |
| `FIELD_TYPE` | `NUMERIC` (cut at the benchmark's quantiles) or `CATEGORICAL` (top values + OTHER) |
| `BINS` | Number of bins (default 10). Missing values get their own bin |
| `BENCHMARK_START`, `BENCHMARK_END` | Fixed dates, or days relative to the run (`-60`, `-30`) |
| `ACTIVE_DAYS` | The recent period compared with the benchmark (default 7) |
| `WARN`, `FAIL` | Limits (defaults 0.10 and 0.25). `WARN` shows in the report; `FAIL` alerts |

### Pointing it at real tables

- **Underwriting**: a source per table: the A1 output table and the full-response table
  (paths to be confirmed), hourly. Comparison checks between them use the input mapping spreadsheet
  (model field ↔ raw field).
- **Risk Monitoring feature store**: a `daily` source per model's feature view (one row per merchant
  per `loaddt`): `VOLUME` (merchants per day), `FRESHNESS` (did today's load arrive), `MISSING_VALUES`
  per feature, and a stability entry per top feature (CSI) and per model score (PSI).

## Results and alerts

Results table: one row per check per run (`SCHEDULE`, `WINDOW_START`, `WINDOW_END`, `CHECK_ID`, `SOURCE`,
`FIELD_NAME`, `CHECK_TYPE`, `STATUS` = PASS / WARN / FAIL / ERROR, `METRIC`, `OBSERVED_VALUE`, `CRITERIA`,
counts, `EXPLANATION`, `INVESTIGATE_SQL`, and `ALERT_SENT` = Yes when the check was in the alert that went
out). Re-running a window replaces its rows. Only counts are stored, never example values. The last three
columns were added later: `make init` (or the next run) adds them to an existing table, and older rows
leave them empty.

One message per run, for checks with `ALERT = Yes`, when a check **starts** failing (or cannot run),
**recovers**, or has been failing for another `DQF_REMIND_HOURS` (a reminder). WARN never alerts by itself.

Each problem in the message says, in plain words, **what is wrong** (for example "AVG_TICKET is empty in
56.7% of rows (17 of 30 rows); at most 30% allowed"), the measured value and the limit, the table, and a
**query to see the rows** behind it, ready to paste into Snowsight. The message also lists everything
else currently not passing, warnings included. Email gets an HTML version; SNS and the log get plain
text (`src/dqf/explain.py` writes the explanations and queries).

## The Streamlit report, and the link in every alert

The report (`app/streamlit_app.py`) is not part of the Lambda: it is a separate web app that reads the
results table. Host it in **Streamlit in Snowflake**: run `sql/03_report_app.sql` once as ACCOUNTADMIN,
then `make report-publish`, which uploads the app and prints its address. Set that address as
`DQF_REPORT_URL` (`.env`) and `REPORT_URL` (`infra/deploy.sh`): every alert then has an *Open the data
quality report* button. For other hosts, the link adds `?schedule=…&window=…` so the report opens on
the run the email is about.

The report has the same layout as the POC's dashboard (`../AWSSuportWorkPOC`): cards (checks, passed,
warnings, failed, could not run, alerts sent) and four tabs: **Executive Summary** (results, record vs
file level, fields needing attention, every window side by side and a field-by-window heatmap), **Types
of Checks**, **All Check Results**, and **Fail Details** (the explanation, the query to see the rows, the
check across windows, and the alerts sent). Pick the schedule and window in the sidebar, or click a
window in a chart; clicking a slice, bar or cell filters every tab.

- **Record level**: each row is judged; the check fails when the share of bad rows passes `MAX_BAD_PCT`.
- **File level**: one number for the whole window: `VOLUME`, `FRESHNESS`, `PSI`, `CSI`.

`DQF_REPORT_DAYS` (default 14) sets how far back it reads; `DQF_REPORT_WINDOWS` (default 48) how many
windows the "across windows" charts show. `make test` checks it with Streamlit's AppTest
(`tests/test_dashboard.py`).

## Great Expectations' own report (Data Docs)

Besides the email, the results table and the Streamlit report, every run also writes GX's standard
HTML report: an index of runs, and one page per group of checks with each expectation, pass or fail,
and the measured value. Set by `DQF_DATA_DOCS`:

| Where it runs | `DQF_DATA_DOCS` | How to view it |
| --- | --- | --- |
| Your machine | `build/data_docs` | `make docs`, then <http://localhost:8777/index.html> (or open `build/data_docs/site/index.html`) |
| Lambda in LocalStack | `s3://<stack bucket>/data_docs` (set by the template) | `make local-docs` prints the URL, e.g. `http://localhost:4566/dqf-local-config-…/data_docs/site/index.html` |
| Lambda in AWS | `s3://<stack bucket>/data_docs` | `aws s3 sync s3://<bucket>/data_docs/site ./site` and open `site/index.html`. The bucket is private; serve it to a team through CloudFront with sign-in if needed |

The checks run in memory, so the Snowflake login is never written into GX's files. Example bad values
are removed from the report, as from the results table: only counts are kept. In S3 the index lists the
last 7 days of runs (`HISTORY_DAYS` in `src/dqf/datadocs.py`); older pages stay in the bucket. If the
report cannot be written, the run, its results and its alerts are not affected.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Snowflake login settings missing: …` | Fill in `.env` (or the secret, in AWS / LocalStack) |
| `JWT token is invalid` | The public key in Snowflake does not match the private key: re-run the `alter user` line |
| A check shows `ERROR` | It could not run (wrong column or table, a SQL error). The message is in `DETAILS` |
| `LocalStack is not running` | `make local-up` |
| `docker build` fails on `docker-credential-desktop.exe` | `~/.docker/config.json` names a Windows credential helper WSL cannot run: remove its `credsStore` line, or start Docker Desktop with WSL integration |
| Lambda times out at login in AWS | No network path from its subnets to Snowflake |
