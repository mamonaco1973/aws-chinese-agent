# ================================================================================
# Cognito locals
# ================================================================================

locals {
  # Resolve origin at apply time — custom domain takes priority over CloudFront default
  spa_origin         = var.custom_domain != "" ? "https://${var.custom_domain}" : "https://${aws_cloudfront_distribution.app.domain_name}"
  identity_providers = var.google_client_id != "" ? ["COGNITO", "Google"] : ["COGNITO"]
}

# ================================================================================
# Cognito User Pool
# ================================================================================

resource "aws_cognito_user_pool" "app" {
  name = "agent-app-user-pool-${random_id.bucket_suffix.hex}"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# ================================================================================
# Cognito Hosted UI domain
# ================================================================================

resource "aws_cognito_user_pool_domain" "app" {
  domain       = "agent-app-auth-${random_id.bucket_suffix.hex}"
  user_pool_id = aws_cognito_user_pool.app.id
}

# ================================================================================
# Cognito User Pool Client — SPA PKCE client
# ================================================================================

resource "aws_cognito_user_pool_client" "app" {
  name         = "agent-app-spa-client-${random_id.bucket_suffix.hex}"
  user_pool_id = aws_cognito_user_pool.app.id

  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH"
  ]

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]

  supported_identity_providers = local.identity_providers

  callback_urls = ["${local.spa_origin}/callback.html"]
  logout_urls   = ["${local.spa_origin}/index.html"]

  depends_on = [aws_cognito_identity_provider.google]
}

# ================================================================================
# Google identity provider — only created when credentials are supplied
# ================================================================================

resource "aws_cognito_identity_provider" "google" {
  count = var.google_client_id != "" ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.app.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id            = var.google_client_id
    client_secret        = var.google_client_secret
    authorize_scopes     = "email profile openid"
    token_request_method = "POST"
    oidc_issuer          = "https://accounts.google.com"
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
  }
}

# ================================================================================
# Outputs consumed by apply.sh to configure the frontend
# ================================================================================

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.app.id
}

output "cognito_user_pool_client_id" {
  value = aws_cognito_user_pool_client.app.id
}

output "cognito_domain" {
  value = aws_cognito_user_pool_domain.app.domain
}

output "cognito_hosted_ui_base" {
  value = "https://${aws_cognito_user_pool_domain.app.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}
