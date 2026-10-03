#!/usr/bin/env python3
"""Probe which Bedrock models can drive an agent, what they support, and how fast.

Shared between aws-openclaw and aws-deepseek-agent: everything below the
per-project block is identical in both copies, so a fix is a copy.

Why this exists
    bedrock-config.sh lists the models the project serves, and check_env.sh
    calls this script with --check on each of them before a deploy starts.
    The full probe answers the question you have when choosing what goes in
    that list: which models will this account serve today, in this region,
    what do they support, and what do they cost you in latency?

    Bedrock availability is not uniform. An inference profile can be ACTIVE,
    be listed by list-inference-profiles, and still return AccessDenied for
    this account -- access never granted, Marketplace subscription missing,
    or the model gated to specific customers. The only reliable test is to
    make the call, so this makes it, through the Converse API.

What it tests, per model
    1. Tool use. The model is asked the weather in Pune and handed a
       get_weather tool. Only models that call it can drive an agent, so only
       they are tested further and ranked. Three things can go wrong, and
       only the first is obvious:
         - access denied or retired: Converse returns AccessDenied;
         - no tool use at all: Converse rejects the toolConfig (DeepSeek R1
           answers a plain prompt in a third of a second and cannot run an
           agent at all);
         - the model accepts the tool and answers in prose anyway.
    2. Image input: a tiny PNG in the user message. Text-only models answer
       ValidationException.
    3. Prompt caching: a cachePoint after the system prompt. Models without
       caching answer AccessDenied for ANY request that carries one.
    Passing only clears a model to be tried; verify it in the app before
    relying on it.

Which ids it covers
    All three kinds a bedrock-config.sh entry can use, in one list:
      us         us.* cross-region inference profiles
      global     global.* inference profiles
      on-demand  bare foundation-model ids, for models offered without a
                 profile (deepseek.v3.2 is one)
    The id printed is exactly what goes in bedrock-config.sh.

Reading the output
    CSV: status,kind,model,tools,image,cache,details. Timings are the latency
    Bedrock itself reports, not the wall clock, and they RANK models against
    each other -- they are not a throughput measure. A short reply is mostly
    time to first token; --tokens 800 adds a timing call per tool-capable
    model, closer to the length a real turn generates. A "think" mark means
    the model spent reasoning tokens before answering; those count as output.

Usage
    python3 probe_bedrock.py                    # every text model, all kinds
    python3 probe_bedrock.py claude deepseek    # only ids matching a filter
    python3 probe_bedrock.py --kind on-demand   # one kind: us, global, on-demand
    python3 probe_bedrock.py --tokens 800       # add a timing call
    python3 probe_bedrock.py --region us-west-2
    python3 probe_bedrock.py --jobs 2           # fewer calls at a time
    python3 probe_bedrock.py --check us.anthropic.claude-sonnet-4-6
    python3 probe_bedrock.py --check deepseek.v3.2 --image false --caching false

    --check verifies one id and communicates through the exit code, so
    check_env.sh can gate a deploy on it. Without --image/--caching it checks
    tool use only. With them it also fails when a switch claims a capability
    the model does not have -- the combination that makes every request fail.
    A "false" switch for a model that does support it passes with a note.

Requirements
    The aws CLI with working credentials. Deliberately NO Python
    dependencies: boto3 would mean a venv just to run a pre-flight check.
    The cost is ~1s of CLI start-up per call, which is why timings use the
    latency Bedrock reports.
"""

import base64
import json
import re
import struct
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# Per-project settings -- the ONLY lines that differ between the copies
# ==============================================================================

# The region the project deploys to.
DEFAULT_REGION = "us-east-1"

# The fields of one BEDROCK_MODELS entry in this project's bedrock-config.sh,
# in order. Used to print ready-to-paste lines for the models that qualify.
#   aws-openclaw:        ("key", "model_id", "label")
#   aws-deepseek-agent:  ("key", "model_id", "label", "image", "caching")
PASTE_FIELDS = ("key", "model_id", "label", "image", "caching")

# ==============================================================================

KINDS = ("us", "global", "on-demand")

