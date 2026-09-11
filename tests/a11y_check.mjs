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
if (bad) {
  console.log("\nAccessibility checks found issues (see above).");
} else {
  console.log("\nAccessibility checks pass on every page.");
}
process.exit(bad);
