# ==============================================================================
# bedrock-config.sh
# ==============================================================================
# Single source of truth for Bedrock model selection. Sourced by apply.sh
# and destroy.sh so both stay in sync.
#
# BEDROCK_MODEL_ID is a cross-region inference profile ID. The us.* prefix
# routes requests across us-east-1, us-east-2, and us-west-2 automatically.
#
# The model must support tool use and image input: the worker's Converse loop
# hands it the images it renders in the sandbox so it can check its own work.
# Sonnet over Haiku for the demo: it gets a working figure on the first try
# far more often, which matters on camera. (Sonnet 5 is listed ACTIVE in this
# account but refuses Converse until access is granted; check_env.sh catches
# that. Switch here once it is enabled.)
#
# To switch models, edit this value. It flows to:
#   • check_env.sh  — pre-flight profile check + invoke probe (via exported env)
#   • 02-core       — worker Lambda BEDROCK_MODEL_ID env var (via Terraform var)
# ==============================================================================

export BEDROCK_MODEL_ID="us.anthropic.claude-sonnet-4-6"
