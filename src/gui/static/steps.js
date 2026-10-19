/* A finished run's steps, read back from the session (DR-055).
 *
 * A run this page watched keeps the step record app.js built from its events.
 * Any other finished run — one from before a reload, or picked from further
 * up the conversation — has a trace stored with the turn instead. This file
 * reads that trace from `/api/steps` when the details panel needs it, and
 * shapes it like a watched run, so the Steps, Usage and Checks tabs draw it
 * with the renderers they already use (inspector.js).
 *
 * It also lets a person pick a run: a click on a finished run's card shows
 * that run in the details panel and marks the card; a click on it again, or a
 * new run starting, goes back to the latest.
 *
 * Loaded after app.js, whose helpers and state it uses (desk, focusId, state,
 * api, dom, escapeHtml, humanize, renderInspector, setInspectorOpen). Nothing
 * here writes.
 */

/* Traces fetched so far, by session and turn: { loaded, failed, run }. A
 * finished turn's trace never changes, so one read is enough. */
const savedSteps = new Map();

/* The key includes what identifies the turn beyond its index: clearing the
 * history starts the numbering again, and run 1 is then another run. */
function savedStepsKey(session, turn) {
  const usage = turn.usage || {};
  return `${session}:${turn.index}:${turn.stop_reason}:${usage.total_tokens || 0}:${turn.prompt}`;
}

/* The run the details panel shows when nothing is running: the one picked in
 * the conversation, else the last one this page watched, else the latest
 * stored turn. `undefined` means "nothing stored to add": app.js then keeps
 * to what it had. */
function pickedOrSavedRun(seat) {
  const history = (state && state.history) || [];
  const picked = seat.picked;
  if (picked !== undefined && picked !== null) {
    const turn = history.find((item) => item.index === picked);
    if (!turn) {
      // The pick outlived its turn (the history was cleared).
      seat.picked = undefined;
    } else {
      // The run this page watched has the fuller record of the same steps.
      if (seat.lastRun && seat.lastRun.turnIndex === picked && !seat.lastRun.joined) return seat.lastRun;
      return savedRunFor(turn);
    }
  }
  if (seat.lastRun && seat.lastRun.joined) {
    // Joined part-way (a reload mid-run): the page rebuilt it from a bounded
    // timeline, which drops the oldest events of a long run. The trace stored
    // with the turn kept them.
    const turn = history.find((item) => item.index === seat.lastRun.turnIndex);
    if (turn) return savedRunFor(turn);
  }
  if (seat.lastRun) return seat.lastRun;
  const latest = history[history.length - 1];
  return latest ? savedRunFor(latest) : undefined;
}

/* The stored run for *turn*, shaped like a watched one; fetched on first use. */
function savedRunFor(turn) {
  const session = focusId();
  const key = savedStepsKey(session, turn);
  const known = savedSteps.get(key);
  if (known) return known.run;
  const entry = { loaded: false, failed: "", run: savedRunShell(turn) };
  savedSteps.set(key, entry);
  if (turn.has_steps === false) {
    // A run from before steps were kept: nothing to fetch.
    entry.loaded = true;
    entry.run.loaded = true;
    return entry.run;
  }
  api(`/api/steps?session=${encodeURIComponent(session)}&turn=${encodeURIComponent(turn.index)}`)
    .then((payload) => {
      entry.run = savedRunFromTrace(turn, payload);
      entry.loaded = true;
    })
    .catch((error) => {
      entry.failed = String((error && error.message) || error);
      entry.run.failed = entry.failed;
      entry.run.loaded = true;
    })
    .finally(() => {
      if (focusId() === session) renderInspector();
    });
  return entry.run;
}

/* What the panel draws while the trace is on its way. */
function savedRunShell(turn) {
  return {
    stored: true,
    loaded: false,
    kept: false,
    failed: "",
    turnIndex: turn.index,
    label: `Run ${turn.index + 1}`,
    prompt: turn.prompt || "",
    running: false,
    startedAt: 0,
    finishedAt: 0,
    stages: [],
    steps: [],
    rounds: [],
    stageTimes: {},
    requirements: turn.requirements || [],
    ledger: [],
    verdict: null,
    loopbacks: [],
    openSteps: new Set(),
  };
}

