# ================================================================================
# Provider constraints
#
# Pin a floor rather than an exact version so `terraform init` still tracks
# patch/minor updates. The MicroVM image itself is built in 01-sandbox; nothing
# here needs a MicroVM-aware provider, only the SDK the Lambdas vendor.
# ================================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}
