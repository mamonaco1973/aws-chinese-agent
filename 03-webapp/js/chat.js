/* ============================================================================ */
/* chat.js                                                                      */
/* Renders chat messages and polls pending queries for completion.             */
/* ============================================================================ */

import { getQuery } from "./api.js";

const POLL_INTERVAL_MS = 2000;
// The worker may run for its full 15-minute Lambda budget (model turns plus
// the cells they run in the sandbox), so give up only after that has passed.
const POLL_MAX_ATTEMPTS = 480;

/* ---------------------------------------------------------------------------- */
/* Module state                                                                  */
/* ---------------------------------------------------------------------------- */

let _activePolls    = {};   // queryId → intervalId
let _cancelHandlers = {};   // queryId → onComplete callback

/* ---------------------------------------------------------------------------- */
/* Public: render a full conversation history into #chat-log                    */
/* ---------------------------------------------------------------------------- */

export function renderHistory(queries) {
  const log = document.getElementById("chat-log");
  log.innerHTML = "";

  for (const q of queries) {
    appendUserMessage(q.question || "");

    if (q.status === "complete") {
      appendAssistantMessage(q.answer || "", q.trace || [], q.query_id, q.files || [],
                             q.tokens_used, _elapsedMs(q));
    } else if (q.status === "failed") {
      appendErrorMessage("This query failed. Please try again.", q.query_id);
    } else {
      appendThinkingMessage(q.query_id);
    }
  }

  scrollToBottom();
}

/* ---------------------------------------------------------------------------- */
/* Public: append a user bubble immediately on submit                           */
/* ---------------------------------------------------------------------------- */

export function appendUserBubble(text) {
  appendUserMessage(text);
  scrollToBottom();
}

/* ---------------------------------------------------------------------------- */
/* Public: append a thinking bubble and start polling for query_id             */
/* ---------------------------------------------------------------------------- */

export function appendPendingBubble(convId, queryId, onComplete) {
  // Opening a conversation with a query still running: renderHistory has
  // already drawn its thinking bubble. Drawing another here gave two bubbles
  // and two Cancel buttons, only one of them live.
  if (!document.querySelector(`[data-query-id="${queryId}"]`)) {
    appendThinkingMessage(queryId);
  }
  scrollToBottom();
  _cancelHandlers[queryId] = onComplete;
  _startPolling(convId, queryId, onComplete);
}

/* ---------------------------------------------------------------------------- */
/* Public: stop all active polls (e.g. when switching conversations)           */
/* ---------------------------------------------------------------------------- */

export function stopAllPolls() {
  for (const id of Object.values(_activePolls)) {
    clearInterval(id);
  }
  _activePolls    = {};
  _cancelHandlers = {};
}

/* ---------------------------------------------------------------------------- */
/* Internal message builders                                                     */
/* ---------------------------------------------------------------------------- */

function appendUserMessage(text) {
  const log = document.getElementById("chat-log");
  const row = document.createElement("div");
  row.className = "msg-row msg-row--user";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;

  row.appendChild(bubble);
  log.appendChild(row);
}

function appendThinkingMessage(queryId) {
  const log = document.getElementById("chat-log");
  const row = document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  const dots = document.createElement("div");
  dots.className = "thinking-dots";
  dots.innerHTML = "<span></span><span></span><span></span>";

  // Live progress, filled from the partial trace while the worker runs:
  // "Launched MicroVM…", "running code…". Empty until the first step lands.
  const status = document.createElement("div");
  status.className = "thinking-status";

  // The steps so far, rendered with the same markup as the finished trace,
  // so you can follow the agent while it works instead of only afterwards.
  const live = document.createElement("div");
  live.className = "sources-list trace-list trace-live visible";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "cancel-query-btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => _cancelQuery(queryId));

  bubble.appendChild(dots);
  bubble.appendChild(status);
  bubble.appendChild(live);
  bubble.appendChild(cancelBtn);
  row.appendChild(bubble);
  log.appendChild(row);
}

