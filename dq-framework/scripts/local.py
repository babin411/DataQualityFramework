"""Develop against LocalStack: build the code, deploy the stack, invoke the Lambda, read the alerts.

    python scripts/local.py build      install the Lambda's dependencies and code into build/lambda
    python scripts/local.py deploy     put the Snowflake login in Secrets Manager, deploy the stack,
                                       upload the configuration workbook
    python scripts/local.py invoke [hourly|daily|test_alert|check] [--end "YYYY-MM-DD HH:MM"] [--no-alerts]
    python scripts/local.py alerts     show the messages published to the SNS topic
    python scripts/local.py logs       show the Lambda's latest log

It only ever talks to LocalStack (http://localhost:4566), never to a real AWS account. Settings come
from .env, the same file `dqf` uses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from dqf.settings import SNOWFLAKE_KEYS, load_dotenv  # noqa: E402

ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"
STACK = "dqf-local"
SECRET_NAME = "dqf/snowflake"
BUILD = ROOT / "build" / "lambda"


def client(service: str):
    import boto3

    return boto3.client(service, endpoint_url=ENDPOINT, region_name=REGION,
                        aws_access_key_id="test", aws_secret_access_key="test")


def require_localstack() -> None:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=5) as r:
            health = json.load(r)
    except OSError:
        sys.exit("LocalStack is not running. Start it with `make local-up`.")
    print(f"LocalStack {health.get('version')} ({health.get('edition')}) at {ENDPOINT}")


# ---- build -----------------------------------------------------------------------------------------
def cmd_build(args) -> None:
    """build/lambda = the Lambda's dependencies (Linux, Python 3.12) + the dqf package."""
    requirements = (ROOT / "requirements.txt").read_bytes()
    stamp = BUILD / ".requirements.sha256"
    digest = hashlib.sha256(requirements).hexdigest()
    if not stamp.exists() or stamp.read_text() != digest:
        print("Installing the Lambda's dependencies into build/lambda (once per requirements change)...")
        shutil.rmtree(BUILD, ignore_errors=True)
        BUILD.mkdir(parents=True)
        subprocess.run(["uv", "pip", "install", "-q", "--target", str(BUILD), "--python-version", "3.12",
                        "--python-platform", "x86_64-manylinux_2_28", "-r", str(ROOT / "requirements.txt")],
                       check=True)
        stamp.write_text(digest)
    shutil.rmtree(BUILD / "dqf", ignore_errors=True)
    shutil.copytree(ROOT / "src" / "dqf", BUILD / "dqf", ignore=shutil.ignore_patterns("__pycache__"))
    print(f"Built {BUILD.relative_to(ROOT)} (the code is mounted, so later code changes need only `build`)")


# ---- deploy ----------------------------------------------------------------------------------------
def snowflake_secret() -> dict:
    """The Snowflake login from .env, in the shape the Lambda reads from Secrets Manager."""
    env = os.environ.get
    login = {key: env(f"SNOWFLAKE_{key.upper()}", "") for key in SNOWFLAKE_KEYS}
    login["auth"] = env("SNOWFLAKE_AUTH", "keypair").lower()
    if login["auth"] == "externalbrowser":
        sys.exit("A Lambda cannot open a browser: use SNOWFLAKE_AUTH=keypair (or password) in .env")
    if env("SNOWFLAKE_PRIVATE_KEY_PATH"):
        login["private_key"] = Path(env("SNOWFLAKE_PRIVATE_KEY_PATH")).expanduser().read_text()
    for key in ("private_key_passphrase", "password"):
        if env(f"SNOWFLAKE_{key.upper()}"):
            login[key] = env(f"SNOWFLAKE_{key.upper()}")
    missing = [k for k in SNOWFLAKE_KEYS if not login[k]]
    if missing:
        print(f"Note: .env has no Snowflake {', '.join(missing)} yet. The stack deploys, but runs will fail at login.")
    return login


