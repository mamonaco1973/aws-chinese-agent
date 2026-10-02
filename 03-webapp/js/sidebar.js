/* ============================================================================ */
/* sidebar.js                                                                   */
/* Renders and manages the conversation list in the left sidebar.              */
/* ============================================================================ */

import { listConversations, deleteConversation } from "./api.js";
import { showConfirm } from "./modal.js";

/* ---------------------------------------------------------------------------- */
/* Module state                                                                  */
/* ---------------------------------------------------------------------------- */

let _conversations = [];
let _activeConvId  = null;
let _onSelect      = null;
let _onDelete      = null;

/* ---------------------------------------------------------------------------- */
/* Public init — wire up callbacks                                               */
/* ---------------------------------------------------------------------------- */

export function initSidebar({ onSelect, onDelete }) {
  _onSelect = onSelect;
  _onDelete = onDelete;
}

/* ---------------------------------------------------------------------------- */
/* Public: load and render conversation list                                     */
/* ---------------------------------------------------------------------------- */

export async function refreshSidebar() {
  try {
    _conversations = await listConversations();
  } catch (err) {
    console.error("Failed to load conversations", err);
    _conversations = [];
  }
  _renderList();
}

/* ---------------------------------------------------------------------------- */
/* Public: mark a conversation active in the sidebar                            */
/* ---------------------------------------------------------------------------- */

export function setActiveConversation(convId) {
  _activeConvId = convId;
  _renderList();
}

/* ---------------------------------------------------------------------------- */
/* Public: prepend a newly created conversation to the top of the list         */
/* ---------------------------------------------------------------------------- */

export function prependConversation(conv) {
  _conversations = [conv, ..._conversations.filter(c => c.conv_id !== conv.conv_id)];
  _renderList();
}

/* ---------------------------------------------------------------------------- */
/* Public: update a conversation's title in place (after first query answer)   */
/* ---------------------------------------------------------------------------- */

export function updateConvTitle(convId, title) {
  const conv = _conversations.find(c => c.conv_id === convId);
  if (conv) {
    conv.title = title;
    _renderList();
  }
}

/* ---------------------------------------------------------------------------- */
/* Rendering                                                                     */
/* ---------------------------------------------------------------------------- */

function _renderList() {
  const nav = document.getElementById("conv-list");
  nav.innerHTML = "";

  if (_conversations.length === 0) return;

  const groups = _groupByRecency(_conversations);

  for (const [label, items] of groups) {
    if (items.length === 0) continue;

    const groupLabel = document.createElement("div");
    groupLabel.className = "conv-group-label";
    groupLabel.textContent = label;
    nav.appendChild(groupLabel);

    for (const conv of items) {
      nav.appendChild(_buildItem(conv));
    }
  }
}

function _buildItem(conv) {
  const item = document.createElement("div");
  item.className = "conv-item" + (conv.conv_id === _activeConvId ? " active" : "");
  item.dataset.convId = conv.conv_id;
  item.title = conv.title || "Untitled";

  const title = document.createElement("span");
  title.className = "conv-item-title";
  title.textContent = conv.title || "Untitled";

  const delBtn = document.createElement("button");
  delBtn.className = "conv-delete-btn";
  delBtn.title = "Delete conversation";
  delBtn.innerHTML = `
    <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13"
         viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
    </svg>`;

  delBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const confirmed = await showConfirm(
      "Delete conversation",
      "This will permanently delete this conversation and all its messages."
    );
    if (!confirmed) return;

    try {
      await deleteConversation(conv.conv_id);
      _conversations = _conversations.filter(c => c.conv_id !== conv.conv_id);
      _renderList();
      if (_onDelete) _onDelete(conv.conv_id);
    } catch (err) {
      console.error("Failed to delete conversation", err);
    }
  });

  item.appendChild(title);
  item.appendChild(delBtn);

  item.addEventListener("click", () => {
    if (_onSelect) _onSelect(conv.conv_id);
  });

  return item;
}

/* ---------------------------------------------------------------------------- */
/* Group conversations by recency                                                */
/* ---------------------------------------------------------------------------- */

function _groupByRecency(convs) {
  const now    = new Date();
  const today  = _dayStart(now);
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const week7  = new Date(today);   week7.setDate(today.getDate() - 7);
  const week30 = new Date(today);   week30.setDate(today.getDate() - 30);

  const groups = [
    ["Today",        []],
    ["Yesterday",    []],
    ["Last 7 days",  []],
    ["Last 30 days", []],
    ["Older",        []],
  ];

  for (const conv of convs) {
    // Only a chat created moments ago can lack a timestamp, so treat that as
    // now; the epoch fallback this replaced filed new chats under "Older".
    const d = new Date(conv.updated_at || conv.created_at || Date.now());
    if (d >= today)        groups[0][1].push(conv);
    else if (d >= yesterday) groups[1][1].push(conv);
    else if (d >= week7)   groups[2][1].push(conv);
    else if (d >= week30)  groups[3][1].push(conv);
    else                   groups[4][1].push(conv);
  }

  return groups;
}

function _dayStart(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}