function appendAssistantMessage(text, trace, queryId, files, tokensUsed, elapsedMs) {
  const log   = document.getElementById("chat-log");
  const existing = queryId
    ? log.querySelector(`[data-query-id="${queryId}"]`)
    : null;

  const row = existing || document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  if (queryId) row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  // Trace first, collapsed: while the agent worked its steps were shown at the
  // top of this bubble, so they stay at the top -- folded to one line -- and
  // the answer appears below them instead of the steps jumping to the end.
  if (trace && trace.length > 0) {
    const traceSection = _buildTrace(trace, tokensUsed, elapsedMs);
    traceSection.classList.add("sources-section--top");
    bubble.appendChild(traceSection);
  }

  const body = document.createElement("div");
  body.className = "msg-markdown";
  if (window.marked) {
    const renderer = new window.marked.Renderer();
    renderer.link = ({ href, title, text }) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer" title="${title || href}">${text}</a>`;
    body.innerHTML = window.marked.parse(text, { renderer });
    _foldCodeBlocks(body);
  } else {
    body.innerHTML = text.replace(/\n/g, "<br>");
  }
  bubble.appendChild(body);

  // Files the model showed from its sandbox: the fractal itself, inline.
  if (files && files.length > 0) {
    bubble.appendChild(_buildFiles(files));
  }

  row.innerHTML = "";
  row.appendChild(bubble);

  if (!existing) {
    log.appendChild(row);
  }
}

function appendErrorMessage(text, queryId) {
  const log = document.getElementById("chat-log");
  const existing = queryId
    ? log.querySelector(`[data-query-id="${queryId}"]`)
    : null;

  const row = existing || document.createElement("div");
  row.className = "msg-row msg-row--assistant";
  if (queryId) row.dataset.queryId = queryId;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.style.color = "var(--ring-danger)";
  bubble.textContent = text;

  row.innerHTML = "";
  row.appendChild(bubble);

  if (!existing) log.appendChild(row);
}

/* ---------------------------------------------------------------------------- */
/* Agent trace widget                                                            */
/* Renders the agent's reasoning + tool calls in a collapsible panel. This is    */
/* what makes the demo *teach*: you see the agent decide, call a tool, read the   */
/* result, and answer — the "🔧 called get_costs → 🔧 called list_resources" story.*/
/* ---------------------------------------------------------------------------- */

const _TRACE_ICONS = {
  context:     "📚",
  sandbox:     "🖥️",
  reasoning:   "🧠",
  tool_call:   "🔧",
  tool_result: "📄",
  file:        "🖼️",
  answer:      "✍️",
};

