"""HTTP application and lifecycle-hook listener running inside the MicroVM.

The supervisor owns two persistent sessions and speaks HTTP for both:

  * "python" -- kernel.py, one exec() namespace for the life of the VM
  * "bash"   -- shell.sh, one bash process whose cwd, exports and variables
                carry from command to command

Both run in /workspace, so a file one writes the other can read. Each is its
own process with its own job slot: a long pip install in bash does not block a
plot in Python.

Cells are submitted, not awaited. POST /execute starts a cell and returns a
job id immediately; GET /result/<id> reports on it. The caller never holds a
connection open for the length of the work, so a cell can outlive any HTTP
timeout in the chain, up to the VM's own lifetime. Jobs live in this process's
memory, so they survive a suspend along with everything else.

Two servers on two ports, deliberately:

  * 8080 serves the application (/state, /execute, /result, /file). Endpoint
    auth tokens are scoped to this port only.
  * 8081 serves the AWS lifecycle hooks, so a leaked application token can
    never drive the session's lifecycle.

Standard library only: the supervisor must not depend on anything a cell
might pip-uninstall.
"""
import argparse
from collections import deque
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

# AWS posts lifecycle hooks to this fixed path prefix on the configured port.
HOOK = "/aws/lambda-microvms/runtime/v1/"

# Longest a single cell may run: the VM's own maximum lifetime. Nothing
# outside this process waits on a cell, so there is no HTTP timeout to match.
# A usability guard, not a sandbox -- the VM is the security boundary.
CELL_TIMEOUT = 28800

# Largest file /file will return. Only stops a request for /dev/zero from
# exhausting memory; the controller applies its own, smaller limits.
MAX_FILE_BYTES = 25 * 1024 * 1024

# Largest cell accepted. The controller enforces the same number.
MAX_CODE_CHARS = 50000

HERE = Path(__file__).resolve().parent

# How each session is started. Both speak the same JSON-lines protocol:
# {"code": ...} in, {"ok", "stdout", "execution_ms"} out, after one
# {"ready": true} line at startup.
KERNELS = {
    "python": [sys.executable, str(HERE / "kernel.py")],
    "bash":   ["bash", str(HERE / "shell.sh")],
}


