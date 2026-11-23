import os
import warnings
from pathlib import Path

import pytest

os.environ.setdefault("GX_ANALYTICS_ENABLED", "false")
warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config" / "dq_config.yaml"          # the main configuration
WORKBOOK = ROOT / "config" / "dq_config.xlsx"        # the same, as Excel


def pytest_collection_modifyitems(config, items):
    """Tests that need real Snowflake or a running LocalStack run only when asked to."""
    for marker, env, how in (("snowflake", "DQF_SNOWFLAKE_TESTS", "make test-snowflake"),
                             ("localstack", "DQF_LOCALSTACK_TESTS", "make local-up, then DQF_LOCALSTACK_TESTS=1")):
        if os.environ.get(env) != "1":
            skip = pytest.mark.skip(reason=f"needs {marker}: {how}")
            for item in items:
                if marker in item.keywords:
                    item.add_marker(skip)
