#!/usr/bin/env python3
"""Probe which Bedrock models can drive this agent, and what they support.

Why this exists
    bedrock-config.sh lists the models a user can pick, each with two switches
    the worker obeys: image input (show_file returns the PNG to the model) and
    prompt caching (cachePoint blocks). Get a switch wrong in the "true"
    direction and every request to that model fails -- Converse rejects an
    image block or a cachePoint outright. The catalog does not say which
    models support either, and an ACTIVE model can still be denied to this
    account, so the only reliable test is to make the calls.

What it tests, per model
    1. Tool use. The model is asked the weather in Pune with a get_weather
       tool; only a model that calls it can drive the sandbox. Models that
       fail here are not tested further.
    2. Image input. A tiny PNG in the user message.
    3. Prompt caching. A cachePoint after the system prompt.

Which ids it covers
    All three kinds a BEDROCK_MODELS entry can use, in one list:
      us         us.* cross-region inference profiles
      global     global.* inference profiles
      on-demand  bare foundation-model ids, for models offered without a
                 profile (deepseek.v3.2 is one)
    The id printed is exactly what goes in bedrock-config.sh.

Usage
    python3 probe_bedrock.py                    # every text model, all kinds
    python3 probe_bedrock.py claude deepseek    # only ids matching a filter
    python3 probe_bedrock.py --kind on-demand   # one kind: us, global, on-demand
    python3 probe_bedrock.py --region us-west-2
    python3 probe_bedrock.py --jobs 2           # fewer calls at a time
    python3 probe_bedrock.py --check deepseek.v3.2 --image false --caching false

    --check verifies one id and communicates through the exit code, so
    check_env.sh can gate a deploy on it. With --image / --caching it also
    fails when a switch claims a capability the model does not have -- the
    combination that would break the app at runtime. A switch that is "false"
    for a model that does support it passes with a note: that only gives up a
    feature.

Requirements
    The aws CLI with working credentials. Deliberately NO Python
    dependencies: boto3 would mean a venv just to run a pre-flight check.
"""

import base64
import json
import re
import struct
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

# The region the project deploys to (apply.sh: us-east-1).
DEFAULT_REGION = "us-east-1"

KINDS = ("us", "global", "on-demand")

AWS_ERROR = re.compile(
    r"An error occurred \((\w+)\) when calling the \w+ operation: (.*)", re.S)

TOOL_PROBE = {"tools": [{"toolSpec": {
    "name": "get_weather",
    "description": "Get the weather for a city",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }},
}}]}


