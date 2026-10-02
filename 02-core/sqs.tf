# ================================================================================
# SQS dead-letter queue
# Stores query messages that fail processing after max retries
# ================================================================================

resource "aws_sqs_queue" "query_requests_dlq" {
  name = "agent-query-requests-dlq-${random_id.bucket_suffix.hex}"

  message_retention_seconds = 1209600
  receive_wait_time_seconds = 20

  tags = {
    Name = "agent-query-requests-dlq"
  }
}

# ================================================================================
# SQS main queue
# Receives asynchronous query requests from the API Lambda
# ================================================================================

resource "aws_sqs_queue" "query_requests" {
  name = "agent-query-requests-${random_id.bucket_suffix.hex}"

  # Visibility timeout exceeds the worker's 900s timeout to prevent duplicate
  # processing if the Lambda is still running when the message reappears
  visibility_timeout_seconds = 960
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.query_requests_dlq.arn
    maxReceiveCount     = 1
  })

  tags = {
    Name = "agent-query-requests"
  }
}

# ================================================================================
# DLQ redrive allow policy
# ================================================================================

resource "aws_sqs_queue_redrive_allow_policy" "query_requests_dlq" {
  queue_url = aws_sqs_queue.query_requests_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.query_requests.arn]
  })
}

# ================================================================================
# SQS → Worker Lambda event source mapping
# ================================================================================

resource "aws_lambda_event_source_mapping" "worker_sqs" {
  event_source_arn                   = aws_sqs_queue.query_requests.arn
  function_name                      = aws_lambda_function.worker.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  enabled                            = true
}
