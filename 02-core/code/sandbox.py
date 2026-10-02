# ================================================================================
# sandbox.py
#
# One Lambda MicroVM per conversation, launched on demand.
#
# Nothing launches when a conversation is created. The first time the model
# calls a sandbox tool, ensure() launches a MicroVM from the pre-initialized
# image and records it on the CONV# item; every later tool call in that
# conversation reuses it. An idle VM auto-suspends and costs nothing for
# compute; the next request wakes it with its Python state intact. A VM past
# its 8-hour lifetime is replaced transparently, and the caller is told so the
# model knows earlier variables are gone.
#
# The Lambda runtime's bundled boto3 predates lambda-microvms. A current boto3
# comes from the layer apply.sh builds (dist/boto3-layer.zip).
#
# Record on pk=USER#<id>, sk=CONV#<id>:
#   sandbox_id, sandbox_endpoint, sandbox_image
# ================================================================================

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()

REGION = os.environ["AWS_REGION"]
IMAGE_ARN = os.environ["SANDBOX_IMAGE_ARN"]
IMAGE_VERSION = os.environ["SANDBOX_IMAGE_VERSION"]
SANDBOX_ROLE_ARN = os.environ["SANDBOX_ROLE_ARN"]

APP_PORT = 8080

# Well above the ~10 min/GB break-even for a suspend/resume round trip, so a
# pause while reading an answer does not pay for a snapshot write and read.
IDLE_SUSPEND_SECONDS = 1800

# The service maximum, and the suspended TTL equals it: a suspended sandbox
# stays resumable for as long as it is allowed to exist at all.
MAX_LIFETIME_SECONDS = 28800
SUSPENDED_TTL_SECONDS = 28800

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

_client = None

# Endpoint auth tokens, cached per warm container. Never persisted.
_tokens = {}


def client():
    """Return a lambda-microvms client tuned for short control-plane calls."""
    global _client
    if _client is None:
        _client = boto3.client("lambda-microvms", region_name=REGION, config=Config(
            connect_timeout=3, read_timeout=15,
            retries={"mode": "standard", "max_attempts": 3}))
    return _client


class SandboxError(Exception):
    """A sandbox request failed in a way the model should be told about."""


# ================================================================================
# Lifecycle
# ================================================================================

def state_of(vm_id):
    """Return a MicroVM's lifecycle state, treating a missing VM as TERMINATED."""
    try:
        return client().get_microvm(microvmIdentifier=vm_id)["state"]
    except client().exceptions.ResourceNotFoundException:
        return "TERMINATED"


def gone(state):
    """True for any state a request can no longer wake."""
    return "TERMINAT" in state or "FAIL" in state


