/* The drawing board: a run drawn as a route of stations.
 *
 * Pure renderers, shared by the live sheet and the finished one, in both
 * editions. `app.js` owns the data and the requests; this file only draws.
 *
 * - Stages are stations WP-1, WP-2… on one ink line: done is solid ink, the
 *   current stage is being inked (dotted), what comes next is faint pencil.
 * - A send-back is a redline loop from the check back to the station it
 *   returned to, with a revision letter on the curve and a margin note.
 * - Every position is computed from the station layout, so the arrow starts
 *   and ends on its stations and the letter sits on the curve.
 * - Checks are numbered balloons; a title block closes every sheet.
 */

const ROUTE = { width: 760, left: 34, right: 726, lineY: 54 };

/* The operations and send-backs of a finished run, from its stored route:
 * [{kind: "stage", stage, title}, {kind: "loopback", stage, unmet, detail}]. */
function routeFromRecord(route, succeeded) {
  const order = [];
  const titles = {};
  const passes = {};
  const loopbacks = [];
  (route || []).forEach((step) => {
    if (step.kind === "stage") {
      if (!(step.stage in passes)) order.push(step.stage);
      passes[step.stage] = (passes[step.stage] || 0) + 1;
      titles[step.stage] = step.title || humanize(step.stage);
    } else if (step.kind === "loopback") {
      loopbacks.push({ to: step.stage, unmet: step.unmet || "", detail: step.detail || "" });
    }
  });
  const last = order[order.length - 1];
  return {
    ops: order.map((name) => ({
      name,
      title: titles[name],
      passes: passes[name],
      status: succeeded || name !== last ? "done" : "halted",
      note: "",
    })),
    loopbacks,
    pose: "",
  };
}

/* The words under a station. */
function stationNote(op) {
  if (op.note) return op.note;
  if (op.status === "done") return op.passes > 1 ? `done · ${op.passes} passes` : "done";
  if (op.status === "active") return "working";
  if (op.status === "halted") return "stopped here";
  return "next";
}

