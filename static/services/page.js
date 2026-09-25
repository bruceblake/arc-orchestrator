"use strict";
// Page window for list endpoints. Omit offset when it is 0 so a first
// page stays a one-parameter URL.

function pageQuery(limit, offset) {
  const q = new URLSearchParams();
  if (limit != null) q.set("limit", String(limit));
  if (offset) q.set("offset", String(offset));
  const s = q.toString();
  return s ? ("?" + s) : "";
}
