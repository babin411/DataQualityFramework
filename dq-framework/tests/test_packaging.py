"""What goes to AWS: the image holds only the code, the configuration and the pinned requirements."""
import re

import yaml

from conftest import ROOT


def test_image_contents():
    copied = re.findall(r"^COPY\s+(\S+)", (ROOT / "Dockerfile").read_text(), flags=re.M)
    assert sorted(copied) == ["config/dq_config.yaml", "requirements.txt", "src/dqf"]
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert ignore[0] == "*" and ".env" not in ignore                    # .env is excluded by "*"


def test_runtime_requirements_have_no_development_packages():
    names = {l.split("==")[0].lower() for l in (ROOT / "requirements.txt").read_text().splitlines()
             if l and not l.startswith(("#", " "))}
    assert {"great-expectations", "snowflake-connector-python", "openpyxl", "pyyaml"} <= names
    assert not names & {"pytest", "streamlit", "localstack", "moto"}


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def test_one_template_for_aws_and_localstack():
    template = yaml.load((ROOT / "infra" / "cloudformation.yaml").read_text(), Loader=_Loader)
    assert template["Parameters"]["Target"]["AllowedValues"] == ["aws", "localstack"]
    resources = template["Resources"]
    assert {"Function", "AlertTopic", "ConfigBucket", "HourlyRule", "DailyRule", "FunctionRole"} <= set(resources)
    env = resources["Function"]["Properties"]["Environment"]["Variables"]
    assert {"DQF_SNOWFLAKE_SECRET_ID", "DQF_CONFIG_URI", "DQF_SNS_TOPIC_ARN", "DQF_ALERT_CHANNELS"} <= set(env)
