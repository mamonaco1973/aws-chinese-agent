# ==============================================================================
# Build Role and Logs — permissions Lambda assumes while building the image
# ==============================================================================
# Exists only for the duration of a build. Unrelated to the role a launched
# MicroVM runs as -- that one is the sandbox role in 02-core/sandbox.tf.

# Build output is the only way to diagnose a Dockerfile or /ready failure.
resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/lambda/microvms/${local.image_name}"
  retention_in_days = 1
}

resource "aws_iam_role" "build" {
  # name_prefix so a rebuild under a new image name cannot collide with the
  # role still attached to the outgoing one.
  name_prefix = "${local.name}-build-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      # TagSession as well as AssumeRole: the build service tags the session it
      # assumes, and omitting it fails the build with an opaque AccessDenied.
      Action = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = aws_s3_object.image.arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.build.arn}:*" }
    ]
  })
}
