# Context handover

Read this first in a new session. It explains the work in this folder, where it stands, and how the
user wants it done. Last updated 2026-10-17 (framework rebuilt from scratch in `dq-framework/`).

## 1. Who and what

- The user works on a **Risk / Decision Science team** (GitLab group `gbs/platform-product-risk-ops`).
  The team deploys **Underwriting (UW) real-time decisioning** models on **AWS + Snowflake**.
- **The user's main priority (since 2026-10-08) is the Data Quality (DQ) monitoring framework.**
  Underwriting is its first use case. UW model deployment work runs alongside it; the user shadows the
  deployment team and helps with deployments.
- Manager's target (check-in on about 2026-10-12): **fully automated DQ in about three weeks**, with a
  short catch-up **every other day**. Compliance, model risk management (MRM) and partner-bank
  reporting will want these reports.

### People

Spellings come from emails and Teams where known. Use **they/them** for everyone.

| Person | Role in this work |
| --- | --- |
| The manager | Sets priorities; asked for the template-driven checks, the fixed report pages and the hourly email alerts. The name is not confirmed (transcripts garble it) |
| Austin Geary | Deployment team. Owns an existing Lambda that sends emails |
| Amogh Agarwal | Deployment team (transcripts say "Amok") |
| Kushal Basnet | Deployment team. Michelle's email was addressed to Kushal |
| Aryan Patel | Modeller. Wrote the MF composite-model change (see section 6) |
| Srayasee Chakraborty | Modelling contact asked to fill in the DQ templates; **document owner** of the UW US V2 Implementation Support Document. The transcripts say "Shreya" |
| Ameya Jadhavar | **Technical owner** of the UW US V2 Implementation Support Document |
| Michelle Davis | Business stakeholder. Her email added two requirements (section 3) |
| Mohammed Moheed Tai | Author of the composite-layer config in the MF aggregate repo |

## 2. How the user wants to work

These are firm. Follow them without being asked again.

- **Real Snowflake only, through the Snowflake Python connector.** No Snowflake emulator, no SQLite.
  The user rejected both. The user's own Snowflake account (AWS-hosted, ACCOUNTADMIN, database
  `DQF_DB`, created by `sql/01_setup.sql`) is the development account. Its existing `MY_DB` gave
  ACCOUNTADMIN a "does not exist or not authorized" error, so the framework uses its own database.
- **AWS is simulated with LocalStack** for development (the user asked for it), but everything is
  designed for real AWS: one CloudFormation template serves both (`Target=aws|localstack`).
- **Lift-and-shift.** The project folder deploys straight to AWS: build the image, push to ECR, deploy
  the template. Code that only helps on a laptop must not go into the image.
- **Alerts:** **SNS** in AWS. **Email through SMTP** when running locally (a local mail inbox by default).
- **Credentials never go in the chat.** They go in `dq-framework/.env` and `dq-framework/keys/`
  (git-ignored); in AWS and LocalStack, Secrets Manager. Never print them.
- **Say plainly what has and has not run** against real Snowflake or AWS. The user once asked "how
  did you test this, we haven't connected Snowflake?". Be exact about it.
- **Keep it simple and literal.** When the user asks for one sample table, give one table, not a
  simulated pipeline (this happened; it was corrected).
- **Docs:** clear, plain explanations with Mermaid diagrams whose numbering follows the arrows. The user
  asks "explain properly" when something is abstract; a concrete worked example usually settles it.
- **Nothing is committed to git.** The user has not asked for commits.

## 3. The Data Quality framework: what and why

**Goal:** check UW data in Snowflake once an hour against rules written by stakeholders in an
**Excel template**, store the results, alert by email, and show a simple report.

