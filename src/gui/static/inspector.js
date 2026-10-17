/* The inspector: the panel beside the conversation (DR-037).
 *
 * The conversation says what is happening in a sentence; the inspector shows
 * the run in full — every step with its arguments, its timing and what came
 * back — so the chat can stay readable without anything being hidden.
 *
 * Pure renderers only. Each takes plain data and returns markup; `app.js` owns
 * the state, the event handling and the decision of when to paint. Loaded
 * before `app.js`, and calls its helpers (escapeHtml, humanize, …) only when
 * a renderer runs, by which time they exist. No build step (DR-029).
 */

const INSPECTOR_TABS = [
  { id: "activity", label: "Activity" },
  { id: "requirements", label: "Requirements" },
];

function inspectorTabsMarkup(active) {
  return INSPECTOR_TABS.map(
    (tab) =>
      `<button type="button" role="tab" class="inspector-tab${tab.id === active ? " active" : ""}" ` +
      `aria-selected="${tab.id === active}" data-inspector-tab="${tab.id}">${escapeHtml(tab.label)}</button>`
  ).join("");
}

/* One run's steps, grouped under the stage that took them. */
function activityPanelMarkup(run) {
  if (!run) {
    return `<div class="inspector-empty">Start a request and every step it takes shows up here —
      what it read, ran and changed, how long each took, and what came back.</div>`;
  }
  const steps = run.steps || [];
  const tools = steps.filter((step) => step.type === "tool");
  const failed = tools.filter((step) => step.status === "failed").length;
  // Duration only once the run is over: a ticking clock here would repaint the
  // whole panel every second, and the card already shows one.
  const summary =
    `<div class="inspector-summary">` +
    `<span><strong>${tools.length}</strong> tool call${tools.length === 1 ? "" : "s"}</span>` +
    (failed ? `<span class="bad"><strong>${failed}</strong> failed</span>` : "") +
    (run.finishedAt ? `<span>${escapeHtml(formatElapsed(run.finishedAt - run.startedAt))}</span>` : "") +
    `<span class="inspector-state">${run.running ? "running" : "finished"}</span>` +
    `</div>`;
  if (!steps.length) {
    return summary + `<div class="inspector-empty">Waiting for the first step…</div>`;
  }

  const titles = new Map((run.stages || []).map((stage) => [stage.name, stage.title]));
  let group = null;
  const parts = [];
  steps.forEach((step) => {
    const stage = step.stage || "";
    if (stage !== group) {
      group = stage;
      parts.push(
        `<div class="step-group">${escapeHtml(titles.get(stage) || humanize(stage) || "Steps")}</div>`
      );
    }
    parts.push(step.type === "tool" ? toolStepMarkup(step, run) : noteStepMarkup(step));
  });
  return summary + `<div class="steps">${parts.join("")}</div>`;
}

const STEP_STATUS_LABELS = { running: "running", done: "ok", failed: "failed" };

function toolStepMarkup(step, run) {
  const spec = TOOL_LABELS[step.tool] || { icon: "·", verb: humanize(step.tool || "tool"), key: "" };
  const target = spec.key ? String((step.args || {})[spec.key] || "") : "";
  const open = run.openSteps && run.openSteps.has(step.id);
  const meta = [
    step.elapsedMs !== undefined ? formatDuration(step.elapsedMs) : "",
    STEP_STATUS_LABELS[step.status] || step.status,
  ]
    .filter(Boolean)
    .join(" · ");
  const args = Object.entries(step.args || {})
    .map(([key, value]) => `<div><span class="arg-key">${escapeHtml(key)}</span> ${escapeHtml(String(value))}</div>`)
    .join("");
  const body = open
    ? `<div class="step-body">` +
      (args ? `<div class="step-args">${args}</div>` : "") +
      (step.preview !== undefined
        ? `<pre class="step-output">${escapeHtml(step.preview || "(no output)")}</pre>`
        : `<div class="step-pending">No result yet.</div>`) +
      (step.truncated
        ? `<div class="step-note">The model saw the start and the end of a longer result${step.artifact ? `; the full text is in <span class="mono">${escapeHtml(step.artifact)}</span>` : ""}.</div>`
        : "") +
      `</div>`
    : "";
  return (
    `<div class="step tool status-${step.status}${open ? " open" : ""}">` +
    `<button type="button" class="step-head" data-toggle-step="${escapeHtml(step.id)}" aria-expanded="${Boolean(open)}">` +
    `<span class="act-icon">${spec.icon}</span>` +
    `<span class="step-title">${escapeHtml(spec.verb)}${target ? ` <span class="mono">${escapeHtml(elideMiddle(target, 48))}</span>` : ""}</span>` +
    `<span class="step-meta">${escapeHtml(meta)}</span>` +
    `</button>${body}</div>`
  );
}