# --geo was the old flag (aws-openclaw); still accepted.
GEO_TO_KINDS = {"us": ("us",), "global": ("global",), "ondemand": ("on-demand",),
                "all": KINDS}

AWS_ERROR = re.compile(
    r"An error occurred \((\w+)\) when calling the \w+ operation: (.*)", re.S)

# The "AccessDeniedException: " that aws() prepends. It is the same class on
# almost every failing line, so it costs a column and distinguishes nothing.
ERROR_PREFIX = re.compile(r"^[A-Za-z]+(?:Exception|Error|Failure):\s*")

# Bedrock's AccessDenied message opens by restating the model id, which is
# already the field to its left ("anthropic.claude-opus-4-7 is not available
# for this account."). The token must contain a dot and start with a letter,
# so "This action doesn't support ..." and "3.5 is not a valid value." keep
# their subjects.
LEADING_ID = re.compile(r"^[a-z][\w\-]*\.[\w.\-:]+\s+is\s+", re.I)

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

def csv_field(text):
    """One CSV field: commas become semicolons rather than quoting, which
    parses the same and reads better on screen."""
    return str(text).replace(",", ";")


def first_sentence(text):
    """Reduce an AWS error to its first sentence, minus the exception name
    and the restated model id. Split on ". " -- ids are full of dots that
    never have a space after them."""
    text = " ".join(text.split())
    raw = AWS_ERROR.search(text)
    if raw:
        text = raw.group(2).strip()
    text = ERROR_PREFIX.sub("", text)
    text = LEADING_ID.sub("", text)
    cut = text.find(". ")
    if cut != -1:
        text = text[:cut + 1]
    return text if len(text) <= 120 else text[:119].rstrip() + "…"


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
        return False, "%s: %s" % (m.group(1), m.group(2).strip())
    return False, err.splitlines()[-1] if err else "exit %d" % out.returncode


def caller_account(region):
    """Checked up front so an expired SSO session reads as one clear error
    rather than every model failing with the same ExpiredToken."""
    ok, body = aws(["sts", "get-caller-identity"], region, timeout=30)
    if not ok:
        sys.exit("ERROR: aws CLI is not authenticated.\n  %s\n"
                 "  Run 'aws sso login' or export credentials first." % body)
    return body.get("Account", "?")


# ==============================================================================
# Discovery
# ==============================================================================

def text_models(region):
    """Foundation model id -> summary, for ACTIVE text-in, text-out models.

    Embedding, image and video models appear in the same listings but cannot
    serve Converse; filtering them saves a call each just to read
    ValidationException.
    """
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
        List of (model_id, catalog summary), sorted by kind then id.
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

    ids = sorted(found, key=lambda i: (KINDS.index(kind_of(i)), i))
    if filters:
        ids = [i for i in ids if any(f in i.lower() for f in filters)]
    return [(i, found[i]) for i in ids]


# ==============================================================================
# Probe
# ==============================================================================

def converse(region, model_id, text, system=None, tools=None, image=False,
             max_tokens=128):
    """One Converse call. No temperature: some models reject the field, and
    ranking compares models within one run either way."""
    content = [{"text": text}]
    if image:
        content.append({"image": {"format": "png", "source": {"bytes": PNG_B64}}})
    args = ["bedrock-runtime", "converse", "--model-id", model_id,
            "--messages", json.dumps([{"role": "user", "content": content}]),
            "--inference-config", json.dumps({"maxTokens": max_tokens})]
    if system:
        args += ["--system", json.dumps(system)]
    if tools:
        args += ["--tool-config", json.dumps(tools)]
    return aws(args, region)


def _timing(r, body):
    usage = body.get("usage", {})
    r["in_tok"] = usage.get("inputTokens")
    r["out_tok"] = usage.get("outputTokens")
    r["latency"] = (body.get("metrics", {}).get("latencyMs") or 0) / 1000.0
    content = body.get("output", {}).get("message", {}).get("content", [])
    r["think"] = any("reasoningContent" in c for c in content)
    return content


