# ================================================================================
# conversations.py
#
# Purpose
# CRUD for conversations and queries.
#
# Key Responsibilities
# - Create / list / delete conversations
# - Submit a query (writes question to S3, enqueues SQS message)
# - List queries in a conversation (for rebuilding chat history)
# - Poll a single query for completion status
#
# DynamoDB Keys
#   pk = USER#<user_id>,  sk = CONV#<conv_id>
#   pk = USER#<user_id>,  sk = QUERY#<conv_id>#<query_id>
#
# S3 Layout
#   users/USER#<id>/conversations/CONV#<conv_id>/QUERY#<query_id>/question.txt
#   users/USER#<id>/conversations/CONV#<conv_id>/QUERY#<query_id>/answer.txt
#   users/USER#<id>/conversations/CONV#<conv_id>/QUERY#<query_id>/trace.json
#   users/USER#<id>/conversations/CONV#<conv_id>/QUERY#<query_id>/files/<n>-<name>
#
# A conversation's sandbox MicroVM is recorded on its CONV# item by the worker
# (see sandbox.py) and terminated here when the conversation is deleted.
# ================================================================================

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

import models
import sandbox
from users import token_limit

# --------------------------------------------------------------------------------
# AWS clients
# --------------------------------------------------------------------------------

table  = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sqs    = boto3.client("sqs")
s3     = boto3.client("s3")

# SigV4 against the regional endpoint, so presigned file links work in every
# region rather than only where the global endpoint signs correctly.
s3_presign = boto3.client(
    "s3", region_name=os.environ["AWS_REGION"],
    endpoint_url=f"https://s3.{os.environ['AWS_REGION']}.amazonaws.com",
    config=Config(signature_version="s3v4"),
)

BACKEND_BUCKET  = os.environ["BACKEND_BUCKET_NAME"]
QUERY_QUEUE_URL = os.environ["QUERY_QUEUE_URL"]

# File links are re-signed on every poll and history load, so they only need
# to outlive one page view. A long expiry would be capped anyway by the
# Lambda's temporary credentials, which sign it.
FILE_URL_SECONDS = 3600


# --------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------

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


def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "body": json.dumps(body, default=_decimal_default),
    }


def _decimal_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Not serializable: {type(value).__name__}")


def _s3_prefix(user_id, conv_id, query_id):
    return (
        f"users/USER#{user_id}/conversations/"
        f"CONV#{conv_id}/QUERY#{query_id}"
    )


def _write_s3(key, text):
    s3.put_object(
        Bucket=BACKEND_BUCKET,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def _read_s3(key):
    result = s3.get_object(Bucket=BACKEND_BUCKET, Key=key)
    return result["Body"].read().decode("utf-8")


def _check_token_budget(user_id):
    """Return (tokens_used, token_limit, over_budget)."""
    item = table.get_item(
        Key={"pk": f"USER#{user_id}", "sk": "USER#USAGE"}
    ).get("Item", {})
    used  = int(item.get("tokens_used",  0) or 0)
    limit = token_limit(item)
    return used, limit, used >= limit


# --------------------------------------------------------------------------------
# POST /conversations
# --------------------------------------------------------------------------------

def list_models(event):
    """Keys and labels for the new-chat model picker, plus the default."""
    return json_response(200, models.public())


def create_conversation(event):
    """Create a new empty conversation. Title derives from the first query."""
    user_id = get_user_id(event)
    conv_id = str(uuid.uuid4())
    now     = utc_now()

    table.put_item(Item={
        "pk":         f"USER#{user_id}",
        "sk":         f"CONV#{conv_id}",
        "title":      "New conversation",
        "created_at": now,
        "updated_at": now,
    })

    # Same shape as a list_conversations entry: the sidebar groups chats by
    # updated_at, and a new chat without one was filed under "Older".
    return json_response(200, {
        "conv_id":    conv_id,
        "title":      "New conversation",
        "created_at": now,
        "updated_at": now,
    })


# --------------------------------------------------------------------------------
# GET /conversations
# --------------------------------------------------------------------------------

def list_conversations(event):
    """Return all conversations for the user, newest first."""
    user_id = get_user_id(event)

    result = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"USER#{user_id}") &
            Key("sk").begins_with("CONV#")
        )
    )

    convs = [
        {
            "conv_id":    item["sk"].replace("CONV#", "", 1),
            "title":      item.get("title", ""),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "model":      item.get("model"),
            "model_label": models.get(item["model"])["label"] if item.get("model") else None,
        }
        for item in result.get("Items", [])
    ]

    convs.sort(key=lambda c: c.get("updated_at") or "", reverse=True)

    return json_response(200, convs)


