"use strict";

const KB_PANEL_SEL = { f: "#health-panel", s: "#slots-panel", a: "#agents-panel", h: "#gh-panel", p: "#projects" };
const KB_CHORD_TIMEOUT = 1000;

let kbChordKey = null;
let kbChordTimer = null;
let kbHelpBox = null;
let kbSelIndex = -1;

function kbIsTyping(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName ? String(el.tagName).toLowerCase() : "";
  return tag === "input" || tag === "textarea";
}

function kbIsInteractive(el) {
  if (kbIsTyping(el)) return true;
  if (!el) return false;
  const tag = el.tagName ? String(el.tagName).toLowerCase() : "";
  if (tag === "button" || tag === "a" || tag === "select") return true;
  const role = el.getAttribute ? el.getAttribute("role") : null;
  return role === "button";
}

function kbFocusSearch() {
  const q = $("#f-q");
  if (!q) return false;
  if (q.focus) q.focus();
  if (q.select) q.select();
  return true;
}

function kbClearSearch() {
  const q = $("#f-q");
  if (!q) return false;
  q.value = "";
  if (typeof Event === "function" && q.dispatchEvent) q.dispatchEvent(new Event("input"));
  if (q.blur) q.blur();
  return true;
}

function kbJump(key) {
  let el = null;
  if (key === "p") {
    const c = $("#projects");
    el = c && c.closest ? c.closest(".panel") : null;
  } else {
    el = $(KB_PANEL_SEL[key]);
  }
  if (!el) return false;
  if (el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
  if (el.style) {
    el.style.boxShadow = "inset 0 0 0 2px var(--accent)";
    if (typeof setTimeout === "function") {
      setTimeout(() => { el.style.boxShadow = ""; }, 1200);
    }
  }
  return true;
}

function kbArmChord() {
  kbCancelChord();
  kbChordKey = "g";
  if (typeof setTimeout === "function") {
    kbChordTimer = setTimeout(kbCancelChord, KB_CHORD_TIMEOUT);
  }
  return true;
}

function kbCancelChord() {
  kbChordKey = null;
  if (kbChordTimer !== null && typeof clearTimeout === "function") {
    clearTimeout(kbChordTimer);
    kbChordTimer = null;
  }
}

function kbChordArmed() {
  return !!kbChordKey;
}

function kbToggleHelp() {
  if (kbHelpBox && kbHelpBox.parentNode) {
    kbCloseHelp();
    return false;
  }
  kbHelpBox = kbRenderHelp();
  return true;
}

function kbCloseHelp() {
  if (kbHelpBox && kbHelpBox.parentNode) kbHelpBox.remove();
  kbHelpBox = null;
}

function kbRenderHelp() {
  const box = document.createElement("div");
  box.className = "kb-help";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-label", "keyboard shortcuts");
  const heading = document.createElement("h3");
  heading.textContent = "keyboard shortcuts";
  box.appendChild(heading);
  const list = document.createElement("ul");
  for (const key of Object.keys(KB_BINDINGS)) {
    const li = document.createElement("li");
    const kbd = document.createElement("kbd");
    kbd.textContent = key;
    li.appendChild(kbd);
    li.appendChild(document.createTextNode(" " + KB_BINDINGS[key].label));
    list.appendChild(li);
  }
  box.appendChild(list);
  document.body.appendChild(box);
  return box;
}

function kbRows() {
  const c = $("#projects");
  if (!c || !c.querySelectorAll) return [];
  return Array.from(c.querySelectorAll(".prow"));
}

function kbPaint(list) {
  list.forEach((r, i) => {
    if (i === kbSelIndex) {
      r.classList.add("kb-sel");
      if (r.scrollIntoView) r.scrollIntoView({ block: "nearest" });
    } else {
      r.classList.remove("kb-sel");
    }
  });
}

function kbMove(delta) {
  const list = kbRows();
  if (!list.length) return false;
  if (kbSelIndex < 0 || kbSelIndex >= list.length) kbSelIndex = delta > 0 ? 0 : list.length - 1;
  else kbSelIndex = (kbSelIndex + delta + list.length) % list.length;
  kbPaint(list);
  return true;
}

function kbOpenSelected() {
  const list = kbRows();
  const r = list[kbSelIndex];
  if (!r) return false;
  const raw = r.getAttribute ? r.getAttribute("data-i") : null;
  if (raw == null) return false;
  const p = (typeof PROJECTS !== "undefined" && PROJECTS) ? PROJECTS[+raw] : null;
  if (!p || !p.file) return false;
  const expandable = typeof openDetail === "function" && typeof closeDetail === "function";
  if (expandable && typeof EXPANDED !== "undefined" && EXPANDED === p.file) closeDetail();
  else if (typeof openDetail === "function") openDetail(p.file);
  return true;
}

function kbHandleKey(e) {
  if (!e || !e.key) return false;
  const key = e.key;
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  if (key === "Escape") {
    if (e.preventDefault) e.preventDefault();
    kbCloseHelp();
    kbClearSearch();
    return true;
  }
  if (kbIsInteractive(e.target)) {
    kbCancelChord();
    return false;
  }
  if (kbChordKey) {
    const k = String(key).toLowerCase();
    const ok = KB_PANEL_SEL[k];
    kbCancelChord();
    if (ok) {
      if (e.preventDefault) e.preventDefault();
      return kbJump(k);
    }
    return false;
  }
  const hit = KB_BINDINGS[key];
  if (hit) {
    if (e.preventDefault) e.preventDefault();
    hit.run(e);
    return true;
  }
  return false;
}

const KB_BINDINGS = {
  "/": { label: "focus the project search box", run: () => kbFocusSearch() },
  "?": { label: "toggle keyboard shortcuts", run: () => kbToggleHelp() },
  "g": { label: "jump to a panel (g f/s/a/h/p)", run: () => kbArmChord() },
  "j": { label: "move down the project list", run: () => kbMove(1) },
  "k": { label: "move up the project list", run: () => kbMove(-1) },
  "Enter": { label: "open the selected project", run: () => kbOpenSelected() },
  "Escape": { label: "clear the project search box", run: () => kbClearSearch() },
};

globalThis.__keyboard = {
  handleKey: kbHandleKey, isTyping: kbIsTyping, isInteractive: kbIsInteractive,
  focusSearch: kbFocusSearch,
  clearSearch: kbClearSearch, jump: kbJump, armChord: kbArmChord,
  cancelChord: kbCancelChord, chordArmed: kbChordArmed,
  toggleHelp: kbToggleHelp, closeHelp: kbCloseHelp, renderHelp: kbRenderHelp,
  move: kbMove, openSelected: kbOpenSelected,
  CHORD_TIMEOUT: KB_CHORD_TIMEOUT, BINDINGS: KB_BINDINGS,
};

if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("keydown", kbHandleKey);
}
