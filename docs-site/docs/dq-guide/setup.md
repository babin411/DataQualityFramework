# One-time setup

What you do once, on a laptop, before the first run. All commands run in `dq-framework/`.

## What you need

| Tool | Why |
| --- | --- |
| Python 3.12 and [uv](https://docs.astral.sh/uv/) | The framework, its tests and the report |
| A Snowflake account, as ACCOUNTADMIN (or a role that can create users and roles) | To create the service user and the sample table |
| Docker (WSL integration on Windows) | Only for LocalStack and for building the AWS image |

## 1. Install

```bash
make setup        # creates .venv with the package, the Streamlit report and the tests
make test         # unit tests: no Snowflake, no AWS needed
```

## 2. A key pair for the service user

```bash
scripts/make_snowflake_key.sh
```

This writes `keys/dqf_svc_key.p8` (the private key, which never leaves your machine or Secrets
Manager) and prints the **public** key. `keys/` is git-ignored.

## 3. Snowflake objects: `sql/01_setup.sql`

Open the file in Snowsight, paste the public key into the `alter user DQF_SVC` line, and run all of it
as ACCOUNTADMIN. It creates:

| Object | Purpose |
| --- | --- |
| `DQF_DB` database, `DQF_DB.DQF` schema | Where the sample table and the results table live |
| `DQF_WH` warehouse (X-Small, auto-suspend) | Runs the check queries |
| `DQF_ROLE` role | `SELECT` on the tables it checks, `CREATE TABLE` in `DQF_DB.DQF` (for the results table) |
| `DQF_SVC` user, `TYPE = SERVICE` | Logs in with the key, no password, no MFA prompt. What the Lambda uses |

It also grants `DQF_ROLE` to you, so you can look at the results in Snowsight.

!!! note "Monitoring other tables"
    For each real table the framework must check, grant `SELECT` on it to `DQF_ROLE` (and `USAGE`
    on its database and schema). That is the only access it needs to the monitored data.

## 4. The sample table: `sql/02_sample_table.sql`

This creates **one** table, `DQF_DB.DQF.UW_A1_RECORD`: 60 days of made-up Underwriting A1 records, 30
applications an hour, with problems planted in known hours and drift in the last 7 days. The
[sample table](../next-steps/dq-framework-build.md#the-sample-table) section lists them.

"Hour N" counts back from when the SQL ran, so **re-run it shortly before a demo or a test**.

## 5. `.env`

```bash
cp .env.example .env
```

Fill in at least:

| Setting | Value |
| --- | --- |
| `SNOWFLAKE_ACCOUNT` | Snowsight: your name (bottom left) > Account > *Copy account identifier*, in the form `ORGNAME-ACCOUNTNAME` (e.g. `BGFREGI-JI18204`). The bare locator alone does not work outside `us-west-2` |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | The absolute path the key script printed |
| `DQF_EMAIL_TO` | Who gets the alerts |
| `DQF_SMTP_*` | Where the email goes: see [Configuration](configuration.md#alert-settings) |

`.env` is git-ignored and is never copied into the Lambda image. In AWS the same values come from the
Lambda's environment and Secrets Manager.

## 6. Check it

```bash
make check        # logs in; prints the account, DQF_SVC, DQF_ROLE, DQF_WH
make validate     # reads config/dq_config.yaml, lists any problems
make init         # creates the results table DQF_DB.DQF.DQ_RESULT
```

If `make check` fails, see [Troubleshooting](running.md#troubleshooting).