def probe(region, model_id, extras=True, timing_tokens=0):
    """Tool use; then, for a model that calls the tool, image input, prompt
    caching and (when timing_tokens) a longer timing call.

    Returns:
        Dict: ok, error, tool, image / caching (True, False, or None when
        another error got in the way, see image_note / caching_note),
        latency, in_tok, out_tok, think.
    """
    r = {"ok": False, "error": None, "tool": False, "image": None,
         "caching": None, "image_note": "", "caching_note": "",
         "latency": None, "in_tok": None, "out_tok": None, "think": False}

    ok, body = converse(region, model_id, "What is the weather in Pune?",
                        tools=TOOL_PROBE)
    if not ok:
        r["error"] = first_sentence(body)
        return r
    r["ok"] = True
    content = _timing(r, body)
    # Accepting the tool schema is not the same as using it.
    r["tool"] = (body.get("stopReason") == "tool_use"
                 or any("toolUse" in c for c in content))
    if not r["tool"]:
        return r

    if extras:
        ok, body = converse(region, model_id, "What colour is this pixel? One word.",
                            image=True, max_tokens=16)
        if ok:
            r["image"] = True
        elif "image" in body.lower():
            r["image"] = False
        else:
            r["image_note"] = first_sentence(body)

        ok, body = converse(region, model_id, "Reply with OK.",
                            system=[{"text": "You are terse."},
                                    {"cachePoint": {"type": "default"}}],
                            max_tokens=16)
        if ok:
            r["caching"] = True
        elif "caching" in body.lower():
            r["caching"] = False
        else:
            r["caching_note"] = first_sentence(body)

    if timing_tokens:
        # A paragraph it will keep writing, so tokens per second means
        # something. Separate from the tool test: a prompt that needs no
        # tool would make every model look as if it ignored the tool.
        ok, body = converse(region, model_id,
                            "Write a short paragraph explaining what a resume "
                            "is, in plain language.", max_tokens=timing_tokens)
        if ok:
            _timing(r, body)
    return r


def flag(value):
    return {True: "yes", False: "no", None: "?"}[value]


def paste_line(model_id, summary, r):
    """A BEDROCK_MODELS entry in this project's format (PASTE_FIELDS)."""
    base = model_id.partition(".")[2] if kind_of(model_id) != "on-demand" else model_id
    # Drop the provider ("anthropic.") unless what is left is only a version
    # ("deepseek.v3.2" -> "v3.2" says nothing on its own).
    rest = base.split(".", 1)[-1]
    name = base if re.match(r"v?\d", rest) else rest
    key = re.sub(r"[^a-z0-9]+", "-", name.lower())
    key = re.sub(r"-v\d+(-\d+)?$|-\d{8}.*$", "", key).strip("-")
    values = {"key": key, "model_id": model_id,
              "label": summary.get("modelName") or base,
              "image": "true" if r["image"] else "false",
              "caching": "true" if r["caching"] else "false"}
    return '  "%s"' % "|".join(values[f] for f in PASTE_FIELDS)


# ==============================================================================
# Main
# ==============================================================================

def take_value(args, name, cast, example):
    if name not in args:
        return None
    i = args.index(name)
    try:
        value = cast(args[i + 1])
    except (IndexError, ValueError):
        sys.exit("ERROR: %s needs a value, e.g. %s %s" % (name, name, example))
    del args[i:i + 2]
    return value


def as_bool(text):
    if text.lower() not in ("true", "false"):
        raise ValueError(text)
    return text.lower() == "true"


def check(region, model_id, want_image, want_caching):
    """--check: exit 0 if the model can serve the project as configured."""
    extras = want_image is not None or want_caching is not None
    r = probe(region, model_id, extras=extras)
    if not r["ok"]:
        print("FAIL: %s in %s -- %s" % (model_id, region, r["error"]))
        return 1
    if not r["tool"]:
        print("FAIL: %s in %s -- answered without calling a tool" % (model_id, region))
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
    extra = (", image input %s, caching %s" % (flag(r["image"]), flag(r["caching"]))
             if extras else "")
    print("OK: %s called the tool in %s (%.2fs)%s%s"
          % (model_id, region, r["latency"], extra,
             (" (note: %s)" % "; ".join(notes)) if notes else ""))
    return 0


