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
  { id: "requirements", label: "Checks" },
  { id: "activity", label: "Steps" },
  { id: "changes", label: "Changes" },
  { id: "usage", label: "Usage" },
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
    return `<div class="inspector-empty">Each step of a run appears here with its arguments, its result and
      how long it took.</div>`;
  }
  // DR-055: a run read back from the session says which run it is.
  const head = run.stored ? savedRunHeadMarkup(run) : "";
  if (run.stored && (!run.loaded || run.failed)) {
    return head + `<div class="inspector-empty">${run.failed ? `Could not read this run's steps: ${escapeHtml(run.failed)}` : "Reading this run's steps…"}</div>`;
  }
  const steps = run.steps || [];
  const tools = steps.filter((step) => step.type === "tool");
  const failed = tools.filter((step) => step.status === "failed").length;
  // Duration only once the run is over: a ticking clock here would repaint the
  // whole panel every second, and the card already shows one.
  const summary =
    head +
    `<div class="inspector-summary">` +
    `<span><strong>${tools.length}</strong> tool call${tools.length === 1 ? "" : "s"}</span>` +
    (failed ? `<span class="bad"><strong>${failed}</strong> failed</span>` : "") +
    (run.finishedAt ? `<span>${escapeHtml(formatElapsed(run.finishedAt - run.startedAt))}</span>` : "") +
    `<span class="inspector-state">${run.running ? "running" : "finished"}</span>` +
    `</div>`;
  if (!steps.length && run.stored) {
    return (
      summary +
      `<div class="inspector-empty">${
        run.kept
          ? "This run answered without using any tools."
          : "No steps were kept for this run. Runs from before steps were saved show only their result."
      }</div>`
    );
  }
  if (!steps.length) {
    return summary + `<div class="inspector-empty">Waiting for the model's first reply. On a laptop GPU, this is the slow part.</div>`;
  }

  const titles = new Map((run.stages || []).map((stage) => [stage.name, stage.title]));
  const numbers = new Map((run.stages || []).map((stage, index) => [stage.name, index + 1]));
  let group = null;
  const parts = [];
  steps.forEach((step, index) => {
    if (index === run.sinceIndex) {
      parts.push(`<div class="since-mark"><span>since you looked</span></div>`);
    }
    const stage = step.stage || "";
    if (stage !== group) {
      group = stage;
      const number = numbers.get(stage);
      parts.push(
        `<div class="step-group"><span class="sg-dot">${number || "·"}</span>` +
        `<span class="sg-title">${number ? `<span class="lbl">Step ${number}</span> ` : ""}${escapeHtml(titles.get(stage) || humanize(stage) || "Steps")}</span></div>`
      );
    }
    parts.push(step.type === "tool" ? toolStepMarkup(step, run) : noteStepMarkup(step));
  });
  return summary + `<div class="steps">${parts.join("")}</div>`;
}

/* Which stored run this is, and the way back to the latest one. */
function savedRunHeadMarkup(run) {
  const history = (state && state.history) || [];
  const latest = history.length ? history[history.length - 1].index : -1;
  return (
    `<div class="inspector-run"><span class="lbl">${escapeHtml(run.label)}</span>` +
    `<span class="inspector-run-prompt" title="${escapeHtml(run.prompt)}">${escapeHtml(elideMiddle(run.prompt.split("\n")[0], 60))}</span>` +
    (run.turnIndex !== latest
      ? `<button type="button" class="inline-button" data-unpick-run="1">Show the latest run</button>`
      : "") +
    `</div>`
  );
}

const STEP_STATUS_LABELS = { running: "running", done: "ok", failed: "failed", refused: "refused" };

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
    `<div class="step tool status-${escapeHtml(step.status)}${open ? " open" : ""}">` +
    `<button type="button" class="step-head" data-toggle-step="${escapeHtml(step.id)}" aria-expanded="${Boolean(open)}">` +
    `<span class="step-dot" aria-hidden="true"></span>` +
    `<span class="act-icon">${spec.icon}</span>` +
    `<span class="step-title">${escapeHtml(spec.verb)}${target ? ` <span class="mono">${escapeHtml(elideMiddle(target, 48))}</span>` : ""}</span>` +
    `<span class="step-meta">${escapeHtml(meta)}</span>` +
    `</button>${body}</div>`
  );
}

