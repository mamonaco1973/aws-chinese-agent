# ==============================================================================
# Build Artifact — the source zip Lambda builds the MicroVM image from
# ==============================================================================
# Lambda pulls this zip and builds the ARM64 image remotely, so no local Docker
# daemon, buildx or ECR repository is needed anywhere in this project.

resource "aws_s3_bucket" "artifact" {
  bucket_prefix = "${local.name}-"
  force_destroy = true
}

# Build source only. Nothing here is ever served, so every public path is shut.
resource "aws_s3_bucket_public_access_block" "artifact" {
  bucket                  = aws_s3_bucket.artifact.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifact" {
  bucket = aws_s3_bucket.artifact.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# source_hash tracks the sources, not the zip bytes, for the same reason the
# image name does: a re-zip of unchanged code must not trigger a rebuild.
resource "aws_s3_object" "image" {
  bucket      = aws_s3_bucket.artifact.id
  key         = "${local.image_name}.zip"
  source      = "${path.module}/../dist/sandbox-image.zip"
  source_hash = local.source_hash
}
