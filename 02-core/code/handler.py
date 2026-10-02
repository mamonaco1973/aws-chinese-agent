# ================================================================================
# Lambda API Router
#
# Dispatches API Gateway requests to the appropriate handler functions.
#
# Routes
#   POST /register
#   GET  /usage
#   POST /conversations
#   GET  /conversations
#   DELETE /conversations/{conv_id}
#   POST /conversations/{conv_id}/queries
#   GET  /conversations/{conv_id}/queries
#   GET  /conversations/{conv_id}/queries/{query_id}
# ================================================================================

import json
import logging

from conversations import (
    create_conversation,
    list_conversations,
    delete_conversation,
    submit_query,
    list_queries,
    get_query,
)
from users import register_user, get_usage

# --------------------------------------------------------------------------------
# Configure logger
# --------------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------------
# Lambda entry point
# --------------------------------------------------------------------------------

def lambda_handler(event, context):

    method = event["requestContext"]["http"]["method"]
    path   = event["rawPath"]

    logger.info("API request: %s %s", method, path)

    try:
        # --------------------------------------------------------------------
        # User registration and token usage
        # --------------------------------------------------------------------

        if method == "POST" and path == "/register":
            return register_user(event)

        if method == "GET" and path == "/usage":
            return get_usage(event)

        # --------------------------------------------------------------------
        # Conversation collection
        # --------------------------------------------------------------------

        if method == "POST" and path == "/conversations":
            return create_conversation(event)

        if method == "GET" and path == "/conversations":
            return list_conversations(event)

        # --------------------------------------------------------------------
        # Individual conversation
        # --------------------------------------------------------------------

        if method == "DELETE" and path.startswith("/conversations/"):
            parts = path.split("/")
            # /conversations/{conv_id}
            if len(parts) == 3:
                return delete_conversation(event)

        # --------------------------------------------------------------------
        # Query routes within a conversation
        # --------------------------------------------------------------------

        if path.startswith("/conversations/"):
            parts = path.split("/")

            # /conversations/{conv_id}/queries
            if len(parts) == 4 and parts[3] == "queries":
                if method == "POST":
                    return submit_query(event)
                if method == "GET":
                    return list_queries(event)

            # /conversations/{conv_id}/queries/{query_id}
            if len(parts) == 5 and parts[3] == "queries":
                if method == "GET":
                    return get_query(event)

        # --------------------------------------------------------------------
        # Default
        # --------------------------------------------------------------------

        return {
            "statusCode": 404,
            "body": json.dumps({"error": "not found"}),
        }

    except Exception:
        logger.exception("Unhandled exception")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "internal server error"}),
        }
