"""Settings, read from environment variables.

The Snowflake login comes from Secrets Manager when DQF_SNOWFLAKE_SECRET_ID is set (AWS and LocalStack),
otherwise from SNOWFLAKE_* variables (a .env file on a laptop).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SNOWFLAKE_KEYS = ("account", "user", "role", "warehouse", "database", "schema")


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a file, without overriding real environment variables."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    config_uri: str = "config/dq_config.yaml"     # .yaml, .xlsx or a CSV folder; local path or s3://bucket/key
    result_table: str = "DQ_RESULT"                # DB.SCHEMA.TABLE
    alert_channels: tuple[str, ...] = ("log",)     # any of: log, sns, email
    sns_topic_arn: str = ""
    remind_hours: int = 24                         # repeat an open alert this often; 0 = never
    data_docs: str = ""                            # GX HTML report: "", a folder, or s3://bucket/prefix
    report_url: str = ""                           # the Streamlit report's address, linked from every alert
    snowflake: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            config_uri=env("DQF_CONFIG_URI", "config/dq_config.yaml"),
            result_table=env("DQF_RESULT_TABLE", "DQ_RESULT"),
            alert_channels=tuple(c.strip().lower() for c in env("DQF_ALERT_CHANNELS", "log").split(",") if c.strip()),
            sns_topic_arn=env("DQF_SNS_TOPIC_ARN", ""),
            remind_hours=int(env("DQF_REMIND_HOURS", "24")),
            data_docs=env("DQF_DATA_DOCS", ""),
            report_url=env("DQF_REPORT_URL", ""),
            snowflake=_snowflake_login(),
        )


def _snowflake_login() -> dict:
    secret_id = os.environ.get("DQF_SNOWFLAKE_SECRET_ID")
    if secret_id:
        import boto3

        login = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"])
        login.setdefault("auth", "keypair")
        return login

    env = os.environ.get
    login = {key: env(f"SNOWFLAKE_{key.upper()}", "") for key in SNOWFLAKE_KEYS}
    login["auth"] = env("SNOWFLAKE_AUTH", "keypair").lower()        # keypair | password | externalbrowser
    if env("SNOWFLAKE_PRIVATE_KEY_PATH"):
        login["private_key"] = Path(env("SNOWFLAKE_PRIVATE_KEY_PATH")).expanduser().read_text()
    for key in ("private_key_passphrase", "password"):
        if env(f"SNOWFLAKE_{key.upper()}"):
            login[key] = env(f"SNOWFLAKE_{key.upper()}")
    if env("SNOWFLAKE_CONNECT_ARGS"):
        login["connect_args"] = json.loads(env("SNOWFLAKE_CONNECT_ARGS"))
    return login