def main():
    args = sys.argv[1:]
    region = take_value(args, "--region", str, "us-west-2") or DEFAULT_REGION
    kind = take_value(args, "--kind", str, "on-demand")
    geo = take_value(args, "--geo", str, "all")
    if kind and kind not in KINDS:
        sys.exit("ERROR: --kind must be one of %s" % ", ".join(KINDS))
    if geo and geo not in GEO_TO_KINDS:
        sys.exit("ERROR: --geo must be one of %s" % ", ".join(GEO_TO_KINDS))
    kinds = (kind,) if kind else GEO_TO_KINDS[geo] if geo else KINDS
    jobs = take_value(args, "--jobs", int, "2") or 6
    timing_tokens = take_value(args, "--tokens", int, "800") or 0
    want_image = take_value(args, "--image", as_bool, "false")
    want_caching = take_value(args, "--caching", as_bool, "false")

    account = caller_account(region)

    if len(args) >= 2 and args[0] == "--check":
        return check(region, args[1], want_image, want_caching)

    filters = [a.lower() for a in args]
    print("account : %s" % account)
    print("region  : %s" % region)
    print("kinds   : %s" % ", ".join(kinds))
    print("timing  : %s" % ("%d-token call per tool-capable model" % timing_tokens
                            if timing_tokens else "tool call latency (--tokens 800 for more)"))
    print("filters : %s\n" % (filters or "(none -- every text model)"))

    targets = discover(region, kinds, filters)
    if not targets:
        print("No text models matched.")
        return 1
    print("%d model id(s) to probe, %d at a time\n" % (len(targets), jobs))
    print("status,kind,model,tools,image,cache,details")

    # Concurrency does not skew the ranking: latency is measured by Bedrock
    # on its side of the wire. Drop --jobs if the account starts throttling.
    summaries = dict(targets)
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(probe, region, mid, True, timing_tokens): mid
                   for mid, _ in targets}
        for fut in as_completed(futures):
            mid = futures[fut]
            r = results[mid] = fut.result()
            if not r["ok"]:
                status, detail = "FAIL", r["error"]
            elif not r["tool"]:
                status, detail = "FAIL", "answered without calling a tool"
            else:
                status = "OK"
                detail = "%.2fs in %s out %s%s" % (
                    r["latency"], r["in_tok"], r["out_tok"],
                    " think" if r["think"] else "")
                notes = "; ".join(n for n in (r["image_note"], r["caching_note"]) if n)
                if notes:
                    detail += " (" + notes + ")"
            print("%s,%s,%s,%s,%s,%s,%s" % (
                status, kind_of(mid), mid,
                flag(r["tool"]) if r["ok"] else "-",
                flag(r["image"]) if r["tool"] else "-",
                flag(r["caching"]) if r["tool"] else "-",
                csv_field(detail)))

    # ==========================================================================
    # Result -- fastest first, then ready-to-paste config lines
    # ==========================================================================
    working = sorted(((m, r) for m, r in results.items() if r["ok"] and r["tool"]),
                     key=lambda pair: pair[1]["latency"])
    print()
    if not working:
        print("No model called the tool in %s." % region)
        print("Check model access: "
              "https://console.aws.amazon.com/bedrock/home#/modelaccess")
        return 1

    print("Called the tool in %s (%d of %d, fastest first):"
          % (region, len(working), len(targets)))
    for mid, r in working:
        out_tok = r["out_tok"] or 0
        rate = ("  %6.1f tok/s" % (out_tok / r["latency"])
                if out_tok and r["latency"] else "")
        print("  %7.2fs  %-9s %-48s%s" % (r["latency"], kind_of(mid), mid, rate))

    print()
    print("As BEDROCK_MODELS entries for bedrock-config.sh (pick your own key "
          "and label; check_env.sh probes every entry before each deploy):")
    # Alphabetical here, not fastest-first: this is the list you scan for a
    # model by name.
    for mid, r in sorted(working, key=lambda pair: pair[0].lower()):
        print(paste_line(mid, summaries[mid], r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