class Kernel:
    """One persistent session process and its single job slot.

    The session runs as a separate process so a cell that hangs, segfaults or
    exits can be killed without taking the HTTP server down with it. The
    server survives to report the damage.
    """

    def __init__(self, name, argv, workspace):
        """Start the session and wait for it to report readiness.

        Raises:
            RuntimeError: It failed to start, which must fail the image build
                rather than snapshot a broken VM.
        """
        self.name = name
        self.argv = argv
        self.workspace = workspace
        self.lock = threading.Lock()
        self.job = None                 # the in-flight or last-finished cell
        self.restart_note = ""          # prefixed to the next result after a restart
        self._start()

    def _start(self):
        """Start (or restart) the session process and wait until it is ready.

        Blocks until the session is up -- for Python, until numpy and
        matplotlib are imported. At image build this is what puts them in the
        snapshot's memory.
        """
        self.process = subprocess.Popen(
            self.argv, cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        # A fresh queue per process: the old reader's final "exited" message
        # must never be mistaken for the new process's first result.
        self.responses = queue.Queue()
        threading.Thread(target=self._read, args=(self.process, self.responses),
                         daemon=True).start()
        ready = self.responses.get(timeout=120)
        if not ready.get("ready"):
            raise RuntimeError(f"{self.name} session failed to start")
        self.dead = False

    def _read(self, process, responses):
        """Drain one session process's protocol output onto its queue."""
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except ValueError:
                responses.put({"ok": False, "stdout": "Session protocol corrupted."})
        responses.put({"ok": False, "stdout": f"The {self.name} session exited "
                                              "during this cell; its in-memory "
                                              "state is gone."})

    def alive(self):
        return self.process.poll() is None and not self.dead

    def execute(self, code, job_id):
        """Hand one cell to the session. Returns a job, or a refusal."""
        with self.lock:
            # One cell at a time per session. Refused rather than queued, and
            # deliberately NOT answered with the running job's id: the caller
            # would then report another cell's output as its own.
            if self.job and self.job["state"] == "running":
                elapsed = round(time.time() - self.job["started"])
                return {"state": "refused",
                        "error": f"Another {self.name} cell has been running for "
                                 f"{elapsed}s. Each session runs one cell at a time."}
            if not self.alive():
                # Restart rather than refuse: the caller is a model, and a
                # session it can keep using -- told plainly what was lost --
                # beats a dead end that needs a human to relaunch the VM.
                try:
                    self._start()
                except (OSError, RuntimeError, queue.Empty):
                    return {"state": "refused",
                            "error": f"The {self.name} session is dead and could "
                                     "not be restarted."}
                self.restart_note = (f"[The {self.name} session had exited and was "
                                     "restarted: variables, functions and "
                                     + ("imports" if self.name == "python" else "cd/exports")
                                     + " are reset. Files on disk are intact.]\n")
            self.job = {"id": job_id, "state": "running", "started": time.time()}
            self.process.stdin.write(json.dumps({"code": code}) + "\n")
            self.process.stdin.flush()

        threading.Thread(target=self._collect, args=(job_id,), daemon=True).start()
        return {"job": job_id, "state": "running", "kernel": self.name}

    def _collect(self, job_id):
        """Wait for one cell's result and file it against its job."""
        responses = self.responses
        try:
            result = responses.get(timeout=CELL_TIMEOUT)
        except queue.Empty:
            self.process.kill()
            self.process.wait(timeout=5)
            self.dead = True
            result = {"ok": False, "stdout": f"Cell exceeded {CELL_TIMEOUT}s; "
                                             "session killed."}
        with self.lock:
            if self.restart_note:
                result = dict(result, stdout=self.restart_note + (result.get("stdout") or ""))
                self.restart_note = ""
            if self.job and self.job["id"] == job_id:
                self.job["state"] = "done"
                self.job["result"] = result
        if self.process.poll() is not None:
            self.dead = True

    def result(self, job_id):
        """Report on a job, or None if this session never issued it."""
        with self.lock:
            job = self.job
            if not job or job["id"] != job_id:
                return None
            if job["state"] == "running":
                return {"state": "running",
                        "elapsed_s": round(time.time() - job["started"])}
            return {"state": "done", "result": job["result"]}


class Session:
    """This VM's sessions and identity."""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.kernels = {name: Kernel(name, argv, self.workspace)
                        for name, argv in KERNELS.items()}

        # Generated in the /run hook, after restore, so it is unique per VM.
        # Anything generated here would be shared by every clone of the image.
        self.session_nonce = None
        self.microvm_id = None
        self.events = deque(maxlen=20)

    def state(self):
        """Return this session's identity and health."""
        return {"microvm_id": self.microvm_id, "session_nonce": self.session_nonce,
                "server_pid": os.getpid(), "events": list(self.events),
                "kernels": {name: {"pid": k.process.pid, "alive": k.alive()}
                            for name, k in self.kernels.items()}}

    def hook(self, name, data):
        """Handle one AWS lifecycle hook.

        Raises:
            ValueError: Unknown hook, or a retried /run aimed at a different
                MicroVM, which would mean a session is being reused wrongly.
        """
        if name not in {"ready", "validate", "run", "suspend", "resume", "terminate"}:
            raise ValueError("Unknown hook")
        if name == "run":
            if self.session_nonce is not None:
                if self.microvm_id != data.get("microvmId"):
                    raise ValueError("Session already assigned")
            else:
                self.microvm_id = data.get("microvmId")
                self.session_nonce = str(uuid.uuid4())
        self.events.append({"hook": name, "wall_time": time.time()})
        return {"ok": True}

    def execute(self, code, kernel="python"):
        """Start a cell in the named session and return its job id.

        Raises:
            ValueError: Unknown session, or code of the wrong type or size.
        """
        if kernel not in self.kernels:
            raise ValueError(f"Unknown kernel {kernel!r}; use one of {sorted(self.kernels)}")
        if not isinstance(code, str) or len(code) > MAX_CODE_CHARS:
            raise ValueError(f"Code must be a string of at most {MAX_CODE_CHARS} characters")
        return self.kernels[kernel].execute(code, uuid.uuid4().hex[:8])

    def result(self, job_id):
        """Report on a job from either session.

        Returns:
            {"state": "running", "elapsed_s": n}, {"state": "done", "result":
            {...}}, or {"state": "unknown"} for an id this VM never issued --
            after a relaunch an old id is stale, not an error.
        """
        for kernel in self.kernels.values():
            found = kernel.result(job_id)
            if found is not None:
                return found
        return {"state": "unknown"}


def handler(session, hooks=False):
    """Build a request handler bound to one Session.

    Args:
        session: The session this handler serves.
        hooks: True for the lifecycle listener on 8081. Each port exposes only
            its own routes.
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            """Silence per-request logging."""

        def send_json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if hooks:
                self.send_json({"error": "Not found"}, 404)
            elif self.path in {"/state", "/health"}:
                self.send_json(session.state())
            elif self.path.startswith("/file?"):
                self.send_file()
            elif self.path.startswith("/result/"):
                self.send_json(session.result(self.path[len("/result/"):]))
            else:
                self.send_json({"error": "Not found"}, 404)

        def send_file(self):
            """Return one file's raw bytes.

            Unrestricted as to path on purpose: a cell can already read
            anything this process can, so a traversal guard would prevent
            nothing. Relative paths resolve against the shared workspace,
            which is where both sessions' relative writes land.
            """
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = (query.get("path") or [""])[0]
            if not path:
                self.send_json({"error": "No path given"}, 400)
                return
            path = str(session.workspace / path)  # absolute paths pass through
            try:
                size = os.path.getsize(path)
                if size > MAX_FILE_BYTES:
                    self.send_json({"error": f"File is {size} bytes; the limit "
                                             f"is {MAX_FILE_BYTES}."}, 413)
                    return
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError as exc:
                self.send_json({"error": str(exc)}, 404)
                return
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Sandbox-File-Name", os.path.basename(path))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4 * MAX_CODE_CHARS:
                    raise ValueError("Body too large")
                data = json.loads(self.rfile.read(length) or b"{}")
                if hooks and self.path.startswith(HOOK):
                    self.send_json(session.hook(self.path[len(HOOK):], data))
                elif not hooks and self.path == "/execute":
                    self.send_json(session.execute(data["code"],
                                                   data.get("kernel", "python")))
                else:
                    self.send_json({"error": "Not found"}, 404)
            except (ValueError, KeyError, TypeError) as exc:
                self.send_json({"error": str(exc)}, 400)
    return Handler


def main():
    """Start both servers and block until the application server stops."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--hook-port", type=int, default=8081)
    parser.add_argument("--workspace", default="/workspace")
    args = parser.parse_args()
    session = Session(args.workspace)
    service = ThreadingHTTPServer((args.host, args.port), handler(session))
    lifecycle = ThreadingHTTPServer((args.host, args.hook_port), handler(session, hooks=True))
    threading.Thread(target=lifecycle.serve_forever, daemon=True).start()
    try:
        service.serve_forever()
    finally:
        lifecycle.shutdown()


if __name__ == "__main__":
    main()