def cmd_deploy(args) -> None:
    require_localstack()
    cmd_build(args)                  # always: the mounted folder must hold the current code
    secrets, cfn, s3 = client("secretsmanager"), client("cloudformation"), client("s3")

    value = json.dumps(snowflake_secret())
    try:
        secret_arn = secrets.create_secret(Name=SECRET_NAME, SecretString=value)["ARN"]
    except secrets.exceptions.ResourceExistsException:
        secret_arn = secrets.put_secret_value(SecretId=SECRET_NAME, SecretString=value)["ARN"]
        secret_arn = secrets.describe_secret(SecretId=SECRET_NAME)["ARN"]
    print(f"Snowflake login stored in Secrets Manager: {SECRET_NAME}")

    env = os.environ.get
    # The local mail catcher is "localhost" from this machine but "mailpit" from inside the Lambda.
    smtp_host = env("DQF_SMTP_HOST") or "localhost"
    if smtp_host in ("localhost", "127.0.0.1"):
        smtp_host = "mailpit"
    params = {
        "Target": "localstack",
        "LocalCodePath": str(BUILD),
        "SnowflakeSecretArn": secret_arn,
        "ResultTable": env("DQF_RESULT_TABLE", "DQF_DB.DQF.DQ_RESULT"),
        "AlertChannels": env("DQF_LOCAL_ALERT_CHANNELS", "sns,email,log"),
        "RemindHours": env("DQF_REMIND_HOURS", "24"),
        "ReportUrl": env("DQF_REPORT_URL", ""),
        "ConfigKey": Path(env("DQF_LOCAL_CONFIG_FILE", "config/dq_config.yaml")).name,
        "SchedulesEnabled": env("DQF_LOCAL_SCHEDULES", "DISABLED"),
        "SmtpHost": smtp_host,
        "SmtpPort": "1025" if smtp_host == "mailpit" else env("DQF_SMTP_PORT", "587"),
        "SmtpUser": env("DQF_SMTP_USER", ""),
        "SmtpPassword": env("DQF_SMTP_PASSWORD", ""),
        "EmailFrom": env("DQF_EMAIL_FROM", "dqf@localhost"),
        "EmailTo": env("DQF_EMAIL_TO", "data-quality@example.com"),
    }
    template = (ROOT / "infra" / "cloudformation.yaml").read_text()
    parameters = [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()]
    try:
        cfn.describe_stacks(StackName=STACK)
        exists = True
    except Exception:
        exists = False
    try:
        if exists:
            cfn.update_stack(StackName=STACK, TemplateBody=template, Parameters=parameters, Capabilities=["CAPABILITY_IAM"])
            waiter = "stack_update_complete"
        else:
            cfn.create_stack(StackName=STACK, TemplateBody=template, Parameters=parameters, Capabilities=["CAPABILITY_IAM"])
            waiter = "stack_create_complete"
        print(f"Deploying stack {STACK} ...")
        cfn.get_waiter(waiter).wait(StackName=STACK, WaiterConfig={"Delay": 3, "MaxAttempts": 100})
    except Exception as exc:
        if "No updates are to be performed" not in str(exc):
            events = cfn.describe_stack_events(StackName=STACK)["StackEvents"][:10]
            for e in events:
                if "FAILED" in e["ResourceStatus"]:
                    print(f"  {e['LogicalResourceId']}: {e.get('ResourceStatusReason')}")
            raise
    outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    config_file = ROOT / env("DQF_LOCAL_CONFIG_FILE", "config/dq_config.yaml")
    s3.upload_file(str(config_file), outputs["ConfigBucketName"], config_file.name)
    print(f"Configuration uploaded to s3://{outputs['ConfigBucketName']}/{config_file.name}")
    for key, value in outputs.items():
        print(f"  {key:<18} {value}")


# ---- run and inspect -------------------------------------------------------------------------------
def outputs() -> dict:
    stack = client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def cmd_invoke(args) -> None:
    require_localstack()
    event = {"action": args.what} if args.what in ("test_alert", "check") else {"schedule": args.what}
    if args.end:
        event["window_end"] = args.end
    if args.no_alerts:
        event["send_alerts"] = False
    print(f"Invoking {outputs()['FunctionName']} with {json.dumps(event)} (the first call starts a container) ...")
    started = time.time()
    lam = client("lambda")
    import botocore.config

    lam = __import__("boto3").client("lambda", endpoint_url=ENDPOINT, region_name=REGION,
                                     aws_access_key_id="test", aws_secret_access_key="test",
                                     config=botocore.config.Config(read_timeout=900, retries={"max_attempts": 0}))
    response = lam.invoke(FunctionName=outputs()["FunctionName"], Payload=json.dumps(event).encode())
    payload = json.loads(response["Payload"].read() or b"null")
    print(f"Took {time.time() - started:.1f}s. {'ERROR: ' + response['FunctionError'] if response.get('FunctionError') else 'Result:'}")
    print(json.dumps(payload, indent=2))


def cmd_alerts(args) -> None:
    sqs = client("sqs")
    url = outputs()["AlertInboxUrl"]
    found = 0
    while True:
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])
        if not messages:
            break
        for m in messages:
            note = json.loads(m["Body"])
            print(f"\n===== SNS: {note.get('Subject')} =====\n{note.get('Message')}")
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
            found += 1
    print(f"\n{found} message(s) on the alert topic." if found else "No new messages on the alert topic.")


def cmd_logs(args) -> None:
    logs = client("logs")
    group = f"/aws/lambda/{outputs()['FunctionName']}"
    streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=1)["logStreams"]
    if not streams:
        sys.exit("No logs yet.")
    for e in logs.get_log_events(logGroupName=group, logStreamName=streams[0]["logStreamName"], limit=200)["events"]:
        print(e["message"].rstrip())


def main() -> None:
    os.chdir(ROOT)
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build").set_defaults(func=cmd_build)
    sub.add_parser("deploy").set_defaults(func=cmd_deploy)
    i = sub.add_parser("invoke")
    i.add_argument("what", nargs="?", default="hourly", choices=["hourly", "daily", "test_alert", "check"])
    i.add_argument("--end")
    i.add_argument("--no-alerts", action="store_true")
    i.set_defaults(func=cmd_invoke)
    sub.add_parser("alerts").set_defaults(func=cmd_alerts)
    sub.add_parser("logs").set_defaults(func=cmd_logs)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
