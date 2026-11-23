#!/usr/bin/env bash
# Deploy to a real AWS account: build the image, push it to ECR, deploy the stack, upload the configuration.
# The same template deploys to LocalStack with `make local-deploy`.
#
# Before the first run, create the Snowflake login secret (once):
#   aws secretsmanager create-secret --name dqf/snowflake --secret-string file://secret.json
# with secret.json as described in the README (account, user, role, warehouse, database, schema,
# auth=keypair, private_key). Then, with the AWS CLI logged in to the right account:
#
#   STACK=dqf SNOWFLAKE_SECRET_ARN=arn:aws:secretsmanager:...:secret:dqf/snowflake-AbCdEf \
#   RESULT_TABLE=DQF_DB.DQF.DQ_RESULT ALERT_EMAIL=me@example.com infra/deploy.sh
#
# Optional:
#   SUBNET_IDS / SECURITY_GROUP_IDS   comma separated, to run in a VPC (needed to reach Snowflake privately)
#   REPO                              ECR repository name (default dqf)
#   PERMISSIONS_BOUNDARY_ARN          if your account requires a boundary on new IAM roles
#   SECRET_KMS_KEY_ARN                if the secret is encrypted with your own KMS key
#   TAGS                              tags for every resource, e.g. "CostCenter=1234 Owner=risk-analytics"
#   REPORT_URL                        the Streamlit report's address, linked from every alert
#   CONFIG_FILE                       the configuration to upload (default config/dq_config.yaml; .xlsx also works)
#   SCHEDULES=DISABLED                deploy with the hourly and daily schedules off (default ENABLED)
#   SKIP_BUILD=1 IMAGE_URI=...        deploy an image your CI pipeline already pushed
set -euo pipefail
cd "$(dirname "$0")/.."

: "${STACK:=dqf}"
: "${SNOWFLAKE_SECRET_ARN:?set SNOWFLAKE_SECRET_ARN}"
: "${RESULT_TABLE:?set RESULT_TABLE (DB.SCHEMA.TABLE)}"
: "${ALERT_EMAIL:=}"
: "${SUBNET_IDS:=}"
: "${SECURITY_GROUP_IDS:=}"
: "${REPO:=dqf}"
: "${PERMISSIONS_BOUNDARY_ARN:=}"
: "${SECRET_KMS_KEY_ARN:=}"
: "${TAGS:=}"
: "${SKIP_BUILD:=}"
: "${SCHEDULES:=ENABLED}"
: "${CONFIG_FILE:=config/dq_config.yaml}"
CONFIG_KEY="$(basename "${CONFIG_FILE}")"
: "${REPORT_URL:=}"
if [ -n "${SKIP_BUILD}" ] && [ -z "${IMAGE_URI:-}" ]; then
  echo "SKIP_BUILD needs IMAGE_URI: the image your pipeline pushed" >&2; exit 1
fi

REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${IMAGE_URI:-${REGISTRY}/${REPO}:$(date -u +%Y%m%d%H%M%S)}"
echo "Account ${ACCOUNT}, region ${REGION}, image ${IMAGE}"

if [ -z "${SKIP_BUILD}" ]; then
  aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
         --image-scanning-configuration scanOnPush=true >/dev/null
  aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${REGISTRY}"
  # Lambda needs a plain linux/amd64 image manifest, so provenance attestations are switched off.
  docker build --platform linux/amd64 --provenance=false -t "${IMAGE}" .
  docker push "${IMAGE}"
fi

TAG_ARGS=()
if [ -n "${TAGS}" ]; then
  read -r -a TAG_LIST <<< "${TAGS}"
  TAG_ARGS=(--tags "${TAG_LIST[@]}")
fi

aws cloudformation deploy --region "${REGION}" --stack-name "${STACK}" \
  --template-file infra/cloudformation.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides Target=aws "ImageUri=${IMAGE}" "SnowflakeSecretArn=${SNOWFLAKE_SECRET_ARN}" \
    "ResultTable=${RESULT_TABLE}" "AlertEmail=${ALERT_EMAIL}" \
    "SubnetIds=${SUBNET_IDS}" "SecurityGroupIds=${SECURITY_GROUP_IDS}" \
    "PermissionsBoundaryArn=${PERMISSIONS_BOUNDARY_ARN}" "SecretKmsKeyArn=${SECRET_KMS_KEY_ARN}" \
    "SchedulesEnabled=${SCHEDULES}" "ReportUrl=${REPORT_URL}" "ConfigKey=${CONFIG_KEY}" \
  "${TAG_ARGS[@]}"

BUCKET="$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='ConfigBucketName'].OutputValue" --output text)"
aws s3 cp "${CONFIG_FILE}" "s3://${BUCKET}/${CONFIG_KEY}" --region "${REGION}"

echo "Deployed. Check the Snowflake login and the workbook (no checks run, nothing sent):"
echo "  aws lambda invoke --function-name ${STACK}-run --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"action\": \"check\"}' /dev/stdout"
echo "Then a run without alerts:"
echo "  aws lambda invoke --function-name ${STACK}-run --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"schedule\": \"hourly\", \"send_alerts\": false}' /dev/stdout"
echo "Confirm the SNS email subscription from the email AWS sends to ${ALERT_EMAIL:-the subscriber}."
