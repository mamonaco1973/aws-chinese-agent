# ================================================================================
# Frontend S3 bucket base name
# ================================================================================

variable "frontend_bucket_base_name" {
  description = "Base name for the frontend S3 bucket"
  type        = string
  default     = "agent-app"
}

# ================================================================================
# Backend S3 bucket base name
# ================================================================================

variable "backend_bucket_base_name" {
  description = "Base name for the backend S3 bucket"
  type        = string
  default     = "agent-data"
}

# ================================================================================
# AWS region
# ================================================================================

variable "region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

# ================================================================================
# Model — the Bedrock inference profile the worker's Converse loop calls. It
# must support tool use and image input. Set in bedrock-config.sh.
# ================================================================================

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile id for the Converse loop"
  type        = string
}

# ================================================================================
# Sandbox image — built by 01-sandbox and passed in by apply.sh
# ================================================================================

variable "sandbox_image_arn" {
  description = "ARN of the MicroVM image sandboxes launch from"
  type        = string
}

variable "sandbox_image_version" {
  description = "Image version to launch; pinned to the one 01-sandbox just built"
  type        = string
}

# ================================================================================
# Google OAuth — optional; set to enable Google sign-in via Cognito IdP
# Populated from AWS_AGENTOPS_GOOGLE_CLIENT_ID / AWS_AGENTOPS_GOOGLE_CLIENT_SECRET
# ================================================================================

variable "google_client_id" {
  description = "Google OAuth client ID for Cognito identity provider"
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = "Google OAuth client secret for Cognito identity provider"
  type        = string
  default     = ""
  sensitive   = true
}

# ================================================================================
# Custom domain — leave empty to serve directly from CloudFront's default domain
# When set, also set route53_zone_id to create ACM + DNS records automatically
# ================================================================================

variable "custom_domain" {
  description = "Custom domain name (e.g. sandbox.example.com). Leave empty to use CloudFront default domain. The parent hosted zone is looked up automatically."
  type        = string
  default     = ""
}
