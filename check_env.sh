#!/bin/bash
# ==============================================================================
# check_env.sh
# ==============================================================================
# Validates local tooling, AWS credentials, MicroVM CLI support, and Bedrock
# model access before apply.sh or destroy.sh are allowed to proceed.
# ==============================================================================

set -u

REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "NOTE: Validating that required commands are found in your PATH."

commands=("aws" "terraform" "jq" "zip" "python3" "envsubst" "curl")

missing=0
for cmd in "${commands[@]}"; do
  if ! command -v "$cmd" > /dev/null 2>&1; then
    echo "ERROR: $cmd is not found in the current PATH."
    missing=1
  else
    echo "NOTE: $cmd is found in the current PATH."
  fi
done

# apply.sh vendors boto3 with `python3 -m pip`, so pip must belong to the
# python3 on PATH -- a pip for some other interpreter does not count.
if ! python3 -m pip --version > /dev/null 2>&1; then
  echo "ERROR: python3 has no pip module (install python3-pip)."
  missing=1
else
  echo "NOTE: python3 -m pip is available."
fi

if [ "$missing" -ne 0 ]; then
  echo "ERROR: One or more required commands are missing."
  exit 1
fi

echo "NOTE: Checking AWS cli connection."
if ! aws sts get-caller-identity --query "Account" --output text > /dev/null 2>&1; then
  echo "ERROR: Failed to connect to AWS. Check credentials/environment."
  exit 1
fi
echo "NOTE: Successfully logged into AWS."

# The MicroVM APIs ship in recent CLI v2 builds only; fail early with a clear
# message instead of an opaque "Invalid choice" halfway through apply.
if ! aws lambda-microvms help > /dev/null 2>&1; then
  echo "ERROR: This AWS CLI does not support 'lambda-microvms'. Upgrade to the latest AWS CLI v2."
  exit 1
fi
echo "NOTE: AWS CLI supports the lambda-microvms service."

# Every model in bedrock-config.sh, probed with real Converse calls. Not a
# lookup: a model can be ACTIVE and still be denied to this account, or reject
# the image/cachePoint blocks its switches would make the worker send. Each
# entry's switches are passed along, so a "true" the model cannot honour fails
# here instead of failing every request after deploy.
if [[ -z "${BEDROCK_MODELS+x}" ]]; then
  source "$(dirname "$0")/bedrock-config.sh"
fi

if ! printf '%s\n' "${BEDROCK_MODELS[@]}" | cut -d'|' -f1 | grep -qx "${BEDROCK_DEFAULT}"; then
  echo "ERROR: BEDROCK_DEFAULT '${BEDROCK_DEFAULT}' is not a key in BEDROCK_MODELS."
  exit 1
fi

echo "NOTE: Checking ${#BEDROCK_MODELS[@]} Bedrock model(s) in ${REGION}, default ${BEDROCK_DEFAULT}."
MODEL_FAILED=0
for entry in "${BEDROCK_MODELS[@]}"; do
  IFS='|' read -r key model_id label image caching <<< "${entry}"
  if result=$(python3 "$(dirname "$0")/probe_bedrock.py" --region "${REGION}" \
       --check "${model_id}" --image "${image}" --caching "${caching}" 2>&1); then
    echo "NOTE: [${key}] ${result}"
  else
    echo "ERROR: [${key}] ${result}"
    MODEL_FAILED=1
  fi
done
if [[ "${MODEL_FAILED}" -ne 0 ]]; then
  echo "ERROR: Fix BEDROCK_MODELS in bedrock-config.sh. ./probe_bedrock.py lists"
  echo "       every model this account can use, with measured switches."
  echo "       Model access: https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
  exit 1
fi
echo "NOTE: All Bedrock models confirmed."
