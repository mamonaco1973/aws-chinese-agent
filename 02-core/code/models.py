"""The models a user can pick, as configured in bedrock-config.sh.

apply.sh turns BEDROCK_MODELS into JSON and Terraform hands it to both Lambdas
as MODELS_JSON, with the default key as DEFAULT_MODEL. The API uses this to
validate the picker's choice and to serve GET /models; the worker uses it to
resolve a conversation's key to a model id and its capabilities.
"""

import json
import os

_MODELS = {m["key"]: m for m in json.loads(os.environ.get("MODELS_JSON") or "[]")}
DEFAULT = os.environ.get("DEFAULT_MODEL") or next(iter(_MODELS), "")


def is_valid(key):
    return key in _MODELS


def get(key):
    """The model for `key`, or the default for a missing or retired key.

    A key that is no longer configured (removed from bedrock-config.sh after
    the conversation started) falls back rather than failing the query.

    Returns:
        Dict with key, model_id, label, image_input, prompt_caching.
    """
    return _MODELS.get(key) or _MODELS[DEFAULT]


def public():
    """What the picker needs: keys, labels and the default, nothing more."""
    return {
        "default": DEFAULT,
        "models": [{"key": m["key"], "label": m["label"]} for m in _MODELS.values()],
    }
