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

# Bedrock model ID is set by apply.sh (single source of truth). The fallback
# here only applies if check_env.sh is run standalone.
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"

echo "NOTE: Checking Bedrock inference profile ${BEDROCK_MODEL_ID} in ${REGION}."

if ! aws bedrock list-inference-profiles --region "${REGION}" \
       --query "inferenceProfileSummaries[?inferenceProfileId=='${BEDROCK_MODEL_ID}'].inferenceProfileId" \
       --output text 2>/dev/null | grep -q "${BEDROCK_MODEL_ID}"; then
  echo "ERROR: Inference profile ${BEDROCK_MODEL_ID} not available in ${REGION}."
  echo "       Enable access: https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
  exit 1
fi

# Converse, not invoke-model: it is the API the worker calls, so a pass here
# means the worker's call shape works for this model.
echo "NOTE: Testing Bedrock Converse with ${BEDROCK_MODEL_ID}..."
if ! ERR=$(aws bedrock-runtime converse \
  --region "${REGION}" \
  --model-id "${BEDROCK_MODEL_ID}" \
  --messages '[{"role":"user","content":[{"text":"hi"}]}]' \
  --inference-config '{"maxTokens":1}' 2>&1 > /dev/null); then
  if echo "$ERR" | grep -q "AccessDeniedException"; then
    echo "ERROR: Bedrock invocation failed — model access not enabled."
    echo "       Enable access: https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
    exit 1
  fi
  echo "WARNING: Converse probe failed (not an access error): ${ERR}"
fi
echo "NOTE: Bedrock invocation access confirmed."