# --------------------------------------------------------------------------------
# DELETE /conversations/{conv_id}
# --------------------------------------------------------------------------------

def delete_conversation(event):
    """Delete a conversation, its sandbox, query records and S3 objects."""
    user_id = get_user_id(event)
    conv_id = (event.get("pathParameters") or {}).get("conv_id")

    if not conv_id:
        return json_response(400, {"error": "conv_id is required"})

    pk = f"USER#{user_id}"

    # Terminate first: a conversation record deleted before its MicroVM would
    # leave the VM running with nothing pointing at it. Not awaited -- the
    # service finishes termination on its own, and this request is short.
    conv = table.get_item(Key={"pk": pk, "sk": f"CONV#{conv_id}"}).get("Item")
    if conv:
        sandbox.release(user_id, conv_id, conv)

    # Delete all QUERY# records for this conversation
    result = table.query(
        KeyConditionExpression=(
            Key("pk").eq(pk) &
            Key("sk").begins_with(f"QUERY#{conv_id}#")
        )
    )

    for item in result.get("Items", []):
        table.delete_item(Key={"pk": pk, "sk": item["sk"]})

    # Best-effort S3 cleanup of everything under the conversation, which
    # includes staged files that no single record points at.
    prefix = f"users/USER#{user_id}/conversations/CONV#{conv_id}/"
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=BACKEND_BUCKET, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=BACKEND_BUCKET, Delete={"Objects": keys})
    except Exception:
        pass

    # Delete the conversation record itself
    table.delete_item(Key={"pk": pk, "sk": f"CONV#{conv_id}"})

    return json_response(200, {"conv_id": conv_id, "deleted": True})


# --------------------------------------------------------------------------------
# POST /conversations/{conv_id}/queries
# --------------------------------------------------------------------------------

def submit_query(event):
    """
    Accept a user question, enforce token budget, write question to S3,
    create a pending QUERY record, and enqueue the SQS message.
    """
    user_id = get_user_id(event)
    conv_id = (event.get("pathParameters") or {}).get("conv_id")

    if not conv_id:
        return json_response(400, {"error": "conv_id is required"})

    body     = json.loads(event.get("body") or "{}")
    question = (body.get("question") or "").strip()

    if not question:
        return json_response(400, {"error": "question is required"})

    # Enforce token budget before accepting the query
    used, limit, over = _check_token_budget(user_id)
    if over:
        return json_response(429, {"error": "token_limit_reached"})

    # The first message locks the conversation's model: the picker's choice
    # if valid, else the default. Later messages cannot change it -- history
    # and the sandbox carry across messages, and one model per chat keeps
    # them coherent. The condition makes the lock race-free.
    requested = (body.get("model") or "").strip()
    chosen = requested if models.is_valid(requested) else models.DEFAULT
    try:
        table.update_item(
            Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"},
            UpdateExpression="SET #m = :m",
            ConditionExpression="attribute_exists(sk) AND attribute_not_exists(#m)",
            ExpressionAttributeNames={"#m": "model"},
            ExpressionAttributeValues={":m": chosen},
        )
        model_key = chosen
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        item = table.get_item(Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"}).get("Item")
        if not item:
            return json_response(404, {"error": "conversation not found"})
        model_key = item.get("model") or models.DEFAULT

    query_id = str(uuid.uuid4())
    now      = utc_now()
    prefix   = _s3_prefix(user_id, conv_id, query_id)

    # Write question text to S3
    question_key = f"{prefix}/question.txt"
    _write_s3(question_key, question)

    # Create the QUERY record in DynamoDB with pending status
    table.put_item(Item={
        "pk":               f"USER#{user_id}",
        "sk":               f"QUERY#{conv_id}#{query_id}",
        "conv_id":          conv_id,
        "query_id":         query_id,
        "question_s3_key":  question_key,
        "answer_s3_key":    None,
        "trace_s3_key":     None,
        "status":           "pending",
        "tokens_used":      0,
        "created_at":       now,
        "updated_at":       now,
    })

    # Update conversation title from first message and bump updated_at
    _maybe_set_conv_title(user_id, conv_id, question, now)

    # Enqueue worker
    sqs.send_message(
        QueueUrl=QUERY_QUEUE_URL,
        MessageBody=json.dumps({
            "user_id":  user_id,
            "conv_id":  conv_id,
            "query_id": query_id,
        }),
    )

    return json_response(200, {
        "query_id":    query_id,
        "conv_id":     conv_id,
        "status":      "pending",
        "model":       model_key,
        "model_label": models.get(model_key)["label"],
    })


def _maybe_set_conv_title(user_id, conv_id, question, now):
    """Set conversation title from the question if still the default."""
    pk = f"USER#{user_id}"
    sk = f"CONV#{conv_id}"

    item = table.get_item(Key={"pk": pk, "sk": sk}).get("Item", {})

    # Only overwrite the placeholder title set at creation time
    if item.get("title") == "New conversation":
        title = question
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET title = :t, updated_at = :u",
            ExpressionAttributeValues={":t": title, ":u": now},
        )
    else:
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET updated_at = :u",
            ExpressionAttributeValues={":u": now},
        )


