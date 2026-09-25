// The "Needs you" human checkpoint queue: static/panels/review.js on the
// desktop page and the Review view of static/phone.html.
// `node tests/review_ui.test.mjs`; exits non-zero on any failure.
import fs from "node:fs";

const els = new Map();
const mk = id => {
  let html = "";
  return {
    id, className: "", title: "", value: "", options: [], dataset: {}, checked: false,
    style: {}, disabled: false, classList: {add(){}, remove(){}, contains: () => false},
    querySelectorAll: () => [], appendChild(){}, onclick: null, setAttribute(){}, getAttribute: () => null,
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v == null ? "" : v); },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v == null ? "" : v); },
  };
};
const listeners = {};
const get = sel => { const id = sel.replace(/^#/, ""); if (!els.has(id)) els.set(id, mk(id)); return els.get(id); };
globalThis.document = {
  querySelector: get, getElementById: id => get(id), querySelectorAll: () => [],
  createElement: () => mk("new"),
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener: () => {}, hidden: false,
  body: mk("body"), documentElement: mk("html"), activeElement: null,
};
globalThis.window = { addEventListener(){}, removeEventListener(){},
  matchMedia: () => ({matches: false, addEventListener(){}}), location: {hash: "", search: ""} };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.location = { hash: "", search: "", href: "http://localhost:8787/" };
globalThis.history = { replaceState(){}, pushState(){} };
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
const alerts = [];
globalThis.alert = m => alerts.push(String(m));
globalThis.confirm = () => true;
globalThis.prompt = () => "";
const posts = [];
let REV = null;
globalThis.fetch = async (u, opt) => {
  if (opt && opt.method === "POST") {
    posts.push({url: u, body: JSON.parse(opt.body)});
    return { status: 200, ok: true, headers: {get: () => null}, json: async () => ({ok: true, session: {status: "preparing"}}) };
  }
  if (String(u).startsWith("/api/reviews")) return { status: 200, ok: true, headers: {get: () => null}, json: async () => REV };
  return { status: 200, ok: true, headers: {get: () => null}, json: async () => ({}) };
};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;
globalThis.clearTimeout = () => 0;

let n = 0;
const failures = [];
function ok(cond, name) { n++; if (!cond) { failures.push(name); console.log("FAIL " + name); } }
const EVIL = '<img src=x onerror=alert(1)>';

const item = (over = {}) => ({
  task: "doors", pr: 12, round: 2, project: "game", title: "Cell doors " + EVIL,
  url: "https://github.com/o/game/pull/12", live: true, requested_at: Date.now() / 1000 - 90,
  reviewers: [{model: "GLM-5.3", approve: true, crashed: false, issues: [], follow_ups: [EVIL]}],
  evidence: {attempt: "x3", contact_sheet: "/api/evidence-file?path=game/doors/x3/contact_sheet.png",
    shots: [{name: "a.png", url: "/api/evidence-file?path=game/doors/x3/shots/a.png"}],
    videos: {flythrough: {gif: "/api/evidence-file?path=f.gif", mp4: "/api/evidence-file?path=f.mp4"}},
    compare: [{name: "cam", changed: 0.25, url: "/api/evidence-file?path=c.png"}],
    warnings: ["the scene has NO light " + EVIL]},
  play: {project: "game", build: "task/doors", available: true, reason: "",
    scenes: ["res://world.tscn", "res://prison.tscn"], main_scene: "res://world.tscn",
    base: {build: "main", sha: "abc"}},
  ...over,
});
// Evidence names come from the game repo (studio_cameras.json, playtest shot
// filenames): a double quote must not close the attribute it lands in.
const Q = 'x" onerror="alert(1)';
const quoted = () => item({
  task: 'doors" autofocus onfocus="alert(2)',
  url: 'https://github.com/o/game/pull/12" onmouseover="alert(3)',
  evidence: {attempt: "x1", contact_sheet: "/api/evidence-file?path=" + Q,
    shots: [{name: Q, url: "/api/evidence-file?path=" + Q}],
    videos: {[Q]: {gif: "/api/evidence-file?path=" + Q, mp4: "/api/evidence-file?path=" + Q}},
    compare: [{name: 'cam" onload="alert(4)', changed: 0.1, url: "/api/evidence-file?path=" + Q}],
    warnings: []},
});
// Text nodes may hold a raw quote; only a tag's own attribute names matter.
const breakout = h => [...h.matchAll(/<[a-z][^>]*>/gi)].some(([tag]) =>
  [...tag.replace(/="[^"]*"/g, "").matchAll(/\s([^\s=>\/]+)/g)].some(([, a]) => /^(on|autofocus)/i.test(a)));

// ---- desktop ---------------------------------------------------------------
const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
ok(src.includes('src="/panels/review.js"'), "index.html loads panels/review.js");
ok(src.includes('data-tab="review"') && src.includes('id="review-panel"'), "Needs you tab and panel exist");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f)).map(f => fs.readFileSync(f, "utf8")).join("\n");
const api = new Function(externals + "\n" + js
  + "\nglobalThis.__setRev = v => { REVIEWS = v; };"
  + "\nreturn {renderReviews, rvCard, pollReviews, TABS, RV_DRAFT};")();
ok(api.TABS.review && api.TABS.review.includes("review-panel"), "tabs.js has the review tab");
ok(fs.readFileSync("static/services/refresh.js", "utf8").includes("pollReviews"), "bootPolls polls the queue");

globalThis.__setRev({waiting: [item()], recent: [{task: "old", pr: 3, round: 1, status: "approved", decided_by: "dashboard", decided_at: Date.now()/1000 - 60, comment: EVIL}], global_default: false});
api.renderReviews();
const html = get("#reviews").innerHTML;
ok(!html.includes("<img src=x"), "no user text reaches the DOM unescaped");
ok(html.includes('data-rv-decide="approve"') && html.includes('data-rv-decide="reject"'), "approve and request-changes buttons");
ok(html.includes('data-rv-play="task/doors"') && html.includes('data-rv-play="main"'), "play this build and play main");
ok(html.includes("res://prison.tscn"), "scene picker offers the build's scenes");
ok(html.includes("/api/evidence-file?path=c.png") && html.includes("25.0% of pixels changed"), "before/after with changed share");
ok(html.includes("/api/evidence-file?path=f.gif") && html.includes("f.mp4"), "flythrough gif and mp4");
ok(html.includes("NO light"), "evidence warnings shown");
ok(html.includes('href="https://github.com/o/game/pull/12"'), "PR link");
ok(get("#rv-count").textContent === "1", "tab badge counts waiting PRs");

const click = async target => { for (const fn of listeners.click || []) await fn({target}); await new Promise(r => setImmediate(r)); };
const btn = attrs => ({ disabled: false, getAttribute: k => attrs[k] ?? null,
  closest(sel) { const k = sel.slice(1, -1); return k in attrs ? this : null; } });

// Request changes with no comment: refused locally, nothing posted.
await click(btn({"data-rv-decide": "reject", "data-rv-key": "doors|12|2"}));
ok(posts.length === 0 && alerts.some(a => /what must change/i.test(a)), "reject needs a comment");
api.RV_DRAFT["doors|12|2"] = "the door clips the wall";
await click(btn({"data-rv-decide": "reject", "data-rv-key": "doors|12|2"}));
ok(posts.length === 1 && posts[0].url === "/api/reviews/decide"
   && JSON.stringify(posts[0].body) === JSON.stringify({task: "doors", pr: 12, round: 2, decision: "reject", comment: "the door clips the wall"}),
   "reject posts task/pr/round/decision/comment");
api.RV_DRAFT["doors|12|2|scene"] = "res://prison.tscn";
await click(btn({"data-rv-play": "task/doors", "data-rv-project": "game", "data-rv-key": "doors|12|2"}));
const launch = posts[posts.length - 1];
ok(launch.url === "/api/studio/playtest/launch" && launch.body.build === "task/doors"
   && launch.body.scene === "res://prison.tscn" && launch.body.project === "game", "play posts the build and chosen scene");

globalThis.__setRev({waiting: [item({play: {available: false, reason: "not a studio game project"}})], recent: []});
api.renderReviews();
ok(get("#reviews").innerHTML.includes("disabled") && get("#reviews").innerHTML.includes("not a studio game project"), "unplayable build says why");

globalThis.__setRev({waiting: [quoted()], recent: []});
api.renderReviews();
const dq = get("#reviews").innerHTML;
ok(dq.includes("&quot;") && !breakout(dq), "desktop: a double quote in evidence urls/names cannot break out of an attribute");

// ---- phone -----------------------------------------------------------------
els.clear(); posts.length = 0;
const psrc = fs.readFileSync("static/phone.html", "utf8");
ok(psrc.includes('id="nav-review"') && psrc.includes('id="view-review"'), "phone has a Review view");
const pext = [...psrc.matchAll(/<script[^>]+src="([^"]+)"/g)].map(m => "static" + m[1])
  .filter(p => fs.existsSync(p)).map(p => fs.readFileSync(p, "utf8")).join("\n");
const pjs = psrc.slice(psrc.indexOf("<script>") + 8, psrc.lastIndexOf("</script>"));
const phone = new Function(pext + "\n" + pjs + "\nreturn {S, reviewCard, renderReview, rvDecide, rvDraft, rvKeyOf};")();
phone.S.reviews = {waiting: [item()]};
phone.S.open.review = "doors|12|2";
phone.renderReview();
const ph = get("#reviews").innerHTML;
ok(!ph.includes("<img src=x"), "phone escapes user text");
ok(ph.includes('data-rv-decide="approve"') && ph.includes("c.png") && ph.includes("Play on the PC"), "phone card: evidence, play, decide");
ok(get("#badge-review").textContent === "1", "phone nav badge");
phone.rvDecide("doors|12|2", "approve");
await new Promise(r => setImmediate(r));
ok(posts.length === 1 && posts[0].body.decision === "approve", "phone approve posts");

const pq = quoted();
phone.S.reviews = {waiting: [pq]};
phone.S.open.review = phone.rvKeyOf(pq);
phone.renderReview();
const phq = get("#reviews").innerHTML;
ok(phq.includes("&quot;") && phq.includes("rvimg") && !breakout(phq),
   "phone: a double quote in evidence urls/names cannot break out of an attribute");

console.log(`review ui checks: ${n - failures.length} passed, ${failures.length} failed`);
if (failures.length) process.exit(1);