**Flow** (from Michelle Davis's email, 2026-10-11):

```text
Onboarding system -> MRD (pulls third-party data and look-ups, maps it into the API structure)
                  -> calls the UW model service with the payload
MRD stores the raw data in Snowflake. The model service saves the input payload in Snowflake.
```

- **A1** is today's API payload structure. The new V2 models went live on it on **15 October 2026**
  (Day 1: 56 added input fields, 11 new output fields). **A2** (full response from each source plus a
  header) replaces it in **January 2027** (Day 2).
- Day 1 Snowflake tables: an **A1 output** table and a **full response** table (paths not known yet).
  The full-response table is probably the "raw data" to compare with the payload (inferred).
- The **Implementation Support Document** (UW US V2, draft 9-7-2026) is summarised in
  `docs-site/docs/next-steps/implementation-support-doc.md`: 10-step process flow, data sources
  (application, TINCheck, GNF, RNF, Mastercard MATCH, LexisNexis + Emailage + ThreatMetrix, Neustar,
  Experian bureau (to become Equifax) and BIZ ID), component models `CS_*` and score-of-scores `SS_*`,
  calibration 300–900 (base 600, odds 20, PDO 20), alignment defaults (×1, +0), exclusions (additional
  outlets, WISO), testing and release sign-off (which requires monitoring and alerting).
- The **input mapping spreadsheet** `AWS_US_INPUT_MAPPING_NEW_UW_MODEL_DEVELOPMENT_V6.xlsx` (model
  field → source → raw field → raw file) is the mapping sheet the comparison checks need.
- Monitoring starts with the **input** (the payload). Checks run **after the data lands** in
  Snowflake, hourly; they never block a request.
- **New requirements from Michelle's email:**
  1. **Compare the raw data with the payload** (mapping errors have happened). Built as the check
     types `SOURCE_COVERAGE`, `MATCHES_SOURCE` and `LOST_IN_MAPPING`.
  2. **Alert on no activity**: the A1 analytic dataset got no applications from 2026-10-10 and nobody
     knew. Built as `FRESHNESS` plus a reminder every 24 hours while a check stays failing.

**Kinds of check:** field checks (missing values, field population, range, accepted values, format,
duplicates, length, placeholder, business rules); activity (`VOLUME`, `FRESHNESS`); comparison with
source (the three above).

**The template** has one row per check. Columns: `TABLE, TS_COLUMN, FIELD, CHECK_TYPE, MIN, MAX,
VALUES, REGEX, MAX_BAD_PCT, RULE_NAME, SOURCE_TABLE, SOURCE_FIELD, KEY, COMPARE, TOLERANCE, ALERT`.
`MAX_BAD_PCT` = the share of rows allowed to break the rule. Every row becomes a standard Great
Expectations (GX) expectation at run time; business rules are custom SQL in `business_rules.py`.

## 4. The code: `dq-framework/` (built from scratch on 2026-10-17)

The earlier `dq-poc/` folder was deleted by the user; `dq-framework/` replaces it. Its `README.md` is
the full guide; `docs-site/docs/next-steps/dq-framework-build.md` explains the design.

- **Package `dqf`** (`src/dqf/`): `handler.py` (Lambda), `engine.py` (one run), `workbook.py` (Excel
  config), `plan.py` + `sql.py` (rows → GX expectations and Snowflake SQL), `rules.py` (business rules),
  `snowflake.py` (logins, GX data source, results table), `alerts.py` (sns, email, log), `cli.py` (`dqf`).
- **Configuration**: `config/dq_config.xlsx`, three sheets: **Sources** (table, TS column, schedule
  hourly|daily, row filter), **Checks** (the check types, incl. FRESHNESS, comparisons with a source,
  BUSINESS_RULE), **Stability** (PSI for scores / CSI for features, benchmark as dates or relative days
  like -60..-30, WARN 0.10 / FAIL 0.25). CSV copies in `config/sheets/` (`make config` rebuilds the xlsx).
- **Two schedules**: hourly (previous full hour; UW input/output checks) and daily (previous full day;
  volume and PSI/CSI drift; also the Risk Monitoring feature store, which is a daily batch).
- **Snowflake**: `sql/01_setup.sql` (warehouse DQF_WH, schema DQF_DB.DQF, role DQF_ROLE, service user
  DQF_SVC with a key pair from `scripts/make_snowflake_key.sh`); `sql/02_sample_table.sql` (ONE table,
  `DQF_DB.DQF.UW_A1_RECORD`, A1-record shape, 60 days, problems planted in hours 1–12 and drift in the
  last 7 days). Results go to `DQF_DB.DQF.DQ_RESULT`.
- **AWS**: `Dockerfile` (container image; deps are ~490 MB, above the 250 MB zip limit),
  `infra/cloudformation.yaml` (Lambda, role, S3 workbook bucket, SNS topic + email subscription,
  EventBridge hourly/daily rules, error alarm), `infra/deploy.sh`.
- **LocalStack**: `local/docker-compose.yml` pins **LocalStack 4.9** (last open-source community
  release: no account/token, Apache 2.0, so work use is fine; the current release needs a token and its
  free plan is non-commercial). It has no ECR, so locally the Lambda runs from the mounted folder
  `build/lambda` (CloudFormation `S3Bucket: hot-reload`). Also a mailpit inbox at localhost:8025.
  `scripts/local.py` builds, deploys (secret from .env), invokes, reads alerts and logs.
- **Alerts** explain each problem in plain words with a "see the rows" SQL query (`explain.py`), as
  HTML email (Gmail SMTP from the laptop and LocalStack; the user's Gmail app password is in `.env`)
  and plain text for SNS. **GX Data Docs** (`datadocs.py`, `DQF_DATA_DOCS`): local `build/data_docs`,
  S3 `data_docs/` in AWS/LocalStack (GX 1.24 has no S3 store: built in /tmp, last 7 days pulled, pushed
  back). Example values are stripped everywhere.
- **Docs site**: sections "DQ Framework: How It Works" (`docs/dq-guide/`: setup, configuration,
  running, modules) and "DQ Framework: Lift & Shift to AWS" (`docs/dq-aws/`: what to ask for, step by
  step, after go-live). Lambda `{"action": "check"}` (`make local-check`) tests login + workbook only.
  `deploy.sh` options: PERMISSIONS_BOUNDARY_ARN, SECRET_KMS_KEY_ARN, TAGS, SCHEDULES, SKIP_BUILD+IMAGE_URI.
- **Report hosting**: Streamlit report is separate from the Lambda; recommended host Streamlit in
  Snowflake (`sql/03_report_app.sql` once as ACCOUNTADMIN, then `make report-publish`, not yet run:
  needs those grants). Alerts link to `DQF_REPORT_URL` / deploy `REPORT_URL` with `?schedule=&window=`.
- **No rows leave Snowflake** (2026-10-22, verified in query history): GX result format
  `partial_unexpected_count: 0` (no example values) and `snowflake.column_names_without_rows()` (GX 1.24
  ran query assets unchanged to read column names, returning the window's rows; now `limit 0`).
  Docs page `dq-guide/end-to-end.md` covers the whole flow, plus a proposed YAML config (not built).
- **Configuration is YAML** (2026-10-22): `config/dq_config.yaml` is the main config (sources with
  their checks/stability, and `rules:`). Excel (`dq_config.xlsx`, with a Rules sheet; WHEN/THEN cells
  hold YAML text) and a CSV folder (`config/sheets`) are generated options (`make config-xlsx`,
  `make config-from-xlsx`, `scripts/build_config.py SRC DST`). PyYAML added to requirements.
- **Rules have NO SQL** (2026-10-22, user decision): `when` / `then` field conditions with `applies_to`,
  built as GX built-in expectations + `row_condition` (`rules.py`); one result per rule (`engine.combine`).
  Custom GX expectations for the rest (`expectations.py`: `ssn_tax_id_match_follows_derivation`, a
  MulticolumnMapExpectation with SQLAlchemy + pandas metrics), used via `check:`. The old
  `breaks_when`/`sql` and Python `RULES` are gone. Check IDs unchanged (`SOURCE||RULE`). Rule METRIC is
  now bad_pct (was rows_returned). Verified on Snowflake (planted hours) and LocalStack; test-snowflake
  12/14 (the 2 known sample-data misses).
- **Commands**: `make docs`, `make local-docs`, `make setup`, `make validate|check|init|run-hourly|run-daily|backfill|show|app`,
  `make local-up|local-deploy|local-test-alert|local-invoke|local-daily|local-alerts|local-logs`,
  `make test` (unit), `make test-snowflake` (real Snowflake, planted problems), `make image`.

### Status (be exact about this)

| Part | Status |
| --- | --- |
| Unit tests (20) | Pass |
| PSI/CSI SQL | Matches an independent numpy calculation |
| CloudFormation on LocalStack 4.9 | Deployed; Lambda ran; test alert reached SNS (read via SQS) and the mail inbox |
| AWS container image | Built (1.1 GB) and invoked locally |
| Against the user's **Snowflake** (2026-10-17) | **Run**: account `BGFREGI-JI18204` (AWS ap-south-1), user DQF_SVC (key pair), DB DQF_DB.DQF. Hourly 23 checks ~18 s; daily 8 checks incl. PSI/CSI; `make test-snowflake` 12/14 (2 misses = sample SQL off-by-one hour, fixed; user must re-run 02) |
| Lambda in LocalStack against real Snowflake | **Run**: secret from Secrets Manager, workbook from S3, results written, alert via SNS + mailpit |
| Real AWS deployment | Not done (no AWS access) |

## 5. Where we stopped, and what is next

Done on 2026-10-17: Snowflake set-up (01), sample table (02), `.env` filled, `make check`, hourly and
daily runs, `make test-snowflake` (12/14), Lambda in LocalStack against real Snowflake.

Fixed after the first real runs: GX query assets must start with SELECT (drift query wrapped); sample
SQL bugs (NORMAL() needs constant args; channel mix in the last week; off-by-one hour in the UPDATE).

Next: the user re-runs `sql/02_sample_table.sql` (fixed version), then `make test-snowflake` should
pass 14/14; then `make backfill` and `make app` for the report; then real UW tables when paths are known.

Open questions for the real rollout: Snowflake paths and timestamp columns of the **A1 output** and
**full response** tables (Ameya Jadhavar); the input mapping spreadsheet; the feature store's model
views and which features to drift-check; the no-activity limit; alert recipients; AWS account access.

## 6. Other work in this folder

### Docs site: `docs-site/` (MkDocs Material)

Build: `cd docs-site && uv run mkdocs build`. The only build warning is a pre-existing broken link in
`index.md` to `components.md`. Mermaid diagrams are render-checked with a headless browser in the
scratchpad (`check.mjs`, needs locally downloaded libnspr4/libnss3/libasound2 and `LD_LIBRARY_PATH`).

| Section | What it covers |
| --- | --- |
| Underwriting Decisioning | The UW real-time architecture: config tables cached to S3, ALB → Post-ALB Lambda → Step Functions (MF / IDT / Credit primitives, composites), rules engine, 5a → 5b → 5c logging to Snowflake |
| Feature Risk Monitoring | The feature store steps and backfill runbook |
| Monitoring Kickoff | The kickoff whiteboard, discussion, mapping to the BRD |
| Data Quality Monitoring | The BRD: input monitoring template, feature/score monitoring, implementation, review notes |
| After the BRD: Next Steps | The 1:1 outcomes, what to monitor, the MVP build, emails/Jira, the check-in, **the POC page** (`poc-getting-started.md`, matches the code), **the requirements update** (Michelle's email) and **the Implementation Support Document** summary |
| DQ Framework Blueprint | The full long-term design (GX, phases 1–3, operations, roadmap) |

### UW deployment context

- Branching: developers merge into `staging` (it stays there through cert); the deployment team
  handles `staging` → `main` for production.
- **MF Aggregate composite Lambda** (repo path `NA / … / 2 - Composite Models / MF Aggregate`,
  `src/`: `handler.py`, `main.py`, `predict.py`, `config.py`). Aryan's MR (`aryan-staging` →
  `staging`) adds two lookup tables, `MODEL_AND_KYX_USAGE` and `MODEL_ALIGNMENT`, read from S3 under the
  `from_snowflake` prefix. The deployment team must update `handler.py` to load them from S3 before
  merging, then repeat for the IDT and Credit aggregates. Model `.pkl` files stay in the repo.

## 7. Environment gotchas

- **Docker on this WSL machine:** `~/.docker/config.json` has `"credsStore": "desktop.exe"`, which WSL
  cannot run, so `docker build` fails. Workaround used: run docker with `DOCKER_CONFIG` pointing at a
  folder holding `{}` as `config.json`. Do not edit the user's config without asking.
- Python 3.12 with `uv`. `dq-framework/` has its own `.venv` (`make setup`).
- AWS CLI v2 is installed; no Terraform. LocalStack image `localstack/localstack:4.9` is pulled.
- Dates: the conversation's "today" was 2026-10-17 at the time of writing.

## 8. Assistant memory files

The previous account kept notes in
`~/.claude/projects/-home-babin411-AWSSuportWork/memory/` (DQ priority, Docker workaround,
framework rules). Everything important from them is in this file.
