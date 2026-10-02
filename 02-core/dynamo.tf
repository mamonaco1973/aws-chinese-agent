# ================================================================================
# DynamoDB table
# Single-table design for all application data
#
# Key patterns:
#   pk=USER#<id>, sk=USER#USAGE          — token budget record
#   pk=USER#<id>, sk=CONV#<id>           — conversation metadata
#   pk=USER#<id>, sk=QUERY#<conv>#<id>   — query status and S3 pointers
# ================================================================================

resource "aws_dynamodb_table" "app_table" {
  name         = "agent-app-${random_id.bucket_suffix.hex}"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "pk"
  range_key = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  tags = {
    Name = "agent-app"
  }
}
