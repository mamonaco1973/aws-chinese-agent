#!/bin/bash
# ================================================================================
# apply.sh
# Full deployment of the MicroVM sandbox agent.
#
# Workflow:
#   1. Validate tooling, AWS credentials, MicroVM CLI support, model access
#   2. Package the sandbox image source and a boto3 layer for the Lambdas
#   3. 01-sandbox: build the MicroVM image (Lambda builds it remotely)
#   4. 02-core:    API, worker, Cognito, SQS, DynamoDB, S3, CloudFront
#   5. 03-webapp:  generate config.js and upload the SPA
#   6. validate.sh: smoke-test a sandbox and print the app URL
# ================================================================================

export AWS_DEFAULT_REGION="us-east-1"

# Defines BEDROCK_MODELS / BEDROCK_DEFAULT and bedrock_models_json. Sourced before strict mode so simple assignments
# don't trip the unbound-variable check.
source "$(dirname "$0")/bedrock-config.sh"

set -euo pipefail
cd "$(dirname "$0")"

# Must match the runtime in 02-core/lambdas.tf; selects the vendored wheels.
LAMBDA_PYTHON="3.13"

# ================================================================================
# Environment pre-check
# ================================================================================

echo "NOTE: Running environment validation..."
./check_env.sh || { echo "ERROR: Environment validation failed. Exiting."; exit 1; }

# ================================================================================
# Packaging
# ================================================================================

echo "NOTE: Packaging the sandbox image source..."
rm -rf dist && mkdir -p dist
(cd 01-sandbox/image && zip -q -X -r ../../dist/sandbox-image.zip . -x '*__pycache__*')

# The Lambda runtime's bundled SDK predates lambda-microvms. These wheels
# target the Lambda runtime, not this host, so Requires-Python is checked
# against LAMBDA_PYTHON instead of the local interpreter; every dependency is
# a pure-Python wheel, so the host's platform does not matter.
echo "NOTE: Vendoring boto3 into the Lambda layer..."
python3 -m pip install --quiet --disable-pip-version-check --no-compile \
  --only-binary=:all: --python-version "${LAMBDA_PYTHON}" \
  --ignore-requires-python --no-warn-conflicts \
  --target dist/layer/python "boto3>=1.43.0"

# A too-old boto3 would otherwise ship silently and fail at runtime with
# "Unknown service: lambda-microvms".
if [[ ! -d dist/layer/python/botocore/data/lambda-microvms ]]; then
  echo "ERROR: Vendored boto3 has no lambda-microvms service model."
  exit 1
fi
(cd dist/layer && zip -q -X -r ../boto3-layer.zip python -x '*/__pycache__/*')

# ================================================================================
# Select the managed base image
# ================================================================================
# BaseImageVersion is required by AWS::Lambda::MicrovmImage and versions age
# out, so resolve the newest at deploy time rather than pinning one in source.
# ================================================================================

echo "NOTE: Selecting the newest managed MicroVM base image version..."
BASE_IMAGE_ARN="arn:aws:lambda:${AWS_DEFAULT_REGION}:aws:microvm-image:al2023-1"
BASE_IMAGE_VERSION=$(aws lambda-microvms list-managed-microvm-image-versions \
  --image-identifier "${BASE_IMAGE_ARN}" \
  --query "sort_by(items, &createdAt)[-1].imageVersion" --output text)

if [[ -z "${BASE_IMAGE_VERSION}" || "${BASE_IMAGE_VERSION}" == "None" ]]; then
  echo "ERROR: No managed base image version found for ${BASE_IMAGE_ARN}."
  exit 1
fi
echo "NOTE: Using base image version ${BASE_IMAGE_VERSION}"

# ================================================================================
# 01-sandbox — build the MicroVM image
# ================================================================================

echo "NOTE: Building the sandbox MicroVM image (this takes several minutes)..."
terraform -chdir=01-sandbox init -input=false
terraform -chdir=01-sandbox apply -auto-approve -input=false \
  -var="region=${AWS_DEFAULT_REGION}" \
  -var="base_image_version=${BASE_IMAGE_VERSION}"

IMAGE_ARN=$(terraform -chdir=01-sandbox output -raw image_arn)
IMAGE_VERSION=$(terraform -chdir=01-sandbox output -raw image_version)
echo "NOTE: Sandbox image ${IMAGE_ARN##*:} version ${IMAGE_VERSION}"

# ================================================================================
# 02-core — backend
# ================================================================================

echo "NOTE: Deploying backend (API, worker, auth, storage)..."
terraform -chdir=02-core init -input=false
terraform -chdir=02-core apply -auto-approve -input=false \
  -var="models=$(bedrock_models_json)" \
  -var="default_model=${BEDROCK_DEFAULT}" \
  -var="sandbox_image_arn=${IMAGE_ARN}" \
  -var="sandbox_image_version=${IMAGE_VERSION}" \
  -var="google_client_id=${AWS_AGENTOPS_GOOGLE_CLIENT_ID:-}" \
  -var="google_client_secret=${AWS_AGENTOPS_GOOGLE_CLIENT_SECRET:-}" \
  -var="custom_domain=${AWS_AGENTOPS_CUSTOM_DOMAIN:-}"

export API_BASE_URL=$(terraform -chdir=02-core output -raw api_endpoint)
export COGNITO_DOMAIN=$(terraform -chdir=02-core output -raw cognito_hosted_ui_base)
export COGNITO_CLIENT_ID=$(terraform -chdir=02-core output -raw cognito_user_pool_client_id)
BUCKET_NAME=$(terraform -chdir=02-core output -raw frontend_bucket_name)
CF_DISTRIBUTION_ID=$(terraform -chdir=02-core output -raw cloudfront_distribution_id)

# ================================================================================
# 03-webapp — frontend
# ================================================================================

echo "NOTE: Deploying web application..."

envsubst < 03-webapp/js/config.js.tmpl > 03-webapp/js/config.js
aws s3 cp 03-webapp "s3://${BUCKET_NAME}" --recursive --exclude "*.tmpl"

# Invalidate CloudFront so updated assets are served immediately
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/*" > /dev/null

# ================================================================================
# Post-deploy validation
# ================================================================================

echo "NOTE: Running post-deployment validation..."
./validate.sh