function noteStepMarkup(step) {
  return (
    `<div class="step note kind-${escapeHtml(step.kind || "")}">` +
    `<span class="step-dot" aria-hidden="true"></span><span>${step.html}</span></div>`
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
    return `<div class="inspector-empty">Craft and forge list what "done" means before changing a file, as
      numbered requirements, and Validate checks each one. The list appears here once the plan exists.
      Spark has none.</div>`;
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

  const verified = view.requirements.filter((item) => item.status === "Verified").length;
  const unmet = (view.verdict && view.verdict.unmet) || [];
  const verdict = view.verdict
    ? `<div class="verdict ${view.verdict.passed ? "passed" : "failed"}">` +
      `<strong>${
        view.verdict.passed
          ? `Validate passed · ${verified} of ${view.requirements.length} verified`
          : `Validate failed · ${unmet.length ? `${escapeHtml(unmet.join(", "))} unmet` : "see below"}`
      }</strong>` +
      (view.verdict.feedback ? `<div class="verdict-feedback">${escapeHtml(view.verdict.feedback)}</div>` : "") +
      `</div>`
    : "";
  const loops = (view.loopbacks || [])
    .map(
      (loop) =>
        `<div class="step note kind-fail"><span class="act-icon">↩</span><span>Sent back to ${escapeHtml(humanize(String(loop.target || "")))}${loop.unmet && loop.unmet.length ? `: ${escapeHtml(loop.unmet.join(", "))} unmet` : ""}${loop.feedback ? `. ${escapeHtml(loop.feedback)}` : ""}</span></div>`
    )
    .join("");

  const journey = view.journey || [];
  const rows = view.requirements
    .map((item, index) => {
      const trail = journey
        .map((step, position) => {
          const status = step.statuses[item.id];
          if (!status) return "";
          // The step that first lists a requirement wrote it; "Not Started"
          // there is true but reads as if the plan itself never ran.
          const written = position === 0 && status === "Not Started";
          const word = written ? "written" : status;
          const tone = written ? "work" : requirementTone(status);
          return `<span class="req-step tone-${tone}" title="${escapeHtml(step.title)}: ${escapeHtml(written ? "wrote this requirement" : status)}">${escapeHtml(step.title)} · ${escapeHtml(word)}</span>`;
        })
        .filter(Boolean)
        .join(`<span class="req-arrow">→</span>`);
      return (
        `<div class="req tone-${requirementTone(item.status)}">` +
        `<div class="req-head">${reqStampMarkup(item, index)}` +
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

/* The requirement's stamp, small, in place of its bare ID. */
function reqStampMarkup(item, index) {
  const known = STAMP_SHAPES[item.status] || { shape: "open", word: String(item.status || "").toLowerCase() };
  return `<span class="balloon ${known.shape}" title="${escapeHtml(item.id)}: ${escapeHtml(known.word)}">${index + 1}</span>`;
}

/* ---------- changes (DR-040) ---------- */

/* `view`: { turns: [{index, prompt, files: [{path, kind, added, removed}],
 * added, removed, undoable}], liveEdits, diffs: Map(index -> [change]),
 * open: Set("index:path") }. Newest turn first. */
function changesPanelMarkup(view) {
  const live = view.liveEdits
    ? `<div class="step note"><span class="act-icon">✎</span><span>This run has made ${view.liveEdits} edit${view.liveEdits === 1 ? "" : "s"} so far; its diff appears here when it finishes.</span></div>`
    : "";
  if (!view.turns.length) {
    return (
      live ||
      `<div class="inspector-empty">No files changed in this session. When one is, its diff appears here
        and can be undone.</div>`
    );
  }
  const total = view.turns.reduce(
    (sum, turn) => {
      const undone = turn.files.filter((file) => file.undone).length;
      return {
        files: sum.files + turn.files.length - undone,
        undone: sum.undone + undone,
        added: sum.added + turn.added,
        removed: sum.removed + turn.removed,
      };
    },
    { files: 0, undone: 0, added: 0, removed: 0 }
  );
  const summary =
    `<div class="inspector-summary"><span><strong>${total.files}</strong> file change${total.files === 1 ? "" : "s"}</span>` +
    `<span class="added">+${total.added}</span><span class="removed">−${total.removed}</span>` +
    (total.undone ? `<span>${total.undone} undone</span>` : "") +
    `<span class="inspector-state">${view.turns.length} run${view.turns.length === 1 ? "" : "s"}</span></div>`;
  const turns = view.turns
    .map((turn) => {
      const diffs = view.diffs.get(turn.index);
      const files = turn.files
        .map((file, position) => {
          const symbol = changeBadgeMarkup({ kind: file.kind, reverted: file.undone });
          if (file.undone) {
            return (
              `<div class="step tool rev-undone" title="Undone: the file was put back as it was">` +
              `<div class="step-head">${symbol}` +
              `<span class="step-title mono rev-file">${escapeHtml(elideMiddle(file.path, 46))}</span>` +
              `<span class="step-meta">undone</span></div></div>`
            );
          }
          const key = `${turn.index}:${file.path}`;
          const open = view.open.has(key);
          const change = diffs && diffs.find((item) => item.path === file.path);
          // The same table as the diff viewer, and a way into the viewer itself.
          const body = open
            ? change
              ? `<div class="inline-diff">${unifiedMarkup(parseDiff(change.diff || ""))}</div>` +
                `<div class="inline-diff-actions"><button type="button" class="inline-button" data-open-diff="1" data-turn="${turn.index}" data-path="${escapeHtml(file.path)}">Open in diff viewer</button>` +
                `<span class="inline-diff-hint">or double-click the file</span></div>`
              : `<div class="step-pending">Loading the diff…</div>`
            : "";
          return (
            `<div class="step tool${open ? " open" : ""}">` +
            `<button type="button" class="step-head" data-toggle-change="${escapeHtml(key)}" data-turn="${turn.index}" data-path="${escapeHtml(file.path)}" aria-expanded="${open}" title="Click for the diff here; double-click to open it in the diff viewer">` +
            symbol +
            `<span class="step-title mono">${escapeHtml(elideMiddle(file.path, 46))}</span>` +
            `<span class="step-meta"><span class="added">+${file.added}</span> <span class="removed">−${file.removed}</span>${file.kind && file.kind !== "modified" ? ` · ${escapeHtml(file.kind)}` : ""}</span>` +
            `</button>${body}</div>`
          );
        })
        .join("");
      return (
        `<div class="change-turn">` +
        `<div class="step-group">Run ${turn.index + 1} · ${escapeHtml(elideMiddle(turn.prompt || "", 60))}</div>` +
        files +
        (turn.undoable
          ? `<button type="button" class="ghost-button small change-undo" data-undo-turn="${turn.index}">Undo this run's changes</button>`
          : "") +
        `</div>`
      );
    })
    .join("");
  return summary + live + turns;
}

/* ---------- usage (DR-041) ---------- */

/* `view`: { run: {rounds: [{stage, prompt, completion, estimated, limit}],
 * stageTimes: {stage: {start, end}}, stages, running} | null,
 * turns: [{index, prompt, tier, total, prompt_tokens, completion_tokens}],
 * session: {prompt_tokens, completion_tokens, total_tokens} }. */
function usagePanelMarkup(view) {
  const session = view.session || {};
  const head =
    `<div class="usage-tiles">` +
    usageTile("Session", formatCount(session.total_tokens || 0), "tokens") +
    usageTile("In", formatCount(session.prompt_tokens || 0), "prompt") +
    usageTile("Out", formatCount(session.completion_tokens || 0), "completion") +
    `</div>`;
  const run = view.run;
  let runPart = "";
  if (run && run.stored && !run.loaded) {
    runPart = `<div class="step-group">${escapeHtml(run.label)}</div><div class="inspector-empty">Reading this run's usage…</div>`;
  } else if (run && run.rounds.length) {
    const titles = new Map((run.stages || []).map((stage) => [stage.name, stage.title]));
  const numbers = new Map((run.stages || []).map((stage, index) => [stage.name, index + 1]));
    const byStage = new Map();
    run.rounds.forEach((round) => {
      const key = round.stage || "";
      const entry = byStage.get(key) || { rounds: 0, prompt: 0, completion: 0 };
      entry.rounds += 1;
      entry.prompt += round.prompt || 0;
      entry.completion += round.completion || 0;
      byStage.set(key, entry);
    });
    const rows = [...byStage.entries()]
      .map(([stage, entry]) => {
        const times = (run.stageTimes || {})[stage];
        const seconds = times && times.start ? ((times.end || Date.now() / 1000) - times.start) : null;
        return (
          `<tr><td>${escapeHtml(titles.get(stage) || humanize(stage) || "Run")}</td>` +
          `<td>${entry.rounds}</td><td>${escapeHtml(formatCount(entry.prompt))}</td>` +
          `<td>${escapeHtml(formatCount(entry.completion))}</td>` +
          `<td>${seconds === null ? "" : escapeHtml(formatDuration(Math.round(seconds * 1000)))}</td></tr>`
        );
      })
      .join("");
    runPart =
      `<div class="step-group">${run.running ? "This run, so far" : escapeHtml(run.label || "Last run")}</div>` +
      `<table class="usage-table"><thead><tr><th>Stage</th><th>Rounds</th><th>In</th><th>Out</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table>` +
      `<div class="step-group">Context used each round</div>` +
      contextBarsMarkup(run.rounds);
  }
  const turns = (view.turns || []).filter((turn) => turn.total);
  const max = Math.max(1, ...turns.map((turn) => turn.total));
  const turnPart = turns.length
    ? `<div class="step-group">Tokens per run</div>` +
      turns
        .map(
          (turn) =>
            `<div class="usage-turn"><span class="usage-turn-label">${turn.index + 1}. ${escapeHtml(elideMiddle(turn.prompt || "", 34))}</span>` +
            shareBarMarkup(turn.total / max) +
            `<span class="usage-turn-value">${escapeHtml(formatCount(turn.total))}</span></div>`
        )
        .join("")
    : "";
  if (!runPart && !turnPart) {
    return head + `<div class="inspector-empty">Tokens and time appear here as a run goes: per stage, per round and per run.</div>`;
  }
  return head + runPart + turnPart;
}

function usageTile(label, value, unit) {
  return `<div class="usage-tile"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><em>${escapeHtml(unit)}</em></div>`;
}

/* One bar per round: how much of the window the request filled. The ratio
 * is the reported prompt size when the runtime gives one, else the budget's
 * own estimate (DR-033). Drawn as SVG with sizes as attributes: the page's
 * Content-Security-Policy refuses inline style attributes (DR-029). */
function contextBarsMarkup(rounds) {
  const width = 360;
  const height = 64;
  const gap = 2;
  const barWidth = Math.max(2, Math.min(14, (width - gap * rounds.length) / Math.max(1, rounds.length)));
  const bars = rounds
    .map((round, index) => {
      const used = round.prompt || round.estimated || 0;
      const ratio = round.limit ? Math.min(1, used / round.limit) : 0;
      const tone = ratio > 0.9 ? "hot" : ratio > 0.7 ? "warn" : "";
      const barHeight = Math.max(2, Math.round(ratio * (height - 14)));
      const x = index * (barWidth + gap);
      return (
        `<rect class="ctx-bar ${tone}" x="${x}" y="${height - barHeight}" width="${barWidth}" height="${barHeight}">` +
        `<title>Round ${index + 1}: ${escapeHtml(formatCount(used))} of ${round.limit ? escapeHtml(formatCount(round.limit)) : "?"} tokens (${Math.round(ratio * 100)}%)</title></rect>`
      );
    })
    .join("");
  const peak = Math.max(0, ...rounds.map((round) => (round.limit ? (round.prompt || round.estimated || 0) / round.limit : 0)));
  return (
    `<div class="ctx-chart"><svg class="ctx-bars" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Context used per round">${bars}</svg>` +
    `<div class="ctx-chart-note">peak ${Math.round(peak * 100)}% of the window · ${rounds.length} round${rounds.length === 1 ? "" : "s"}</div></div>`
  );
}

/* A horizontal share bar, as SVG for the same CSP reason. */
function shareBarMarkup(ratio) {
  const width = Math.max(2, Math.round(Math.min(1, ratio) * 100));
  return `<svg class="usage-bar" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="0" width="${width}" height="6"></rect></svg>`;
}
