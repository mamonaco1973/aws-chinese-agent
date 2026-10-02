# ================================================================================
# memory.py
#
# What the model knows when a new message starts. Two sources:
#
#   1. The sandbox. The MicroVM holds the real state -- files, Python names,
#      the shell's cwd -- but the model only knows what it is told. At the end
#      of every message that used the sandbox, capture_inventory() runs two
#      read-only cells (one per session) while the VM is still awake, and the
#      summary is stored on the CONV# item. state_block() hands it to the model
#      at the start of the next message. It never touches the VM itself, so a
#      suspended sandbox stays suspended until the model actually needs it.
#
#   2. The conversation. Each message's complete Converse exchange -- tool
#      calls and results, not just the final answer -- is saved as
#      messages.json and replayed on later messages, newest first, within a
#      size budget. Images become placeholders and long outputs are clipped
#      on save, so replay stays cheap; Bedrock prompt caching (cache_points)
#      makes re-sending the same prefix on every tool turn cheaper still.
#
# CONV# attributes written here: sandbox_inventory, sandbox_inventory_vm
# ================================================================================

import copy
import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

import sandbox

logger = logging.getLogger()

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3")
BACKEND_BUCKET = os.environ["BACKEND_BUCKET_NAME"]

# Earlier messages replayed, newest first, until either limit is reached.
HISTORY_WINDOW = 5
HISTORY_CHAR_BUDGET = 60_000

# Clipping applied when an exchange is saved. The model saw the full text at
# the time; later it only needs to know roughly what happened.
SAVED_RESULT_LIMIT = 4_000
SAVED_CODE_LIMIT = 8_000

INVENTORY_LIMIT = 3_000
INVENTORY_WAIT_SECONDS = 20

STATE_OPEN = "<sandbox_state>"

# ================================================================================
# Sandbox inventory
# ================================================================================

# Runs in the Python session. Everything it defines starts with an underscore
# and is deleted afterwards, so taking the inventory never shows up in it.
# String values are described by length only: a variable may hold a secret the
# user pasted, and this text is stored and replayed.
PY_INVENTORY = r'''
def __sandbox_inventory():
    import inspect, json, types
    # Ranked so what the model can reuse comes first: its own functions and
    # classes, then data, then imports, then loop leftovers (plain scalars).
    ranked = {0: [], 1: [], 2: [], 3: []}
    for name, value in list(globals().items()):
        if name.startswith("_"):
            continue
        try:
            home = getattr(inspect.getmodule(value), "__name__", "__main__")
            if isinstance(value, types.ModuleType):
                ranked[2].append(f"import {value.__name__}" +
                                 ("" if value.__name__ == name else f" as {name}"))
            elif (inspect.isclass(value) or inspect.isfunction(value)) and home != "__main__":
                ranked[2].append(f"from {home} import {name}")
            elif inspect.isclass(value):
                ranked[0].append(f"class {name}")
            elif inspect.isfunction(value):
                ranked[0].append(f"def {name}{inspect.signature(value)}")
            else:
                kind = type(value).__name__
                shape = getattr(value, "shape", None)
                if shape == ():          # numpy scalar: its value says more
                    value, shape = value.item(), None
                if shape is not None and not callable(shape):
                    ranked[1].append(f"{name}: {kind} shape={tuple(shape)}")
                elif isinstance(value, (bool, int, float, complex)):
                    ranked[3].append(f"{name} = {value!r}"[:60])
                elif isinstance(value, (str, bytes, list, tuple, dict, set)):
                    ranked[1].append(f"{name}: {kind} len={len(value)}")
                else:
                    ranked[1].append(f"{name}: {kind}")
        except Exception:
            ranked[1].append(f"{name}: ?")
    rows = ranked[0] + ranked[1] + ranked[2]
    if ranked[3]:
        rows.append("scalars: " + ", ".join(ranked[3][:30]))
    print(json.dumps(rows[:80]))
try:
    __sandbox_inventory()
finally:
    del __sandbox_inventory
'''

# Runs in the bash session. The pipelines run in subshells, and the one
# top-level variable is unset, so nothing lingers. Exports are compared with
# the supervisor's environment (the shell's parent): only names the session
# added are reported, and only names -- values may be credentials.
SH_INVENTORY = r'''
printf 'cwd\t%s\n' "$PWD"
declare -F | while read -r _ _ __inv_f; do
  case "$__inv_f" in _mv_*|exit|logout) ;; *) printf 'function\t%s\n' "$__inv_f" ;; esac
done
__inv_base=$(tr '\0' '\n' < "/proc/$PPID/environ" 2>/dev/null | cut -d= -f1)
compgen -e | while read -r __inv_v; do
  # Bash-maintained names, and locale variables Python sets for its
  # children (PEP 538), are not the session's doing.
  case "$__inv_v" in PWD|OLDPWD|SHLVL|_|LC_*|LANG) continue ;; esac
  grep -qx -- "$__inv_v" <<<"$__inv_base" || printf 'export\t%s\n' "$__inv_v"
done
unset __inv_base
find /workspace -mindepth 1 -maxdepth 2 -name .git -prune -o \
  -printf 'file\t%P\t%s\t%y\n' 2>/dev/null | sort | head -n 40
'''