function noteStepMarkup(step) {
  return (
    `<div class="step note kind-${escapeHtml(step.kind || "")}">` +
    `<span class="act-icon">${step.icon || "·"}</span><span>${step.html}</span></div>`
  );
}

function formatDuration(ms) {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  return formatElapsed(ms);
}

/* ---------- requirements (DR-038) ---------- */

const REQUIREMENT_TONES = {
  "not started": "idle",
  "in progress": "work",
  implemented: "work",
  blocked: "bad",
  verified: "good",
  failed: "bad",
};

function requirementTone(status) {
  return REQUIREMENT_TONES[String(status || "").toLowerCase()] || "idle";
}

/* `view` is either a live run's ledger journey or a finished turn's final
 * ledger: { requirements, journey: [{title, statuses: {id: status}}], verdict,
 * loopbacks, live }. */
function requirementsPanelMarkup(view) {
  if (!view || !view.requirements.length) {
    return `<div class="inspector-empty">Craft and Forge write down what "done" means before they touch a
      file — a numbered list of requirements — and Validate checks each one independently. That
      list appears here as soon as the plan exists. Spark answers directly and has none.</div>`;
  }
  const counts = {};
  view.requirements.forEach((item) => {
    const key = String(item.status || "").toLowerCase();
    counts[key] = (counts[key] || 0) + 1;
  });
  const summary =
    `<div class="inspector-summary"><span><strong>${view.requirements.length}</strong> requirement${view.requirements.length === 1 ? "" : "s"}</span>` +
    Object.entries(counts)
      .map(([status, count]) => `<span class="tone-${requirementTone(status)}"><strong>${count}</strong> ${escapeHtml(status)}</span>`)
      .join("") +
    `<span class="inspector-state">${view.live ? "live" : "final"}</span></div>`;

  const verdict = view.verdict
    ? `<div class="verdict ${view.verdict.passed ? "passed" : "failed"}">` +
      `<strong>${view.verdict.passed ? "Validate: it holds" : "Validate: not yet"}</strong>` +
      (view.verdict.unmet && view.verdict.unmet.length
        ? `<div>Unmet: ${view.verdict.unmet.map((id) => `<span class="mono">${escapeHtml(id)}</span>`).join(", ")}</div>`
        : "") +
      (view.verdict.feedback ? `<div class="verdict-feedback">${escapeHtml(view.verdict.feedback)}</div>` : "") +
      `</div>`
    : "";
  const loops = (view.loopbacks || [])
    .map(
      (loop) =>
        `<div class="step note kind-fail"><span class="act-icon">↺</span><span>Sent back to ${escapeHtml(humanize(String(loop.target || "")))} (pass ${escapeHtml(String(loop.iteration || ""))})${loop.feedback ? ` — ${escapeHtml(loop.feedback)}` : ""}</span></div>`
    )
    .join("");

  const journey = view.journey || [];
  const rows = view.requirements
    .map((item) => {
      const trail = journey
        .map((step) => {
          const status = step.statuses[item.id];
          if (!status) return "";
          return `<span class="req-step tone-${requirementTone(status)}" title="${escapeHtml(step.title)}: ${escapeHtml(status)}">${escapeHtml(step.title)} · ${escapeHtml(status)}</span>`;
        })
        .filter(Boolean)
        .join(`<span class="req-arrow">→</span>`);
      return (
        `<div class="req tone-${requirementTone(item.status)}">` +
        `<div class="req-head"><span class="req-id mono">${escapeHtml(item.id)}</span>` +
        `<span class="req-desc">${escapeHtml(item.description)}</span>` +
        `<span class="req-status">${escapeHtml(item.status)}</span></div>` +
        (trail ? `<div class="req-trail">${trail}</div>` : "") +
        (item.evidence ? `<div class="req-evidence"><span class="arg-key">evidence</span> ${escapeHtml(item.evidence)}</div>` : "") +
        `</div>`
      );
    })
    .join("");
  return summary + verdict + loops + `<div class="reqs">${rows}</div>`;
}
