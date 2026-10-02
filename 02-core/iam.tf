# ================================================================================
# Lambda execution role
# ================================================================================

resource "aws_iam_role" "lambda_exec" {
  name = "agent-app-lambda-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# ================================================================================
# CloudWatch logging
# ================================================================================

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ================================================================================
# DynamoDB access
# ================================================================================

resource "aws_iam_policy" "lambda_dynamodb" {
  name = "agent-app-dynamodb-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        # Scan used to count registered users for the USER_CAP check
        "dynamodb:Scan"
      ]
      Resource = aws_dynamodb_table.app_table.arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# ================================================================================
# S3 access — user data only: question/answer/trace payloads and the files the
# model showed from its sandbox. ListBucket lets a conversation delete sweep
# its whole prefix.
# ================================================================================

resource "aws_iam_policy" "lambda_s3" {
  name = "agent-s3-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BackendBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.backend.arn
      },
      {
        Sid      = "UserDataAccess"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.backend.arn}/users/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_s3_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_s3.arn
}

# ================================================================================
# SQS access
# ================================================================================

resource "aws_iam_policy" "lambda_sqs" {
  name = "agent-app-sqs-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "QueryQueueAccess"
      Effect = "Allow"
      Action = [
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility"
      ]
      Resource = [
        aws_sqs_queue.query_requests.arn,
        aws_sqs_queue.query_requests_dlq.arn
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_sqs_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_sqs.arn
}

# ================================================================================
# Bedrock access — the worker calls the model directly through Converse, which
# is authorized as bedrock:InvokeModel. A cross-region inference profile needs
# the grant on the profile AND on the foundation model in every region the
# profile may route to, hence the wildcard region on the model ARN.
# ================================================================================

resource "aws_iam_policy" "lambda_bedrock" {
  name = "agent-bedrock-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "InvokeModel"
      Effect = "Allow"
      Action = ["bedrock:InvokeModel"]
      Resource = [
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
        "arn:aws:bedrock:*::foundation-model/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_bedrock_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_bedrock.arn
}

# ================================================================================
# MicroVM access — launch, reach, and terminate sandboxes from this image only
# ================================================================================

resource "aws_iam_policy" "lambda_microvms" {
  name = "agent-microvms-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SandboxLifecycle"
        Effect = "Allow"
        Action = [
          "lambda:RunMicrovm",
          "lambda:GetMicrovm",
          "lambda:TerminateMicrovm",
          "lambda:CreateMicrovmAuthToken",
        ]
        Resource = var.sandbox_image_arn
      },
      # Required to launch with an execution role, and absent from the
      # least-privilege example in the MicroVMs documentation. Deliberately
      # NO iam:PassedToService condition: the service does not populate that
      # key, so the condition never matches and the grant silently grants
      # nothing -- both found the hard way in aws-lambda-microvms.
      {
        Sid      = "PassSandboxRole"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = aws_iam_role.sandbox.arn
      },
      {
        Sid    = "SandboxNetworking"
        Effect = "Allow"
        Action = ["lambda:PassNetworkConnector"]
        Resource = [
          "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
          "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:ALL_INGRESS",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_microvms_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_microvms.arn
}
