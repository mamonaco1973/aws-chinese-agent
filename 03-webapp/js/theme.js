/* ============================================================================ */
/* theme.js                                                                     */
/* Manages light / dark / system theme preference via localStorage.            */
/* The same key is read by the inline script in index.html's <head>, which     */
/* applies the theme before first paint so the page never flashes light.       */
/* ============================================================================ */

const _KEY = "sa-theme";

function _read() {
  // Storage can be blocked (private windows, strict settings); fall back to
  // following the OS rather than failing the whole app.
  try { return localStorage.getItem(_KEY) || "system"; } catch { return "system"; }
}

function _resolve(pref) {
  if (pref === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark" : "light";
  }
  return pref;
}

function _apply(pref) {
  document.documentElement.setAttribute("data-theme", _resolve(pref));
}

export function initTheme() {
  _apply(_read());
  // Re-apply when the OS setting changes — only matters while following it
  window.matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => {
      if (_read() === "system") _apply("system");
    });
}

export function setTheme(pref) {
  try { localStorage.setItem(_KEY, pref); } catch { /* applies for this page only */ }
  _apply(pref);
}

export function getTheme() {
  return _read();
}
