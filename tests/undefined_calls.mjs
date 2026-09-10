// Flag calls to functions that are never defined anywhere the page can see.
//
// node --check only parses; a call to a function that does not exist is
// perfectly valid syntax and fails at RUNTIME, on click. That is exactly how
// loadProjects() shipped: called at five sites, defined nowhere, so every
// archive and phase-filter control threw ReferenceError the moment it was
// used. check.sh's DOM-reference check could not see it.
import fs from "node:fs";

const BUILTINS = new Set([
  "require","fetch","alert","confirm","prompt","setTimeout","setInterval",
  "clearInterval","clearTimeout","parseInt","parseFloat","isNaN","String",
  "Number","Boolean","Array","Object","JSON","Math","Date","Promise","Map",
  "Set","RegExp","Error","encodeURIComponent","decodeURIComponent","btoa",
  "atob","structuredClone","queueMicrotask","URLSearchParams","URL","if",
  "for","while","switch","catch","return","typeof","function","await","new",
  "console","document","window","localStorage","history","location",
  // keywords that a naive `name(` scan sees as calls
  "async","of","in","var","let","const","do","else","try","finally","yield",
  "delete","void","instanceof","case","throw",
]);


// Comments and string bodies are full of words followed by "(" — "filters(",
// "var(", "of(" — so they must go before anything is matched. Template
// interpolations are kept: ${esc(x)} is real code.
function lastMeaningful(out) {
  for (let k = out.length - 1; k >= 0; k--) {
    if (!/\s/.test(out[k])) return out[k];
  }
  return "";
}

function strip(src) {
  let out = "", i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i], d = src[i + 1];
    if (c === "/" && d === "/") { while (i < n && src[i] !== "\n") i++; continue; }
    if (c === "/" && d === "*") { i += 2; while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++; i += 2; continue; }
    if (c === '"' || c === "'") {
      const q = c; i++;
      while (i < n && src[i] !== q) { if (src[i] === "\\") i++; i++; }
      i++; out += ' ""'; continue;
    }
    // Regex literals: /-x(\d+)$/ looks exactly like a call to x(). A "/" starts
    // a regex when the last meaningful character cannot end an expression.
    if (c === "/" && /[(,=:[!&|?{};+\-*%<>~^\n]|^$/.test(lastMeaningful(out))) {
      i++;
      let inClass = false;
      while (i < n) {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === "[") inClass = true;
        else if (src[i] === "]") inClass = false;
        else if (src[i] === "/" && !inClass) break;
        else if (src[i] === "\n") break;
        i++;
      }
      i++;
      while (i < n && /[gimsuyd]/.test(src[i])) i++;   // flags
      out += " 0"; continue;
    }
    if (c === "`") {
      i++;
      while (i < n && src[i] !== "`") {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === "$" && src[i + 1] === "{") {          // keep the code inside
          i += 2; let depth = 1;
          while (i < n && depth) {
            if (src[i] === "{") depth++;
            else if (src[i] === "}") depth--;
            if (depth) out += src[i];
            i++;
          }
          out += " ";
          continue;
        }
        i++;
      }
      i++; out += ' ""'; continue;
    }
    out += c; i++;
  }
  return out;
}

let bad = 0;
for (const file of process.argv.slice(2)) {
  const html = fs.readFileSync(file, "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(m => m[1]).join("\n");
  const externals = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(m => m[1]);
  let src = strip(scripts);
  for (const ext of externals) {                 // include same-origin sibling files
    const p = "static/" + ext.replace(/^\//, "");
    if (fs.existsSync(p)) src += "\n" + strip(fs.readFileSync(p, "utf8"));
  }
  const defined = new Set(BUILTINS);
  const add = raw => String(raw).split(",").forEach(n => {
    const name = n.trim().split(/[:=]/)[0].trim().replace(/^\.\.\./, "");
    if (/^[A-Za-z_$][\w$]*$/.test(name)) defined.add(name);
  });
  for (const m of src.matchAll(/function\s*\*?\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)/g)) {
    if (m[1]) defined.add(m[1]);
    add(m[2]);                                   // function parameters
  }
  // const/let/var, including multi-declarator: `const a = 1, b = 2`
  for (const m of src.matchAll(/(?:const|let|var)\s+([^;\n]+)/g)) add(m[1].replace(/[{}\[\]]/g, ","));
  for (const m of src.matchAll(/\(([^()]*)\)\s*=>/g)) add(m[1]);   // arrow params
  for (const m of src.matchAll(/([A-Za-z_$][\w$]*)\s*=>/g)) add(m[1]);
  for (const m of src.matchAll(/catch\s*\(([^)]*)\)/g)) add(m[1]);
  for (const m of src.matchAll(/for\s*\(\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)/g)) add(m[1]);
  // calls that are bare identifiers: `name(` not preceded by . or a keyword
  const called = new Map();
  for (const m of src.matchAll(/(^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = m[2];
    if (!defined.has(name)) called.set(name, (called.get(name) || 0) + 1);
  }
  const missing = [...called].filter(([n]) => !BUILTINS.has(n));
  if (missing.length) {
    bad = 1;
    console.log(`FAIL: ${file} calls function(s) that are never defined:`);
    for (const [n, c] of missing) console.log(`         ${n}()  x${c}`);
  }
}
process.exit(bad);
