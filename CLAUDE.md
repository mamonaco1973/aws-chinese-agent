# CLAUDE.md

Guidance for working in **aws-chinese-agent** (product name: **MicroVM Agent Sandbox**).
Read this before changing `01-sandbox/image/` or `02-core/code/worker.py`;
several things here look like obvious improvements and are not.

## What This App Is

A chat app whose agent runs code in its own **Lambda MicroVM**, one per
conversation, launched on demand. The demo prompt is *"Build me a fractal tree
and get me the results"*: the agent writes Python, runs it in the VM, looks at
the PNG it rendered, and attaches it to the answer.

The MicroVM plumbing (image build, supervisor, lifecycle, IAM) comes from the
sibling project `../aws-lambda-microvms`, whose CLAUDE.md records the hard-won
MicroVM gotchas. The chat spine (SPA → API → SQS → worker → poll, on Cognito,
DynamoDB, S3 and CloudFront) comes from the earlier Bedrock Agents version of
this repo.

## Architecture

    01-sandbox/          MicroVM image: Terraform + image/ (Dockerfile, server.py, kernel.py, shell.sh)
    02-core/             Backend Terraform + code/ (handler, conversations, users, worker,
                         sandbox, memory, models)
    03-webapp/           Vanilla-JS SPA, uploaded by apply.sh (no Terraform)

### Request flow

1. `POST /conversations/{id}/queries` → the API Lambda writes `question.txt`,
   creates a `QUERY#` record (`pending`) and enqueues SQS.
2. The worker marks the query `processing`, sets `trace_s3_key` right away,
   and runs the **Converse tool loop**. It rewrites `trace.json` after every
   step, which is how the browser shows live progress while it polls.
3. Tools run against the conversation's sandbox. `sandbox.ensure()` launches
   one on first use and records it on the `CONV#` item.
4. When the loop ends, the worker writes `answer.txt` and the final
   `trace.json`, and stores `artifacts` (the staged files) on the query. The
   API re-signs those as short-lived URLs on every poll.

### Why a Converse loop, not Bedrock Agents

- **The model can see its own image** (on models with image input; see
  Models). `show_file` returns the PNG as an image block, so the model can
  catch a bad render. Bedrock Agents action groups return text only.
- **The worker waits, not the model.** `run_code`/`run_shell` block in the worker (up to
  4 minutes, then it hands back a job id for `get_result`). The model never
  has to poll from inside its own reasoning.

### The sandbox (01-sandbox/image)

- `server.py` is the stdlib supervisor. It serves `/execute` (returns a job
  id), `/result/<id>`, `/file?path=` and `/state` on 8080, and the lifecycle
  hooks on 8081. Endpoint tokens are scoped to 8080.
- `server.py` runs two sessions, each a `Kernel` with its own process and
  job slot, so a long install in bash never blocks a plot in Python.
  `/execute` takes `{"code", "kernel": "python"|"bash"}`.
- `kernel.py` is the persistent Python session: one `exec` namespace for the
  whole life of the VM (the `run_code` tool).
- `shell.sh` is the persistent bash session, adapted from aws-lambda-microvms'
  `worker.sh` (the `run_shell` tool). Both sessions start in `/workspace`.
- A dead session (bash `set -e` failure, `builtin exit`, Python `os._exit`)
  is **restarted on its next command**, and that command's output starts
  with a note saying what was reset. The caller is a model; a usable session
  with an honest note beats a dead end that needs a human to relaunch.

### Context between messages (02-core/code/memory.py)

- **History:** each message's full Converse exchange is saved as
  `messages.json` (images replaced by placeholders, results clipped to 4K,
  the `<sandbox_state>` block dropped) and replayed newest-first, up to 5
  exchanges or 60K characters. Older rows without `messages.json` fall back
  to question + answer text.
- **Sandbox inventory:** after a message that used the sandbox, two read-only
  cells (Python namespace; bash cwd, functions, added exports, files) run
  while the VM is awake. The result is stored on the CONV# item, tagged with
  the VM id. The next message gets it as a `<sandbox_state>` block ahead of
  the question. `state_block()` only calls GetMicrovm, so it never wakes a
  suspended VM, and it reports an expired sandbox as gone.
