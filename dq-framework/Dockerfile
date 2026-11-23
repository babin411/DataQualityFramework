# The Lambda image for AWS. From this folder:
#   docker build --platform linux/amd64 --provenance=false -t dqf .
# It holds the pinned dependencies, the dqf package and the default configuration (a fallback: the
# Lambda reads the configuration from S3).
# .dockerignore keeps everything else out (tests, scripts, the report, .env, keys).
FROM public.ecr.aws/lambda/python:3.12

ENV GX_ANALYTICS_ENABLED=false \
    HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY src/dqf "${LAMBDA_TASK_ROOT}/dqf"
COPY config/dq_config.yaml "${LAMBDA_TASK_ROOT}/config/dq_config.yaml"

CMD ["dqf.handler.handler"]
