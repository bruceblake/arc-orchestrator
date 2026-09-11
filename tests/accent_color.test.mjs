import { readFileSync } from 'node:fs';

const files = {
  index: 'static/index.html',
  usage: 'static/usage.html',
  phone: 'static/phone.html',
  common: 'static/common.js',
};

let failures = [];

function assert(condition, msg) {
  if (!condition) failures.push(msg);
}

// Check dark accent present in all three pages
for (const [key, path] of Object.entries({index: files.index, usage: files.usage, phone: files.phone})) {
  const content = readFileSync(path, 'utf8');
  assert(/--accent:\s*#bc8cff/.test(content), `${path} missing dark accent #bc8cff`);
}

// Check light accent in phone
const phoneContent = readFileSync(files.phone, 'utf8');
assert(/@media\s*\(prefers-color-scheme:\s*light\)\s*{[^}]*--accent:\s*#8250df/.test(phoneContent), `static/phone.html missing light accent #8250df`);

// Ensure no old accent values in any static file (excluding common.js)
const staticFiles = ['static/index.html', 'static/usage.html', 'static/phone.html'];
for (const path of staticFiles) {
  const content = readFileSync(path, 'utf8');
  assert(!/--accent:\s*#58a6ff/.test(content), `${path} still contains old accent #58a6ff`);
  assert(!/--accent:\s*#0969da/.test(content), `${path} still contains old accent #0969da`);
}

// Ensure common.js still contains #58a6ff (guard against over‑replace)
const commonContent = readFileSync(files.common, 'utf8');
assert(/#58a6ff/.test(commonContent), `static/common.js missing expected #58a6ff`);

if (failures.length) {
  console.error('FAILURES:');
  failures.forEach(f => console.error('- ' + f));
  console.log(`${failures.length} failure(s) detected.`);
  process.exit(1);
} else {
  console.log('All accent colour checks passed.');
  process.exit(0);
}