- **Caching** (models with `prompt_caching`, see Models): checkpoints after
  the system prompt, after the replayed history, and on the latest message.
  The budget counts cache reads at 0.1x and cache writes at 1.25x
  (`worker.budget_tokens`).
- The inventory cells must leave nothing behind: Python names start with `_`
  and are deleted; bash pipelines run in subshells. Values of exports and
  strings are never captured, because they may be secrets.

## Rules That Are Load-Bearing

- **The kernel takes the protocol descriptors before anything else runs.** It
  dups fds 0 and 1 for the JSON-lines protocol, then points 0/1/2 at
  `/dev/null`, or at a capture file while a cell runs. Without that, a cell's
  `print`, a subprocess, or `input()` would corrupt or swallow protocol lines.
  Never print to real stdout in `kernel.py`.
- **Catch `BaseException` around a cell.** `exit()` inside a cell must fail
  that cell, not end the session.
- **Never add `set -euo pipefail` to `shell.sh`.** Commands run in the session
  shell itself, so `-e` would end the session on the first failing command.
  The same goes for commands the model sends; the system prompt says so.
- **`eval` runs in the session shell, never a subshell,** and `</dev/null` on
  that line is load-bearing: stdin is the protocol channel, so a command
  running `read` or `cat` would otherwise swallow the next request.
- **`exit` is shadowed by a function in `shell.sh`.** Models write `exit 1` out
  of habit; the guard turns it into a failed command. It returns rather than
  stopping the rest of the line, which is acceptable.
- **numpy and matplotlib are imported before readiness is reported**, so they
  sit in the snapshot's memory. Moving those imports after the ready line
  makes every first plot cold. Anything imported there is paid for once, at
  image build.
- **Nothing per-session goes into the snapshot.** The session nonce and VM id
  are created in the `/run` hook, after restore.
