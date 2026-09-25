"use strict";
// Conditional poll + visibility pause + a one-second clock.
//
// jgetRev sends the last ETag. A 304 keeps the previous render: the
// server ignores clock fields, and startClock moves [data-since] labels
// without rebuilding the DOM. paint() remembers the template on the
// element so a ticker's textContent edits do not look like new data.

const POLL_ETAG = {};
const POLL_BODY = {};

async function jgetRev(url, slot) {
  const headers = {};
  if (POLL_ETAG[slot]) headers["If-None-Match"] = POLL_ETAG[slot];
  const r = await fetch(url, {cache: "no-store", headers});
  if (r.status === 304) return {unchanged: true, data: POLL_BODY[slot] || null};
  if (!r.ok) throw new Error("HTTP " + r.status);
  const tag = r.headers.get("ETag");
  const data = await r.json();
  if (tag) POLL_ETAG[slot] = tag;
  else delete POLL_ETAG[slot];
  POLL_BODY[slot] = data;
  return {unchanged: false, data};
}

function forgetEtag(slot) {
  delete POLL_ETAG[slot];
  delete POLL_BODY[slot];
}

const ON_SHOW = [];
let SHOW_BOUND = false;

function every(fn, ms, delay) {
  const run = () => { if (!document.hidden) fn(); };
  setTimeout(() => { run(); setInterval(run, ms); }, delay || 0);
  ON_SHOW.push(run);
  if (SHOW_BOUND) return;
  SHOW_BOUND = true;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    for (const poll of ON_SHOW) poll();
  });
}

function paint(el, html) {
  if (!el) return false;
  if (el._paint === html) return false;
  el._paint = html;
  el.innerHTML = html;
  return true;
}

function sinceStamp(secondsAgo) {
  if (secondsAgo == null || secondsAgo === "") return "";
  const n = Number(secondsAgo);
  if (!Number.isFinite(n)) return "";
  return String(Math.round(Date.now() / 1000 - n));
}

function startClock() {
  setInterval(() => {
    document.querySelectorAll("[data-since]").forEach(el => {
      const t = Number(el.dataset.since);
      if (!t) return;
      const text = tick(Math.max(0, Date.now() / 1000 - t)) + (el.dataset.suffix || "");
      if (el.textContent !== text) el.textContent = text;
    });
  }, 1000);
}
