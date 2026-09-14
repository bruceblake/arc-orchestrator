// usage.html's hourly view: the date picker, the stacked bar chart and the
// per-model table. `node tests/usage_hourly.test.mjs`; exits non-zero on any
// failure.
//
// Same DOM stub + concatenated static/common.js + inline script harness as
// tests/usage_visibility.test.mjs and tests/ui_render.test.mjs. The render
// functions are called with real API payload shapes, because a chart that
// renders at all is not the same as one that renders the right bars.
import fs from "node:fs";

const src = fs.readFileSync(new URL("../static/usage.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));

let failures = 0;
const ok = (cond, what) => {
  if (!cond) { console.error(`usage_hourly: FAIL — ${what}`); failures++; }
};

// ---- DOM stub --------------------------------------------------------------
const els = new Map();
const mk = id => {
  let html = "";
  return {
    id, className: "", title: "", value: "", dataset: {}, children: [],
    style: {}, disabled: false, checked: false,
    classList: { add(){}, remove(){}, contains: () => false, toggle(){} },
    querySelectorAll: () => [], appendChild(){}, onclick: null, addEventListener(){},
    focus(){}, blur(){},
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v == null ? "" : v); },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v == null ? "" : v); },
  };
};
const q = sel => {
  const id = String(sel).replace(/^#/, "");
  if (!els.has(id)) els.set(id, mk(id));
  return els.get(id);
};
const listeners = {};
globalThis.document = {
  hidden: false,
  querySelector: q, querySelectorAll: () => [], createElement: () => mk("new"),
  addEventListener: (n, fn) => { (listeners[n] ||= []).push(fn); },
  removeEventListener: () => {}, body: mk("body"), documentElement: mk("html"),
};
globalThis.window = { addEventListener: () => {}, removeEventListener: () => {},
                      matchMedia: () => ({ matches: false, addEventListener(){} }),
                      location: { hash: "", search: "" } };
globalThis.location = { hash: "", search: "", href: "http://x/usage.html" };
globalThis.history = { replaceState(){}, pushState(){} };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;

const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8")).join("\n");

const api = new Function(externals + "\n" + js
  + "\nglobalThis.__hourly = { renderHourly, updateHourlyTable, hourlyAxis,"
  + " stepHourDate, setHourDate, hoursOf, pickDefaultDate, modelFamilyOf };"
  + "\nreturn globalThis.__hourly;")();

// ---- fixture: two active hours, one busy, one quiet -------------------------
const hour = (h, models, totals) => ({
  hour: h, by_model: models, totals,
});
const t = (requests, tokens, extra) => Object.assign(
  { requests, ok: requests, errors: 0, failed_attempts: 0, tokens,
    prompt_tokens: 0, completion_tokens: 0 }, extra || {});
const DAY = {
  date: "2026-09-10",
  hours: Array.from({ length: 24 }, (_, h) => hour(h, {}, t(0, 0))),
  totals: t(5, 640),
};
DAY.hours[3] = hour(3, { "GLM-5.3": { requests: 2, tokens: 300 },
                         "DeepSeek-V4.1-Flash-thinking-max": { requests: 1, tokens: 40 } },
                    t(3, 340));
DAY.hours[14] = hour(14, { "GLM-5.3": { requests: 2, tokens: 300 } }, t(2, 300));
DAY.hours[23] = hour(23, { "GLM-5.3": { requests: 0, tokens: 0 } }, t(0, 0, { failed_attempts: 1 }));

// ---- the chart renders a bar per occupied hour -----------------------------
api.renderHourly(DAY);
const svg = q("#hourly-chart").innerHTML;
ok(svg.includes("<svg"), "the chart did not render an <svg>");
const bars = (svg.match(/class="hbar"/g) || []).length;
ok(bars === 24, `the chart drew ${bars} bars; every hour of the day needs a slot`);
const stacks = (svg.match(/class="hseg"/g) || []).length;
ok(stacks === 3,
   `the chart drew ${stacks} stacked segments for 3 model-hours (2 at 03:00 + 1 at 14:00)`);
ok(/data-hour="3"/.test(svg), "no bar carries data-hour, so a bar cannot be identified");
ok(!/NaN|undefined/.test(svg), "the chart rendered NaN/undefined into the markup");

// A day with nothing in it must draw the axis, not crash or say nothing.
api.renderHourly({ date: "2020-01-01", hours: Array.from({ length: 24 },
  (_, h) => hour(h, {}, t(0, 0))), totals: t(0, 0) });
ok(q("#hourly-chart").innerHTML.includes("<svg"), "an empty day must still draw the chart");
ok(q("#hourly-empty").style.display !== "none", "an empty day must say so");

// ---- the per-model table ---------------------------------------------------
api.renderHourly(DAY);
const rows = q("#hourly-body").innerHTML;
ok(rows.includes("GLM 5.3"), "the hourly table has no GLM row");
ok(/title="GLM-5\.3"/.test(rows),
   "the hourly table carries no exact model id, only the friendly name");
ok(rows.includes("DeepSeek"), "the hourly table has no DeepSeek row");
ok(rows.includes("03:00"), "the hourly table does not name the hour it reports");
ok(!/NaN|undefined/.test(rows), "the hourly table rendered NaN/undefined");

// ---- totals must agree with the daily numbers for the same date ------------
ok(q("#hourly-sum").textContent.includes("2026-09-10"),
   "the panel summary does not name the day it is showing");
ok(q("#hourly-sum").textContent.includes("5"),
   `the panel summary does not show the payload's 5 requests: ${q("#hourly-sum").textContent}`);
// Sum the RENDERED table's request column: agreement has to hold in what the
// operator reads, not only in the payload the panel was handed.
const reqCol = [...rows.matchAll(/<td class="r">(\d+)<\/td>/g)].map(m => Number(m[1]));
ok(reqCol.length > 0, "the hourly table rendered no numeric cells");
ok(reqCol.filter((_, i) => i % 2 === 0).reduce((a, b) => a + b, 0) === 5,
   `the rendered request column sums to ${reqCol.filter((_, i) => i % 2 === 0).reduce((a, b) => a + b, 0)}, not the day's 5`);
ok(q("#hour-date").value === "2026-09-10",
   "the date picker was not moved to the date being shown");

// ---- the date picker -------------------------------------------------------
api.setHourDate("2026-09-10");
api.stepHourDate(1);
ok(q("#hour-date").value === "2026-09-11",
   `next-day gave ${q("#hour-date").value}, expected 2026-09-11`);
api.stepHourDate(-1);
api.stepHourDate(-1);
ok(q("#hour-date").value === "2026-09-09",
   `prev-day gave ${q("#hour-date").value}, expected it to step back past 09-10`);
// A month boundary, where naive day arithmetic produces "2026-09-00".
api.setHourDate("2026-09-01");
api.stepHourDate(-1);
ok(q("#hour-date").value === "2026-08-31",
   `stepping back over a month boundary gave ${q("#hour-date").value}, expected 2026-08-31`);

// A missing or malformed picker value must not produce "NaN-NaN-NaN".
api.setHourDate("garbage");
ok(/^\d{4}-\d{2}-\d{2}$/.test(q("#hour-date").value),
   `a malformed picker value became ${q("#hour-date").value}`);
api.setHourDate("");
ok(/^\d{4}-\d{2}-\d{2}$/.test(q("#hour-date").value),
   "an empty picker value must fall back to a real date");

// ---- the axis label --------------------------------------------------------
ok(api.hourlyAxis(0) === "00:00" && api.hourlyAxis(23) === "23:00",
   "the hour axis is not zero-padded 24h");

// ---- markup: the controls exist and are labelled ---------------------------
ok(/<input[^>]+type="date"/.test(src), "no date input in the page markup");
ok(/<label[^>]+for="hour-date"/.test(src), "the date input has no associated <label>");
ok(/id="hour-prev"/.test(src) && /id="hour-next"/.test(src),
   "the prev/next-day buttons are missing");
ok(/id="hourly-chart"/.test(src) && /id="hourly-body"/.test(src),
   "the chart or table mount point is missing from the markup");
ok(/fetch\(["']\/api\/usage\/hourly/.test(js),
   "the page never calls /api/usage/hourly");

if (failures) process.exit(1);
console.log("usage_hourly: PASS");
