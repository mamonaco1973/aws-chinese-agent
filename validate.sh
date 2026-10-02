#!/bin/bash
# ==============================================================================
# validate.sh
# ==============================================================================
# Smoke-tests the sandbox image directly, then prints the app URL.
#
#   1. Launch a MicroVM from the image, exactly as the worker does
#   2. Run a cell that renders a fractal tree, then fetch the PNG through /file
#   3. Suspend it, resume it with an ordinary HTTPS request, and prove both
#      sessions survived: a Python variable, and a bash cd + export
#   4. Terminate it -- always, including on failure
#
# It does not exercise Cognito, the worker or Bedrock: sign in and ask for a
# fractal tree for that.
# ==============================================================================

export AWS_DEFAULT_REGION="us-east-1"
set -euo pipefail
cd "$(dirname "$0")"

CUSTOM_URL=$(terraform -chdir=02-core output -raw custom_domain_url      2>/dev/null || true)
COGNITO_UI=$(terraform -chdir=02-core output -raw cognito_hosted_ui_base 2>/dev/null || true)
ROLE_ARN=$(terraform   -chdir=02-core output -raw sandbox_role_arn       2>/dev/null || true)
IMAGE_ARN=$(terraform  -chdir=01-sandbox output -raw image_arn           2>/dev/null || true)
IMAGE_VERSION=$(terraform -chdir=01-sandbox output -raw image_version    2>/dev/null || true)

if [ -z "${CUSTOM_URL}" ] || [ -z "${IMAGE_ARN}" ] || [ -z "${ROLE_ARN}" ]; then
  echo "ERROR: Could not read Terraform outputs. Run ./apply.sh first."
  exit 1
fi

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

wait_for_state() {
  local vm_id="$1" desired="$2" attempt state=""
  for ((attempt = 1; attempt <= 60; attempt++)); do
    state=$(aws lambda-microvms get-microvm --microvm-identifier "${vm_id}" \
      --query "state" --output text)
    [[ "${state}" == "${desired}" ]] && return 0
    sleep 1
  done
  echo "ERROR: MicroVM ${vm_id} never reached ${desired} (last state: ${state})."
  exit 1
}

# Never curl -f: under set -e it aborts on any HTTP error with no message.
# 502/503/504 mean a suspended VM is still being restored, so they are retried.
vm_request() {
  local method="$1" path="$2" data="${3:-}" out="${4:-}"
  local attempt response status body args
  for ((attempt = 1; attempt <= 10; attempt++)); do
    args=(-s --max-time 60 -X "${method}" "https://${ENDPOINT}${path}"
          -H "X-aws-proxy-auth: ${TOKEN}" -H "X-aws-proxy-port: 8080")
    [[ -n "${data}" ]] && args+=(-H "Content-Type: application/json" -d "${data}")
    if [[ -n "${out}" ]]; then
      status=$(curl "${args[@]}" -o "${out}" -w '%{http_code}') || status=000
      body=""
    else
      response=$(curl "${args[@]}" -w $'\n%{http_code}') || response=$'\n000'
      status="${response##*$'\n'}"
      body="${response%$'\n'*}"
    fi
    if [[ "${status}" == "200" ]]; then
      printf '%s' "${body}"
      return 0
    fi
    if [[ "${status}" =~ ^(502|503|504|000)$ ]]; then
      echo "NOTE: ${path} returned ${status}; MicroVM still resuming, retry ${attempt}/10..." >&2
      sleep 3
      continue
    fi
    echo "ERROR: ${method} ${path} returned HTTP ${status}: ${body}" >&2
    return 1
  done
  echo "ERROR: ${method} ${path} never succeeded." >&2
  return 1
}

# Submit a cell and poll it to completion; echoes the result object.
# Usage: run_cell <code> [python|bash]
run_cell() {
  local submitted job polled state waited=0
  submitted=$(vm_request POST /execute \
    "$(jq -n --arg code "$1" --arg kernel "${2:-python}" '{code: $code, kernel: $kernel}')") || return 1
  job=$(echo "${submitted}" | jq -r '.job // empty')
  [[ -n "${job}" ]] || { echo "ERROR: Cell refused: ${submitted}" >&2; return 1; }
  while (( waited < 300 )); do
    polled=$(vm_request GET "/result/${job}") || return 1
    state=$(echo "${polled}" | jq -r '.state')
    if [[ "${state}" == "done" ]]; then
      echo "${polled}" | jq -c '.result'
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "ERROR: Cell did not finish within 300s." >&2
  return 1
}

VM_ID=""
cleanup() {
  if [[ -n "${VM_ID}" ]]; then
    echo "NOTE: Terminating validation sandbox ${VM_ID}..."
    aws lambda-microvms terminate-microvm --microvm-identifier "${VM_ID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# Launch
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
  --maximum-duration-in-seconds 1800)

