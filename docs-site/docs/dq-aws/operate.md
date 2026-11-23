# After go-live

## Making changes

| Change | What to do | Redeploy code? |
| --- | --- | --- |
| A check, a limit, a new source, `alert` on/off | Edit `dq_config.yaml`, `make validate`, then `aws s3 cp config/dq_config.yaml s3://<bucket>/dq_config.yaml` | No. The next run reads it |
| A new business rule | Add it to the `rules:` section of `dq_config.yaml` and use it from a check; then the same as above | No |
| A custom check, for logic field conditions cannot express (rare) | A developer adds a custom GX expectation to `src/dqf/expectations.py`, tests it, then `infra/deploy.sh` | Yes |
| Code | Test (`make test`, LocalStack), then `infra/deploy.sh` | Yes |
| Alert recipients | Add or remove SNS subscriptions (console or `aws sns subscribe`) | No |
| Schedule times | `HourlySchedule` / `DailySchedule` template parameters | Stack update only |
| Rotate the Snowflake key | `alter user DQF_SVC set rsa_public_key_2 = '…'`, update the secret, check, then unset the old key | No |

The bucket is versioned: to undo a configuration change, restore the previous version of
`dq_config.yaml`, or upload the previous version from Git.

Test a configuration change in LocalStack first (`make local-deploy`, `make local-invoke`). It runs the
same code against the same Snowflake.

## Watching it

| What | Where |
| --- | --- |
| Data problems | The alert emails; the Streamlit report; the GX report in S3 (`data_docs/site/index.html`) |
| The framework itself crashed | The CloudWatch alarm emails the same topic (Lambda `Errors` ≥ 1 in an hour) |
| A check could not run | `ERROR` in the email and the results table, with the reason |
| Each run's summary | CloudWatch Logs `/aws/lambda/dqf-run`: one JSON line per run with the counts |
| Duration and memory | The `REPORT` line in the log. Raise `MemorySize` in the template if *Max Memory Used* nears 2048 MB |

To read the GX report: `aws s3 sync s3://<bucket>/data_docs/site ./site`, then open `site/index.html`. To
share it with the team without downloads, put the bucket behind CloudFront with company sign-in, or
use the [Streamlit report in Snowflake](report.md).

## Costs (rough)

| Item | Estimate |
| --- | --- |
| Lambda | About 750 runs a month × ~30 s × 2 GB: under **$1** a month |
| S3, SNS, Secrets Manager, CloudWatch, EventBridge | A few dollars a month; the secret is about $0.40 |
| ECR | Cents. Delete old images now and then |
| Snowflake warehouse | The main cost. An X-Small warehouse resumed 25 times a day for about a minute each: roughly **0.4 credits a day**. It grows with the size of the tables and the number of sources. Share an existing warehouse if allowed |

## Rolling back

| Problem | Undo |
| --- | --- |
| A bad configuration | Restore the previous S3 version of `dq_config.yaml` (or upload the previous one from Git) |
| A bad code release | Re-run `deploy.sh` with `SKIP_BUILD=1 IMAGE_URI=<previous image tag>`. Every image is kept in ECR with its timestamp tag |
| Alerts too noisy | Set `ALERT = No` on the noisy checks; or disable the schedules (`SCHEDULES=DISABLED`) |
| Remove it entirely | `aws cloudformation delete-stack --stack-name dqf`. Empty the bucket first; the secret, the ECR repository and the Snowflake objects stay until removed by hand |

## Troubleshooting in AWS

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `check` times out on Snowflake | No route from the subnets to Snowflake, or the OCSP host is blocked | Network team: allow the `system$allowlist()` hosts; check the NAT or PrivateLink |
| `check` times out on the secret or the configuration | A Lambda in a VPC with no route to AWS APIs | NAT, or VPC endpoints for Secrets Manager and S3 |
| `AccessDeniedException` reading the secret | Wrong ARN, or a custom KMS key | Deploy with the right `SNOWFLAKE_SECRET_ARN`; set `SECRET_KMS_KEY_ARN` |
| `Incoming request with IP … is not allowed` | A Snowflake network policy | Add the NAT's egress IPs to the policy for `DQF_SVC` |
| Stack fails creating the role | A permissions boundary is required, or IAM is not allowed | `PERMISSIONS_BOUNDARY_ARN`; or ask the platform team for a deployment pipeline |
| Lambda: `Runtime.InvalidEntrypoint` or exec format error | The image was built for ARM | Build with `--platform linux/amd64` (`deploy.sh` does) |
| Lambda times out | Very large tables or many sources in one run | Raise `Timeout` (max 900 s) or the warehouse size; add a `ROW_FILTER`; split sources across schedules |
| No alert emails | SNS subscription not confirmed, or held by a mail filter | `aws sns list-subscriptions-by-topic`; confirm; ask IT to allow `no-reply@sns.amazonaws.com` |
| Every check `ERROR` | The role lost `SELECT`, or a table was renamed | Re-grant; update `dq_config.yaml` |
