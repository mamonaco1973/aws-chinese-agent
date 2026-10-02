# ==============================================================================
# Provider Configuration
# ==============================================================================
# The MicroVM image is created through Cloud Control, which speaks to the same
# AWS::Lambda::MicrovmImage resource type as CloudFormation. No stack is created.

terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "aws-agent-ops", ManagedBy = "Terraform" }
  }
}

locals {
  name = "agent-sandbox"

  # Hash the sources, not the zip: zip entries carry file mtimes, so a fresh
  # checkout would otherwise mint a new image -- and every image bills for at
  # least a week of storage whether or not anything launches from it.
  source_files = sort(fileset("${path.module}/image", "*"))
  source_hash = sha256(join("", [
    for f in local.source_files : "${f}:${filesha256("${path.module}/image/${f}")}"
  ]))

  # A source change produces a new image name, so a snapshot built from
  # different code can never be mistaken for the current one.
  image_name = "${local.name}-${substr(local.source_hash, 0, 10)}"
}
