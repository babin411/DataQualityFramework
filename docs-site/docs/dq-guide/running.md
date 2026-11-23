# Running it

There are three ways to run the framework. All three run the same code against the same real Snowflake
account.

| Where | How it is started | What it is for |
| --- | --- | --- |
| **Your laptop** | `dqf` command (`make run-hourly`, …) | Developing, trying configuration changes, backfills |
| **Lambda in LocalStack** | `make local-invoke` | Testing the AWS packaging and wiring (S3, Secrets Manager, SNS) before AWS |
| **Lambda in AWS** | EventBridge, every hour and every day | Production. See [Lift and shift to AWS](../dq-aws/index.md) |

## On your laptop

```bash
make run-hourly      # dqf run hourly: the previous full hour
make run-daily       # dqf run daily: yesterday; volume and PSI / CSI
make backfill        # 24 hourly and 7 daily runs of history, without alerts
make show            # a summary of the results table
make test-alert      # a test message through the alert channels in .env
```

The `dqf` command has more options:

| Command | Does |
| --- | --- |
| `dqf run hourly --end "2026-10-17 10:00"` | Check the window ending then (UTC), e.g. to re-run a past hour |
| `dqf run hourly --no-alerts` | Store results but send nothing |
| `dqf backfill hourly 48 --end "2026-10-17 00:00"` | 48 past hourly windows, oldest first, no alerts |
| `dqf describe-table DB.SCHEMA.TABLE` | A table's columns and row count, to help write the checks |
| `dqf check-connection`, `dqf validate`, `dqf init` | Log in; check the configuration; create the results table |
| `dqf -v …` | Verbose log |

A run prints one line per check:

```text
hourly window 2026-10-17 09:00 to 2026-10-17 10:00 UTC   run 3f9c2a1b7d4e
----------------------------------------------------------------------------------
FAIL  UW_A1_INPUT      AVG_TICKET               MISSING_VALUES   56.7% bad rows   at most 30% bad rows
ok    UW_A1_INPUT      (table)                  VOLUME           30 rows          min 1
...
Great Expectations report: /home/…/dq-framework/build/data_docs/site/index.html
23 checks: 22 passed, 0 warnings, 1 failed, 0 errors.
```

## In LocalStack (AWS on your laptop)

LocalStack runs fake AWS services in Docker. The stack deployed there uses **the same CloudFormation
template** as AWS, with `Target=localstack`.

```bash
make local-up            # start LocalStack 4.9 and the mail inbox (http://localhost:8025)
make local-deploy        # build the Lambda folder, put the login in Secrets Manager, deploy, upload dq_config.yaml
make local-test-alert    # the Lambda sends a test alert through SNS and email
make local-invoke        # the Lambda runs the hourly checks
make local-daily         # the Lambda runs the daily checks
make local-alerts        # what the Lambda published to SNS
make local-logs          # the Lambda's log
make local-docs          # the URL of the GX report the Lambda wrote to S3
make local-down          # stop it
```

| After you change | Run |
| --- | --- |
| Code in `src/dqf/` | `make local-build` (the folder is mounted; no redeploy) |
| `dq_config.yaml` (checks, business rules) | `make local-deploy` (uploads it to S3 again) |
| `requirements.txt` or the template | `make local-deploy` |

LocalStack is pinned to **4.9**, the last open-source community release: no account or token, and the
Apache 2.0 licence allows work use. It has no ECR, so the Lambda runs from a mounted folder instead of
the container image AWS uses. The image is tested separately (`make image`).

## What a run produces

### 1. The results table

`DQF_DB.DQF.DQ_RESULT`: one row per check per run.

| Column | Meaning |
| --- | --- |
| `RUN_ID`, `RUN_TS`, `SCHEDULE`, `WINDOW_START`, `WINDOW_END` | Which run, and which window it checked |
| `CHECK_ID` | A stable name: `SOURCE|FIELD|TYPE`, e.g. `UW_A1_INPUT|AVG_TICKET|MISSING_VALUES` |
| `SOURCE`, `TABLE_NAME`, `FIELD_NAME`, `CHECK_TYPE`, `RULE_NAME`, `CRITERIA` | What was checked, and the limit |
| `STATUS` | `PASS`, `WARN` (drift between the warn and fail limits), `FAIL`, or `ERROR` (could not run) |
| `METRIC`, `OBSERVED_VALUE` | What was measured: `bad_pct`, `row_count`, `hours_since_last`, `rows_returned`, `stability_index` |
| `ELEMENT_COUNT`, `UNEXPECTED_COUNT` | Rows checked, rows breaking the check |
| `ALERT_FLAG`, `DETAILS` | Whether it alerts; GX's numbers (or the error) as JSON |

