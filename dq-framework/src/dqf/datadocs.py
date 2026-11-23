"""Great Expectations' own HTML report ("Data Docs") for every run.

The checks run in an in-memory GX context, so the Snowflake login is never written to disk. After a
run, each validation result is handed to a second GX context whose only job is to keep results and
render the report. It is configured by DQF_DATA_DOCS:

    (empty)                 no report
    build/data_docs         a local folder: open build/data_docs/site/index.html
    s3://bucket/prefix      S3 (AWS, LocalStack). GX 1.x has no S3 store, so the report is built in /tmp:
                            the last HISTORY_DAYS of results are fetched from S3 first (so the index
                            shows recent history), and the new pages and results are uploaded after.

The report is extra: if it cannot be written, the run, its results and its alerts are not affected.
"""
from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("dqf")
HISTORY_DAYS = 7                     # S3 only: how much history the report's index lists


def _backend(root: Path, sub: str) -> dict:
    return {"class_name": "TupleFilesystemStoreBackend", "base_directory": str(root / sub)}


def _split(target: str) -> tuple[str, str]:
    bucket, _, prefix = target[5:].partition("/")
    return bucket, prefix.strip("/")


def site_location(target: str) -> str:  # noqa: D401
    """Where the report's index page is."""
    if target.startswith("s3://"):
        return f"{target.rstrip('/')}/site/index.html"
    return str(Path(target).resolve() / "site" / "index.html")


class DataDocs:
    def __init__(self, target: str):
        self.target = target
        self.on_s3 = target.startswith("s3://")
        self.root = Path(tempfile.mkdtemp(prefix="dqf_docs_")) if self.on_s3 else Path(target).resolve()
        self._context = None
        self._keys: list = []

    @property
    def context(self):
        if self._context is None:
            import great_expectations as gx
            from great_expectations.data_context.types.base import DataContextConfig

            t = self.root
            if self.on_s3:
                self._pull()
            config = DataContextConfig(
                expectations_store_name="expectations_store",
                validation_results_store_name="validation_results_store",
                checkpoint_store_name="checkpoint_store",
                stores={
                    "expectations_store": {"class_name": "ExpectationsStore", "store_backend": _backend(t, "expectations")},
                    "validation_results_store": {"class_name": "ValidationResultsStore",
                                                 "store_backend": _backend(t, "validations")},
                    "checkpoint_store": {"class_name": "CheckpointStore", "store_backend": _backend(t, "checkpoints")},
                    "validation_definition_store": {"class_name": "ValidationDefinitionStore",
                                                    "store_backend": _backend(t, "validation_definitions")},
                },
                data_docs_sites={"dqf": {
                    "class_name": "SiteBuilder",
                    "show_how_to_buttons": False,
                    "store_backend": _backend(t, "site"),
                    "site_index_builder": {"class_name": "DefaultSiteIndexBuilder"},
                }},
                analytics_enabled=False,
            )
            self._context = gx.get_context(project_config=config)
        return self._context

    def add(self, suite_name: str, expectations: list, result, schedule: str, start: datetime, run_time: datetime) -> None:
        """Keep one validation result (one query group of one run)."""
        import great_expectations as gx
        from great_expectations.core.run_identifier import RunIdentifier
        from great_expectations.data_context.types.resource_identifiers import (
            ExpectationSuiteIdentifier, ValidationResultIdentifier)

        self.context.suites.add_or_update(gx.ExpectationSuite(name=suite_name, expectations=list(expectations)))
        run = RunIdentifier(run_name=f"{schedule} {start:%Y-%m-%d %H:%M}", run_time=run_time)
        key = ValidationResultIdentifier(expectation_suite_identifier=ExpectationSuiteIdentifier(name=suite_name),
                                         run_id=run, batch_identifier=f"{schedule}_{start:%Y%m%dT%H%M}")
        result.meta["run_id"] = run
        self.context.validation_results_store.set(key, result)
        self._keys.append(key)

    def build(self) -> str | None:
        """Render the pages for this run and refresh the index. Returns where the index page is."""
        if not self._keys:
            return None
        started = time.time()
        self.context.build_data_docs(resource_identifiers=self._keys)
        self._keys = []
        if self.on_s3:
            self._push(since=started)
        return site_location(self.target)

    # ---- S3 ------------------------------------------------------------------------------------------
    def _pull(self) -> None:
        """Fetch the results of the last HISTORY_DAYS from S3, so the index lists them."""
        import boto3

        bucket, prefix = _split(self.target)
        s3 = boto3.client("s3")
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/validations/"):
            for obj in page.get("Contents", []):
                if obj["LastModified"] >= cutoff:
                    path = self.root / obj["Key"][len(prefix) + 1:]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    s3.download_file(bucket, obj["Key"], str(path))

    def _push(self, since: float) -> None:
        """Upload what this run wrote: the new results and the report pages."""
        import boto3

        bucket, prefix = _split(self.target)
        s3 = boto3.client("s3")
        for folder in ("validations", "expectations", "site"):
            for path in (self.root / folder).rglob("*"):
                if path.is_file() and (folder == "site" or path.stat().st_mtime >= since - 600):
                    if folder == "site" and path.stat().st_mtime < since and path.name != "index.html":
                        continue                         # unchanged page from an earlier run
                    kind = "text/html" if path.suffix == ".html" else (
                        "text/css" if path.suffix == ".css" else "application/json" if path.suffix == ".json" else None)
                    extra = {"ContentType": kind} if kind else {}
                    s3.upload_file(str(path), bucket, f"{prefix}/{path.relative_to(self.root)}", ExtraArgs=extra)