function routeMarkup(route) {
  if (!route || !route.ops.length) return "";
  const ops = route.ops;
  const n = ops.length;
  const span = ROUTE.right - ROUTE.left;
  const xs = ops.map((_, i) => (n === 1 ? ROUTE.left : ROUTE.left + (span * i) / (n - 1)));
  const y = ROUTE.lineY;
  const anchor = (i) => (n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle");

  // Segments: ink up to the current station, inking after it, pencil beyond.
  const segments = [];
  for (let i = 0; i < n - 1; i++) {
    const next = ops[i + 1].status;
    const cls =
      next === "done" || next === "active" || next === "halted"
        ? "r-done"
        : ops[i].status === "active"
          ? "r-now"
          : "r-next";
    segments.push(`<line class="${cls}" x1="${xs[i]}" y1="${y}" x2="${xs[i + 1]}" y2="${y}"></line>`);
  }

  const stations = ops
    .map((op, i) => {
      const status = op.status || "next";
      const cls = status === "done" ? "wp done" : status === "active" ? "wp" : status === "halted" ? "wp halted" : "wp next";
      const r = status === "active" ? 11 : 9;
      const noteCls = status === "active" ? (op.waiting ? "wp-s ink" : "wp-s ink") : status === "halted" ? "wp-s red" : "wp-s";
      return (
        `<circle class="${cls}" cx="${xs[i]}" cy="${y}" r="${r}"></circle>` +
        `<text class="wp-l" x="${xs[i]}" y="${y + 36}" text-anchor="${anchor(i)}">WP-${i + 1}</text>` +
        `<text class="wp-n" x="${xs[i]}" y="${y + 57}" text-anchor="${anchor(i)}">${escapeHtml(op.title)}</text>` +
        `<text class="${noteCls}" x="${xs[i]}" y="${y + 77}" text-anchor="${anchor(i)}">${escapeHtml(stationNote(op))}</text>`
      );
    })
    .join("");

  // The nib sits over the station it is working at, lifted when it waits.
  const at = ops.findIndex((op) => op.status === "active");
  // A drop of ink on the current station for every new step: the ring is
  // re-drawn (and so replays) only when the count changes.
  const drop =
    at >= 0 && route.drops
      ? `<circle class="ink-drop" data-n="${route.drops}" cx="${xs[at]}" cy="${y}" r="11"></circle>`
      : "";
  // Poses are plain SVG transforms: lifted and leaning towards you while it
  // waits, drooping on a slow step, upright while it draws.
  const angle = route.pose === "waiting" ? -14 : route.pose === "slow" ? 18 : 0;
  const lift = route.pose === "waiting" ? 2 : 14;
  const nib =
    at >= 0 && route.pose
      ? `<g class="nib-on-route nib-${escapeHtml(route.pose)}" transform="translate(${xs[at] + 14} ${lift}) rotate(${angle} 13 31)"><use href="#nib" width="26" height="31"></use></g>`
      : "";

  // The send-back: one redline per distinct station it returned to, from the
  // check's station to that one, with the revision letter on its lowest point.
  const from = ops.findIndex((op) => op.name === "validate");
  let loops = "";
  let height = y + 92;
  const targets = [...new Set(route.loopbacks.map((item) => item.to))];
  if (from >= 0 && targets.length) {
    const top = y + 88;
    const depth = 46;
    targets.forEach((target, k) => {
      const to = ops.findIndex((op) => op.name === target);
      if (to < 0 || to === from) return;
      const x1 = xs[from];
      const x2 = xs[to];
      const dip = top + depth + k * 18;
      // A cubic with both control points at the dip, so its lowest point is
      // exactly halfway: x at the midpoint, y = (y1 + 3*dip + 3*dip + y2) / 8.
      const midX = (x1 + x2) / 2;
      const midY = (top + 3 * dip + 3 * dip + (top + 2)) / 8;
      loops +=
        `<path class="redloop" d="M${x1} ${top} C ${x1} ${dip}, ${x2} ${dip}, ${x2} ${top + 2}" marker-end="url(#route-arrow)"></path>`;
      const letter = String.fromCharCode(66 + k + Math.max(0, route.loopbacks.length - targets.length));
      loops +=
        `<path class="revtri" d="M${midX} ${midY - 13} L${midX + 12} ${midY + 8} L${midX - 12} ${midY + 8} Z"></path>` +
        `<text class="revtxt" x="${midX}" y="${midY + 5}" text-anchor="middle">${letter}</text>`;
      height = Math.max(height, midY + 22);
    });
    const last = route.loopbacks[route.loopbacks.length - 1];
    const times = route.loopbacks.length === 1 ? "once" : `${route.loopbacks.length} times`;
    const why = last.unmet ? `${last.unmet} unmet` : (last.detail || "").slice(0, 48);
    const toX = (xs[from] + xs[ops.findIndex((op) => op.name === targets[targets.length - 1])]) / 2;
    loops += `<text class="margin-note" x="${toX}" y="${height + 16}" text-anchor="middle">sent back ${times}${why ? `: ${escapeHtml(why)}` : ""}</text>`;
    height += 26;
  }

  // On a phone the same route reads top to bottom: one row per station.
  const list =
    `<ol class="route-list" aria-hidden="true">` +
    ops
      .map(
        (op, i) =>
          `<li class="rl-${escapeHtml(op.status || "next")}"><span class="rl-dot"></span>` +
          `<span class="rl-text"><span class="lbl">WP-${i + 1}</span> <b>${escapeHtml(op.title)}</b> <span class="rl-note">${escapeHtml(stationNote(op))}</span></span></li>`
      )
      .join("") +
    (route.loopbacks.length
      ? `<li class="rl-back"><span class="rl-dot"></span><span class="rl-text hand">sent back ${route.loopbacks.length === 1 ? "once" : `${route.loopbacks.length} times`}${route.loopbacks[route.loopbacks.length - 1].unmet ? `: ${escapeHtml(route.loopbacks[route.loopbacks.length - 1].unmet)} unmet` : ""}</span></li>`
      : "") +
    `</ol>`;
  return (
    `<div class="route">${list}<svg viewBox="0 0 ${ROUTE.width} ${Math.ceil(height)}" role="img" aria-label="${escapeHtml(routeLabel(route))}">` +
    `<defs><marker id="route-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="redhead" d="M0 1 L10 5 L0 9 Z"></path></marker></defs>` +
    segments.join("") +
    loops +
    stations +
    drop +
    nib +
    `</svg></div>`
  );
}

function routeLabel(route) {
  const parts = route.ops.map((op) => `${op.title}: ${stationNote(op)}`);
  if (route.loopbacks.length) parts.push(`sent back ${route.loopbacks.length} time${route.loopbacks.length === 1 ? "" : "s"}`);
  return parts.join(", ");
}

/* Checks, as numbered balloons on the sheet. */
const STAMP_SHAPES = {
  "Not Started": { shape: "open", word: "not started" },
  "In Progress": { shape: "open", word: "in progress" },
  Blocked: { shape: "failed", word: "blocked" },
  Implemented: { shape: "implemented", word: "implemented, not yet checked" },
  Verified: { shape: "verified", word: "holds" },
  Failed: { shape: "failed", word: "does not hold" },
};

function stampsMarkup(requirements) {
  if (!requirements || !requirements.length) return "";
  let held = 0;
  let failed = 0;
  const balloons = requirements
    .map((item, index) => {
      const known = STAMP_SHAPES[item.status] || { shape: "open", word: String(item.status || "").toLowerCase() };
      if (known.shape === "verified") held += 1;
      if (known.shape === "failed") failed += 1;
      return `<span class="balloon ${known.shape}" title="${escapeHtml(`${item.id}: ${item.description || ""} (${known.word})`)}">${index + 1}<span class="visually-hidden"> ${escapeHtml(known.word)}</span></span>`;
    })
    .join("");
  const tally = `${held} of ${requirements.length} checks hold${failed ? ` · ${failed} ${failed === 1 ? "does" : "do"} not` : ""}`;
  return `<div class="check-row">${balloons}<span class="check-tally">${tally}</span></div>`;
}

/* The title block: Sheet, Title, Drawn by, Checked by, Rev, Scale. */
function titleBlockMarkup(cells) {
  return (
    `<dl class="title-block">` +
    cells
      .map(
        ([label, value, kind]) =>
          `<div class="tb-cell"><dt>${escapeHtml(label)}</dt><dd${kind ? ` class="${kind}"` : ""} title="${escapeHtml(String(value))}">${escapeHtml(String(value))}</dd></div>`
      )
      .join("") +
    `</dl>`
  );
}

/* The verdict, stamped on a finished sheet. */
function verdictStampMarkup(tone, word) {
  return `<span class="verdict-stamp verdict-${tone}" aria-hidden="true">${escapeHtml(word)}</span>`;
}

/* Every file a run changed, as a revision schedule. */
function revisionBlockMarkup(changes) {
  if (!changes || !changes.length) return "";
  const rows = changes
    .map(
      (change, index) =>
        `<tr${change.reverted ? ` class="rev-undone" title="Erased: put back as it was"` : ""}>` +
        `<td class="rev-sym"><span>${String.fromCharCode(65 + (index % 26))}</span></td>` +
        `<td class="rev-file">${escapeHtml(change.path)}</td>` +
        `<td class="rev-kind">${escapeHtml(change.reverted ? "erased" : { created: "new", deleted: "deleted" }[change.kind] || "edit")}</td>` +
        `<td class="rev-lines"><span class="added">+${change.added}</span> / <span class="removed">−${change.removed}</span></td>` +
        `</tr>`
    )
    .join("");
  return (
    `<table class="revision-block"><caption>Revisions</caption>` +
    `<thead><tr><th scope="col">Rev</th><th scope="col">File</th><th scope="col">Kind</th><th scope="col">Lines</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`
  );
}

/* A small line figure per sheet, seeded by its id, so parallel sessions are
 * told apart at a glance. */
function sheetGlyphPath(id) {
  let seed = 0;
  for (const ch of String(id || "")) seed = (seed * 31 + ch.charCodeAt(0)) >>> 0;
  const a = 1 + (seed % 4);
  const b = 2 + ((seed >> 3) % 3);
  const phase = ((seed >> 6) % 628) / 100;
  let d = "";
  for (let i = 0; i <= 96; i++) {
    const t = (i / 96) * Math.PI * 2;
    const x = 16 + 12.5 * Math.sin(a * t + phase);
    const y = 16 + 12.5 * Math.sin(b * t);
    d += `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }
  return d;
}
