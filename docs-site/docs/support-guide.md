# Support engineer guide

How to reason about issues in this system, organised by the three processes.

## 1. "My config change isn't being used"

Walk the chain left to right:

| Check                   | Where                                        | What to look for                                                                                                                                            |
| ----------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Row approved?           | Admin console UI                             | The row is in an approved/decisioned state.                                                                                                                 |
| Data in Snowflake?      | Snowflake config schema                      | `SELECT` the table; confirm the new value exists.                                                                                                           |
| Cache job ran?          | CloudWatch Logs for the SF2AWS Config Lambda | Recent invocation, no errors, Snowflake connection OK.                                                                                                      |
| Fingerprint changed?    | State file                                   | Fingerprint for that table's row updated after the change. If the query in the _query_ column doesn't select the changed column/row, the hash won't change. |
| S3 file updated?        | S3 reference bucket                          | `LastModified` timestamp and contents of the object.                                                                                                        |
| State machine reads it? | CloudWatch Logs of the Lambda that reads S3 (most likely `uw-normalizer-cert`) | There is no separate read state; the S3 read happens inside a Lambda. Confirm it loaded the expected object/version. Watch for Lambda-level caching of reference data across warm invocations **(inferred risk)**. |

```sql
-- Example: confirm the value landed in Snowflake (adjust names)
SELECT * FROM <db>.<config_schema>.TMX_CODE_CATEGORIES WHERE key = '<code>';
```

```bash
# Example: confirm the cached object refreshed
aws s3api head-object --bucket <reference-bucket> --key <path/to/file>
aws logs tail /aws/lambda/<sf2aws-config-lambda> --since 1h
```

## 2. "The API is slow / erroring"

| Symptom                      | Likely place                                                                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 5xx from ALB, no Lambda logs | ALB target health / Lambda permissions for ALB.                                                                                             |
| 502 from ALB                 | Post-ALB Lambda returned a malformed response or timed out.                                                                                 |
| Timeouts                     | One slow primitive model or third-party source. Lanes run in parallel, so the slowest lane sets the time. In the execution timeline, find the slowest lane, then the slowest `*-cert` primitive inside it. |
| Execution failed             | Open the execution in the Step Functions console; the failed state is red. A failing primitive fails its whole `Parallel` state unless `Retry`/`Catch` is defined (confirm in the ASL). See [Inside the state machine](state-machine.md#what-this-means-for-support). |
| Wrong decision               | Compare the transaction log for that request with the expected config; check which composite model scenario was chosen (sources available). |

Useful places: ALB access logs, CloudWatch metrics for each Lambda (Duration, Errors, Throttles,
ConcurrentExecutions), Step Functions execution history.

## 3. "Decisions missing in Snowflake history"

| Check                  | Where                                                                                                     |
| ---------------------- | --------------------------------------------------------------------------------------------------------- |
| Log written? (5a)      | Transaction log S3 bucket, by date/prefix. The writer is `uw-rules-engine-cert`; check its CloudWatch Logs if the file is missing. |
| Lambda triggered? (5b) | S3 event notification config; CloudWatch Logs for AWS2SF Trans Log Pipe.                                  |
| Load succeeded? (5c)   | Lambda errors, Snowflake `QUERY_HISTORY` / `COPY_HISTORY` for the historical table.                       |
| Duplicates?            | S3 events are at-least-once; ensure loads are idempotent (e.g. MERGE on a transaction id) **(inferred)**. |

```sql
-- Example: what reached the historical table recently
SELECT COUNT(*), MAX(<event_ts>) FROM <db>.<schema>.<historical_table>
WHERE <event_ts> > DATEADD(hour, -1, CURRENT_TIMESTAMP());
```

## Key design facts to remember

- Snowflake is **never** in the real-time path; Snowflake outages affect freshness and history, not live decisions.
- The real-time path depends on S3 reference data being **present and valid**; an empty or malformed
  cached file can break every request.
- Most config tables are **still empty** today (per the transcript), so expect missing-config issues while
  they are being populated.
- Monthly MCC stats tables may be **manually** updated for now; confirm their freshness when MCC-based
  features look stale.
