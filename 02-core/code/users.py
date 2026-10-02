# ================================================================================
# users.py
#
# Purpose
# Handles user registration and token usage tracking.
#
# Key Responsibilities
# - Enforce user cap at registration time (returns 403 if full)
# - Track Bedrock token consumption per user in DynamoDB
# - Expose GET /usage so the frontend ring indicator can show remaining tokens
#
# DynamoDB Keys
#   pk = USER#<user_id>
#   sk = USER#USAGE
# ================================================================================

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

# ------------------------------------------------------------------------------
# AWS clients — shared table reference
# ------------------------------------------------------------------------------

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

# Every user's token budget. Applied at read time rather than stored on the
# usage record, so changing it here takes effect for existing users too.
TOKEN_LIMIT_DEFAULT = 1_000_000


def token_limit(item):
    """Return a usage record's effective token limit.

    A `token_limit_override` on the record (set by hand in DynamoDB) wins;
    otherwise the default applies. The `token_limit` attribute that older
    registrations wrote is deliberately ignored: it froze the default of the
    day into every record, so raising the default never reached anyone.
    """
    override = (item or {}).get("token_limit_override")
    return int(override) if override else TOKEN_LIMIT_DEFAULT

# Hard cap — registration is rejected once this many user records exist
USER_CAP = 500


# ------------------------------------------------------------------------------
# Helpers (duplicated from jobs.py to keep modules self-contained)
# ------------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_user_id(event):
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return claims.get("cognito:username") or claims.get("sub") or "demo"


def json_serializer(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def response(code, body):
    return {
        "statusCode": code,
        "body": json.dumps(body, default=json_serializer)
    }


# ------------------------------------------------------------------------------
# POST /register
# ------------------------------------------------------------------------------

# --------------------------------------------------------------------------------
# Function: register_user
#
# Purpose
# Idempotent registration endpoint. Creates a user usage record the first
# time a user signs in, and enforces USER_CAP before creating it.
#
# Returns 200 for existing users, 403 with error "user_limit_reached" when
# the cap is hit so the frontend can show the waitlist message and sign out.
#
# Arguments
# - event : API Gateway Lambda event dict
#
# Returns
# - {"status": "ok"} on success, {"error": "user_limit_reached"} on 403
# --------------------------------------------------------------------------------
def register_user(event):
    user_id = get_user_id(event)
    pk = f"USER#{user_id}"
    sk = "USER#USAGE"

    existing = table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
    if existing:
        return response(200, {"status": "ok"})

    # Scan for user records — at ≤1000 users this is fast and cheap
    result = table.scan(
        FilterExpression=Attr("sk").eq("USER#USAGE"),
        Select="COUNT"
    )
    total = result.get("Count", 0)
    if total >= USER_CAP:
        return response(403, {"error": "user_limit_reached"})

    table.put_item(Item={
        "pk":           pk,
        "sk":           sk,
        "tokens_used":  0,
        "created_at":   utc_now(),
    })
    return response(200, {"status": "ok"})


# ------------------------------------------------------------------------------
# GET /usage
# ------------------------------------------------------------------------------

# --------------------------------------------------------------------------------
# Function: get_usage
#
# Purpose
# Returns the current user's Bedrock token consumption and their limit.
# Returns zero values when no usage record exists yet.
#
# Arguments
# - event : API Gateway Lambda event dict
#
# Returns
# - {"tokens_used": int, "token_limit": int}
# --------------------------------------------------------------------------------
def get_usage(event):
    user_id = get_user_id(event)
    item = table.get_item(
        Key={"pk": f"USER#{user_id}", "sk": "USER#USAGE"}
    ).get("Item")

    if not item:
        return response(200, {
            "tokens_used": 0,
            "token_limit": TOKEN_LIMIT_DEFAULT,
        })

    return response(200, {
        "tokens_used": int(item.get("tokens_used", 0) or 0),
        "token_limit": token_limit(item),
    })