/* The stored trace as the record app.js keeps for a watched run. */
function savedRunFromTrace(turn, trace) {
  const run = savedRunShell(turn);
  run.loaded = true;
  // A trace was kept, even if the run took no steps worth listing.
  run.kept = Boolean(trace.started_at);
  run.startedAt = (trace.started_at || 0) * 1000;
  run.finishedAt = (trace.finished_at || 0) * 1000;
  run.stages = (trace.stages || []).map((stage) => ({
    name: stage.stage,
    title: stage.title || humanize(stage.stage),
    status: "done",
  }));
  // The latest pass through each stage, as the live view keeps it.
  (trace.stage_times || []).forEach((pass) => {
    run.stageTimes[pass.stage] = { start: pass.start, end: pass.end };
  });
  run.rounds = trace.rounds || [];
  run.ledger = trace.ledger || [];
  const steps = trace.steps || [];
  steps.forEach((step, index) => {
    if (trace.dropped_steps && index === trace.dropped_after) run.steps.push(droppedStepsNote(trace, step.stage));
    if (step.type === "tool") {
      run.steps.push(savedToolStep(step, index));
      if (step.truncated) {
        run.steps.push({
          type: "note",
          stage: step.stage,
          kind: "",
          icon: "▭",
          html: `${escapeHtml(humanize(String(step.tool || "tool")))} returned more than fits; kept the head and the tail`,
        });
      }
      return;
    }
    run.steps.push({ type: "note", stage: step.stage, ...savedNoteLine(step) });
    if (step.kind === "validation_verdict") run.verdict = { passed: step.passed, overridden: step.overridden, target: step.target, unmet: step.unmet || [] };
    if (step.kind === "loopback") run.loopbacks.push({ target: step.target, unmet: step.unmet || [] });
  });
  if (trace.dropped_steps && trace.dropped_after >= steps.length) run.steps.push(droppedStepsNote(trace, ""));
  return run;
}

function savedToolStep(step, index) {
  const refused = step.status === "refused";
  return {
    type: "tool",
    id: `saved-${index}-${step.id || ""}`,
    tool: String(step.tool || ""),
    args: step.args || {},
    stage: step.stage || "",
    status: step.status || "done",
    elapsedMs: typeof step.elapsed_ms === "number" && !refused ? step.elapsed_ms : undefined,
    // A refused call never ran, so its "result" is why.
    preview: refused ? `Not run: ${step.reason || "refused"}` : step.preview,
    truncated: Boolean(step.truncated),
    artifact: step.artifact || "",
  };
}

function droppedStepsNote(trace, stage) {
  const count = trace.dropped_steps;
  return {
    type: "note",
    stage,
    kind: "",
    icon: "⋯",
    html: `${count} more step${count === 1 ? "" : "s"} here were not kept with the session`,
  };
}

/* A stored note, worded as handleEvent (app.js) words the live event, so a
 * run reads the same after a reload as it did while it ran. */
