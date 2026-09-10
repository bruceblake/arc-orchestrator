"use strict";
// static/common.js — the one canonical copy of the small helpers that
// index.html, usage.html and phone.html each used to carry for themselves
// (and had drifted apart on). No modules: every page loads
//   <script src="/common.js"></script>
// before its inline script so these globals exist before page code runs.
// Where pages disagreed, index.html's version won; each such call is noted.

// Model display names. phone.html used to abbreviate harder ("K3", "GLM",
// "120B", "V4"); index.html's longer labels are canonical, so the phone page
// now spells model names out too.
const SHORT = {"Kimi-K3":"Kimi K3","GLM-5.3":"GLM 5.3","gpt-oss-120b":"gpt-oss 120B","DeepSeek-V4-Flash":"DeepSeek V4 Flash"};
const short = m => SHORT[m] || m || "—";

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
const STATUSC = {merged: "#3fb950", done: "#3fb950", running: "#58a6ff", failed: "#f85149", conflict: "#f0883e", pending: "#484f58"};