// Accent colour test
import { readFileSync } from 'node:fs';

function fail(msg) { console.error('FAIL:', msg); process.exit(1); }

let failures = 0;
function assert(condition, msg) { if (!condition) { console.error('FAIL:', msg); failures++; } }

// Helper to load file content
function load(path) { return readFileSync(path, 'utf8'); }

// Files to check
const index = load('static/index.html');
const usage = load('static/usage.html');
const phone = load('static/phone.html');
const common = load('static/common.js');

// Accent definitions
assert(index.includes('--accent: #bc8cff'), 'index.html missing dark accent #bc8cff');
assert(usage.includes('--accent: #bc8cff'), 'usage.html missing dark accent #bc8cff');
assert(phone.includes('--accent: #bc8cff'), 'phone.html missing dark accent #bc8cff');
assert(phone.includes('--accent: #8250df'), 'phone.html missing light accent #8250df');

// Ensure no old accent colours appear in any static HTML file
const htmlFiles = ['static/index.html', 'static/usage.html', 'static/phone.html'];
for (const f of htmlFiles) {
  const content = load(f);
  assert(!content.includes('#58a6ff'), `${f} contains old accent #58a6ff`);
  assert(!content.includes('#0969da'), `${f} contains old light accent #0969da`);
}

// Ensure common.js still contains the model family colour #58a6ff (guard)
assert(common.includes('#58a6ff'), 'static/common.js should still contain #58a6ff');

if (failures) process.exit(1);
console.log('All accent colour checks passed.');
process.exit(0);