Only counts are stored, never example values, because the data includes personal information.

### 2. The alert email

| When | Example subject |
| --- | --- |
| A check **starts** failing, or can no longer run | `[DQ hourly] 1 new problem in UW_A1_INPUT` |
| A failing check **passes again** | `[DQ hourly] 1 recovered in UW_A1_INPUT` |
| A check is **still failing** after another `DQF_REMIND_HOURS` | `[DQ hourly] 1 still failing in UW_A1_INPUT` |

Nothing is sent when nothing changed. A `WARN` never alerts by itself. Each problem in the email says:

- **what is wrong**, in plain words: *"AVG_TICKET is empty in 56.7% of rows (17 of 30 rows); at most 30%
  allowed."*
- the measured value, the limit, and the table;
- **a query to see the rows**, ready to paste into Snowsight.

The email also lists everything else that is not passing. It has an **Open the data quality report**
button, which links to the Streamlit report (`DQF_REPORT_URL`), and it links to the GX report.

### 3. Great Expectations' report (Data Docs)

GX's standard HTML report: an index of runs, and one page per group of checks, with each expectation,
its result and the measured value.

| Where it ran | How to view it |
| --- | --- |
| Laptop | `make docs`, then <http://localhost:8777/index.html> (or open `build/data_docs/site/index.html`) |
| LocalStack | `make local-docs` prints the URL |
| AWS | `aws s3 sync s3://<bucket>/data_docs/site ./site`, then open `site/index.html` |

Example values are removed from the report, as from the results table.

### 4. The Streamlit report

```bash
make app        # http://localhost:8501
```

It reads only the results table and shows what needs attention now, the status of each field, and the
history of any one check. It is not part of the Lambda. It is a separate web app that keeps running
and reads whatever the latest runs wrote.

The alert's link opens it on the run the email is about:
`http://localhost:8501?schedule=hourly&window=2026-10-17 12:00`. To host it for the team, publish it
to **Streamlit in Snowflake** (`make report-publish`). See [Hosting the report](../dq-aws/report.md).

## Tests

| Command | What it checks | Needs |
| --- | --- | --- |
| `make test` | Reading the configuration in every format, business rules, planning, SQL, alert decisions and messages, the Lambda handler, the image contents | Nothing |
| `make test-snowflake` | Every planted problem in the sample table is caught by the right check, and nothing else fails | `.env`, the sample table re-run recently |
| `DQF_LOCALSTACK_TESTS=1 .venv/bin/python -m pytest -m localstack` | The deployed stack in LocalStack | `make local-up` and `make local-deploy` |

`make test-snowflake` writes only to `DQF_TEST_RESULT_TABLE`, never to the real results table.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Snowflake login settings missing: …` | Fill in `.env` (or the secret, in AWS / LocalStack) |
| `404 Not Found` at login | `SNOWFLAKE_ACCOUNT` is the bare locator; use `ORGNAME-ACCOUNTNAME` |
| `JWT token is invalid` | The public key in Snowflake does not match the private key: re-run the `alter user` line |
| A check shows `ERROR` | It could not run: a wrong column or table, or no `SELECT` grant. The error is in `DETAILS` and in the email |
| No email arrives | `DQF_SMTP_HOST=localhost` sends to the local inbox (<http://localhost:8025>), not to your mailbox. For Gmail use port 587 and an app password |
| `LocalStack is not running` | `make local-up` |
| `docker build` fails on `docker-credential-desktop.exe` | `~/.docker/config.json` names a Windows credential helper WSL cannot run: build with a temporary `DOCKER_CONFIG` folder, or start Docker Desktop with WSL integration |
| Lambda times out at login in AWS | No network path from its subnets to Snowflake: see [Network](../dq-aws/deploy.md#2-network) |