function _esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function _fmtBytes(n) {
  if (!n && n !== 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function _traceStepHtml(step) {
  const icon = _TRACE_ICONS[step.type] || "•";
  if (step.type === "sandbox" || step.type === "context") {
    return `<div class="trace-step"><span class="trace-ic">${icon}</span>
      <span class="trace-sandbox">${_esc(step.text)}</span></div>`;
  }
  if (step.type === "reasoning") {
    return `<div class="trace-step"><span class="trace-ic">${icon}</span>
      <span class="trace-reason">${_esc(step.text)}</span></div>`;
  }
  if (step.type === "tool_call") {
    const input = step.input || {};
    // Code is the interesting part of run_code / run_shell, so it gets a
    // real (folded) block instead of being flattened into key=value args.
    const src  = typeof input.code === "string" ? input.code
               : typeof input.command === "string" ? input.command : null;
    if (src !== null) {
      const lang = step.tool === "run_shell" ? "bash" : "python";
      return `<div class="trace-step trace-step--block"><span class="trace-ic">${icon}</span>
        <div class="trace-body">called <code class="trace-tool">${_esc(step.tool)}</code>
        ${_foldHtml(`${lang} · ${_lineCount(src)}`,
                    `<pre class="trace-code">${_esc(src)}</pre>`)}</div></div>`;
    }
    const args = Object.keys(input).length
      ? "(" + Object.entries(input).map(([k, v]) => `${_esc(k)}=${_esc(v)}`).join(", ") + ")"
      : "()";
    return `<div class="trace-step"><span class="trace-ic">${icon}</span>
      <span>called <code class="trace-tool">${_esc(step.tool)}</code>${args}</span></div>`;
  }
  if (step.type === "tool_result") {
    const cls = step.ok === false ? " trace-result--error" : "";
    const ms = typeof step.ms === "number" ? `<span class="trace-ms">${step.ms} ms</span>` : "";
    const ic = step.ok === false ? "⚠️" : icon;
    const text = String(step.text ?? "");
    // One-liners ("saved", a job id) read fine inline; anything longer is a
    // wall of output that folds, labelled so it can be found again.
    if (text.split("\n").length <= 2 && text.length <= 160) {
      return `<div class="trace-step"><span class="trace-ic">${ic}</span>
        <span class="trace-result${cls}">${_esc(text)}${ms}</span></div>`;
    }
    const label = `${step.ok === false ? "error" : "output"} · ${_lineCount(text)}`;
    return `<div class="trace-step trace-step--block"><span class="trace-ic">${ic}</span>
      <div class="trace-body">${_foldHtml(label,
        `<pre class="trace-code trace-result${cls}">${_esc(text)}</pre>`,
        step.ok === false ? "code-fold--error" : "")}${ms}</div></div>`;
  }
  if (step.type === "file") {
    return `<div class="trace-step"><span class="trace-ic">${icon}</span>
      <span>${step.replaced ? "re-rendered" : "showed"} <code class="trace-tool">${_esc(step.name)}</code>
      ${_esc(step.mime)} · ${_fmtBytes(step.size)}</span></div>`;
  }
  if (step.type === "answer") {
    return `<div class="trace-step"><span class="trace-ic">${icon}</span>
      <span class="trace-answer">answered</span></div>`;
  }
  return "";
}

/* ---------------------------------------------------------------------------- */
/* Code folding                                                                  */
/* Code appears in three places — the final code in an answer, every cell the    */
/* agent ran, and each cell's output — and shown open it buries the answer and  */
/* the image under walls of source. Each block is a native <details>, collapsed */
/* by default, whose summary says what it is and how long.                      */
/* ---------------------------------------------------------------------------- */

function _lineCount(text) {
  const n = String(text).replace(/\n$/, "").split("\n").length;
  return `${n} line${n !== 1 ? "s" : ""}`;
}

function _foldHtml(label, innerHtml, extraClass = "") {
  return `<details class="code-fold ${extraClass}"><summary>${_esc(label)}</summary>${innerHtml}</details>`;
}

/* Wrap each fenced code block of rendered markdown in a collapsed <details>. */
function _foldCodeBlocks(root) {
  root.querySelectorAll("pre").forEach(pre => {
    const code = pre.querySelector("code");
    const lang = (code?.className.match(/language-([\w+-]+)/) || [])[1] || "code";
    const details = document.createElement("details");
    details.className = "code-fold";
    const summary = document.createElement("summary");
    summary.textContent = `${lang} · ${_lineCount(pre.textContent)}`;
    pre.replaceWith(details);
    details.append(summary, pre);
  });
}

function _fmtTokens(n) {
  n = Math.round(Number(n) || 0);
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
}

/* One-line summary of the latest step, for the live status under the dots. */
function _progressText(trace) {
  const step = trace[trace.length - 1];
  if (!step) return "";
  if (step.type === "sandbox")     return step.text;
  if (step.type === "context")     return "Loading the conversation and sandbox state…";
  if (step.type === "tool_call")   return step.tool === "run_code"  ? "Running Python in the sandbox…"
                                       : step.tool === "run_shell" ? "Running a shell command in the sandbox…"
                                       : step.tool === "show_file" ? "Fetching a file from the sandbox…"
                                       : `Calling ${step.tool}…`;
  if (step.type === "tool_result") return step.ok === false ? "Cell failed — the agent is reading the error…"
                                                            : "Reading the output…";
  if (step.type === "file")        return `Showing ${step.name}…`;
  if (step.type === "reasoning")   return "Thinking…";
  return "";
}

/* Files the model showed: raster images inline, everything else a download. */
function _buildFiles(files) {
  const wrap = document.createElement("div");
  wrap.className = "msg-files";
  for (const f of files) {
    if (!f.url) continue;
    if (/^image\/(png|jpeg|gif|webp)$/.test(f.mime || "")) {
      const link = document.createElement("a");
      link.href = f.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = `${f.name} — open full size`;
      const img = document.createElement("img");
      img.className = "msg-file-image";
      img.src = f.url;
      img.alt = f.name || "sandbox output";
      img.loading = "lazy";
      link.appendChild(img);
      wrap.appendChild(link);
    } else if (f.mime === "text/html" || /\.html?$/i.test(f.name || "")) {
      // A page the agent built (a game, say): the API signs it to open
      // inline, so it runs in a new tab instead of downloading.
      const link = document.createElement("a");
      link.className = "msg-file-link";
      link.href = f.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = `▶ Open ${f.name} in a new tab`;
      wrap.appendChild(link);
    } else {
      const link = document.createElement("a");
      link.className = "msg-file-link";
      link.href = f.url;
      link.rel = "noopener noreferrer";
      link.textContent = `⬇ ${f.name} (${_fmtBytes(f.size)})`;
      wrap.appendChild(link);
    }
  }
  return wrap;
}

// Wall time the user waited: sent (created_at) to answer stored (updated_at,
// set by the worker's finalize). Includes queueing and sandbox start-up.
function _elapsedMs(q) {
  const ms = Date.parse(q.updated_at) - Date.parse(q.created_at);
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}

function _fmtElapsed(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function _buildTrace(trace, tokensUsed, elapsedMs) {
  const section = document.createElement("div");
  section.className = "sources-section";

  const toolCalls = trace.filter(s => s.type === "tool_call").length;
  // Tokens the whole query burned: every model turn in the loop, images
  // included. It is the exact amount deducted from the user's budget.
  const tokens = tokensUsed ? ` · ${_fmtTokens(tokensUsed)} tokens` : "";
  const took   = elapsedMs ? ` · ${_fmtElapsed(elapsedMs)}` : "";
  const summary = toolCalls
    ? `Reasoning · ${toolCalls} tool call${toolCalls !== 1 ? "s" : ""}${tokens}${took}`
    : `Reasoning${tokens}${took}`;

  const toggle = document.createElement("button");
  toggle.className = "sources-toggle";
  toggle.innerHTML = `
    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"
         viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
    ${summary}`;

  const list = document.createElement("div");
  list.className = "sources-list trace-list";
  list.innerHTML = trace.map(_traceStepHtml).join("");

  toggle.addEventListener("click", () => {
    const open = list.classList.toggle("visible");
    toggle.classList.toggle("open", open);
  });

  section.appendChild(toggle);
  section.appendChild(list);
  return section;
}

/* ---------------------------------------------------------------------------- */
/* Polling                                                                       */
/* ---------------------------------------------------------------------------- */

function _startPolling(convId, queryId, onComplete) {
  _stopPolling(queryId);
  let attempts = 0;

  const intervalId = setInterval(async () => {
    attempts++;

    // Give up after POLL_MAX_ATTEMPTS — worker likely crashed before updating status
    if (attempts > POLL_MAX_ATTEMPTS) {
      _stopPolling(queryId);
      delete _cancelHandlers[queryId];
      appendErrorMessage("Query timed out. Please try again.", queryId);
      scrollToBottom();
      if (onComplete) onComplete({ status: "failed" });
      return;
    }

    try {
      const q = await getQuery(convId, queryId);

      if (q.status === "complete") {
        _stopPolling(queryId);
        delete _cancelHandlers[queryId];
        appendAssistantMessage(q.answer || "", q.trace || [], queryId, q.files || [],
                               q.tokens_used, _elapsedMs(q));
        scrollToBottom();
        if (onComplete) onComplete(q);
      } else if (q.status === "failed") {
        _stopPolling(queryId);
        delete _cancelHandlers[queryId];
        appendErrorMessage("Query failed. Please try again.", queryId);
        scrollToBottom();
        if (onComplete) onComplete(q);
      } else if (q.trace && q.trace.length) {
        _updateProgress(queryId, q.trace);
      }
    } catch (err) {
      // Transient network error — keep polling until timeout
      console.warn("Poll error for query", queryId, err);
    }
  }, POLL_INTERVAL_MS);

  _activePolls[queryId] = intervalId;
}

function _updateProgress(queryId, trace) {
  const row = document.querySelector(`[data-query-id="${queryId}"]`);
  if (!row) return;
  const status = row.querySelector(".thinking-status");
  if (status) status.textContent = _progressText(trace);

  const live = row.querySelector(".trace-live");
  if (!live) return;

  // Only scroll along if the reader was already at the bottom; someone
  // reading an earlier step should not be yanked down every 2 seconds.
  const log = document.getElementById("chat-log");
  const pinned = log && log.scrollHeight - log.scrollTop - log.clientHeight < 80;

  // Update step by step instead of replacing the list: a fold the reader
  // opened stays open, and the page does not jump. A step is re-rendered
  // only if its markup changed.
  const html = trace.map(_traceStepHtml);
  while (live.children.length > html.length) live.lastElementChild.remove();
  html.forEach((h, i) => {
    let el = live.children[i];
    if (el && el._stepHtml === h) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = h;
    const fresh = tmp.firstElementChild || document.createElement("div");
    fresh._stepHtml = h;
    if (el) el.replaceWith(fresh); else live.appendChild(fresh);
  });

  if (pinned) scrollToBottom();
}

function _stopPolling(queryId) {
  if (_activePolls[queryId]) {
    clearInterval(_activePolls[queryId]);
    delete _activePolls[queryId];
  }
}

function _cancelQuery(queryId) {
  _stopPolling(queryId);
  const onComplete = _cancelHandlers[queryId];
  delete _cancelHandlers[queryId];
  appendErrorMessage("Query cancelled.", queryId);
  scrollToBottom();
  if (onComplete) onComplete({ status: "cancelled" });
}

/* ---------------------------------------------------------------------------- */
/* Scroll helper                                                                 */
/* ---------------------------------------------------------------------------- */

function scrollToBottom() {
  const log = document.getElementById("chat-log");
  log.scrollTop = log.scrollHeight;
}
