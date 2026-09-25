"use strict";
// static/common.js — the one canonical copy of the small helpers that
// index.html, usage.html and phone.html each used to carry for themselves
// (and had drifted apart on). No modules: every page loads
//   <script src="/common.js"></script>
// before its inline script so these globals exist before page code runs.
// Where pages disagreed, index.html's version won; each such call is noted.

// Model display names. phone.html used to abbreviate harder ("K3", "GLM",
// "120B", "V4"); index.html's longer labels are canonical, so the phone page
// now spells model names out too. Retired models stay mapped (and say so) so
// historical usage/harness_runs rows still render a name, never a raw id.
const SHORT = {"Claude-Opus-5.5":"Opus 5.5","GPT-6-Sol":"GPT-6 Sol","GPT-6-Astra":"GPT-6 Astra","GPT-6-Luna":"GPT-6 Luna","Grok-4.7":"Grok 4.7","Gemini-3.8-Flash":"Gemini 3.8 Flash","GLM-5.3":"GLM 5.3","DeepSeek-V4.1-Flash-thinking-max":"DeepSeek V4.1 Flash max","DeepSeek-V4.1-Flash":"DeepSeek V4.1 Flash","Kimi-K3":"Kimi K3 (retired)","gpt-oss-120b":"gpt-oss 120B (retired)","DeepSeek-V4-Flash":"DeepSeek V4 Flash (retired)"};
const short = m => SHORT[m] || m || "—";

// The fleet plans with config.PLANNER_MODEL (GLM-5.3 on the 2026-09-12
// two-model roster). Pages say its name in prompts and thinking states; keep
// that ONE constant here so a roster move is one edit, and so nothing spells
// out a retired model (Kimi-K3) as if it were live.
const PLANNER_SHORT = "GLM 5.3";

// Seconds → "45s" / "12m" / "1.4h". index.html's, verbatim.
const tick = s => { s = Math.round(s||0); if (s < 60) return s+"s"; if (s < 3600) return Math.floor(s/60)+"m"; return (s/3600).toFixed(1)+"h"; };

// Compact numbers: one implementation under both names — index.html calls it
// fmtK, usage.html/phone.html call it fmt. The old fmt()s rendered null as
// "—"/"–" and millions with two decimals; index.html's fmtK (null → "0",
// one decimal) is canonical now.
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n || 0);
const fmt = fmtK;

// HTML-escape user-controlled strings. index.html's replace chain is
// canonical; phone.html's regex version behaved identically, so nothing
// changes there.
const esc = s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Per-family accent colours. index.html has no family map at all, so the
// canonical copy is usage.html's — phone.html's map had the same colours
// minus the "unknown" fallback key, which it never read.
const COLORS = {"gpt-oss":"#3fb950","glm":"#58a6ff","kimi":"#bc8cff","deepseek":"#d29922","kimi-code":"#8b949e","harness":"#e3b341","unknown":"#484f58"};

