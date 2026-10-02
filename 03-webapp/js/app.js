/* ============================================================================ */
/* app.js                                                                       */
/* Main application controller.                                                */
/* Initializes auth, loads conversations, handles the chat input loop.        */
/* ============================================================================ */

import { isLoggedIn, getLoginUrl, clearTokens, getUserInfo } from "./auth.js";
import {
  registerUser, getUsage,
  createConversation, listQueries, submitQuery,
} from "./api.js";
import {
  initSidebar, refreshSidebar,
  setActiveConversation, prependConversation, updateConvTitle,
} from "./sidebar.js";
import {
  renderHistory, appendUserBubble,
  appendPendingBubble, stopAllPolls,
} from "./chat.js";
import { showAlert } from "./modal.js";
import { initTheme, setTheme, getTheme } from "./theme.js";

// localStorage key for the desktop sidebar's collapsed state.
const _SIDEBAR_KEY = "sa-sidebar";

/* ---------------------------------------------------------------------------- */
/* Application state                                                             */
/* ---------------------------------------------------------------------------- */

let _activeConvId = null;
let _sending      = false;

/* ---------------------------------------------------------------------------- */
/* Boot                                                                          */
/* ---------------------------------------------------------------------------- */

async function boot() {
  initTheme();

  if (!isLoggedIn()) {
    _showSignIn();
    return;
  }

  // Register user (idempotent — creates usage record on first visit)
  try {
    const reg = await registerUser();
    if (reg?.error === "user_limit_reached") {
      _showSignIn();
      await showAlert(
        "Access unavailable",
        "The demo is currently at capacity. Email mamonaco1973@gmail.com to request access."
      );
      return;
    }
  } catch (err) {
    if (err.status === 403) {
      _showSignIn();
      await showAlert(
        "Access unavailable",
        "The demo is currently at capacity. Email mamonaco1973@gmail.com to request access."
      );
      return;
    }
  }

  // Show app shell; restore the sidebar's collapsed state before it paints
  const appShell = document.getElementById("app-shell");
  if (_storageGet(_SIDEBAR_KEY) === "collapsed") {
    appShell.classList.add("sidebar-collapsed");
  }
  appShell.classList.remove("hidden");

  const { name, email } = getUserInfo();
  const initial     = (name || email || "?")[0].toUpperCase();
  const displayName = name || (email || "").split("@")[0];
  document.getElementById("user-menu-avatar").textContent              = initial;
  document.getElementById("user-menu-popup-avatar").textContent        = initial;
  document.getElementById("user-avatar-collapsed-initial").textContent = initial;
  document.getElementById("user-menu-name").textContent                = displayName;
  document.getElementById("user-menu-email").textContent               = email || "";
  document.getElementById("user-menu-container").classList.remove("hidden");

  // Init sidebar
  initSidebar({
    onSelect: _selectConversation,
    onDelete: _onConversationDeleted,
  });

  // Wire controls
  _wireControls();

  // Load sidebar + token ring behind the spinner, then reveal the empty state
  // (unless a conversation got selected while loading).
  await Promise.all([refreshSidebar(), _refreshUsage()]);
  document.getElementById("boot-spinner").classList.add("hidden");
  if (!_activeConvId) {
    document.getElementById("empty-state").classList.remove("hidden");
  }
}

/* ---------------------------------------------------------------------------- */
/* Sign-in modal                                                                 */
/* ---------------------------------------------------------------------------- */

function _showSignIn() {
  document.getElementById("sign-in-modal").classList.remove("hidden");
  document.getElementById("btn-cognito-sign-in").addEventListener("click", () => {
    window.location.href = getLoginUrl();
  });
}

/* ---------------------------------------------------------------------------- */
/* Control wiring                                                                */
/* ---------------------------------------------------------------------------- */

function _wireControls() {
  // New chat button
  document.getElementById("btn-new-chat").addEventListener("click", _startNewChat);

  // Mobile sidebar toggle
  const toggle  = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  const overlay = document.getElementById("sidebar-overlay");
  if (toggle) {
    toggle.addEventListener("click", () => {
      sidebar.classList.toggle("open");
      overlay.classList.toggle("visible");
    });
    overlay.addEventListener("click", () => {
      sidebar.classList.remove("open");
      overlay.classList.remove("visible");
    });
  }

  // Desktop sidebar collapse/expand, remembered across visits. In the mobile
  // drawer the collapse button is hidden by CSS.
  const appShell = document.getElementById("app-shell");
  function _setSidebarCollapsed(collapsed) {
    appShell.classList.toggle("sidebar-collapsed", collapsed);
    _storageSet(_SIDEBAR_KEY, collapsed ? "collapsed" : "open");
  }
  document.getElementById("btn-sidebar-collapse")
    .addEventListener("click", () => _setSidebarCollapsed(true));
  document.getElementById("btn-sidebar-open")
    .addEventListener("click", () => _setSidebarCollapsed(false));
  document.getElementById("btn-new-chat-collapsed")
    .addEventListener("click", _startNewChat);

  _wireUserMenu();
  _wireSettings();

  // Send button
  document.getElementById("btn-send").addEventListener("click", _handleSend);

  // Enter to send (Shift+Enter for newline)
  document.getElementById("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      _handleSend();
    }
  });

  // Auto-resize textarea
  document.getElementById("chat-input").addEventListener("input", _autoResize);

  // Starter question buttons
  document.querySelectorAll(".starter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.getElementById("chat-input").value = btn.dataset.q;
      _autoResize();
      _handleSend();
    });
  });
}

