#!/bin/bash
# ==============================================================================
# validate.sh
# ==============================================================================
# Smoke-tests the sandbox image, then prints the app URL.
#
#   1. Launch a MicroVM from the image, as the worker does
#   2. Run one Python cell and check that it answers
#   3. Terminate it -- always, including on failure
#
# It does not exercise Cognito, the worker or Bedrock: sign in and ask for a
# fractal tree for that.
# ==============================================================================

export AWS_DEFAULT_REGION="us-east-1"
set -euo pipefail
cd "$(dirname "$0")"

CUSTOM_URL=$(terraform -chdir=02-core output -raw custom_domain_url   2>/dev/null || true)
ROLE_ARN=$(terraform   -chdir=02-core output -raw sandbox_role_arn    2>/dev/null || true)
IMAGE_ARN=$(terraform  -chdir=01-sandbox output -raw image_arn        2>/dev/null || true)
IMAGE_VERSION=$(terraform -chdir=01-sandbox output -raw image_version 2>/dev/null || true)

if [ -z "${CUSTOM_URL}" ] || [ -z "${IMAGE_ARN}" ] || [ -z "${ROLE_ARN}" ]; then
  echo "ERROR: Could not read Terraform outputs. Run ./apply.sh first."
  exit 1
fi

VM_ID=""
cleanup() {
  if [[ -n "${VM_ID}" ]]; then
    aws lambda-microvms terminate-microvm --microvm-identifier "${VM_ID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# Launch and wait for RUNNING
# ------------------------------------------------------------------------------

echo "NOTE: Launching a validation sandbox from ${IMAGE_ARN##*:}..."
RUN=$(aws lambda-microvms run-microvm \
  --image-identifier "${IMAGE_ARN}" \
  --image-version "${IMAGE_VERSION}" \
  --run-hook-payload '{"purpose":"validate"}' \
  --execution-role-arn "${ROLE_ARN}" \
  --egress-network-connectors "arn:aws:lambda:${AWS_DEFAULT_REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
  --ingress-network-connectors "arn:aws:lambda:${AWS_DEFAULT_REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS" \
  --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":60,"suspendedDurationSeconds":900}' \
  --maximum-duration-in-seconds 600)

VM_ID=$(echo "${RUN}" | jq -r '.microvmId')
ENDPOINT=$(echo "${RUN}" | jq -r '.endpoint')
ENDPOINT="${ENDPOINT#https://}"

STATE=""
for ((i = 1; i <= 60; i++)); do
  STATE=$(aws lambda-microvms get-microvm --microvm-identifier "${VM_ID}" \
    --query "state" --output text)
  [[ "${STATE}" == "RUNNING" ]] && break
  sleep 1
done
if [[ "${STATE}" != "RUNNING" ]]; then
  echo "ERROR: MicroVM ${VM_ID} never reached RUNNING (last state: ${STATE})."
  exit 1
fi

TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "${VM_ID}" \
  --expiration-in-minutes 10 \
  --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)

# ------------------------------------------------------------------------------
# One Python cell: submit, then poll for the result
# ------------------------------------------------------------------------------

# Never curl -f: under set -e it aborts on any HTTP error with no message.
# 502/503/504 right after launch mean the endpoint is still coming up.
vm_request() {
  local method="$1" path="$2" data="${3:-}" response status body args
  for ((attempt = 1; attempt <= 10; attempt++)); do
    args=(-s --max-time 60 -X "${method}" "https://${ENDPOINT}${path}"
          -H "X-aws-proxy-auth: ${TOKEN}" -H "X-aws-proxy-port: 8080")
    [[ -n "${data}" ]] && args+=(-H "Content-Type: application/json" -d "${data}")
    response=$(curl "${args[@]}" -w $'\n%{http_code}') || response=$'\n000'
    status="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "${status}" == "200" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    if [[ "${status}" =~ ^(502|503|504|000)$ ]]; then
      sleep 3
      continue
    fi
    echo "ERROR: ${method} ${path} returned HTTP ${status}: ${body}" >&2
    return 1
  done
  echo "ERROR: ${method} ${path} never succeeded." >&2
  return 1
}

SUBMITTED=$(vm_request POST /execute '{"code": "print(6 * 7)", "kernel": "python"}')
JOB=$(echo "${SUBMITTED}" | jq -r '.job // empty')
if [[ -z "${JOB}" ]]; then
  echo "ERROR: Sandbox refused the cell: ${SUBMITTED}"
  exit 1
fi

OUTPUT=""
for ((i = 1; i <= 60; i++)); do
  POLLED=$(vm_request GET "/result/${JOB}")
  if [[ "$(echo "${POLLED}" | jq -r '.state')" == "done" ]]; then
    OUTPUT=$(echo "${POLLED}" | jq -r '.result.stdout' | tr -d '\r\n')
    break
  fi
  sleep 1
done
if [[ "${OUTPUT}" != "42" ]]; then
  echo "ERROR: Sandbox did not answer the test cell (got: '${OUTPUT}')."
  exit 1
fi
echo "NOTE: MicroVM came up and ran Python."

echo ""
echo "========================================================"
echo "  MicroVM Agent Sandbox — deployment validated"
echo "========================================================"
echo "  App : ${CUSTOM_URL}"
echo "  Try : Build me a fractal tree and get me the results"
echo "========================================================"
echo ""
