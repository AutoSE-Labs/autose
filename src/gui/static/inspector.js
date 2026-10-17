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

const INSPECTOR_TABS = [{ id: "activity", label: "Activity" }];

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