def _wait_running(vm_id, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        state = state_of(vm_id)
        if state == "RUNNING":
            return
        if gone(state):
            raise SandboxError(f"Sandbox reached {state} while starting")
        time.sleep(0.25)
    raise SandboxError(f"Sandbox did not start within {timeout}s")


def _conv_key(user_id, conv_id):
    return {"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"}


def _launch():
    """Run a fresh MicroVM and return {"id", "endpoint"} once it is RUNNING."""
    response = client().run_microvm(
        imageIdentifier=IMAGE_ARN,
        imageVersion=IMAGE_VERSION,
        clientToken=str(uuid.uuid4()),
        runHookPayload=json.dumps({"purpose": "agent-sandbox"}),
        # Required, and confirmed the hard way in aws-lambda-microvms: the
        # caller also needs iam:PassRole on this role (see iam.tf).
        executionRoleArn=SANDBOX_ROLE_ARN,
        egressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"],
        ingressNetworkConnectors=[
            f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"],
        idlePolicy={"autoResumeEnabled": True,
                    "maxIdleDurationSeconds": IDLE_SUSPEND_SECONDS,
                    "suspendedDurationSeconds": SUSPENDED_TTL_SECONDS},
        maximumDurationInSeconds=MAX_LIFETIME_SECONDS,
        logging={"disabled": {}})  # Guest logs would carry submitted code.
    session = {"id": response["microvmId"], "endpoint": response["endpoint"]}
    try:
        _wait_running(session["id"])
    except SandboxError:
        terminate(session["id"])
        raise
    return session


def ensure(user_id, conv_id):
    """Return this conversation's sandbox, launching one if there is none.

    Returns:
        (session, event) where event is None when an existing sandbox was
        reused, or a dict describing a launch for the trace:
        {"launched": id, "ms": n, "replaced": bool}.
    """
    key = _conv_key(user_id, conv_id)
    item = table.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    old_id = item.get("sandbox_id")

    # A different image means a redeploy; the old VM is not this code.
    if old_id and item.get("sandbox_image") == IMAGE_ARN:
        state = state_of(old_id)
        if not gone(state):
            return {"id": old_id, "endpoint": item["sandbox_endpoint"],
                    "state": state}, None

    started = time.perf_counter()
    session = _launch()
    try:
        # Conditional, so two concurrent queries in one conversation cannot
        # each launch a VM and leave one orphaned and billing.
        table.update_item(
            Key=key,
            UpdateExpression="SET sandbox_id = :n, sandbox_endpoint = :e, sandbox_image = :i",
            # attribute_exists(pk): never resurrect a conversation deleted
            # while this launch was in flight.
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(sandbox_id) OR sandbox_id = :o)"),
            ExpressionAttributeValues={":n": session["id"], ":e": session["endpoint"],
                                       ":i": IMAGE_ARN, ":o": old_id or "-"})
    except ClientError as exc:
        # Either way this VM is not recorded anywhere, so it must not live on.
        terminate(session["id"])
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        if not table.get_item(Key=key, ConsistentRead=True).get("Item"):
            raise SandboxError("The conversation was deleted") from None
        # Another query won the race; use its sandbox instead.
        return ensure(user_id, conv_id)
    session["state"] = "RUNNING"
    return session, {"launched": session["id"], "replaced": bool(old_id),
                     "ms": round((time.perf_counter() - started) * 1000)}


def terminate(vm_id):
    """Terminate a MicroVM without waiting. Best effort: a VM already gone is fine."""
    try:
        client().terminate_microvm(microvmIdentifier=vm_id)
    except Exception:
        logger.exception("terminate_microvm failed for %s", vm_id)


def release(user_id, conv_id, item=None):
    """Terminate a conversation's sandbox, if it has one. Used on delete."""
    if item is None:
        item = table.get_item(Key=_conv_key(user_id, conv_id)).get("Item") or {}
    if item.get("sandbox_id"):
        terminate(item["sandbox_id"])


# ================================================================================
# Talking to the VM
# ================================================================================

def _token(vm_id):
    """Mint, and cache, an endpoint token scoped to the application port only."""
    cached = _tokens.get(vm_id)
    if not cached or cached[1] < time.time():
        result = client().create_microvm_auth_token(
            microvmIdentifier=vm_id, expirationInMinutes=30,
            allowedPorts=[{"port": APP_PORT}])
        cached = (result["authToken"]["X-aws-proxy-auth"], time.time() + 25 * 60)
        _tokens[vm_id] = cached
    return cached[0]


def _request(session, path, body=None, raw=False):
    """Send an authenticated request to the VM's own HTTPS endpoint.

    Any request auto-resumes a suspended VM. Lambda answers 502/503/504 while
    the snapshot is still being restored, so those are retried rather than
    reported as failures.

    Returns:
        Parsed JSON, or (bytes, headers) when raw is True.
    """
    # The service returns a bare host name; an explicit scheme is kept so a
    # local test can point this at http://localhost.
    endpoint = session["endpoint"]
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    headers = {"X-aws-proxy-auth": _token(session["id"]),
               "X-aws-proxy-port": str(APP_PORT)}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    last = None
    for attempt in range(10):
        request = urllib.request.Request(endpoint + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if raw:
                    return response.read(), response.headers
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 503, 504):
                last = f"HTTP {exc.code}"
                time.sleep(min(1 + attempt, 4))
                continue
            try:
                message = json.load(exc).get("error", "")
            except ValueError:
                message = ""
            raise SandboxError(message or f"Sandbox returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last = str(exc)
            time.sleep(min(1 + attempt, 4))
    raise SandboxError(f"Sandbox unreachable ({last})")


def submit(session, code, kernel="python"):
    """Hand a cell to one of the VM's sessions, "python" or "bash".

    Returns:
        {"job": id} or {"state": "refused", "error": ...}.
    """
    return _request(session, "/execute", {"code": code, "kernel": kernel})


def result(session, job):
    """Poll a job: {"state": "running"|"done"|"unknown", ...}."""
    return _request(session, f"/result/{urllib.parse.quote(job)}")


def wait(session, job, deadline):
    """Poll a job until it finishes or `deadline` (time.monotonic) passes.

    Returns:
        The last /result response. state "running" means the deadline hit.
    """
    delay = 0.3
    while True:
        status = result(session, job)
        if status.get("state") != "running" or time.monotonic() + delay >= deadline:
            return status
        time.sleep(delay)
        delay = min(delay * 1.5, 3.0)


def fetch_file(session, path):
    """Read one file out of the sandbox. Returns (bytes, declared_mime, name)."""
    body, headers = _request(session, "/file?" + urllib.parse.urlencode({"path": path}),
                             raw=True)
    return (body, headers.get("Content-Type", "application/octet-stream"),
            headers.get("X-Sandbox-File-Name") or os.path.basename(path) or "file")
