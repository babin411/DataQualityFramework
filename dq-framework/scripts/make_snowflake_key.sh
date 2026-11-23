#!/usr/bin/env bash
# Create the key pair the service user DQF_SVC logs in with.
#   keys/dqf_svc_key.p8   the PRIVATE key. Keep it secret: it is git-ignored and never leaves your machine
#                          except into Secrets Manager.
#   keys/dqf_svc_key.pub  the PUBLIC key, to paste into sql/01_setup.sql
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p keys
chmod 700 keys
if [ -f keys/dqf_svc_key.p8 ]; then
  echo "keys/dqf_svc_key.p8 already exists; not overwriting it."
else
  openssl genrsa 2048 2>/dev/null | openssl pkcs8 -topk8 -inform PEM -out keys/dqf_svc_key.p8 -nocrypt
  chmod 600 keys/dqf_svc_key.p8
fi
openssl rsa -in keys/dqf_svc_key.p8 -pubout -out keys/dqf_svc_key.pub 2>/dev/null
echo
echo "Paste this into sql/01_setup.sql, in: alter user DQF_SVC set rsa_public_key = '...';"
echo
grep -v -- "-----" keys/dqf_svc_key.pub | tr -d '\n'
echo
echo
echo "Fingerprint (Snowflake shows it as RSA_PUBLIC_KEY_FP after the ALTER USER):"
echo "SHA256:$(openssl rsa -in keys/dqf_svc_key.p8 -pubout -outform DER 2>/dev/null | openssl dgst -sha256 -binary | openssl enc -base64)"
echo
echo "In .env:  SNOWFLAKE_AUTH=keypair   SNOWFLAKE_PRIVATE_KEY_PATH=$(pwd)/keys/dqf_svc_key.p8"
