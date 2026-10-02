# =================================================================================
# Backend S3 bucket
# Stores per-query payloads: question.txt, answer.txt, trace.json, and files/
# (images and other files the model showed from its sandbox)
# Bucket name = <base>-<random>
# =================================================================================

resource "aws_s3_bucket" "backend" {
  bucket        = "${var.backend_bucket_base_name}-${random_id.bucket_suffix.hex}"
  force_destroy = true
}

# =================================================================================
# Block all public access
# =================================================================================

resource "aws_s3_bucket_public_access_block" "backend" {
  bucket = aws_s3_bucket.backend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =================================================================================
# Enable server-side encryption
# =================================================================================

resource "aws_s3_bucket_server_side_encryption_configuration" "backend" {
  bucket = aws_s3_bucket.backend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "backend_bucket_name" {
  value = aws_s3_bucket.backend.bucket
}