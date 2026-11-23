# Shortcuts. Run `make help`. Settings come from .env (copy .env.example).
PY := .venv/bin/python
DQF := .venv/bin/dqf
S := $(PY) scripts/local.py
COMPOSE := docker compose -f local/docker-compose.yml

.PHONY: config-xlsx config-from-xlsx report-publish docs local-docs help setup validate check init run-hourly run-daily backfill show test-alert app test \
        test-snowflake local-up local-down local-deploy local-build local-check local-invoke local-daily local-test-alert \
        local-alerts local-logs image lock

help:
	@grep -E '^[a-z-]+:.*## ' Makefile | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## create .venv and install the package, the report and the tests
	uv venv -q && uv pip install -q -e ".[dev]"

config-xlsx: ## write config/dq_config.yaml as an Excel workbook and as CSVs (config/sheets/)
	$(PY) scripts/build_config.py config/dq_config.yaml config/dq_config.xlsx
	$(PY) scripts/build_config.py config/dq_config.yaml config/sheets
config-from-xlsx: ## write an edited config/dq_config.xlsx back to config/dq_config.yaml (comments are not kept)
	$(PY) scripts/build_config.py config/dq_config.xlsx config/dq_config.yaml

# ---- on your machine, against real Snowflake ------------------------------------------------------
validate: ## read the configuration workbook and list any problems
	$(DQF) validate
check: ## log in to Snowflake
	$(DQF) check-connection
init: ## create the results table
	$(DQF) init
run-hourly: ## run the hourly checks for the last full hour
	$(DQF) run hourly
run-daily: ## run the daily checks (volume, PSI / CSI drift) for yesterday
	$(DQF) run daily
backfill: ## 24 hourly runs and 7 daily runs of history, without alerts
	$(DQF) backfill hourly 24 && $(DQF) backfill daily 7
show: ## summarise the results table
	$(DQF) show
test-alert: ## send a test message through the alert channels in .env
	$(DQF) test-alert
docs: ## where Great Expectations' HTML report (Data Docs) is; serves it at http://localhost:8777
	@echo "Open http://localhost:8777/index.html  (Ctrl+C to stop)"; cd build/data_docs/site && python3 -m http.server 8777
local-docs: ## the URL of the report the LocalStack Lambda writes to S3
	@echo "http://localhost:4566/$$($(PY) -c "import sys; sys.path.insert(0,'scripts'); import local; print(local.outputs()['ConfigBucketName'])")/data_docs/site/index.html"
report-publish: ## publish the report to Streamlit in Snowflake (after sql/03_report_app.sql); prints its address
	$(PY) scripts/publish_report.py
app: ## the report at http://localhost:8501
	.venv/bin/streamlit run app/streamlit_app.py

test: ## unit tests (no Snowflake, no LocalStack)
	$(PY) -m pytest -q -m "not snowflake and not localstack"
test-snowflake: ## tests against real Snowflake, using .env and the sample table
	DQF_SNOWFLAKE_TESTS=1 $(PY) -m pytest -q -m snowflake

# ---- LocalStack: AWS on your machine --------------------------------------------------------------
local-up: ## start LocalStack and the mail catcher (http://localhost:8025)
	$(COMPOSE) up -d && $(PY) -c "import time,urllib.request;[time.sleep(2) for _ in range(60) if not __import__('subprocess').run(['curl','-sf','http://localhost:4566/_localstack/health'],capture_output=True).returncode==0]"
local-down: ## stop LocalStack
	$(COMPOSE) down
local-build: ## build the Lambda code folder (build/lambda)
	$(S) build
local-deploy: ## deploy infra/cloudformation.yaml to LocalStack, with the login from .env
	$(S) deploy
local-invoke: ## invoke the Lambda: the hourly checks
	$(S) invoke hourly
local-daily: ## invoke the Lambda: the daily checks
	$(S) invoke daily
local-check: ## invoke the Lambda: can it log in to Snowflake and read the configuration?
	$(S) invoke check
local-test-alert: ## invoke the Lambda: send a test alert (SNS + email)
	$(S) invoke test_alert
local-alerts: ## show what the Lambda published to SNS
	$(S) alerts
local-logs: ## show the Lambda's latest log
	$(S) logs

# ---- AWS ------------------------------------------------------------------------------------------
image: ## build the Lambda container image, as AWS runs it
	docker build --platform linux/amd64 --provenance=false -t dqf .
lock: ## re-pin the Lambda's dependencies
	uv pip compile requirements.in --python-version 3.12 --python-platform x86_64-manylinux_2_28 -q -o requirements.txt
