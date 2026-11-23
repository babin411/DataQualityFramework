"""Publish the report (app/streamlit_app.py) as a Streamlit in Snowflake app, and print its address.

    python scripts/publish_report.py          (or: make report-publish)

Needs sql/03_report_app.sql run once. Logs in like `dqf` does (the SNOWFLAKE_* settings in .env) and
works as DQF_ROLE: creates the stage DQF_APP, uploads the app, creates or updates the app DQ_REPORT
in the same database and schema. Re-run it after changing the app.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from dqf.settings import Settings, load_dotenv  # noqa: E402
from dqf.snowflake import Snowflake  # noqa: E402

APP = "DQ_REPORT"
STAGE = "DQF_APP"
# Streamlit in Snowflake installs the app's packages from this file, from the Snowflake Anaconda
# channel. Without a streamlit line Snowflake picks an old default; clicking a chart to filter needs
# Streamlit 1.35+ and Altair 5+. To see what the channel offers:
#   select version from information_schema.packages where language = 'python' and package_name = 'streamlit';
ENVIRONMENT = ("name: dqf_report\nchannels:\n  - snowflake\ndependencies:\n"
               "  - streamlit=1.52.2\n  - pandas\n  - altair=5.5.0\n")


def main() -> None:
    load_dotenv(ROOT / ".env")
    settings = Settings.from_env()
    login = settings.snowflake
    where = f"{login['database']}.{login['schema']}"
    build = ROOT / "build" / "report_app"
    build.mkdir(parents=True, exist_ok=True)
    (build / "streamlit_app.py").write_text((ROOT / "app" / "streamlit_app.py").read_text())
    (build / "environment.yml").write_text(ENVIRONMENT)

    with Snowflake(settings).connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"create stage if not exists {where}.{STAGE} comment = 'The data quality report app'")
        except Exception as exc:
            sys.exit(f"Could not create the stage ({exc}).\nRun sql/03_report_app.sql once as ACCOUNTADMIN first.")
        for name in ("streamlit_app.py", "environment.yml"):
            cur.execute(f"put 'file://{(build / name).as_posix()}' @{where}.{STAGE} overwrite = true auto_compress = false")
        cur.execute(f"create or replace streamlit {where}.{APP} from '@{where}.{STAGE}' "
                    f"main_file = 'streamlit_app.py' query_warehouse = {login['warehouse']} "
                    f"title = 'Data quality' comment = 'Results of the data quality framework'")
        org, account = cur.execute("select current_organization_name(), current_account_name()").fetchone()

    url = f"https://app.snowflake.com/{org.lower()}/{account.lower()}/#/streamlit-apps/{where}.{APP}"
    print(f"Published {where}.{APP}.\nOpen it at:\n  {url}\n")
    print("Link it from every alert:\n"
          f"  .env:            DQF_REPORT_URL={url}\n"
          f"  infra/deploy.sh: REPORT_URL={url}\n"
          "Let your team open it:  grant usage on streamlit "
          f"{where}.{APP} to role <TEAM_ROLE>;  (plus usage on the database and schema)")


if __name__ == "__main__":
    main()