# --------------------------------------------------------------------------------
# GET /conversations/{conv_id}/queries
# --------------------------------------------------------------------------------

def list_queries(event):
    """
    Return all queries for a conversation in chronological order,
    with question and answer text fetched from S3.
    """
    user_id = get_user_id(event)
    conv_id = (event.get("pathParameters") or {}).get("conv_id")

    if not conv_id:
        return json_response(400, {"error": "conv_id is required"})

    result = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"USER#{user_id}") &
            Key("sk").begins_with(f"QUERY#{conv_id}#")
        )
    )

    items = sorted(
        result.get("Items", []),
        key=lambda x: x.get("created_at") or "",
    )

    queries = []
    for item in items:
        q = _hydrate_query(item)
        queries.append(q)

    return json_response(200, queries)


# --------------------------------------------------------------------------------
# GET /conversations/{conv_id}/queries/{query_id}
# --------------------------------------------------------------------------------

def get_query(event):
    """Poll a single query for completion. Returns status + content when done."""
    user_id  = get_user_id(event)
    params   = event.get("pathParameters") or {}
    conv_id  = params.get("conv_id")
    query_id = params.get("query_id")

    if not conv_id or not query_id:
        return json_response(400, {"error": "conv_id and query_id are required"})

    item = table.get_item(
        Key={
            "pk": f"USER#{user_id}",
            "sk": f"QUERY#{conv_id}#{query_id}",
        }
    ).get("Item")

    if not item:
        return json_response(404, {"error": "query not found"})

    return json_response(200, _hydrate_query(item))


# --------------------------------------------------------------------------------
# Hydration helper — fetch S3 text for a query record
# --------------------------------------------------------------------------------

def _hydrate_query(item):
    """Resolve S3 pointers on a DynamoDB query item to inline text.

    `trace` is the agent's reasoning/tool-call trace. It is read while the
    query is still processing too, which is how the browser shows progress.
    `files` are the artifacts the model showed, as short-lived signed URLs.
    """
    question = None
    answer   = None
    trace    = None

    if item.get("question_s3_key"):
        try:
            question = _read_s3(item["question_s3_key"])
        except Exception:
            question = None

    if item.get("answer_s3_key"):
        try:
            answer = _read_s3(item["answer_s3_key"])
        except Exception:
            answer = None

    if item.get("trace_s3_key"):
        try:
            trace = json.loads(_read_s3(item["trace_s3_key"]))
        except Exception:
            trace = []

    files = []
    for artifact in item.get("artifacts") or []:
        mime = artifact.get("mime") or "application/octet-stream"
        params = {"Bucket": BACKEND_BUCKET, "Key": artifact["key"],
                  "ResponseContentType": mime}
        # Only raster images render inline. Anything else downloads, so a
        # generated HTML or SVG file is never executed by the browser.
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            name = str(artifact.get("name") or "file").replace('"', "")
            params["ResponseContentDisposition"] = f'attachment; filename="{name}"'
        try:
            url = s3_presign.generate_presigned_url(
                "get_object", ExpiresIn=FILE_URL_SECONDS, Params=params)
        except Exception:
            continue
        files.append({"name": artifact.get("name"), "mime": artifact.get("mime"),
                      "size": artifact.get("size"), "url": url})

    return {
        "query_id":    item.get("query_id") or item["sk"].split("#", 2)[-1],
        "conv_id":     item.get("conv_id"),
        "status":      item.get("status"),
        "question":    question,
        "answer":      answer,
        "trace":       trace,
        "files":       files,
        "tokens_used": int(item.get("tokens_used") or 0),
        "created_at":  item.get("created_at"),
        "updated_at":  item.get("updated_at"),
    }
