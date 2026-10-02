#!/bin/bash
# ================================================================================
# File: destroy.sh
# ================================================================================
#
# Purpose:
#   Tears down everything apply.sh deployed, in reverse order.
#
#   MicroVMs are not owned by Terraform -- the worker launches them on demand --
#   so every sandbox launched from the image is terminated first. The image
#   cannot be deleted while sessions launched from it are still alive, and a
#   forgotten sandbox would bill until its 8-hour lifetime runs out.
#
# ================================================================================

export AWS_DEFAULT_REGION="us-east-1"

# Terraform needs the model ID during destroy to resolve variable references.
source "$(dirname "$0")/bedrock-config.sh"

set -euo pipefail
cd "$(dirname "$0")"

# ================================================================================
# TERMINATE SANDBOXES
# ================================================================================
# Inventory the service rather than DynamoDB, so orphans left by a failed
# worker call are found too.
# ================================================================================

IMAGE_ARN=""
IMAGE_VERSION="1"
BASE_IMAGE_VERSION="1"
if [[ -f 01-sandbox/terraform.tfstate ]]; then
  IMAGE_ARN=$(terraform -chdir=01-sandbox output -raw image_arn 2>/dev/null || true)
  IMAGE_VERSION=$(terraform -chdir=01-sandbox output -raw image_version 2>/dev/null || echo 1)
  BASE_IMAGE_VERSION=$(terraform -chdir=01-sandbox output -raw base_image_version 2>/dev/null || echo 1)
fi

if [[ -n "${IMAGE_ARN}" ]]; then
  echo "NOTE: Terminating sandboxes launched from ${IMAGE_ARN##*:}..."
  VM_IDS=$(aws lambda-microvms list-microvms --image-identifier "${IMAGE_ARN}" \
    --query "items[?state!='TERMINATED'].microvmId" --output text)

  for vm_id in ${VM_IDS}; do
    echo "NOTE: Terminating ${vm_id}..."
    aws lambda-microvms terminate-microvm --microvm-identifier "${vm_id}" >/dev/null || true
  done

  # The image delete fails while any session is still TERMINATING.
  for vm_id in ${VM_IDS}; do
    for ((attempt = 1; attempt <= 60; attempt++)); do
      state=$(aws lambda-microvms get-microvm --microvm-identifier "${vm_id}" \
        --query "state" --output text 2>/dev/null || echo TERMINATED)
      [[ "${state}" == "TERMINATED" ]] && break
      sleep 2
    done
    [[ "${state}" == "TERMINATED" ]] || {
      echo "ERROR: ${vm_id} is ${state}; retry destroy after termination completes."
      exit 1
    }
  done
  echo "NOTE: All sandboxes terminated."
fi

# ================================================================================
# RESTORE BUILD ARTIFACTS THE CONFIGURATION REFERENCES
# ================================================================================
# Terraform evaluates the whole configuration before destroying anything, and
# both phases hash files under dist/. Only existence matters for a destroy, so
# rebuild cheaply rather than requiring a full apply-time package.
# ================================================================================

mkdir -p dist
if [[ ! -f dist/sandbox-image.zip ]]; then
  (cd 01-sandbox/image && zip -q -X -r ../../dist/sandbox-image.zip . -x '*__pycache__*')
fi
if [[ ! -f dist/boto3-layer.zip ]]; then
  mkdir -p dist/layer/python
  (cd dist/layer && zip -q -X -r ../boto3-layer.zip python)
fi

# ================================================================================
# DESTROY TERRAFORM RESOURCES IN REVERSE ORDER
# ================================================================================
# 03-webapp holds no Terraform state: its files live in the frontend bucket,
# which 02-core destroys with force_destroy.
# ================================================================================

if [[ -f 02-core/terraform.tfstate ]]; then
  echo "NOTE: Destroying 02-core..."
  terraform -chdir=02-core init -input=false
  terraform -chdir=02-core destroy -auto-approve -input=false \
    -var="bedrock_model_id=${BEDROCK_MODEL_ID}" \
    -var="sandbox_image_arn=${IMAGE_ARN:-arn:aws:lambda:${AWS_DEFAULT_REGION}:000000000000:microvm-image:unused}" \
    -var="sandbox_image_version=${IMAGE_VERSION}" \
    -var="google_client_id=${AWS_AGENTOPS_GOOGLE_CLIENT_ID:-}" \
    -var="google_client_secret=${AWS_AGENTOPS_GOOGLE_CLIENT_SECRET:-}" \
    -var="custom_domain=${AWS_AGENTOPS_CUSTOM_DOMAIN:-}"
fi

if [[ -f 01-sandbox/terraform.tfstate ]]; then
  echo "NOTE: Destroying 01-sandbox..."
  terraform -chdir=01-sandbox init -input=false
  terraform -chdir=01-sandbox destroy -auto-approve -input=false \
    -var="region=${AWS_DEFAULT_REGION}" \
    -var="base_image_version=${BASE_IMAGE_VERSION}"
fi

echo "NOTE: Infrastructure teardown complete."