- **The sandbox role has no policies, on purpose.** Model-written code runs
  there, so anything that role can do, any prompt can do. Files leave the VM
  through the worker (`/file` → S3 under the worker's role). Do not give the
  guest S3 access to "simplify" this.
- **`show_file` on the same path replaces the attachment.** The system prompt
  tells the model to fix a bad render by saving to the same path and showing
  it again. Appending instead would show the user every draft.
- **The token budget is applied at read time.** `users.token_limit()` returns
  `TOKEN_LIMIT_DEFAULT` (1M) unless the usage record has a hand-set
  `token_limit_override`. The legacy `token_limit` attribute is ignored on
  purpose: stored per record, it froze old defaults in place.
- **Image names hash the source files, not the zip.** Every new image name is
  a new image with a one-week minimum storage charge, and zip entries carry
  mtimes that change on every checkout.

## MicroVM Gotchas (inherited, all confirmed)

- `iam:PassRole` on the sandbox role is required to launch with an execution
  role, and the MicroVMs least-privilege example omits it. Do not add an
  `iam:PassedToService` condition: the service never populates that key, so
  the grant silently stops working.
- Any role Lambda assumes (build role, sandbox role) needs `sts:TagSession`
  alongside `sts:AssumeRole`.
- The Lambda runtime's bundled boto3 predates `lambda-microvms`. Both
  functions use the layer that `apply.sh` vendors (`dist/boto3-layer.zip`).
- While a suspended VM is restoring, its endpoint answers 502/503/504.
  `sandbox._request` retries those; do not treat them as failures.
- `GetMicrovm` never wakes a VM. Any request to the endpoint does.
- The base image's `dnf` is microdnf. Do not add `coreutils` to the
  Dockerfile: it conflicts with `coreutils-single`.
- MicroVMs are not owned by Terraform. `destroy.sh` terminates every VM
  launched from the image before destroying anything.

## Models

`bedrock-config.sh` lists the models a user can pick (`BEDROCK_MODELS`, one
`key|model id|label|image input|prompt caching` per line) and the default
(`BEDROCK_DEFAULT`). `apply.sh` passes them to Terraform as JSON; both Lambdas
get them as `MODELS_JSON` / `DEFAULT_MODEL` and read them through
`code/models.py`.

- **Locked per conversation, by its first message.** The new-chat screen shows
  a picker; `POST .../queries` carries `model`, and `submit_query` stores it on
  the CONV# item with a conditional write (`attribute_not_exists(model)`), so
  later messages cannot change it. History and the sandbox carry across
  messages; one model per chat keeps them coherent. The picker then becomes
  the locked model's name. `GET /models` feeds the picker.
- **The worker reads the model per query** from the CONV# item
  (`_conversation_model`); an unknown or retired key falls back to the
  default. Every trace starts with a "Model: ..." context step.
- **The two switches are per model.** `image_input: false` -- `show_file`
  still stages the file for the user (S3 + signed URL) but tells the model in
  text, and the system prompt drops the "review your image" rules
  (`SYSTEM_PROMPTS[False]`). `prompt_caching: false` -- no cachePoints are
  sent. A switch that is `true` for a model that cannot honour it makes every
  request to that model fail.
- **Token use counts the same for every model.** The budget caps the bill; it
  does not price models.
- **IAM** allows every inference profile in the list plus any foundation
  model (on-demand ids such as `deepseek.v3.2` have no profile).

`./probe_bedrock.py` lists every model this account can use -- `us.*`,
`global.*` and on-demand ids in one table -- tests tool use, image input and
prompt caching with real calls, and prints ready-to-paste `BEDROCK_MODELS`
lines. `check_env.sh` runs `probe_bedrock.py --check <id> --image --caching`
on every entry, which fails a switch the model cannot honour.

Configured: Claude Sonnet 4.6, DeepSeek V3.2 (default) and Qwen3 Coder Next.
Probed 2026-10-02/03: Sonnet 4.6 supports all three. DeepSeek V3.2 and Qwen3
Coder Next call tools but reject image blocks (ValidationException) and any
cachePoint (AccessDeniedException). Without vision DeepSeek tends to make
"another version" of what it rendered; `show_file`'s text reply tells it the
request is done, and its leaked `<｜DSML｜function_calls` markup is stripped
from text blocks.

## Webapp (03-webapp)

- **Model picker:** a pill in the input box's toolbar, left of send, filled
  from `GET /models`. Disabled and showing the conversation's model once the
  first message locks it. The last choice is remembered in localStorage.
- **Input box:** two rows -- text on top, toolbar (model, send) pinned
  underneath. The text grows to 40% of the window, then scrolls
  (`_autoResize` and the CSS `max-height` share that limit). Five-line
  minimum, three on windows under 760px tall.
- **Live trace:** while a query runs, each poll adds new trace steps to the
  thinking bubble (step by step, so open folds stay open). The finished
  answer shows them folded into a "Reasoning" bar *above* the answer, with
  tool calls, tokens and elapsed time (`created_at` to `updated_at`).
- **Files:** raster images inline; HTML opens in a new tab -- the API signs
  it `inline` as `text/html`, safe because the S3 link is a different origin
  from the app; everything else (SVG included) downloads.
- **Sidebar:** the title is refreshed right after a send (switching chats
  drops the poll, so waiting for the answer missed it). Shift- or Ctrl-click
  on delete removes every conversation. An empty conversation shows the
  starter questions, not an empty log.
- **User messages** keep their line breaks (`white-space: pre-wrap`).

## Testing Without Deploying

- `01-sandbox/image` builds locally on
  `public.ecr.aws/amazonlinux/amazonlinux:2023-minimal` (same OS, microdnf and
  Python 3.9 as the MicroVM base) by swapping the `FROM` line. Run it and
  drive it with curl on :8080.
- `worker.Run.converse` can run against real Bedrock with
  `sandbox.ensure`/`_token` patched to return
  `{"endpoint": "http://<container>:8080"}`. The client accepts an explicit
  scheme for exactly this reason.
- `validate.sh` (run by `apply.sh`) smoke-tests a real VM: launch it, run
  one Python cell, terminate it.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*.