// Status → colour. index.html's map is canonical: phone.html's old DOT map
// dropped the "done" state and tinted conflicts yellow (#d29922); the
// canonical map keeps "done" and index.html's orange conflict (#f0883e).
const STATUSC = {merged: "#3fb950", done: "#3fb950", running: "#58a6ff", failed: "#f85149", conflict: "#f0883e", pending: "#484f58", skipped: "#6e7681"};
// ---- the dashboard token: ONE copy for every page ---------------------------
// Every POST changes state (a run starts, a task file is written). When the
// server has ARC_DASHBOARD_TOKEN it answers 401 until the request carries it.
// The token is kept in localStorage, which the browser scopes to the ORIGIN:
// http://localhost:8787, http://10.0.0.153:8787 and the WSL-internal
// 172.x address are three separate stores, and the WSL address changes on
// every reboot. That — plus a restart that swapped a token-less copy of the
// server for the unit that has one — is why the operator was asked for the
// token again "after a restart". So:
//   * the header shows whether actions are locked or unlocked (#auth-lock),
//     and clicking it sets or clears the token for this address;
//   * a stored token the server rejects is reported as stale and re-asked
//     once, never retried in a loop;
//   * a link ending in #token=<value> stores it and strips it from the URL
//     (a fragment never reaches the server or its logs).
const TOKEN_KEY = "arc.dashboard.token";
function arcToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function arcSetToken(t) {
  t = String(t == null ? "" : t).trim();
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); }
  catch (e) { /* storage blocked: the prompt asks each time */ }
  return t;
}
function authHeaders() {
  const t = arcToken();
  return t ? {"Authorization": "Bearer " + t} : {};
}
const _where = () => { try { return location.host || "this address"; } catch (e) { return "this address"; } };
// state: "open" (server has no token), "unlocked", "locked", or null (unknown)
let AUTH_STATE = null;
function authPaint(state) {
  if (state) AUTH_STATE = state;
  let el = null;
  try { el = document.querySelector("#auth-lock"); } catch (e) { el = null; }
  if (!el) return AUTH_STATE;
  const s = AUTH_STATE;
  el.textContent = s === "unlocked" ? "actions: unlocked" : s === "locked" ? "actions: locked"
                 : s === "open" ? "actions: no token" : "actions: …";
  el.className = "pill auth-" + (s || "unknown");
  el.title = s === "unlocked" ? "The token saved for " + _where() + " is accepted. Click to change or forget it."
           : s === "locked" ? "Run/stop/retry need the dashboard token (ARC_DASHBOARD_TOKEN). Click to enter it for " + _where() + "."
           : s === "open" ? "The server has no ARC_DASHBOARD_TOKEN: anyone who can reach it can act. Click to save a token anyway."
           : "Checking whether actions need a token…";
  return s;
}
async function authRefresh() {
  try {
    const r = await fetch("/api/auth", {cache: "no-store", headers: authHeaders()});
    const b = await r.json();
    return authPaint(!b.required ? "open" : b.ok ? "unlocked" : "locked");
  } catch (e) { return authPaint(null); }
}
function authAsk(stale) {
  if (typeof prompt !== "function") return null;
  const msg = stale
    ? "The saved dashboard token was rejected — the server's ARC_DASHBOARD_TOKEN changed, or it was mistyped.\nEnter the current token (saved in this browser for " + _where() + "):"
    : "This dashboard requires a token for actions (ARC_DASHBOARD_TOKEN).\nEnter it once; it is saved in this browser for " + _where() + ":";
  const t = prompt(msg);
  return t && String(t).trim() ? String(t).trim() : null;
}
// The lock pill's click: set a token, or submit an empty answer to forget it.
async function authClick() {
  if (typeof prompt !== "function") return;
  const t = prompt("Dashboard token for " + _where() + " (ARC_DASHBOARD_TOKEN).\nLeave empty and press OK to forget the saved one:", "");
  if (t === null) return;              // cancelled: change nothing
  arcSetToken(t);
  return authRefresh();
}
async function jpost(u, body) {
  const send = () => fetch(u, {method: "POST",
    headers: Object.assign({"Content-Type": "application/json"}, authHeaders()),
    body: JSON.stringify(body)});
  let r = await send();
  if (r.status === 401) {
    // A token that was stored and still refused is STALE: say so, ask once,
    // and never keep a token the server just rejected.
    const t = authAsk(!!arcToken());
    if (t) {
      arcSetToken(t);
      r = await send();
      if (r.status === 401) arcSetToken("");
    } else {
      arcSetToken("");
    }
    authPaint(r.status === 401 ? "locked" : "unlocked");
  } else if (r.status < 400 && arcToken()) {
    authPaint("unlocked");
  }
  return {code: r.status, body: await r.json()};
}
// #token=<value> (alone or among other hash params) -> stored, then removed.
(function importTokenFromHash() {
  try {
    const h = new URLSearchParams(String(location.hash || "").slice(1));
    const t = h.get("token");
    if (!t) return;
    arcSetToken(t);
    h.delete("token");
    const rest = h.toString();
    if (typeof history !== "undefined" && history.replaceState) {
      history.replaceState(null, "", location.pathname + location.search + (rest ? "#" + rest : ""));
    } else {
      location.hash = rest;
    }
  } catch (e) { /* no location in this context */ }
})();
