/* ============================================================================ */
/* api.js                                                                       */
/* HTTP client for the MicroVM Agent Sandbox backend API.                     */
/* All requests include the Cognito JWT Bearer token from localStorage.        */
/* ============================================================================ */

import { CONFIG } from "./config.js";
import { getAccessToken, isTokenExpired, refreshTokens, clearTokens } from "./auth.js";

const BASE = CONFIG.API_BASE_URL;

/* ---------------------------------------------------------------------------- */
/* Internal fetch wrapper                                                        */
/* Silently refreshes the access token if expired before sending the request.   */
/* On a 401 response, attempts one refresh and retries before giving up.        */
/* ---------------------------------------------------------------------------- */

async function apiFetch(method, path, body, _retry = false) {
  // Proactively refresh if the token is expired or about to expire
  if (isTokenExpired(getAccessToken())) {
    const ok = await refreshTokens();
    if (!ok) {
      clearTokens();
      window.location.reload();
      return;
    }
  }

  const token = getAccessToken();
  const opts = {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  };

  const res = await fetch(`${BASE}${path}`, opts);

  // On 401, attempt one silent refresh and retry
  if (res.status === 401 && !_retry) {
    const ok = await refreshTokens();
    if (!ok) {
      clearTokens();
      window.location.reload();
      return;
    }
    return apiFetch(method, path, body, true);
  }

  const data = await res.json();

  if (!res.ok) {
    const err = new Error(data?.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data   = data;
    throw err;
  }

  return data;
}

/* ---------------------------------------------------------------------------- */
/* User management                                                               */
/* ---------------------------------------------------------------------------- */

export function registerUser()      { return apiFetch("POST", "/register"); }
export function getUsage()          { return apiFetch("GET",  "/usage"); }

/* ---------------------------------------------------------------------------- */
/* Conversations                                                                 */
/* ---------------------------------------------------------------------------- */

export function listConversations()      { return apiFetch("GET",    "/conversations"); }
export function createConversation()     { return apiFetch("POST",   "/conversations"); }
export function deleteConversation(id)   { return apiFetch("DELETE", `/conversations/${id}`); }

/* ---------------------------------------------------------------------------- */
/* Queries                                                                       */
/* ---------------------------------------------------------------------------- */

export function listQueries(convId) {
  return apiFetch("GET", `/conversations/${convId}/queries`);
}

export function submitQuery(convId, question) {
  return apiFetch("POST", `/conversations/${convId}/queries`, { question });
}

export function getQuery(convId, queryId) {
  return apiFetch("GET", `/conversations/${convId}/queries/${queryId}`);
}