def _run_quiet(session, code, kernel):
    """Run one inventory cell. Returns its stdout, or None if it did not finish."""
    submitted = sandbox.submit(session, code, kernel)
    if "job" not in submitted:
        return None
    status = sandbox.wait(session, submitted["job"],
                          time.monotonic() + INVENTORY_WAIT_SECONDS)
    result = status.get("result") or {}
    if status.get("state") != "done" or not result.get("ok"):
        return None
    return result.get("stdout") or ""


def _size(n):
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def capture_inventory(session):
    """Describe what the sandbox holds right now, for the next message.

    Best effort: a busy or dead session is simply left out.

    Returns:
        (text, summary) -- the block for the model, and a short line for the
        trace -- or (None, None) if nothing could be captured.
    """
    py_rows, files, cwd, functions, exports = None, [], None, [], []

    py_out = _run_quiet(session, PY_INVENTORY, "python")
    if py_out is not None:
        try:
            py_rows = json.loads(py_out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            py_rows = None

    sh_out = _run_quiet(session, SH_INVENTORY, "bash")
    for line in (sh_out or "").splitlines():
        parts = line.split("\t")
        if parts[0] == "cwd" and len(parts) > 1:
            cwd = parts[1]
        elif parts[0] == "function" and len(parts) > 1:
            functions.append(parts[1])
        elif parts[0] == "export" and len(parts) > 1:
            exports.append(parts[1])
        elif parts[0] == "file" and len(parts) > 3:
            files.append(parts[1] + "/" if parts[3] == "d" else f"{parts[1]}  ({_size(parts[2])})")

    if py_rows is None and sh_out is None:
        return None, None

    lines = [f"Captured at the end of your previous message, in MicroVM {session['id']}."]
    lines.append(f"Files in /workspace ({len(files)}{'+' if len(files) >= 40 else ''}):")
    lines += [f"  {f}" for f in files] or ["  (none)"]
    if py_rows is not None:
        lines.append("Python session (run_code):")
        lines += [f"  {r}" for r in py_rows] or ["  (nothing defined)"]
    if sh_out is not None:
        lines.append("Bash session (run_shell):")
        lines.append(f"  cwd: {cwd or '/workspace'}")
        lines.append(f"  exported: {', '.join(exports) if exports else '(none)'}")
        lines.append(f"  functions: {', '.join(functions) if functions else '(none)'}")
    text = "\n".join(lines)
    if len(text) > INVENTORY_LIMIT:
        text = text[:INVENTORY_LIMIT] + "\n  ... (truncated)"

    summary = (f"{len(files)} file(s), {len(py_rows or [])} Python name(s), "
               f"bash cwd {cwd or '/workspace'}")
    return text, summary


def save_inventory(user_id, conv_id, vm_id, text, summary):
    """Record the inventory on the conversation, tagged with the VM it describes."""
    try:
        table.update_item(
            Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"},
            UpdateExpression=("SET sandbox_inventory = :t, sandbox_inventory_vm = :v, "
                              "sandbox_inventory_summary = :s"),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": text, ":v": vm_id, ":s": summary})
    except Exception:
        logger.exception("Could not save sandbox inventory (non-fatal)")


def state_block(user_id, conv_id):
    """Tell the model what the conversation's sandbox holds, before it acts.

    Uses GetMicrovm only, which never wakes a suspended VM.

    Returns:
        (text, summary): text is a <sandbox_state> block to put in front of
        the question, or None when this conversation has never had a sandbox.
    """
    item = table.get_item(Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"},
                          ConsistentRead=True).get("Item") or {}
    vm_id = item.get("sandbox_id")
    if not vm_id:
        return None, None

    gone = item.get("sandbox_image") != sandbox.IMAGE_ARN
    if not gone:
        try:
            gone = sandbox.gone(sandbox.state_of(vm_id))
        except Exception:
            logger.exception("GetMicrovm failed; assuming the sandbox is alive")
    if gone:
        return (f"{STATE_OPEN}\nThe sandbox used earlier in this conversation no "
                "longer exists (MicroVMs live 8 hours at most, and a redeploy "
                "replaces them). Its files, variables and shell state are gone. A "
                "fresh sandbox launches when you next run code; do not assume "
                "anything from earlier messages is still defined.\n</sandbox_state>",
                "earlier sandbox expired")

    if item.get("sandbox_inventory") and item.get("sandbox_inventory_vm") == vm_id:
        return (f"{STATE_OPEN}\n{item['sandbox_inventory']}\n\nAll of this still "
                "exists in the conversation's sandbox. Reuse it -- call existing "
                "functions, read existing files -- instead of recreating it.\n"
                "</sandbox_state>",
                f"sandbox state: {item.get('sandbox_inventory_summary') or 'captured'}")

    return (f"{STATE_OPEN}\nThis conversation has a sandbox (MicroVM {vm_id}) from "
            "earlier messages, but its contents were not captured. Inspect it "
            "before assuming what is defined.\n</sandbox_state>",
            "sandbox state unknown")


# ================================================================================
# Conversation history
# ================================================================================

def sanitize_exchange(messages, answer):
    """Prepare one message's Converse exchange for storage and later replay.

    Drops the injected <sandbox_state> block (stale by the next message),
    replaces image bytes with a placeholder, clips long outputs and code, and
    makes sure the exchange ends on an assistant turn -- the loop can stop on
    a tool result when it runs out of time or turns, and replaying that would
    leave two user turns in a row.
    """
    out = []
    for message in copy.deepcopy(messages):
        content = []
        for block in message.get("content") or []:
            if "text" in block and block["text"].startswith(STATE_OPEN):
                continue
            if "cachePoint" in block:
                continue
            if "toolUse" in block:
                args = block["toolUse"].get("input") or {}
                for field in ("code", "command"):
                    if isinstance(args.get(field), str) and len(args[field]) > SAVED_CODE_LIMIT:
                        args[field] = args[field][:SAVED_CODE_LIMIT] + "\n# ... (clipped)"
            if "toolResult" in block:
                kept = []
                for part in block["toolResult"].get("content") or []:
                    if "image" in part:
                        kept.append({"text": "[image shown to the user and to you at "
                                             "the time; not replayed]"})
                    elif "text" in part and len(part["text"]) > SAVED_RESULT_LIMIT:
                        kept.append({"text": part["text"][:SAVED_RESULT_LIMIT]
                                     + "\n... (clipped in history)"})
                    else:
                        kept.append(part)
                block["toolResult"]["content"] = kept or [{"text": "(no output)"}]
            content.append(block)
        if content:
            out.append({"role": message["role"], "content": content})

    if not out or out[-1]["role"] != "assistant":
        out.append({"role": "assistant", "content": [{"text": answer or "(no answer)"}]})
    return out


def _read_json(key):
    return json.loads(s3.get_object(Bucket=BACKEND_BUCKET, Key=key)["Body"].read())


def _read_text(key):
    return s3.get_object(Bucket=BACKEND_BUCKET, Key=key)["Body"].read().decode("utf-8")


def load_history(user_id, conv_id, current_query_id):
    """Replay recent exchanges, full where possible, within the size budget.

    Returns:
        (messages, count) -- Converse messages in chronological order, and how
        many earlier exchanges they cover.
    """
    items = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"USER#{user_id}") &
            Key("sk").begins_with(f"QUERY#{conv_id}#")
        )
    ).get("Items", [])
    done = sorted(
        (i for i in items
         if i.get("status") == "complete" and i.get("query_id") != current_query_id),
        key=lambda i: i.get("created_at") or "",
    )

    picked, used = [], 0
    for item in reversed(done):
        if len(picked) >= HISTORY_WINDOW:
            break
        exchange = None
        if item.get("messages_s3_key"):
            try:
                exchange = _read_json(item["messages_s3_key"])
            except Exception:
                logger.exception("Could not read %s", item["messages_s3_key"])
        # Too big, or saved before full history existed: fall back to the
        # question and final answer, which is what this used to replay.
        if exchange is None or used + len(json.dumps(exchange)) > HISTORY_CHAR_BUDGET:
            try:
                exchange = [
                    {"role": "user", "content": [{"text": _read_text(item["question_s3_key"])}]},
                    {"role": "assistant", "content": [{"text": _read_text(item["answer_s3_key"])}]},
                ]
            except Exception:
                continue
        size = len(json.dumps(exchange))
        if picked and used + size > HISTORY_CHAR_BUDGET:
            break
        picked.append(exchange)
        used += size

    messages = [m for exchange in reversed(picked) for m in exchange]
    return messages, len(picked)


# ================================================================================
# Prompt caching
# ================================================================================

CACHE_POINT = {"cachePoint": {"type": "default"}}


def cache_points(messages, history_len):
    """Return messages with cache checkpoints, without modifying the originals.

    Two checkpoints (the system prompt carries a third): the end of the
    replayed history, which is identical on every turn of this message, and
    the latest message, so each tool turn re-reads everything before it from
    cache instead of paying for it again. Prefixes under the model's minimum
    cacheable size are simply not cached.
    """
    marked = list(messages)
    for index in {history_len - 1, len(messages) - 1}:
        if 0 <= index < len(marked):
            message = marked[index]
            marked[index] = dict(message, content=list(message["content"]) + [CACHE_POINT])
    return marked
