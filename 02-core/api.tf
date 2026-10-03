# ================================================================================
# API Gateway HTTP API
# ================================================================================

resource "aws_apigatewayv2_api" "api" {
  name          = "agent-api-${random_id.bucket_suffix.hex}"
  protocol_type = "HTTP"

  cors_configuration {
    # Origins the SPA is actually served from: the S3 website endpoint, the
    # CloudFront default domain, and the custom domain when one is configured.
    allow_origins = compact([
      "https://${aws_s3_bucket.frontend.bucket}.s3.${data.aws_region.current.region}.amazonaws.com",
      "https://${aws_cloudfront_distribution.app.domain_name}",
      var.custom_domain != "" ? "https://${var.custom_domain}" : "",
    ])

    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["*"]
    max_age       = 300
  }
}

# ================================================================================
# Cognito JWT authorizer
# ================================================================================

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id          = aws_apigatewayv2_api.api.id
  name            = "agent-cognito-jwt"
  authorizer_type = "JWT"

  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.app.id]

    issuer = join("", [
      "https://cognito-idp.",
      data.aws_region.current.region,
      ".amazonaws.com/",
      aws_cognito_user_pool.app.id
    ])
  }
}

# ================================================================================
# Lambda integration
# ================================================================================

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

# ================================================================================
# Routes — user management
# ================================================================================

resource "aws_apigatewayv2_route" "register_user" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /register"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "get_usage" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /usage"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Keys and labels for the new-chat model picker.
resource "aws_apigatewayv2_route" "list_models" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /models"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# ================================================================================
# Routes — conversations
# ================================================================================

resource "aws_apigatewayv2_route" "list_conversations" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /conversations"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "create_conversation" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /conversations"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "delete_conversation" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "DELETE /conversations/{conv_id}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# ================================================================================
# Routes — queries within a conversation
# ================================================================================

resource "aws_apigatewayv2_route" "submit_query" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /conversations/{conv_id}/queries"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "list_queries" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /conversations/{conv_id}/queries"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "get_query" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "GET /conversations/{conv_id}/queries/{query_id}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# ================================================================================
# Stage
# ================================================================================

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
}

# ================================================================================
# Allow API Gateway to invoke Lambda
# ================================================================================

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# ================================================================================
# API endpoint output
# ================================================================================

output "api_endpoint" {
  description = "Base URL for the API Gateway endpoint"
  value       = aws_apigatewayv2_api.api.api_endpoint
}