VM_ID=$(echo "${RUN}" | jq -r '.microvmId')
ENDPOINT=$(echo "${RUN}" | jq -r '.endpoint')
ENDPOINT="${ENDPOINT#https://}"
wait_for_state "${VM_ID}" "RUNNING"
echo "NOTE: MicroVM ${VM_ID} is RUNNING."

TOKEN=$(aws lambda-microvms create-microvm-auth-token \
  --microvm-identifier "${VM_ID}" \
  --expiration-in-minutes 30 \
  --allowed-ports '[{"port":8080}]' \
  --query 'authToken."X-aws-proxy-auth"' --output text)

# ------------------------------------------------------------------------------
# Render a fractal tree and fetch it
# ------------------------------------------------------------------------------

echo "NOTE: Rendering a fractal tree in the sandbox..."
RESULT=$(run_cell 'import numpy as np, matplotlib.pyplot as plt
marker = "validated"
def branch(ax, x, y, a, n, d):
    if d == 0: return
    x2, y2 = x + n * np.cos(a), y + n * np.sin(a)
    ax.plot([x, x2], [y, y2], color=plt.cm.inferno(d / 10), lw=d * 0.5)
    for s in (-1, 1): branch(ax, x2, y2, a + s * np.radians(25), n * 0.75, d - 1)
fig, ax = plt.subplots(figsize=(6, 6), facecolor="black")
ax.set_facecolor("black"); ax.axis("off")
branch(ax, 0, 0, np.pi / 2, 1.0, 10)
fig.savefig("validate_tree.png", dpi=100, facecolor="black"); plt.close(fig)
print("rendered")')
echo "${RESULT}" | jq -e '.ok == true' >/dev/null || {
  echo "ERROR: Render cell failed: $(echo "${RESULT}" | jq -r '.stdout')"
  exit 1
}

mkdir -p dist
vm_request GET "/file?path=validate_tree.png" "" dist/validate_tree.png >/dev/null
if ! head -c 8 dist/validate_tree.png | grep -q "PNG"; then
  echo "ERROR: /file did not return a PNG."
  exit 1
fi
echo "NOTE: Fetched the rendered PNG ($(wc -c < dist/validate_tree.png) bytes) -> dist/validate_tree.png"

# ------------------------------------------------------------------------------
# Suspend, resume, and prove both sessions survived
# ------------------------------------------------------------------------------

echo "NOTE: Seeding bash session state (cd + export)..."
SEEDED=$(run_cell 'cd /tmp && export MARKER=validated && echo seeded' bash) || exit 1
echo "${SEEDED}" | jq -e '.ok == true' >/dev/null || {
  echo "ERROR: Bash seed failed: $(echo "${SEEDED}" | jq -r '.stdout')"
  exit 1
}

echo "NOTE: Suspending the MicroVM..."
aws lambda-microvms suspend-microvm --microvm-identifier "${VM_ID}" >/dev/null
wait_for_state "${VM_ID}" "SUSPENDED"
echo "NOTE: SUSPENDED; compute charges have stopped."

echo "NOTE: Resuming with an ordinary HTTPS request..."
RESUMED=$(run_cell 'marker') || exit 1
OUTPUT=$(echo "${RESUMED}" | jq -r '.stdout' | tr -d '\r\n')
if [[ "${OUTPUT}" != "'validated'" ]]; then
  echo "ERROR: Python state did not survive suspension (got: ${OUTPUT})."
  exit 1
fi
echo "NOTE: Python session survived suspend/resume: marker = ${OUTPUT}"

SHELL_OUT=$(run_cell 'echo "${MARKER} $(pwd)"' bash | jq -r '.stdout' | tr -d '\r\n')
if [[ "${SHELL_OUT}" != "validated /tmp" ]]; then
  echo "ERROR: Bash state did not survive suspension (got: ${SHELL_OUT})."
  exit 1
fi
echo "NOTE: Bash session survived suspend/resume: \$MARKER and cwd = ${SHELL_OUT}"

# The endpoint must refuse a request with no token.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "https://${ENDPOINT}/state")
if [[ "${STATUS}" != "403" ]]; then
  echo "ERROR: Sandbox endpoint returned ${STATUS} without a token; expected 403."
  exit 1
fi
echo "NOTE: Sandbox endpoint rejects unauthenticated requests (403)."

echo ""
echo "========================================================"
echo "  MicroVM Agent Sandbox — deployment validated"
echo "========================================================"
echo "  App : ${CUSTOM_URL}"
echo "  Try : Build me a fractal tree and get me the results"
echo "========================================================"
echo ""
echo "  Google IDP — Authorized redirect URI:"
echo "  ${COGNITO_UI}/oauth2/idpresponse"
echo ""
