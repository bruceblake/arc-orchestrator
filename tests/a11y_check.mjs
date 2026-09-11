// Accessibility gate for the dashboard's static HTML pages.
//
// node --check and the DOM-reference check verify that the pages PARSE and that
// a script's $("#id") reaches an element that exists. Neither can see that a
// control is unusable to a keyboard/screen-reader user: a clickable <div> with
// no role="button", a control with no accessible name, an <img> with no alt, an
// <input> with no label, or a page that never draws a focus outline.
//
// This scans the static markup (script/style bodies are runtime-generated or
// CSS and are not elements) and each page's own <style> blocks. It fails when
// any of the five checks below trip, printing every finding and a summary.
import fs from "node:fs";

const VOID = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
  "meta", "param", "source", "track", "wbr",
]);

function hasAttr(attrs, name) {
  return new RegExp("(?:^|\\s)" + name + "\\s*=").test(attrs);
}

function getAttr(attrs, name) {
  const m = attrs.match(
    new RegExp("(?:^|\\s)" + name + "\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)')")
  );
  return m ? (m[1] !== undefined ? m[1] : m[2]) : null;
}

function stripTags(s) {
  return s.replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/gi, " ").replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<").replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"').replace(/&#39;/gi, "'")
    .replace(/&#(\d+);/g, (_, n) => {
      const c = parseInt(n, 10);
      return c < 0x10000 ? String.fromCharCode(c) : "";
    })
    .trim();
}

// The raw HTML inside an element, from just after its opening tag to its
// matching close tag, by depth so nested same-name elements do not truncate it.
function elementInner(html, openEnd, name) {
  const tagRe = /<\/?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?>/g;
  tagRe.lastIndex = openEnd;
  let depth = 1, close = -1, m;
  while ((m = tagRe.exec(html)) !== null) {
    const isClose = m[0][1] === "/";
    const n = m[1].toLowerCase();
    if (n !== name) continue;
    if (isClose) { if (--depth === 0) { close = m.index; break; } }
    else depth++;
  }
  return close === -1 ? "" : html.slice(openEnd, close);
}

