"""Snowflake: the logins, the data source Great Expectations reads, and the results table.

Every connection goes through the Snowflake Python connector (GX uses it too, through SQLAlchemy).
Nothing here writes to any table except the results table.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from dqf.settings import SNOWFLAKE_KEYS
from dqf.sql import ts

RESULT_COLUMNS = (
    "RUN_ID", "RUN_TS", "SCHEDULE", "WINDOW_START", "WINDOW_END", "CHECK_ID", "SOURCE", "TABLE_NAME",
    "FIELD_NAME", "CHECK_TYPE", "RULE_NAME", "CRITERIA", "STATUS", "METRIC", "OBSERVED_VALUE",
    "ELEMENT_COUNT", "UNEXPECTED_COUNT", "ALERT_FLAG", "DETAILS",
    "EXPLANATION", "INVESTIGATE_SQL", "ALERT_SENT",
)
# Added after the first version of the table; ensure_result_table adds them to an older table.
ADDED_COLUMNS = ("EXPLANATION", "INVESTIGATE_SQL", "ALERT_SENT")
_TYPES = {"RUN_TS": "timestamp_ntz", "WINDOW_START": "timestamp_ntz", "WINDOW_END": "timestamp_ntz",
          "OBSERVED_VALUE": "float", "ELEMENT_COUNT": "number", "UNEXPECTED_COUNT": "number"}
CREATE_TABLE = "create table if not exists {table} (" + ", ".join(
    f"{c} {_TYPES.get(c, 'varchar')}" for c in RESULT_COLUMNS) + ")"

# The connector probes cloud metadata endpoints at every login to describe its platform; that costs
# about two seconds wherever none answers. All work is in UTC.
DEFAULT_CONNECT_ARGS = {"platform_detection_timeout_seconds": 0}


def column_names_without_rows() -> None:
    """Make Great Expectations read a query's column names without fetching its rows.

    For a query asset, GX 1.24 learns the column names by running the query unchanged and reading the
    cursor's description (`column_reflection_fallback`). Snowflake then computes the whole window and
    sends its first rows back with the description. Wrapped in `limit 0`, the same call returns the same
    names and no rows. Applied once; harmless if GX changes, because the original is still called.
    """
    import sqlalchemy as sa
    from great_expectations.expectations.metrics import util

    original = util.column_reflection_fallback
    if getattr(original, "_dqf_no_rows", False):
        return

    def fallback(selectable, dialect, sqlalchemy_engine, schema_name=None):
        if isinstance(selectable, sa.TextClause):
            selectable = sa.text(f"select * from ({selectable.text}) as dqf_columns limit 0")
        return original(selectable, dialect, sqlalchemy_engine, schema_name)

    fallback._dqf_no_rows = True
    util.column_reflection_fallback = fallback


def _null(value):
    """pandas turns a missing number into NaN, which Snowflake would read as a column name."""
    return None if isinstance(value, float) and value != value else value


class Snowflake:
    def __init__(self, settings):
        self.settings = settings
        self.table = settings.result_table

    # ---- login -----------------------------------------------------------------------------------
    def login(self) -> dict:
        d = self.settings.snowflake
        missing = [k for k in SNOWFLAKE_KEYS if not d.get(k)]
        auth = d.get("auth", "keypair")
        if auth == "keypair" and not d.get("private_key"):
            missing.append("private_key")
        if auth == "password" and not d.get("password"):
            missing.append("password")
        if missing:
            raise RuntimeError(f"Snowflake login settings missing: {', '.join(missing)}")
        return d

    def _connect_args(self) -> dict:
        custom = self.login().get("connect_args", {})
        sessions = {"TIMEZONE": "UTC", **custom.get("session_parameters", {})}
        return {**DEFAULT_CONNECT_ARGS, **custom, "session_parameters": sessions}

    def _private_key(self) -> tuple[str, bytes]:
        """The private key as (base64 text for GX, DER bytes for the connector). Encrypted keys need
        private_key_passphrase."""
        from cryptography.hazmat.primitives import serialization

        d = self.login()
        passphrase = d.get("private_key_passphrase")
        key = serialization.load_pem_private_key(
            d["private_key"].encode(), password=passphrase.encode() if passphrase else None)
        der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        return base64.b64encode(der).decode(), der

    def add_gx_source(self, context):
        column_names_without_rows()
        d = self.login()
        base = {k: d[k] for k in SNOWFLAKE_KEYS}
        kwargs = {"connect_args": self._connect_args()}
        auth = d.get("auth", "keypair")
        if auth == "keypair":
            return context.data_sources.add_snowflake(name="snowflake", **base,
                                                      private_key=self._private_key()[0], kwargs=kwargs)
        if auth == "password":
            return context.data_sources.add_snowflake(name="snowflake", **base, password=d["password"], kwargs=kwargs)
        # Browser single sign-on, from a laptop only. GX insists on a password field.
        kwargs["connect_args"]["authenticator"] = "externalbrowser"
        return context.data_sources.add_snowflake(name="snowflake", **base, password="unused", kwargs=kwargs)

    @contextmanager
    def connect(self):
        import snowflake.connector

        d = self.login()
        args = {**{k: d[k] for k in SNOWFLAKE_KEYS}, **self._connect_args()}
        auth = d.get("auth", "keypair")
        if auth == "keypair":
            args["private_key"] = self._private_key()[1]
        elif auth == "password":
            args["password"] = d["password"]
        else:
            args["authenticator"] = "externalbrowser"
        conn = snowflake.connector.connect(**args, autocommit=False)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- results ---------------------------------------------------------------------------------
    def ensure_result_table(self) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(CREATE_TABLE.format(table=self.table))
            for column in ADDED_COLUMNS:
                cur.execute(f"alter table {self.table} add column if not exists {column} varchar")

    def mark_alerted(self, run_id: str, check_ids: list[str]) -> None:
        """Record which checks of a run were in the alert that was sent (ALERT_SENT = Yes)."""
        if not check_ids:
            return
        with self.connect() as conn:
            conn.cursor().execute(
                f"update {self.table} set ALERT_SENT = 'Yes' where RUN_ID = %s "
                f"and CHECK_ID in ({', '.join(['%s'] * len(check_ids))})", (run_id, *check_ids))

    def record(self, rows: list[dict], schedule: str, start: datetime, end: datetime):
        """Store one run, and return what was true before it. One connection, one transaction.

        A re-run of the same schedule and window replaces its rows. Returns
        (previous, failing_since, previous_end): the status of each check in the latest earlier run of
        the same schedule, when each still-failing check started failing, and that earlier run's end.
        """
        t, s, e = self.table, ts(start), ts(end)
        same = "SCHEDULE = %s"
        since_sql = f"""
            select r.CHECK_ID, min(r.WINDOW_END) from {t} r
            left join (select CHECK_ID, max(WINDOW_END) as LAST_OK from {t}
                       where STATUS in ('PASS', 'WARN') and WINDOW_END < %s and {same} group by CHECK_ID) ok
              on ok.CHECK_ID = r.CHECK_ID
            where r.WINDOW_END < %s and r.SCHEDULE = %s and r.STATUS in ('FAIL', 'ERROR')
              and (ok.LAST_OK is null or r.WINDOW_END > ok.LAST_OK)
            group by r.CHECK_ID"""
        insert = (f"insert into {t} ({', '.join(RESULT_COLUMNS)}) "
                  f"values ({', '.join(['%s'] * len(RESULT_COLUMNS))})")
        with self.connect() as conn:
            cur = conn.cursor()
            previous_end = cur.execute(f"select max(WINDOW_END) from {t} where WINDOW_END < %s and {same}",
                                       (e, schedule)).fetchone()[0]
            previous = {}
            if previous_end is not None:
                previous_end = pd.Timestamp(previous_end).to_pydatetime()
                previous = dict(cur.execute(f"select CHECK_ID, STATUS from {t} where WINDOW_END = %s and {same}",
                                            (ts(previous_end), schedule)).fetchall())
            failing_since = {k: pd.Timestamp(v).to_pydatetime()
                             for k, v in cur.execute(since_sql, (e, schedule, e, schedule)).fetchall()}
            cur.execute(f"delete from {t} where SCHEDULE = %s and WINDOW_START = %s and WINDOW_END = %s",
                        (schedule, s, e))
            if rows:
                cur.executemany(insert, [tuple(_null(r[c]) for c in RESULT_COLUMNS) for r in rows])
        return previous, failing_since, previous_end

    def read_results(self, since: datetime | None = None) -> pd.DataFrame:
        query, params = f"select {', '.join(RESULT_COLUMNS)} from {self.table}", ()
        if since is not None:
            query, params = query + " where WINDOW_END >= %s", (ts(since),)
        with self.connect() as conn:
            rows = conn.cursor().execute(query, params).fetchall()
        df = pd.DataFrame(rows, columns=list(RESULT_COLUMNS))
        for col in ("RUN_TS", "WINDOW_START", "WINDOW_END"):
            df[col] = pd.to_datetime(df[col])
        return df

    # ---- read-only helpers -----------------------------------------------------------------------
    def whoami(self) -> dict:
        with self.connect() as conn:
            row = conn.cursor().execute(
                "select current_account(), current_user(), current_role(), current_warehouse(), "
                "current_database(), current_schema(), current_region(), current_version()").fetchone()
        return dict(zip(("account", "user", "role", "warehouse", "database", "schema", "region", "version"), row))

    def describe(self, table: str) -> tuple[list[tuple], int]:
        with self.connect() as conn:
            cur = conn.cursor()
            columns = [(r[0], r[1]) for r in cur.execute(f"describe table {table}").fetchall()]
            count = cur.execute(f"select count(*) from {table}").fetchone()[0]
        return columns, count