/* ---------------------------------------------------------------------------- */
/* User menu — avatar + token ring trigger, popup with Help / Settings / Sign out */
/* ---------------------------------------------------------------------------- */

function _wireUserMenu() {
  const trigger     = document.getElementById("user-menu-trigger");
  const popup       = document.getElementById("user-menu-popup");
  const helpToggle  = document.getElementById("user-menu-help-toggle");
  const helpSubmenu = document.getElementById("help-submenu");

  function close() {
    popup.classList.add("hidden");
    popup.classList.remove("popup-anchored-collapsed");
    trigger.classList.remove("open");
    // Collapse Help so it starts closed next time the menu opens
    helpSubmenu.classList.add("hidden");
    helpToggle.classList.remove("open");
  }

  function toggle(anchoredCollapsed) {
    const opening = popup.classList.contains("hidden");
    if (!opening) { close(); return; }
    popup.classList.remove("hidden");
    // With the sidebar collapsed the popup's usual anchor is off-screen, so it
    // is pinned beside the floating avatar instead.
    popup.classList.toggle("popup-anchored-collapsed", anchoredCollapsed);
    trigger.classList.add("open");
  }

  trigger.addEventListener("click", (e) => { e.stopPropagation(); toggle(false); });
  document.getElementById("user-avatar-collapsed")
    .addEventListener("click", (e) => { e.stopPropagation(); toggle(true); });

  // Clicks inside the popup don't bubble up to the document and close it
  popup.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", close);

  helpToggle.addEventListener("click", () => {
    const opening = helpSubmenu.classList.contains("hidden");
    helpSubmenu.classList.toggle("hidden", !opening);
    helpToggle.classList.toggle("open", opening);
  });
  helpSubmenu.querySelectorAll(".user-menu-subitem")
    .forEach(link => link.addEventListener("click", close));

  document.getElementById("user-menu-settings").addEventListener("click", () => {
    close();
    _refreshThemePicker();
    document.getElementById("settings-modal").classList.remove("hidden");
  });

  document.getElementById("user-menu-signout").addEventListener("click", () => {
    clearTokens();
    window.location.reload();
  });
}

/* ---------------------------------------------------------------------------- */
/* Settings dialog — Appearance (theme)                                          */
/* ---------------------------------------------------------------------------- */

function _wireSettings() {
  const modal = document.getElementById("settings-modal");
  document.getElementById("settings-close")
    .addEventListener("click", () => modal.classList.add("hidden"));
  // Clicking the dimmed backdrop closes it too, but not clicks in the dialog
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });

  // Applied immediately on click — there is no Save step
  document.querySelectorAll(".theme-option").forEach(btn => {
    btn.addEventListener("click", () => {
      setTheme(btn.dataset.theme);
      _refreshThemePicker();
    });
  });
}

function _refreshThemePicker() {
  const current = getTheme();
  document.querySelectorAll(".theme-option").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.theme === current);
  });
}

/* ---------------------------------------------------------------------------- */
/* New chat                                                                      */
/* ---------------------------------------------------------------------------- */

function _closeMobileSidebar() {
  document.getElementById("sidebar")?.classList.remove("open");
  document.getElementById("sidebar-overlay")?.classList.remove("visible");
}

async function _startNewChat() {
  stopAllPolls();
  _setSending(false);
  _closeMobileSidebar();

  try {
    const conv = await createConversation();
    _activeConvId = conv.conv_id;

    prependConversation(conv);
    setActiveConversation(_activeConvId);

    // Show empty state, hide log
    document.getElementById("empty-state").classList.remove("hidden");
    document.getElementById("chat-log").classList.add("hidden");
    document.getElementById("chat-log").innerHTML = "";

    document.getElementById("chat-input").focus();
  } catch (err) {
    console.error("Failed to create conversation", err);
    await showAlert("Error", "Failed to start a new chat. Please try again.");
  }
}

/* ---------------------------------------------------------------------------- */
/* Select existing conversation                                                  */
/* ---------------------------------------------------------------------------- */

