# ==============================================================================
# Sandbox Image — a pre-initialized Python session snapshot
# ==============================================================================
# AWS runs the Dockerfile, starts the supervisor, waits for /ready (which does
# not pass until numpy and matplotlib are imported), then captures memory AND
# disk. Every sandbox launches from that snapshot with the libraries already
# loaded.
#
# Sent as JSON through Cloud Control rather than the AWSCC provider: nearly
# every property of AWS::Lambda::MicrovmImage is Required, including the empty
# arrays below, and AWSCC drops empty sets, which Cloud Control then rejects.

resource "aws_cloudcontrolapi_resource" "image" {
  type_name = "AWS::Lambda::MicrovmImage"

  desired_state = jsonencode({
    Name        = local.image_name
    Description = "Python sandbox for the agent (numpy, matplotlib)"

    # AWS-managed AL2023 base. There is exactly one; only its version varies.
    BaseImageArn     = "arn:aws:lambda:${var.region}:aws:microvm-image:al2023-1"
    BaseImageVersion = var.base_image_version

    BuildRoleArn = aws_iam_role.build.arn
    CodeArtifact = { Uri = "s3://${aws_s3_bucket.artifact.id}/${aws_s3_object.image.key}" }

    CpuConfigurations = [{ Architecture = "ARM_64" }]

    # Smallest baseline (0.5 GB / 0.25 vCPU), bursting to 4x under load --
    # enough for matplotlib, and the size aws-lambda-microvms was proven on.
    Resources = [{ MinimumMemoryInMiB = 512 }]

    # Required by the schema, and both must stay present even when empty.
    AdditionalOsCapabilities = ["ALL"]
    EnvironmentVariables     = []

    # Internet egress so the model can pip install something the image lacks.
    EgressNetworkConnectors = ["arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"]

    Logging = { CloudWatch = { LogGroup = aws_cloudwatch_log_group.build.name } }

    Hooks = {
      # Hooks on 8081, the application on 8080. Endpoint tokens are scoped to
      # 8080, so application traffic can never drive a lifecycle hook.
      Port = 8081

      MicrovmImageHooks = {
        Ready    = "ENABLED", ReadyTimeoutInSeconds = 120
        Validate = "ENABLED", ValidateTimeoutInSeconds = 60
      }

      # Run generates the per-VM nonce after restore: anything created before
      # the snapshot is shared by every clone launched from it.
      MicrovmHooks = {
        Run       = "ENABLED", RunTimeoutInSeconds = 10
        Suspend   = "ENABLED", SuspendTimeoutInSeconds = 10
        Resume    = "ENABLED", ResumeTimeoutInSeconds = 10
        Terminate = "ENABLED", TerminateTimeoutInSeconds = 10
      }
    }

    Tags = [{ Key = "Project", Value = "aws-agent-ops" }]
  })

  # The build role must be able to read the artifact before the build starts.
  depends_on = [aws_iam_role_policy.build, aws_s3_bucket_public_access_block.artifact]
}