def _tiny_png():
    """A valid 1x1 PNG, built here so the probe needs no image file."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


# The CLI takes blob fields inside JSON input as base64 text.
PNG_B64 = base64.b64encode(_tiny_png()).decode()


# ==============================================================================
# aws CLI plumbing
# ==============================================================================

def aws(args, region, timeout=180):
    """Run an aws CLI command and return (ok, parsed_json_or_error_text)."""
    cmd = ["aws"] + args + ["--region", region, "--output", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        sys.exit("ERROR: aws CLI not found in PATH.")
    except subprocess.TimeoutExpired:
        return False, "Timeout: no answer in %ds" % timeout
    if out.returncode == 0:
        try:
            return True, json.loads(out.stdout or "{}")
        except ValueError:
            return False, "BadJSON: %s" % out.stdout.strip()[:60]
    err = out.stderr.strip()
    m = AWS_ERROR.search(err)
    if m:
        return False, "%s: %s" % (m.group(1), " ".join(m.group(2).split()))
    return False, err.splitlines()[-1] if err else "exit %d" % out.returncode


def short(text):
    """First sentence of an AWS error, without the exception name."""
    text = re.sub(r"^[A-Za-z]+(?:Exception|Error):\s*", "", text)
    cut = text.find(". ")
    text = text[:cut + 1] if cut != -1 else text
    return text if len(text) <= 100 else text[:99].rstrip() + "…"


def caller_account(region):
    ok, body = aws(["sts", "get-caller-identity"], region, timeout=30)
    if not ok:
        sys.exit("ERROR: aws CLI is not authenticated.\n  %s\n"
                 "  Run 'aws sso login' or export credentials first." % body)
    return body.get("Account", "?")


# ==============================================================================
# Discovery
# ==============================================================================

def text_models(region):
    """Foundation model id -> summary, for ACTIVE text-in, text-out models."""
    ok, body = aws(["bedrock", "list-foundation-models"], region)
    if not ok:
        return {}
    return {m["modelId"]: m for m in body.get("modelSummaries", [])
            if "TEXT" in m.get("outputModalities", [])
            and "TEXT" in m.get("inputModalities", [])
            and m.get("modelLifecycle", {}).get("status") == "ACTIVE"}


def kind_of(model_id):
    prefix = model_id.split(".", 1)[0]
    return prefix if prefix in ("us", "global") else "on-demand"


def discover(region, kinds, filters):
    """Every invocable id of the requested kinds, with its catalog entry.

    Returns:
        List of (model_id, catalog summary) sorted by id.
    """
    models = text_models(region)
    found = {}

    if "us" in kinds or "global" in kinds:
        ok, body = aws(["bedrock", "list-inference-profiles",
                        "--type-equals", "SYSTEM_DEFINED"], region)
        if not ok:
            print("WARNING: list-inference-profiles failed -- %s" % body)
            body = {}
        for p in body.get("inferenceProfileSummaries", []):
            pid = p.get("inferenceProfileId", "")
            base = pid.partition(".")[2]
            if kind_of(pid) in kinds and p.get("status") == "ACTIVE" \
                    and base in models:
                found[pid] = models[base]

    if "on-demand" in kinds:
        for mid, m in models.items():
            # Provisioned-only variants (":N:Mk" suffixes) need purchased
            # throughput; only ON_DEMAND ids can be called as they are.
            if "ON_DEMAND" in m.get("inferenceTypesSupported", []):
                found[mid] = m

    ids = sorted(found)
    if filters:
        ids = [i for i in ids if any(f in i.lower() for f in filters)]
    return [(i, found[i]) for i in ids]


# ==============================================================================
# Probe
# ==============================================================================

def converse(region, model_id, messages, system=None, tools=None, max_tokens=128):
    args = ["bedrock-runtime", "converse", "--model-id", model_id,
            "--messages", json.dumps(messages),
            "--inference-config", json.dumps({"maxTokens": max_tokens})]
    if system:
        args += ["--system", json.dumps(system)]
    if tools:
        args += ["--tool-config", json.dumps(tools)]
    return aws(args, region)


def probe(region, model_id):
    """Tool use, then (only if that passes) image input and prompt caching.

    Returns:
        Dict: ok, error, tool (bool), latency (s), image / caching
        (True, False, or None when the result was some other error).
    """
    out = {"ok": False, "error": None, "tool": False, "latency": None,
           "image": None, "caching": None, "image_note": "", "caching_note": ""}

    ok, body = converse(region, model_id,
                        [{"role": "user", "content": [{"text": "What is the weather in Pune?"}]}],
                        tools=TOOL_PROBE)
    if not ok:
        out["error"] = short(body)
        return out
    out["ok"] = True
    out["latency"] = (body.get("metrics", {}).get("latencyMs") or 0) / 1000.0
    content = body.get("output", {}).get("message", {}).get("content", [])
    out["tool"] = (body.get("stopReason") == "tool_use"
                   or any("toolUse" in c for c in content))
    if not out["tool"]:
        return out

    # Image input: Converse answers ValidationException ("doesn't support the
    # image content block") for a text-only model.
    ok, body = converse(region, model_id, [{"role": "user", "content": [
        {"text": "What colour is this pixel? One word."},
        {"image": {"format": "png", "source": {"bytes": PNG_B64}}}]}],
        max_tokens=16)
    if ok:
        out["image"] = True
    elif "image" in body.lower():
        out["image"] = False
    else:
        out["image_note"] = short(body)

    # Prompt caching: an unsupported model answers AccessDenied mentioning
    # prompt caching for ANY request carrying a cachePoint.
    ok, body = converse(region, model_id,
                        [{"role": "user", "content": [{"text": "Reply with OK."}]}],
                        system=[{"text": "You are terse."}, {"cachePoint": {"type": "default"}}],
                        max_tokens=16)
    if ok:
        out["caching"] = True
    elif "caching" in body.lower():
        out["caching"] = False
    else:
        out["caching_note"] = short(body)
    return out


def flag(value):
    return {True: "yes", False: "no", None: "?"}[value]


def suggest_line(model_id, summary, r):
    """A BEDROCK_MODELS entry for this model, ready to paste."""
    base = model_id.partition(".")[2] if kind_of(model_id) != "on-demand" else model_id
    # Drop the provider ("anthropic.") unless what is left is only a version
    # ("deepseek.v3.2" -> "v3.2" says nothing on its own).
    rest = base.split(".", 1)[-1]
    name = base if re.match(r"v?\d", rest) else rest
    key = re.sub(r"[^a-z0-9]+", "-", name.lower())
    key = re.sub(r"-v\d+(-\d+)?$|-\d{8}.*$", "", key).strip("-")
    label = summary.get("modelName") or base
    return '  "%s|%s|%s|%s|%s"' % (key, model_id, label,
                                   "true" if r["image"] else "false",
                                   "true" if r["caching"] else "false")


# ==============================================================================
# Main
# ==============================================================================

def take_value(args, flag_name, cast, example):
    if flag_name not in args:
        return None
    i = args.index(flag_name)
    try:
        value = cast(args[i + 1])
    except (IndexError, ValueError):
        sys.exit("ERROR: %s needs a value, e.g. %s %s" % (flag_name, flag_name, example))
    del args[i:i + 2]
    return value


def as_bool(text):
    if text.lower() not in ("true", "false"):
        raise ValueError(text)
    return text.lower() == "true"


def check(region, model_id, want_image, want_caching):
    """--check: exit 0 if the model can serve this app as configured."""
    r = probe(region, model_id)
    if not r["ok"]:
        print("FAIL: %s -- %s" % (model_id, r["error"]))
        return 1
    if not r["tool"]:
        print("FAIL: %s answered without calling the tool; it cannot drive "
              "the sandbox." % model_id)
        return 1
    problems, notes = [], []
    for name, want, got in (("image input", want_image, r["image"]),
                            ("prompt caching", want_caching, r["caching"])):
        if want is None:
            continue
        if want and got is False:
            problems.append("%s is true in bedrock-config.sh but the model "
                            "rejects it -- every request would fail" % name)
        elif not want and got is True:
            notes.append("%s is supported but switched off" % name)
    if problems:
        print("FAIL: %s -- %s" % (model_id, "; ".join(problems)))
        return 1
    print("OK: %s calls tools (%.2fs), image input %s, caching %s%s"
          % (model_id, r["latency"], flag(r["image"]), flag(r["caching"]),
             (" (note: %s)" % "; ".join(notes)) if notes else ""))
    return 0


def main():
    args = sys.argv[1:]
    region = take_value(args, "--region", str, "us-west-2") or DEFAULT_REGION
    kind = take_value(args, "--kind", str, "on-demand")
    if kind and kind not in KINDS:
        sys.exit("ERROR: --kind must be one of %s" % ", ".join(KINDS))
    jobs = take_value(args, "--jobs", int, "2") or 6
    want_image = take_value(args, "--image", as_bool, "false")
    want_caching = take_value(args, "--caching", as_bool, "false")

    account = caller_account(region)

    if len(args) >= 2 and args[0] == "--check":
        return check(region, args[1], want_image, want_caching)

    filters = [a.lower() for a in args]
    kinds = (kind,) if kind else KINDS
    print("account : %s" % account)
    print("region  : %s" % region)
    print("kinds   : %s" % ", ".join(kinds))
    print("filters : %s\n" % (filters or "(none -- every text model)"))

    targets = discover(region, kinds, filters)
    if not targets:
        print("No text models matched.")
        return 1
    print("%d model id(s) to probe, %d at a time. Tool-capable ones get two "
          "more calls (image, caching).\n" % (len(targets), jobs))
    print("%-5s %-9s %-52s %-5s %-6s %-6s %s"
          % ("", "KIND", "MODEL ID", "TOOLS", "IMAGE", "CACHE", "DETAIL"))

    summaries = dict(targets)
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(probe, region, mid): mid for mid, _ in targets}
        for fut in as_completed(futures):
            mid = futures[fut]
            r = results[mid] = fut.result()
            if not r["ok"]:
                status, detail = "NO", r["error"]
            elif not r["tool"]:
                status, detail = "NO", "answered without calling the tool"
            else:
                status = "OK"
                detail = "%.2fs" % r["latency"]
                extra = "; ".join(n for n in (r["image_note"], r["caching_note"]) if n)
                if extra:
                    detail += "  " + extra
            print("%-5s %-9s %-52s %-5s %-6s %-6s %s"
                  % (status, kind_of(mid), mid,
                     flag(r["tool"]) if r["ok"] else "-",
                     flag(r["image"]) if r["tool"] else "-",
                     flag(r["caching"]) if r["tool"] else "-", detail))

    usable = sorted((m for m, r in results.items() if r["ok"] and r["tool"]),
                    key=lambda m: (KINDS.index(kind_of(m)), m))
    print()
    if not usable:
        print("No model called the tool in %s." % region)
        return 1
    print("Usable here (%d of %d). As BEDROCK_MODELS entries -- pick a key and "
          "label you like; the last two fields are measured:\n"
          % (len(usable), len(targets)))
    for m in usable:
        print(suggest_line(m, summaries[m], results[m]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