async function _selectConversation(convId) {
  if (convId === _activeConvId) return;

  stopAllPolls();
  _setSending(false);
  _closeMobileSidebar();
  _activeConvId = convId;
  setActiveConversation(convId);

  // Show log, hide empty state (and the boot spinner, if this click beat it)
  document.getElementById("boot-spinner").classList.add("hidden");
  document.getElementById("empty-state").classList.add("hidden");
  document.getElementById("chat-log").classList.remove("hidden");
  document.getElementById("chat-log").innerHTML = "";

  try {
    const queries = await listQueries(convId);
    renderHistory(queries);

    // Re-attach polls for any still-pending queries
    for (const q of queries) {
      if (q.status === "pending" || q.status === "processing") {
        appendPendingBubble(convId, q.query_id, (completed) => {
          _refreshUsage();
        });
      }
    }
  } catch (err) {
    console.error("Failed to load queries", err);
  }
}

/* ---------------------------------------------------------------------------- */
/* Handle send                                                                   */
/* ---------------------------------------------------------------------------- */

async function _handleSend() {
  if (_sending) return;

  const input    = document.getElementById("chat-input");
  const errorEl  = document.getElementById("input-error");
  const question = input.value.trim();

  errorEl.classList.add("hidden");

  if (!question) return;

  // Create a conversation if none is active
  if (!_activeConvId) {
    try {
      const conv = await createConversation();
      _activeConvId = conv.conv_id;
      prependConversation(conv);
      setActiveConversation(_activeConvId);
    } catch (err) {
      errorEl.textContent = "Failed to start conversation. Please try again.";
      errorEl.classList.remove("hidden");
      return;
    }
  }

  // Switch from empty state to chat log
  document.getElementById("empty-state").classList.add("hidden");
  const log = document.getElementById("chat-log");
  log.classList.remove("hidden");

  // Clear and lock input
  input.value = "";
  _autoResize();
  _setSending(true);

  // Append user bubble immediately
  appendUserBubble(question);

  try {
    const result = await submitQuery(_activeConvId, question);

    appendPendingBubble(_activeConvId, result.query_id, (completed) => {
      _setSending(false);
      _refreshUsage();
      if (completed.status === "complete") {
        // Refresh sidebar to pick up auto-generated title on first query
        refreshSidebar().then(() => setActiveConversation(_activeConvId));
      }
    });
  } catch (err) {
    _setSending(false);

    if (err.status === 503) {
      await showAlert(
        "Content not available",
        "The portfolio knowledge base hasn't been loaded yet. Please check back soon."
      );
    } else if (err.status === 429) {
      await showAlert(
        "Token limit reached",
        "You have used your full token budget. Email mamonaco1973@gmail.com to request a reset."
      );
    } else {
      errorEl.textContent = "Failed to send. Please try again.";
      errorEl.classList.remove("hidden");
    }
  }
}

/* ---------------------------------------------------------------------------- */
/* Conversation deleted callback                                                 */
/* ---------------------------------------------------------------------------- */

function _onConversationDeleted(convId) {
  if (convId === _activeConvId) {
    _activeConvId = null;
    document.getElementById("chat-log").innerHTML = "";
    document.getElementById("chat-log").classList.add("hidden");
    document.getElementById("empty-state").classList.remove("hidden");
  }
}

/* ---------------------------------------------------------------------------- */
/* Token usage ring                                                              */
/* ---------------------------------------------------------------------------- */

async function _refreshUsage() {
  try {
    const usage = await getUsage();
    const used  = usage.tokens_used || 0;
    const limit = usage.token_limit || 1_000_000;
    const pct   = Math.min(100, Math.round((used / limit) * 100));

    const stroke = pct >= 90 ? "var(--ring-danger)"
                 : pct >= 70 ? "var(--ring-warn)"
                 : "var(--ring-color)";
    // At a full ring the two round end-caps overlap into a bump at 12 o'clock;
    // butt caps close it cleanly. Round looks nicer for a partial fill.
    const cap = pct >= 100 ? "butt" : "round";

    // The sidebar avatar and the collapsed-sidebar avatar show the same ring
    for (const id of ["token-ring-arc", "token-ring-arc-collapsed"]) {
      const arc = document.getElementById(id);
      if (!arc) continue;
      arc.setAttribute("stroke-dasharray", `${pct} ${100 - pct}`);
      arc.style.stroke = stroke;
      arc.style.strokeLinecap = cap;
    }

    document.getElementById("token-usage-label").textContent =
      `${_fmtTok(used)} / ${_fmtTok(limit)} tokens`;
  } catch (err) {
    console.warn("Failed to fetch usage", err);
  }
}

function _fmtTok(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000)     return `${Math.round(n / 1_000)}K`;
  return String(n);
}

/* ---------------------------------------------------------------------------- */
/* Helpers                                                                       */
/* ---------------------------------------------------------------------------- */

function _setSending(active) {
  _sending = active;
  document.getElementById("btn-send").disabled       = active;
  document.getElementById("chat-input").disabled     = active;
}

// Storage can be blocked (private windows, strict settings); the UI then just
// forgets preferences between visits instead of failing to boot.
function _storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function _storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* not persisted */ }
}

function _autoResize() {
  const ta = document.getElementById("chat-input");
  ta.style.height = "auto";
  ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
}

/* ---------------------------------------------------------------------------- */
/* Entry point                                                                   */
/* ---------------------------------------------------------------------------- */

boot();
