"""Against a running LocalStack with the stack deployed (make local-up && make local-deploy)."""
import json
import subprocess
import sys

import pytest

from conftest import ROOT

pytestmark = pytest.mark.localstack


def local(*args) -> str:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "local.py"), *args],
                          capture_output=True, text=True, check=True, timeout=900).stdout


def test_test_alert_reaches_sns_and_email():
    import urllib.request

    before = json.load(urllib.request.urlopen("http://localhost:8025/api/v1/messages"))["total"]
    out = local("invoke", "test_alert")
    assert '"sns"' in out and '"email"' in out
    assert "[DQ] Test alert" in local("alerts")
    assert json.load(urllib.request.urlopen("http://localhost:8025/api/v1/messages"))["total"] == before + 1
