# ================================================================================
# boto3 layer
# The Lambda runtime's bundled SDK predates lambda-microvms, so apply.sh vendors
# a current boto3 into dist/boto3-layer.zip. Both functions need it: the worker
# launches and drives sandboxes, the API terminates one when its conversation is
# deleted.
# ================================================================================

resource "aws_lambda_layer_version" "boto3" {
  layer_name          = "agent-boto3-${random_id.bucket_suffix.hex}"
  filename            = "${path.module}/../dist/boto3-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/../dist/boto3-layer.zip")
  compatible_runtimes = ["python3.13"]
}

locals {
  # Shared by both functions: sandbox.py reads these at import, and
  # conversations.py imports sandbox.py.
  lambda_env = {
    TABLE_NAME            = aws_dynamodb_table.app_table.name
    BACKEND_BUCKET_NAME   = aws_s3_bucket.backend.bucket
    QUERY_QUEUE_URL       = aws_sqs_queue.query_requests.id
    SANDBOX_IMAGE_ARN     = var.sandbox_image_arn
    SANDBOX_IMAGE_VERSION = var.sandbox_image_version
    SANDBOX_ROLE_ARN      = aws_iam_role.sandbox.arn
  }
}

# ================================================================================
# API Lambda function
# Handles all synchronous API Gateway requests (conversations, queries, usage)
# ================================================================================

resource "aws_lambda_function" "api" {
  function_name = "agent-api-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "handler.lambda_handler"
  runtime = "python3.13"
  layers  = [aws_lambda_layer_version.boto3.arn]

  role    = aws_iam_role.lambda_exec.arn
  timeout = 15

  environment {
    variables = local.lambda_env
  }
}

# ================================================================================
# CloudWatch log group for API Lambda
# ================================================================================

resource "aws_cloudwatch_log_group" "api_logs" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 7
}

# ================================================================================
# Worker Lambda function
# SQS-triggered — runs the Converse tool loop against the conversation's
# MicroVM sandbox, and stores the answer, trace and any files it produced.
# ================================================================================

resource "aws_lambda_function" "worker" {
  function_name = "agent-worker-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "worker.lambda_handler"
  runtime = "python3.13"
  layers  = [aws_lambda_layer_version.boto3.arn]

  role = aws_iam_role.lambda_exec.arn

  # The Lambda maximum. A query is several model turns plus the cells they
  # run, and a pip install inside a cell can take minutes; the loop watches
  # the remaining time and finishes cleanly before this.
  timeout     = 900
  memory_size = 512

  environment {
    variables = merge(local.lambda_env, {
      BEDROCK_MODEL_ID = var.bedrock_model_id
    })
  }
}

# ================================================================================
# CloudWatch log group for worker Lambda
# ================================================================================

resource "aws_cloudwatch_log_group" "worker_logs" {
  name              = "/aws/lambda/${aws_lambda_function.worker.function_name}"
  retention_in_days = 7
}
