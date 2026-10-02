/* ========================================================================== */
/* auth.js                                                                     */
/* Cognito OAuth2 helpers: build login/logout URLs, exchange authorization    */
/* codes for tokens, and manage token storage in localStorage.                */
/* ========================================================================== */

// -----------------------------------------------------------------------------
// Cognito configuration
// -----------------------------------------------------------------------------

import { CONFIG } from "./config.js";

const COGNITO_DOMAIN = CONFIG.COGNITO_DOMAIN;
const CLIENT_ID = CONFIG.COGNITO_CLIENT_ID;

const REDIRECT_URI = `${window.location.origin}/callback.html`;

// -----------------------------------------------------------------------------
// Build Cognito Hosted UI login URL
// -----------------------------------------------------------------------------

export function getLoginUrl() {
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: REDIRECT_URI
  });

  return `${COGNITO_DOMAIN}/oauth2/authorize?${params.toString()}`;
}

// -----------------------------------------------------------------------------
// Exchange authorization code for tokens
// -----------------------------------------------------------------------------

export async function exchangeCodeForTokens(code) {
  const tokenUrl = `${COGNITO_DOMAIN}/oauth2/token`;

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CLIENT_ID,
    code,
    redirect_uri: REDIRECT_URI
  });

  const response = await fetch(tokenUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: body.toString()
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(
      `Token exchange failed: ${response.status} ${errorText}`
    );
  }

  return response.json();
}

// -----------------------------------------------------------------------------
// Token storage
// -----------------------------------------------------------------------------

export function storeTokens(tokens) {
  localStorage.setItem("id_token", tokens.id_token || "");
  localStorage.setItem("access_token", tokens.access_token || "");
  localStorage.setItem("refresh_token", tokens.refresh_token || "");
}

// -----------------------------------------------------------------------------
// Token retrieval
// -----------------------------------------------------------------------------

export function getIdToken() {
  return localStorage.getItem("id_token") || "";
}

export function getAccessToken() {
  return localStorage.getItem("access_token") || "";
}

export function getRefreshToken() {
  return localStorage.getItem("refresh_token") || "";
}

// -----------------------------------------------------------------------------
// JWT helpers — decode payload and check expiration without a library
// -----------------------------------------------------------------------------

function decodeJwtPayload(token) {
  try {
    const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64));
  } catch (_) {
    return null;
  }
}

// Returns true if the token is missing or within 30 seconds of expiring.
export function isTokenExpired(token) {
  if (!token) return true;
  const payload = decodeJwtPayload(token);
  if (!payload || !payload.exp) return true;
  return Date.now() / 1000 >= payload.exp - 30;
}

// -----------------------------------------------------------------------------
// Silent token refresh using the Cognito refresh_token grant
// Returns true on success, false if the refresh token is missing or rejected.
// Note: Cognito does not issue a new refresh_token in the response.
// -----------------------------------------------------------------------------

export async function refreshTokens() {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: CLIENT_ID,
    refresh_token: refreshToken
  });

  try {
    const response = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString()
    });

    if (!response.ok) return false;

    const tokens = await response.json();
    localStorage.setItem("id_token", tokens.id_token || "");
    localStorage.setItem("access_token", tokens.access_token || "");
    return true;
  } catch (_) {
    return false;
  }
}

// -----------------------------------------------------------------------------
// Session helpers
// -----------------------------------------------------------------------------

// Returns display name and email from the id_token payload.
export function getUserInfo() {
  const payload = decodeJwtPayload(getIdToken());
  if (!payload) return { name: "", email: "" };
  return {
    name:  payload.name || payload.given_name || "",
    email: payload.email || ""
  };
}

// Checks both presence and expiration; does not attempt an async refresh.
export function isLoggedIn() {
  const token = getIdToken();
  return Boolean(token) && !isTokenExpired(token);
}

export function clearTokens() {
  localStorage.removeItem("id_token");
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
}

export function getPostLoginRedirectUrl() {
  return `${window.location.origin}/index.html`;
}

export function getLogoutUrl() {
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    logout_uri: `${window.location.origin}/index.html`
  });

  return `${COGNITO_DOMAIN}/logout?${params.toString()}`;
}
