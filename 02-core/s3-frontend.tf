# =================================================================================
# Frontend S3 bucket
# Hosts the static web application
# Bucket name = <base>-<random>
# =================================================================================

resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.frontend_bucket_base_name}-${random_id.bucket_suffix.hex}"
  force_destroy = true
}

# =================================================================================
# Enable static website hosting
# =================================================================================

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  index_document {
    suffix = "index.html"
  }
}

# =================================================================================
# Public read access for website objects
# =================================================================================

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

# =================================================================================
# Public read bucket policy
# =================================================================================

data "aws_iam_policy_document" "frontend_public_read" {
  statement {
    actions = ["s3:GetObject"]

    resources = [
      "${aws_s3_bucket.frontend.arn}/*"
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_public_read.json

  depends_on = [
    aws_s3_bucket_public_access_block.frontend
  ]
}

# =================================================================================
# Outputs
# =================================================================================

# ---------------------------------------------------------------------------------
# Frontend bucket name
# ---------------------------------------------------------------------------------

output "frontend_bucket_name" {
  description = "Name of the S3 bucket hosting the web application"
  value       = aws_s3_bucket.frontend.bucket
}

# ---------------------------------------------------------------------------------
# Frontend website URL
# ---------------------------------------------------------------------------------

output "frontend_website_url" {
  description = "Public S3 website endpoint for the web application"
  value       = "https://${aws_s3_bucket.frontend.bucket}.s3.${var.region}.amazonaws.com"
}