let bad = 0;
for (const file of process.argv.slice(2)) {
  if (!fs.existsSync(file)) continue;
  const html = fs.readFileSync(file, "utf8");
  const page = file.replace(/^.*\//, "");
  const findings = [];
  const add = msg => findings.push(msg);

  // Check 5 — a page must draw a focus outline somewhere in its own CSS so a
  // keyboard user can see where the focus is.
  const styles = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)]
    .map(m => m[1]).join("\n");
  if (!/:focus-visible\s*\{/i.test(styles)) {
    add("page has no :focus-visible rule in its CSS");
  }

  // Static markup only: runtime-generated DOM cannot be assessed by a regex
  // scanner, and the template strings that build it are full of data-* tokens
  // that would be false "clickable" signals.
  const staticHtml = html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");

  const labelFor = new Set();
  for (const m of staticHtml.matchAll(/<label\b[^>]*\bfor\s*=\s*["']([^"']+)["']/g)) {
    labelFor.add(m[1]);
  }
  const labelBlocks = [];
  for (const m of staticHtml.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)) {
    labelBlocks.push([m.index, m.index + m[0].length]);
  }

  const tagRe = /<([a-zA-Z][a-zA-Z0-9-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let m;
  while ((m = tagRe.exec(staticHtml))) {
    const name = m[1].toLowerCase();
    const attrs = m[2];
    const start = m.index;
    const snippet = m[0];
    const hasRole = /\srole\s*=\s*["']button["']/.test(attrs);
    const clickable =
      /\sonclick\s*=/.test(attrs) || /(\s|^)data-[\w-]+\s*=/.test(attrs);

    // Interactive controls: text-named ones (<button>/<a>/role="button" and
    // any clickable element) plus the form controls <textarea> and <select>,
    // which the original scanner never looked at.
    const interactive =
      name === "button" || name === "a" || name === "textarea" ||
      name === "select" || hasRole || clickable;

    if (interactive) {
      // Check 1 — a clickable control that is not a button/link and has no
      // role="button" is not announced as actionable to assistive tech.
      if (clickable && name !== "button" && name !== "a" && !hasRole) {
        add(`clickable ${snippet} without role="button" or button/link semantics`);
      }
      // Check 2 — an interactive control must have an accessible name. For
      // text-named controls that is its text content, an aria-label or a
      // title; for <textarea>/<select> it is an associated <label>, an
      // aria-label or a title (a placeholder or option text is not a name).
      const id = getAttr(attrs, "id");
      const aria = getAttr(attrs, "aria-label");
      const title = getAttr(attrs, "title");
      let named = !!(aria || title);
      if (!named && (name === "textarea" || name === "select")) {
        const wrapped = labelBlocks.some(([a, b]) => start > a && start < b);
        named = !!wrapped || !!(id && labelFor.has(id));
      } else if (!named && !VOID.has(name)) {
        const inner = elementInner(staticHtml, tagRe.lastIndex, name);
        const text = stripTags(inner);
        // An element whose only content is an <img alt="..."> still has an
        // accessible name from that alt, even though tag-stripping leaves the
        // inner text empty ("<a><img alt=Logo></a>" is named "Logo").
        const imgAlt = inner.match(/<img\b[^>]*\balt\s*=\s*["']([^"']*)["']/i);
        named = !!text || !!(imgAlt && imgAlt[1]);
      }
      if (!named) {
        add(`interactive ${snippet} with no accessible name (no text, aria-label or title)`);
      }
    }

    // Check 3 — an image needs an alternative text.
    if (name === "img" && !hasAttr(attrs, "alt")) {
      add(`${snippet} without an alt attribute`);
    }

    // Check 4 — a form control (input/textarea/select) needs an associated
    // <label> or an aria-label.
    if (name === "input" || name === "textarea" || name === "select") {
      const type = getAttr(attrs, "type");
      const id = getAttr(attrs, "id");
      if (type !== "hidden" && !getAttr(attrs, "aria-label")) {
        const wrapped = labelBlocks.some(([a, b]) => start > a && start < b);
        const associated = id && labelFor.has(id);
        if (!wrapped && !associated) {
          add(`${snippet} with no associated <label> or aria-label`);
        }
      }
    }
  }

  if (findings.length) {
    bad = 1;
    console.log(`FAIL: ${page} has ${findings.length} accessibility issue(s):`);
    for (const f of findings) console.log(`      - ${f}`);
  } else {
    console.log(`OK:   ${page}`);
  }
}
// Keyboard access: the /, ?, g-chord and j/k/Enter shortcuts must dispatch to
// the right dashboard behaviour, and every character binding must be inert while
// the user is typing in a form control (Escape stays available to clear it).
const kbPath = new URL("../static/panels/keyboard.js", import.meta.url);
const kbFindings = [];
const kbok = (name, cond) => { if (!cond) kbFindings.push(name); };
const kbExists = fs.existsSync(kbPath);
kbok("static/panels/keyboard.js exists", kbExists);

// The feature only exists at runtime if the dashboard page actually loads the
// script and ships the CSS the selection/overlay depend on. A test that feeds
// the source straight to new Function().call() cannot catch a forgotten
// <script src> or a missing .kb-sel/.kb-help rule, so check the real page —
// UNCONDITIONALLY. If the keyboard file is ever renamed, moved or deleted this
// must fail, not silently skip the whole binding suite.
const dashPath = process.argv.slice(2).find(f => /\/index\.html$/.test(f));
if (dashPath) {
  const dashHtml = fs.readFileSync(dashPath, "utf8");
  kbok("index.html loads static/panels/keyboard.js",
    /<script[^>]*\bsrc="\/panels\/keyboard\.js"/.test(dashHtml));
  kbok("index.html styles the selection and help overlay (.kb-sel/.kb-help)",
    /\.kb-sel\b/.test(dashHtml) && /\.kb-help\b/.test(dashHtml));
}

if (kbExists) {
  const kbSrc = fs.readFileSync(kbPath, "utf8");
  const kels = new Map();
  const mkEl = (tag, id) => {
    const el = {
      tagName: String(tag).toUpperCase(), id: id || "", className: "", value: "",
      style: {}, dataset: {}, children: [], parentNode: null, focused: false,
      classList: {
        add(c) { el.className = el.className ? el.className + " " + c : c; },
        remove(c) { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
        contains(c) { return el.className.split(/\s+/).includes(c); },
      },
      setAttribute(k, v) { el[k] = v; },
      getAttribute(k) { return el[k] == null ? null : el[k]; },
      appendChild(c) { c.parentNode = el; el.children.push(c); return c; },
      removeChild(c) { const i = el.children.indexOf(c); if (i >= 0) el.children.splice(i, 1); c.parentNode = null; return c; },
      remove() { if (el.parentNode) el.parentNode.removeChild(el); },
      querySelectorAll() { return []; },
      scrollIntoView() { el.scrolled = true; },
      focus() { el.focused = true; },
      blur() { el.focused = false; },
      select() { el.selected = true; },
      dispatchEvent() { return true; },
    };
    return el;
  };
  const q = sel => { const id = String(sel).replace(/^#/, ""); if (!kels.has(id)) kels.set(id, mkEl("div", id)); return kels.get(id); };
  globalThis.$ = q;
  const body = mkEl("body", "body");
  const prows = [0, 1, 2].map(i => { const r = mkEl("div", "prow" + i); r.setAttribute("data-i", String(i)); return r; });
  const projPanel = mkEl("div", "proj-panel");
  const projectsEl = q("#projects");
  projectsEl.closest = () => projPanel;
  projectsEl.querySelectorAll = s => (s === ".prow" ? prows : []);
  const timers = [];
  globalThis.setTimeout = (fn, ms) => { const id = timers.push({ fn, ms }) - 1; return id; };
  globalThis.clearTimeout = id => { if (timers[id]) timers[id].fn = null; };
  globalThis.document = {
    querySelector: q, querySelectorAll: () => [], createElement: t => mkEl(t),
    createTextNode: t => ({ nodeType: 3, textContent: t }),
    addEventListener: () => {}, removeEventListener: () => {}, body,
  };
  globalThis.Event = class Event { constructor(type) { this.type = type; } };
  globalThis.PROJECTS = prows.map((_, i) => ({ file: "proj" + i + ".json" }));
  globalThis.EXPANDED = "";
  const opened = [];
  globalThis.openDetail = f => { opened.push(f); };
  globalThis.closeDetail = () => {};
  new Function(kbSrc)();
  const kb = globalThis.__keyboard;
  const qSearch = () => q("#f-q");

  kb.handleKey({ key: "/", target: body });
  kbok("/ focuses the project search box", qSearch().focused === true);

  kb.handleKey({ key: "?", target: body });
  kbok("? opens the shortcut overlay (one entry per bound key)",
     body.children.length === 1 && body.children[0].className === "kb-help"
     && body.children[0].children.filter(c => c.tagName === "UL")[0].children.length === Object.keys(kb.BINDINGS).length);
  kb.handleKey({ key: "?", target: body });
  kbok("? closes the shortcut overlay", body.children.length === 0);

  q("#health-panel").scrolled = false;
  kb.handleKey({ key: "g", target: body });
  kbok("g arms the two-key chord", kb.chordArmed() === true);
  kb.handleKey({ key: "f", target: body });
  kbok("g f jumps to the fleet panel", q("#health-panel").scrolled === true && kb.chordArmed() === false);

  q("#health-panel").scrolled = false;
  kb.handleKey({ key: "g", target: body });
  const chordTimer = timers[timers.length - 1];
  if (chordTimer && chordTimer.fn) chordTimer.fn();
  kbok("the g chord times out rather than staying armed", kb.chordArmed() === false);
  kb.handleKey({ key: "f", target: body });
  kbok("a timed-out g chord no longer jumps", q("#health-panel").scrolled === false);

  kb.handleKey({ key: "g", target: body });
  kb.handleKey({ key: "p", target: body });
  kbok("g p jumps to the projects panel via its .panel ancestor", projPanel.scrolled === true && kb.chordArmed() === false);

  kb.handleKey({ key: "j", target: body });
  kbok("j selects the first project row", prows[0].classList.contains("kb-sel") && !prows[1].classList.contains("kb-sel"));
  kb.handleKey({ key: "k", target: body });
  kbok("k wraps the selection to the last row", prows[2].classList.contains("kb-sel") && !prows[0].classList.contains("kb-sel"));
  kb.handleKey({ key: "j", target: body });
  kb.handleKey({ key: "Enter", target: body });
  kbok("Enter opens the selected project detail", opened[opened.length - 1] === "proj0.json");

  const selBeforeCtrl = prows[0].classList.contains("kb-sel");
  kb.handleKey({ key: "j", target: body, ctrlKey: true });
  kbok("Ctrl+J is not hijacked and leaves the selection unchanged",
    prows[0].classList.contains("kb-sel") === selBeforeCtrl && !prows[1].classList.contains("kb-sel"));

  qSearch().focused = false;
  qSearch().value = "";
  const inInput = mkEl("input", "");
  kb.handleKey({ key: "/", target: inInput });
  kbok("/ is inert in an input", qSearch().focused === false);
  kb.handleKey({ key: "?", target: inInput });
  kbok("? is inert in an input", body.children.length === 0);
  kb.handleKey({ key: "g", target: inInput });
  kbok("g is inert in an input", kb.chordArmed() === false);
  kb.handleKey({ key: "j", target: inInput });
  kb.handleKey({ key: "j", target: inInput });
  kbok("j is inert in an input (two j presses must not move the selection)",
    prows[0].classList.contains("kb-sel") && !prows[2].classList.contains("kb-sel"));

  const inSelect = mkEl("select", "");
  kb.handleKey({ key: "j", target: inSelect });
  kb.handleKey({ key: "j", target: inSelect });
  kbok("j is inert in a select (two j presses must not move the selection)",
    prows[0].classList.contains("kb-sel") && !prows[2].classList.contains("kb-sel"));

  const openedBefore = opened.length;
  kb.handleKey({ key: "Enter", target: inInput });
  kbok("Enter is inert in an input", opened.length === openedBefore);
  qSearch().value = "abc";
  qSearch().focused = true;
  kb.handleKey({ key: "Escape", target: inInput });
  kbok("Escape clears the search box and returns focus to the page",
    qSearch().value === "" && qSearch().focused === false);

  // The Enter binding must stay out of the way of an element that is already
  // interactive (role="button"): the pre-existing row handler in projects.js
  // fires its own click(), so the global binding must not also open a detail.
  const btnRow = mkEl("div", "prow-btn");
  btnRow.setAttribute("role", "button");
  btnRow.setAttribute("data-i", "1");
  const openedBtnBefore = opened.length;
  kb.handleKey({ key: "Enter", target: btnRow });
  kbok("Enter is inert on an interactive element", opened.length === openedBtnBefore);
  kb.handleKey({ key: "j", target: btnRow });
  kbok("j is inert on an interactive element", !prows[1].classList.contains("kb-sel") && prows[0].classList.contains("kb-sel"));
}

if (kbFindings.length) {
  bad = 1;
  console.log(`FAIL: keyboard shortcuts have ${kbFindings.length} issue(s):`);
  for (const f of kbFindings) console.log(`      - ${f}`);
} else {
  console.log("OK:   keyboard shortcuts");
}

if (bad) {
  console.log("\nAccessibility checks found issues (see above).");
} else {
  console.log("\nAccessibility checks pass on every page.");
}
process.exit(bad);
