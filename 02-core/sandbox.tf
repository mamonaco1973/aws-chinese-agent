# ================================================================================
# Sandbox Execution Role — the MicroVM guest's AWS identity
# ================================================================================
# The MicroVM equivalent of an EC2 instance profile, attached by RunMicrovm.
# Deliberately empty: model-written code runs in the sandbox, so whatever this
# role can do, any prompt can do. With no policy the guest can reach the
# internet (for pip) but cannot call a single AWS API as itself.
#
# Files leave the sandbox through the worker, which fetches them from the VM
# and writes them to S3 under its own role -- the guest never touches S3.

resource "aws_iam_role" "sandbox" {
  name = "agent-sandbox-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      # TagSession alongside AssumeRole: the service tags the session it
      # assumes, and omitting it fails with an opaque AccessDenied.
      Action = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

# validate.sh launches a sandbox directly, with the same role the worker uses.
output "sandbox_role_arn" {
  value = aws_iam_role.sandbox.arn
}
