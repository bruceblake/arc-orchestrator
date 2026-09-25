"use strict";
// First-paint placeholders. Render paths replace the container's
// innerHTML, which removes these. They are not shown again on refresh.

function skeletonBars(n) {
  const rows = Array.from({length: n}, () => '<div class="sk-row"></div>').join("");
  return `<div class="sk" aria-busy="true" aria-label="Loading">${rows}</div>`;
}