function savedNoteLine(note) {
  const message = escapeHtml(note.message || "");
  const stage = escapeHtml(humanize(String(note.stage || "")));
  switch (note.kind) {
    case "classification_finished":
      return {
        kind: "",
        icon: "◈",
        html: note.ratcheted
          ? `Classified as <em>${escapeHtml(String(note.tier))}</em>, raised to <em>${escapeHtml(String(note.effective_tier))}</em> for this session`
          : `Classified as <em>${escapeHtml(String(note.effective_tier || note.tier))}</em>`,
      };
    case "stage_started":
      return { kind: "", icon: "↺", html: `${escapeHtml(note.title || humanize(String(note.stage)))} again, pass ${escapeHtml(String(note.iteration))}` };
    case "stage_retry":
      return { kind: "fail", icon: "△", html: `Retrying ${stage}: ${message}` };
    case "stage_finished":
      return { kind: "", icon: "☰", html: `${escapeHtml(note.title || note.message || humanize(String(note.stage)))} done` };
    case "validation_verdict":
      return {
        kind: note.passed ? "pass" : "fail",
        icon: note.passed ? "✓" : "✗",
        html: note.passed
          ? "Checked the work against the plan — it holds"
          : `Checked the work: ${escapeHtml(String((note.unmet || []).join(", ") || "gaps remain"))}`,
      };
    case "loopback": {
      const unmet = note.unmet || [];
      return {
        kind: "fail",
        icon: "↩",
        html: `Sent back to ${escapeHtml(humanize(String(note.target || "")))}${unmet.length ? `: ${escapeHtml(unmet.join(", "))} unmet` : ""}`,
      };
    }
    case "approval_resolved":
      return {
        kind: note.approved ? "pass" : "denied",
        icon: note.approved ? "✓" : "⊘",
        html: `${note.approved ? "Approved" : "Refused"} ${escapeHtml(String(note.tool || ""))}${note.reason ? ` — ${escapeHtml(String(note.reason))}` : ""}`,
      };
    case "agent_nudged":
      return { kind: "", icon: "·", html: message || "Asked it to continue" };
    case "tool_call_repeated":
      return { kind: "fail", icon: "⊘", html: message || "Refused a repeated call" };
    case "tool_call_malformed":
      return {
        kind: "fail",
        icon: "△",
        html: `The model wrote a tool call the runtime could not parse; asked it to write it again (${escapeHtml(String(note.attempt || 1))}/3)`,
      };
    case "agent_stalled":
      return { kind: "fail", icon: "△", html: message || "Stopped going in circles" };
    case "agent_context_thrashing":
      return { kind: "fail", icon: "△", html: message || "The context window is too small" };
    case "agent_round_cap_reached":
      return { kind: "fail", icon: "△", html: message || "Hit the tool-round cap" };
    case "stop_requested":
      return { kind: "", icon: "△", html: message || "Stopping" };
    case "agent_cancelled":
      return {
        kind: "fail",
        icon: "△",
        html: escapeHtml(`${note.agent || "It"} stopped after ${note.rounds || 0} round(s) — keeping what it had`),
      };
    case "context_truncated_by_runtime":
      return { kind: "fail", icon: "△", html: "The runtime is serving a smaller context window than it reported" };
    default:
      return { kind: "", icon: "·", html: message || escapeHtml(humanize(String(note.kind || ""))) };
  }
}

/* ---------- picking a run in the conversation ---------- */

/* Mark the picked card, and only it. Cards are cached and reused between
 * paints, so the mark is set on every paint rather than when a card is made. */
function markPickedCard() {
  const picked = desk(focusId()).picked;
  dom.messages.querySelectorAll("[data-run-card]").forEach((node) => {
    const on = picked !== undefined && picked !== null && Number(node.dataset.runCard) === picked;
    node.classList.toggle("picked", on);
    if (on) node.setAttribute("aria-current", "true");
    else node.removeAttribute("aria-current");
  });
}

function pickRun(index) {
  const seat = desk(focusId());
  // Clicking the picked card again goes back to the latest run.
  seat.picked = seat.picked === index ? undefined : index;
  markPickedCard();
  if (seat.picked !== undefined && !dom.shell.classList.contains("inspector-open")) setInspectorOpen(true);
  else renderInspector();
}

dom.messages.addEventListener("click", (event) => {
  const card = event.target.closest("[data-run-card]");
  if (!card) return;
  // Buttons, links and disclosures inside a card do their own thing, and a
  // click that ends a text selection is someone copying, not picking.
  if (event.target.closest("button, a, summary, details, input, textarea, select, .file-ref")) return;
  const selection = window.getSelection ? String(window.getSelection()) : "";
  if (selection.trim()) return;
  pickRun(Number(card.dataset.runCard));
});

el("inspector-body").addEventListener("click", (event) => {
  if (!event.target.closest("[data-unpick-run]")) return;
  desk(focusId()).picked = undefined;
  markPickedCard();
  renderInspector();
});
