/* The route card: a run drawn as a machine-shop traveler.
 *
 * Pure renderers, shared by the live card and the finished one, in both
 * editions. `app.js` owns the data and the requests; this file only draws.
 *
 * - Operations are the stages, numbered 10, 20, 30… in workflow order, with
 *   a pass count when a stage ran more than once.
 * - A loop-back is a red tag ("Sent back to 30 Execute: R3 unmet") and a
 *   dashed arc from Validate back to the operation it returned to.
 * - Each requirement gets a stamp box. The shapes follow inspection practice:
 *   a circle means implemented, not yet inspected; a square means verified,
 *   with evidence; a struck square means failed.
 * - Every file a run changed is a row in a revision block.
 */

/* Stage name → operation number, in the order the workflow runs them. */
function opNumbers(names) {
  const numbers = {};
  names.forEach((name, index) => {
    numbers[name] = (index + 1) * 10;
  });
  return numbers;
}

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
      // A run that stopped part-way leaves its last operation open.
      status: succeeded || name !== last ? "done" : "halted",
    })),
    loopbacks,
  };
}

function routeMarkup(route) {
  if (!route || !route.ops.length) return "";
  const numbers = opNumbers(route.ops.map((op) => op.name));
  const count = route.ops.length;
  const ops = route.ops
    .map(
      (op) =>
        `<li class="op op-${escapeHtml(op.status || "pending")}">` +
        `<span class="op-no">${numbers[op.name]}</span>` +
        `<span class="op-name">${escapeHtml(op.title)}</span>` +
        (op.passes > 1 ? `<span class="op-pass" title="Ran ${op.passes} times">×${op.passes}</span>` : "") +
        `</li>`
    )
    .join("");
  const from = route.ops.findIndex((op) => op.name === "validate");
  // One dashed arc per distinct send-back target, from Validate back to it.
  // Percentages of the strip's width, so the arc lines up with the columns
  // at any width; the stroke does not scale with them.
  const targets = [...new Set(route.loopbacks.map((item) => item.to))];
  const arcs =
    from >= 0 && targets.length
      ? `<svg class="route-arcs" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">` +
        targets
          .map((target) => {
            const to = route.ops.findIndex((op) => op.name === target);
            if (to < 0) return "";
            const x1 = ((from + 0.5) / count) * 100;
            const x2 = ((to + 0.5) / count) * 100;
            return `<path d="M${x1.toFixed(2)} 2 C ${x1.toFixed(2)} 22, ${x2.toFixed(2)} 22, ${x2.toFixed(2)} 2" vector-effect="non-scaling-stroke"></path>`;
          })
          .join("") +
        `</svg>`
      : "";
  const tags = route.loopbacks
    .map((item) => {
      const op = route.ops.find((entry) => entry.name === item.to);
      const name = op ? op.title : humanize(item.to);
      const why = item.unmet ? `${item.unmet} unmet` : "";
      return (
        `<li class="rework-tag" title="${escapeHtml(item.detail || why)}">` +
        `Sent back to ${numbers[item.to] || ""} ${escapeHtml(name)}${why ? `: ${escapeHtml(why)}` : ""}</li>`
      );
    })
    .join("");
  return (
    `<div class="route"><ol class="ops ops-${count}">${ops}</ol>${arcs}` +
    (tags ? `<ul class="rework-tags">${tags}</ul>` : "") +
    `</div>`
  );
}

/* One stamp per requirement. `fresh` holds the IDs whose status changed in
 * this paint, so only those get the landing animation. */
const STAMP_SHAPES = {
  "Not Started": { shape: "open", word: "not started" },
  "In Progress": { shape: "open", word: "in progress" },
  Blocked: { shape: "blocked", word: "blocked" },
  Implemented: { shape: "circle", word: "implemented, not yet checked" },
  Verified: { shape: "square", word: "verified" },
  Failed: { shape: "failed", word: "failed" },
};

function stampsMarkup(requirements, fresh) {
  if (!requirements || !requirements.length) return "";
  const counts = { square: 0, failed: 0 };
  const boxes = requirements
    .map((item) => {
      const known = STAMP_SHAPES[item.status] || { shape: "open", word: String(item.status || "").toLowerCase() };
      if (known.shape in counts) counts[known.shape] += 1;
      const isNew = fresh && fresh.has(`${item.id}:${item.status}`);
      const label = `${item.id} ${known.word}${item.description ? `: ${item.description}` : ""}`;
      return (
        `<li class="stamp stamp-${known.shape}${isNew ? " is-new" : ""}" title="${escapeHtml(label)}">` +
        `<span class="stamp-id">${escapeHtml(item.id)}</span>` +
        `<span class="visually-hidden">${escapeHtml(known.word)}</span></li>`
      );
    })
    .join("");
  const tally =
    `${counts.square} of ${requirements.length} verified` +
    (counts.failed ? ` · ${counts.failed} failed` : "");
  return `<div class="stamp-row"><ul class="stamps" aria-label="Requirements">${boxes}</ul><span class="stamp-tally">${tally}</span></div>`;
}

/* The card's title block: labelled cells, hairline-ruled. */
function titleBlockMarkup(cells) {
  const shown = cells.filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (!shown.length) return "";
  return (
    `<dl class="title-block">` +
    shown
      .map(([label, value]) => `<div class="tb-cell"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`)
      .join("") +
    `</dl>`
  );
}

/* The verdict, as a stamp on the finished card. */
function verdictStampMarkup(tone, word) {
  return `<span class="verdict-stamp verdict-${tone}" aria-hidden="true">${escapeHtml(word)}</span>`;
}

/* Every file a run changed, as a revision block: symbol, file, lines. */
function revisionBlockMarkup(changes) {
  if (!changes || !changes.length) return "";
  const rows = changes
    .map(
      (change, index) =>
        `<tr${change.reverted ? ` class="rev-undone" title="Undone"` : ""}>` +
        `<td class="rev-sym"><span>${index + 1}</span></td>` +
        `<td class="rev-file">${escapeHtml(change.path)}</td>` +
        `<td class="rev-kind">${escapeHtml({ created: "new", deleted: "deleted" }[change.kind] || "edit")}</td>` +
        `<td class="rev-lines"><span class="added">+${change.added}</span> <span class="removed">−${change.removed}</span></td>` +
        `</tr>`
    )
    .join("");
  return (
    `<table class="revision-block"><caption>Changes</caption>` +
    `<thead><tr><th scope="col">Rev</th><th scope="col">File</th><th scope="col">Kind</th><th scope="col">Lines</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`
  );
}
