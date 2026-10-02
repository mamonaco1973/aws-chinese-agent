"""A persistent Python session: one namespace that lives as long as the VM.

This process holds the session's state. Variables, imports, open figures and
files survive between cells and across suspend and resume, because AWS
checkpoints the VM's memory -- there is no pickling, no save path, no replay.

Protocol: one JSON object per line on the ORIGINAL stdin/stdout, which are
duplicated to private descriptors before any cell runs. Descriptors 0, 1 and 2
are then pointed elsewhere, so nothing a cell does -- print(), input(), a
subprocess writing to its inherited stdout -- can corrupt the protocol or eat
the next request line.

The VM is the security boundary, not this process. Cells run through exec()
with full access to the interpreter, which is acceptable only because the
MicroVM around it is isolated and its execution role grants nothing.
"""
import ast
import json
import os
import sys
import tempfile
import time
import traceback

# ==============================================================================
# Protocol channel -- taken before anything else can write to descriptor 1
# ==============================================================================
_proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
_proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)

# Between cells, 0/1/2 point at /dev/null: input() gets EOF instead of the
# next request, and stray output from a background thread goes nowhere.
_devnull = os.open(os.devnull, os.O_RDWR)
for _fd in (0, 1, 2):
    os.dup2(_devnull, _fd)

# Line-buffered, so a cell's print() lands in order with output from the
# subprocesses it starts, which write to the same descriptor unbuffered.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Keep head AND tail when truncating: a traceback's useful line is the last
# one, and a progress-spamming loop's useful line is often the first.
OUTPUT_LIMIT = 64000

# ==============================================================================
# Preload -- anything imported here is part of the image snapshot
# ==============================================================================
# Importing matplotlib takes seconds cold. Done here, before readiness is
# reported, it happens once at image build and every launched MicroVM starts
# with the modules already in memory. Cells still write `import numpy as np`;
# it just returns instantly.
import numpy  # noqa: E402,F401
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot  # noqa: E402,F401

# The session namespace. A module-like dict so pickling, dataclasses and
# `if __name__ == "__main__":` behave as they would in a script.
SESSION = {"__name__": "__main__", "__builtins__": __builtins__}

_scratch = tempfile.mkdtemp(prefix="kernel-")


def run(source):
    """Execute one cell in the session namespace and capture its output.

    A trailing expression is echoed like a REPL, so a cell ending in `x` or
    `df.shape` shows its value without an explicit print.

    Args:
        source: Python source for the cell.

    Returns:
        (ok, output) where ok is False when the cell raised.
    """
    capture_path = os.path.join(_scratch, "cell.out")
    with open(capture_path, "w+b") as capture:
        os.dup2(capture.fileno(), 1)
        os.dup2(capture.fileno(), 2)
        ok = True
        try:
            tree = ast.parse(source, "<cell>", "exec")
            tail = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                tail = ast.Expression(tree.body.pop().value)
            exec(compile(tree, "<cell>", "exec"), SESSION)
            if tail is not None:
                value = eval(compile(tail, "<cell>", "eval"), SESSION)
                if value is not None:
                    print(repr(value))
        # BaseException, not Exception: exit() or sys.exit() in a cell must be
        # reported as a failed cell, not end the session it is running in.
        except BaseException as exc:  # noqa: BLE001
            ok = False
            # Drop frames above the cell: the model should see its own code in
            # the traceback, not the machinery that ran it.
            tb = exc.__traceback__
            while tb is not None and tb.tb_frame.f_code.co_filename != "<cell>":
                tb = tb.tb_next
            sys.stderr.write("".join(traceback.format_exception(type(exc), exc, tb)))
        finally:
            # A cell may have replaced sys.stdout; put the real ones back so
            # the next cell's output is captured too.
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(_devnull, 1)
            os.dup2(_devnull, 2)
        capture.seek(0)
        raw = capture.read()

    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    if len(text) > OUTPUT_LIMIT:
        half = OUTPUT_LIMIT // 2
        text = (text[:half] + f"\n... [{len(text) - OUTPUT_LIMIT} characters "
                "truncated] ...\n" + text[-half:])
    return ok, text


def main():
    """Report readiness, then serve cells until the supervisor goes away."""
    _proto_out.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    for line in _proto_in:
        try:
            source = json.loads(line)["code"]
        except (ValueError, KeyError, TypeError):
            # Answer anyway. Silence would hang the supervisor waiting on a
            # response that never arrives.
            _proto_out.write(json.dumps({"ok": False,
                                         "stdout": "Malformed request line."}) + "\n")
            continue
        started = time.monotonic()
        ok, output = run(source)
        _proto_out.write(json.dumps({
            "ok": ok, "stdout": output,
            "execution_ms": round((time.monotonic() - started) * 1000)}) + "\n")


if __name__ == "__main__":
    main()
