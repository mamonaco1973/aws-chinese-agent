# Consumed by apply.sh, which passes them into 02-core so the worker knows
# which image to launch sandboxes from.
output "image_arn" {
  value = jsondecode(aws_cloudcontrolapi_resource.image.properties).ImageArn
}

# Each build produces a new version. Pinning the worker to the version just
# built stops it launching from a half-finished later one.
output "image_version" {
  value = jsondecode(aws_cloudcontrolapi_resource.image.properties).LatestActiveImageVersion
}

output "image_name" { value = local.image_name }

output "base_image_version" { value = var.base_image_version }

# Where a failed image build explains itself.
output "build_log_group" { value = aws_cloudwatch_log_group.build.name }
