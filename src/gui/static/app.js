/* AutoSE web dashboard.
 *
 * A rendering layer and nothing else: every decision — which tier a prompt
 * gets, whether a shell command may run, what a stage produced — is made by
 * src/logic and arrives here as a numbered frame on one event stream. This
 * file draws those frames, and posts what the user does back.
 *
 * Several sessions can be open at once and each of them can be generating
 * (DR-031), so every frame carries the session it belongs to and this file
 * keeps one live card per session. Only the focused one is painted; the rest
 * accumulate their activity in memory, which is what makes switching back to a
 * run in progress instant rather than a refetch that has lost the feed.
 *
 * No build step, no dependencies, no remote assets. The markdown renderer and
 * the diff view are small on purpose: a local-first tool should not fetch a
 * library from a CDN to show you your own code.
 */

"use strict";

const TOKEN = new URLSearchParams(window.location.search).get("token") || "";

const STARTERS = [
  { label: "Explain this project", text: "Walk me through what this project does and how it is laid out." },
  { label: "Fix a bug", text: "Something is wrong: describe what you did, what you expected, and what happened instead." },
  { label: "Add a test", text: "Add a test for " },
  { label: "Tidy up", text: "Look over the recent changes and clean up anything messy, then run the tests." },
];

/* Grounded in what this dashboard actually does, not generic advice — the
 * bulb in the status bar shows one at random and the full list on click. */
const TIPS = [
  {
    title: "Pick the tier the request actually needs",
    text: "Spark answers or makes one small edit. Craft plans, makes the change and checks it against numbered requirements, for anything that needs a plan. A small question in Craft takes longer for nothing.",
  },
  {
    title: "Let it plan before it writes",
    text: "Craft writes a plan with numbered requirements before changing a file, and Validate checks the result against them. Tick Review plan first to correct it while it is still text.",
  },
  {
    title: "A refusal with a reason changes what it tries next",
    text: "Shell commands and network calls wait for your answer. A reason typed before Refuse goes back to the model, so it tries another way instead of asking again.",
  },
  {
    title: "Review what changed before you trust it",
    text: "The Changes tab shows a diff of every file this session changed. Undo puts the last run's files back, and the record keeps them, struck through.",
  },
  {
    title: "Stop keeps what it produced. Reloading loses it",
    text: "Stop ends a run after the current step and keeps everything it produced so far.",
  },
  {
    title: "Run several things at once",
    text: "The rail holds several sessions. Switching away from one that is working does not pause it.",
  },
  {
    title: "Forgetting history doesn't touch your files",
    text: "Clear history removes what is sent back to the model as context. Files it wrote stay as they are; Undo is what puts them back.",
  },
  {
    title: "When the context meter runs near full, start a new session",
    text: "CTX in the status bar is how full the context window was on the last round. Near full, the model starts losing earlier runs, and a new session helps more than a longer request.",
  },
  {
    title: "No model responding? Run Machine check",
    text: "It looks for Ollama, LM Studio, vLLM and llama.cpp on this machine and says which answer, what they serve, and whether the model can call tools.",
  },
  {
    title: "Everything here runs local, by default",
    text: "No cloud call is made unless you point it at one. Set AUTOSE_BASE_URL and AUTOSE_MODEL to use any OpenAI-compatible endpoint instead of what was autodetected.",
  },
  {
    title: "Enter starts, Shift+Enter adds a line",
    text: "Enter starts the run. Shift+Enter adds a new line for a longer request.",
  },
  {
    title: "Starter chips fill the box; they do not send",
    text: "A chip on the first screen fills the request box. Edit it, then press Start.",
  },
  {
    title: "Tier and model are per session",
    text: "Changing either one changes only the session on screen. Open a second session to compare tiers or models side by side.",
  },
  {
    title: "Deleting a stored session asks twice",
    text: "The × beside a stored session becomes ! on the first click. Only a second click on it deletes the session.",
  },
  {
    title: "Jump to the latest instead of fighting the scroll",
    text: "Scrolling up to reread does not lose your place. A ↓ button appears when you leave the bottom.",
  },
  {
    title: "Network calls are approved like writes are",
    text: "Web searches and page fetches wait for your answer too, because they bring back content from outside this machine. A fetch can be allowed for one site for the session.",
  },
  {
    title: "The workspace path in the status bar is the boundary",
    text: "DIR names the one folder AutoSE reads and writes. Commands that reach outside it are refused.",
  },
  {
    title: "Trust the pass or fail over the write-up",
    text: "Validate fails a requirement it cannot confirm. A square stamp means it was checked with evidence; a circle means it was only implemented.",
  },
  {
    title: "Closing a session isn't deleting it",
    text: "Closing takes a session off the rail; it stays on disk and reopens from Stored. Only Delete removes it.",
  },
];

/* Tools, described the way a person would describe them. The names come from
 * logic/common/tools; anything unknown falls through to a readable default. */
const TOOL_LABELS = {
  read_file: { icon: "◉", verb: "Reading", key: "path" },
  list_files: { icon: "▤", verb: "Listing", key: "directory" },
  find_files: { icon: "⌕", verb: "Finding", key: "name_pattern" },
  search_files: { icon: "⌕", verb: "Searching the code for", key: "text" },
  write_file: { icon: "✎", verb: "Writing", key: "path" },
  edit_file: { icon: "✎", verb: "Editing", key: "path" },
  run_command: { icon: "▸", verb: "Running", key: "command" },
  git_status: { icon: "⎇", verb: "git status", key: "" },
  git_diff: { icon: "⎇", verb: "Reading the git diff", key: "" },
  git_log: { icon: "⎇", verb: "Reading the git log", key: "" },
  web_search: { icon: "⌖", verb: "Searching the web for", key: "query" },
  fetch_url: { icon: "⌖", verb: "Fetching", key: "url" },
};

const el = (id) => document.getElementById(id);

const dom = {
  shell: el("app-shell"),
  messages: el("messages"),
  conversation: el("conversation"),
  jump: el("jump-latest"),
  prompt: el("prompt"),
  composer: el("composer"),
  send: el("send-button"),
  stop: el("stop-button"),
  hint: el("composer-hint"),
  tier: el("tier-select"),
  model: el("model-select"),
  sessionList: el("session-list"),
  newSession: el("new-session"),
  presence: el("presence"),
  presenceText: el("presence-text"),
  modelLabel: el("model-label"),
  dirLabel: el("dir-label"),
  sessionLabel: el("session-label"),
  tokensLabel: el("tokens-label"),
  ctxFill: el("ctx-fill"),
  ctxLabel: el("ctx-label"),
  stamp: el("status-stamp"),
  toasts: el("toasts"),
};

const overlays = {
  setup: el("setup-overlay"),
  diff: el("diff-overlay"),
  doctor: el("doctor-overlay"),
  tips: el("tips-overlay"),
  allowed: el("allowed-overlay"),
  files: el("files-overlay"),
  context: el("context-overlay"),
  settings: el("settings-overlay"),
  automation: el("automation-overlay"),
};

let state = null;
let stored = [];
let storedProblems = [];
/* One desk entry per session this tab has seen: the live card if a turn is
 * running in it, and any card that has to survive without a history entry
 * behind it (a failure). Keyed by session id, so a frame from a session the
 * user is not looking at still lands somewhere. */
const desks = new Map();
let approvals = [];
let armedDelete = "";
let connected = false;
let lastPromptTokens = 0;
/* True while a snapshot's timeline is being replayed into a work card
 * (DR-033). Toasts announce what is happening now; replaying them would
 * re-announce what already happened. */
let replaying = false;
let railTimer = 0;

function desk(id) {
  if (!id) return { run: null, extras: [] };
  if (!desks.has(id)) desks.set(id, { run: null, extras: [] });
  return desks.get(id);
}

const focusId = () => (state && state.focus) || "";
const focusedRun = () => desk(focusId()).run;
const isFocused = (id) => Boolean(id) && id === focusId();

/* The rail rows the server sent, addressable by id. */
function openEntry(id) {
  return ((state && state.open) || []).find((entry) => entry.session_id === id);
}

function runningElsewhere() {
  return ((state && state.open) || []).filter(
    (entry) => entry.running && entry.session_id !== focusId()
  );
}

/* ---------- transport ---------- */

async function api(path, body) {
  const options = {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-AutoSE-Token": TOKEN },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function connect() {
  // `since` is the frame id the snapshot we already drew ended at, so a
  // reconnect catches up instead of replaying a turn twice.
  const since = (state && state.sequence) || 0;
  const source = new EventSource(
    `/api/events?token=${encodeURIComponent(TOKEN)}&since=${since}`
  );
  source.onopen = () => {
    connected = true;
    renderStatus();
  };
  source.onerror = () => {
    connected = false;
    renderStatus();
  };
  source.onmessage = (message) => {
    let frame = null;
    try {
      frame = JSON.parse(message.data);
    } catch (error) {
      return;
    }
    handleFrame(frame);
  };
}

async function refreshState() {
  try {
    state = await api("/api/state");
  } catch (error) {
    toast(String(error.message || error), "bad");
    return;
  }
  adoptState();
}

async function refreshSessions() {
  try {
    const payload = await api("/api/sessions");
    stored = payload.sessions || [];
    storedProblems = payload.problems || [];
  } catch (error) {
    stored = [];
  }
  renderRail();
}

/* ---------- frames ---------- */

function handleFrame(frame) {
  const id = frame.session || "";
  switch (frame.kind) {
    case "open":
      connected = true;
      renderStatus();
      break;
    case "lagged":
      refreshState();
      break;
    case "state":
      state = frame.state;
      adoptState();
      refreshSessions();
      break;
    case "concurrent":
      // DR-031: two runs share one workspace, and their change journals are
      // separate — so an undo in one cannot see what the other wrote.
      toast(
        `${frame.sessions.length} sessions are working in this workspace at once. Each records its own file changes, so Undo covers only the session you run it from.`,
        "",
        "concurrent-runs"
      );
      break;
    case "busy": {
      const entry = openEntry(id);
      if (entry) {
        entry.running = Boolean(frame.running);
        entry.current = frame.current || null;
        if (!frame.running) entry.stopping = false;
      }
      // The authoritative "is a turn running" for everything that renders off
      // it — the composer, the tier and model pickers, the presence label, the
      // Stop control. The server only broadcasts a full state frame when a
      // turn *ends*, so without this line `state.running` stays false for the
      // whole run and every one of those reads the wrong answer.
      if (state && isFocused(id)) {
        state.running = Boolean(frame.running);
        if (!frame.running) state.stopping = false;
        stopRequested = false;
      }
      if (frame.running) {
        startRun(id, frame.current || {});
      } else {
        const seat = desk(id);
        if (seat.run) {
          seat.run.running = false;
          stopTimer(seat.run);
          scheduleRun(seat.run);
        }
      }
      // `renderHeader` reads `state.tiers`, and a first state fetch that failed
      // leaves `state` null while the stream still connects.
      if (state) {
        renderHeader();
        renderStatus();
        renderRail();
        renderRate();
      }
      break;
    }
    case "stopping": {
      const entry = openEntry(id);
      if (entry) entry.stopping = true;
      if (state && isFocused(id)) state.stopping = true;
      toast(
        isFocused(id)
          ? "Stopping after the current step. What it produced is kept."
          : `Stopping ${shortId(id)} after its current step.`
      );
      renderStatus();
      renderRail();
      break;
    }
    case "answer": {
      const active = desk(id).run;
      if (active) {
        noteStream(active, "write");
        active.answer += frame.text;
        scheduleRun(active);
      }
      break;
    }
    case "turn":
      markUnseen(id);
      refreshFileIndex();
      finishRun(id, frame.turn);
      notifyAbout(id, {
        title: frame.turn.succeeded ? "Run finished" : "Run did not finish",
        body: `${frame.turn.prompt || ""}${(frame.turn.files_changed || []).length ? ` · ${plural(frame.turn.files_changed.length, "file")} changed` : ""}`,
        tag: `turn-${id}`,
      });
      break;
    case "failed":
      markUnseen(id);
      failRun(id, frame.title || "Stopped", frame.message || "");
      notifyAbout(id, { title: `Run stopped: ${frame.title || "failed"}`, body: frame.message || "", tag: `turn-${id}` });
      break;
    case "approval":
      approvals.push({ ...frame.request, session: id });
      markUnseen(id);
      // DR-043: the one thing that cannot proceed without the person.
      notifyAbout(id, {
        title: "Needs you",
        body: `${humanize(String(frame.request.tool || "a call"))}: ${frame.request.summary || ""}`,
        tag: `ask-${frame.request.id}`,
        urgent: true,
        always: true,
      });
      if (!isFocused(id)) {
        toast(`Session ${shortId(id)} is waiting for your decision.`, "", `ask-${frame.request.id}`);
      }
      renderApproval();
      renderRail();
      break;
    case "approval_closed":
      approvals = approvals.filter((item) => item.id !== frame.id);
      renderApproval();
      renderRail();
      break;
    case "queue": {
      // DR-042: this session's waiting prompts changed.
      const entry = openEntry(id);
      if (entry) {
        entry.queued = (frame.queue || []).length;
        entry.queue_held = Boolean(frame.held);
      }
      if (frame.held && (frame.queue || []).length) {
        notifyAbout(id, {
          title: `Queue held: ${plural(frame.queue.length, "request")}`,
          body: "Resume them when you are ready.",
          tag: `queue-${id}`,
        });
      }
      if (state && isFocused(id)) {
        state.queue = frame.queue || [];
        state.queue_held = Boolean(frame.held);
        renderConversation();
      }
      renderRail();
      break;
    }
    case "plan_review":
      if (state && isFocused(id)) {
        state.pending_plan = frame.review;
        renderConversation();
      }
      {
        const entry = openEntry(id);
        if (entry) entry.plan_waiting = true;
      }
      notifyAbout(id, {
        title: "Plan ready for review",
        body: "Nothing has been changed yet. Approve it, ask for changes, or edit it.",
        tag: `plan-${frame.review.id}`,
        urgent: true,
        always: true,
      });
      renderRail();
      break;
    case "plan_review_closed": {
      if (state && isFocused(id)) state.pending_plan = null;
      const entry = openEntry(id);
      if (entry) entry.plan_waiting = false;
      planCards.delete(frame.id);
      if (state && isFocused(id)) renderConversation();
      renderRail();
      break;
    }
    case "automation": {
      // DR-047: the runner started or skipped something on its own.
      const summary = automationFrameSummary(frame);
      if (summary) {
        toast(summary, (frame.skipped || []).length && !(frame.ran || []).length ? "bad" : "");
        (frame.ran || []).forEach((item) =>
          notifyAbout(item.session, { title: `${item.name} started`, body: summary, tag: `automation-${item.id}` })
        );
      }
      if (!overlays.automation.hidden) loadAutomation();
      break;
    }
    case "allowances":
      if (state && isFocused(id)) {
        state.allowances = frame.allowances || [];
        renderNeedsYou();
      }
      break;
    case "event":
      handleEvent(id, frame);
      break;
    default:
      break;
  }
}

/* Deltas of a stream: not progress in their own right. */
const STREAM_EVENTS = new Set(["stage_output_delta", "agent_reasoning_delta", "round_usage"]);
/* Steps worth a drop of ink on the route. */
const STEP_EVENTS = new Set(["tool_called", "tool_completed", "stage_started", "stage_finished", "validation_verdict", "approval_resolved"]);

function handleEvent(id, frame) {
  const data = frame.data || {};
  const run = desk(id).run;
  if (
    !run &&
    ![
      "session_save_failed",
      "model_selected",
      "history_cleared",
      "tier_changed",
      "session_resumed",
      "context_window_changed",
    ].includes(frame.event)
  ) {
    // Events that belong to a turn we are not tracking (another tab started
    // it, or this one reloaded); the next state frame will catch us up.
    return;
  }
  if (run && !STREAM_EVENTS.has(frame.event)) {
    // Anything but a streamed token counts as progress: what "slow" is
    // measured from, and what drops ink on the current station.
    run.lastEventAt = Date.now();
    if (STEP_EVENTS.has(frame.event)) run.inkDrops = (run.inkDrops || 0) + 1;
  }
  switch (frame.event) {
    case "classification_started":
      setThinking(run, "Choosing a tier");
      break;
    case "classification_finished":
      run.tier = data.effective_tier || data.tier || run.tier;
      pushActivity(run, {
        icon: "◈",
        html: data.ratcheted
          ? `Classified as <em>${escapeHtml(String(data.tier))}</em>, raised to <em>${escapeHtml(String(data.effective_tier))}</em> for this session`
          : `Classified as <em>${escapeHtml(String(data.effective_tier || data.tier))}</em>`,
      });
      break;
    case "workflow_started":
      run.stages = (data.stages || []).map((name) => ({ name, title: humanize(name), status: "" }));
      scheduleRun(run);
      break;
    case "stage_started":
      run.stages.forEach((stage) => {
        if (stage.name === data.stage) {
          stage.status = "active";
          stage.title = data.title || stage.title;
        } else if (stage.status === "active") {
          stage.status = "done";
        }
      });
      run.narration = "";
      run.passes = run.passes || {};
      run.passes[data.stage] = (run.passes[data.stage] || 0) + 1;
      run.currentStage = String(data.stage || "");
      run.stageTimes[run.currentStage] = { start: frame.timestamp || Date.now() / 1000, end: 0 };
      setThinking(run, data.title || humanize(String(data.stage || "")));
      if (data.iteration > 1) {
        pushActivity(run, { icon: "↺", html: `${escapeHtml(data.title || humanize(String(data.stage)))} again, pass ${data.iteration}` });
      }
      break;
    case "stage_output_delta":
      noteStream(run, "write");
      run.narration += frame.message || "";
      scheduleRun(run);
      break;
    case "stage_retry":
      pushActivity(run, { kind: "fail", icon: "△", html: `Retrying ${escapeHtml(humanize(String(data.stage || "")))}: ${escapeHtml(frame.message || "")}` });
      break;
    case "stage_finished":
      if (run.stageTimes[data.stage]) run.stageTimes[data.stage].end = frame.timestamp || Date.now() / 1000;
      run.stages.forEach((stage) => {
        if (stage.name === data.stage) stage.status = "done";
      });
      if (data.content) run.notes[data.artifact || data.stage] = data.content;
      if (Array.isArray(data.requirements) && data.requirements.length) {
        noteRequirementChanges(run, data.requirements);
        run.requirements = data.requirements;
        run.ledger.push({
          title: data.title || humanize(String(data.stage || "")),
          statuses: Object.fromEntries(data.requirements.map((item) => [item.id, item.status])),
        });
      }
      pushActivity(run, { icon: "☰", html: `${escapeHtml(data.title || frame.message || humanize(String(data.stage)))} done` });
      break;
    case "validation_verdict":
      run.verdict = data;
      pushActivity(run, {
        kind: data.passed ? "pass" : "fail",
        icon: data.passed ? "✓" : "✗",
        html: data.passed
          ? "Checked the work against the plan — it holds"
          : `Checked the work: ${escapeHtml(String((data.unmet || []).join(", ") || "gaps remain"))}`,
      });
      break;
    case "loopback": {
      // The verdict just before it names the requirements that sent it back.
      const unmet = (run.verdict && run.verdict.unmet) || [];
      run.loopbacks.push({ ...data, unmet, at: Date.now() });
      pushActivity(run, {
        kind: "fail",
        icon: "↩",
        html: `Sent back to ${escapeHtml(humanize(String(data.target || "")))}${unmet.length ? `: ${escapeHtml(unmet.join(", "))} unmet` : ""}`,
      });
      break;
    }
    case "tool_called":
      pushActivity(run, { ...describeTool(String(data.tool || ""), data.arguments || {}), stepless: true });
      run.steps.push({
        type: "tool",
        id: String(data.call_id || `step-${run.steps.length}`),
        tool: String(data.tool || ""),
        args: data.arguments || {},
        stage: run.currentStage,
        status: "running",
      });
      scheduleRun(run);
      break;
    case "tool_completed": {
      // Matched by call id; an older server without ids pairs the newest
      // still-running call of the same tool.
      const step = [...run.steps]
        .reverse()
        .find(
          (item) =>
            item.type === "tool" &&
            item.status === "running" &&
            (data.call_id ? item.id === String(data.call_id) : item.tool === data.tool)
        );
      if (step) {
        step.status = data.failed ? "failed" : "done";
        step.preview = typeof data.preview === "string" ? data.preview : undefined;
        step.elapsedMs = typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined;
        step.truncated = Boolean(data.truncated);
        step.artifact = data.artifact || "";
        scheduleRun(run);
      }
      if (data.truncated) {
        pushActivity(run, { icon: "▭", html: `${escapeHtml(humanize(String(data.tool || "tool")))} returned more than fits; kept the head and the tail` });
      }
      break;
    }
    case "approval_requested":
      setThinking(run, "Needs you");
      break;
    case "approval_resolved": {
      // Back to the stage it was in: "Needs you" is only true until the
      // answer arrives, and with auto-approve it never was.
      const active = (run.stages || []).find((stage) => stage.name === run.currentStage);
      setThinking(run, active ? active.title : run.currentStage ? humanize(run.currentStage) : "Thinking");
      if (!data.approved) {
        // A refused call never ran: its step is struck through, not failed.
        const refused = [...run.steps]
          .reverse()
          .find((item) => item.type === "tool" && item.status === "running" && item.tool === data.tool);
        if (refused) refused.status = "refused";
      }
      pushActivity(run, {
        kind: data.approved ? "pass" : "denied",
        icon: data.approved ? "✓" : "⊘",
        html: `${data.approved ? "Approved" : "Refused"} ${escapeHtml(String(data.tool || ""))}${data.reason ? ` — ${escapeHtml(String(data.reason))}` : ""}`,
      });
      break;
    }
    // DR-033: tokens as they are spent, not only when a stage ends — a long
    // stage otherwise shows zero for minutes and reads as "nothing happening".
    case "round_usage":
      run.rounds.push({
        stage: String(data.stage || ""),
        prompt: data.prompt_tokens || 0,
        completion: data.completion_tokens || 0,
        estimated: data.estimated_input_tokens || 0,
        limit: data.context_limit || 0,
      });
      run.liveTokens = (run.liveTokens || 0) + (data.prompt_tokens || 0) + (data.completion_tokens || 0);
      {
        const before = run.lastPrompt || 0;
        run.lastPrompt = data.prompt_tokens || data.estimated_input_tokens || before;
        noteRound(run, data.completion_tokens || 0, before, run.lastPrompt, data.context_limit || (state && state.context_limit) || 0);
      }
      if (isFocused(id)) {
        lastPromptTokens = data.prompt_tokens || data.estimated_input_tokens || lastPromptTokens;
        renderStatus();
      }
      break;
    // Covered by the `busy`, `turn` and `state` frames, which carry the same
    // facts with the payload the card needs; named so their absence from the
    // switch reads as a decision rather than an oversight.
    case "turn_started":
    case "turn_finished":
    case "workflow_finished":
      break;
    case "context_window_changed":
      reloadPending = true;
      toast(`Context window is now ${formatWindow(data.tokens)} tokens.`, "good");
      break;
    case "tier_changed":
      if (!run) toast(`Tier is now ${humanize(String(data.tier || ""))}.`, "good");
      break;
    case "session_resumed":
      toast(`Reopened session ${shortId(String(data.session_id || ""))} (${plural(data.turns || 0, "earlier run")}).`, "good");
      break;
    case "agent_reasoning_delta":
      noteStream(run, "think");
      run.thinking += frame.message || "";
      scheduleRun(run);
      break;
    case "agent_reasoning":
      run.thinking += `${frame.message || ""}\n`;
      scheduleRun(run);
      break;
    case "agent_nudged":
      pushActivity(run, { icon: "·", html: escapeHtml(frame.message || "Asked it to continue") });
      break;
    // A refused repeat is the visible run-up to a stall. Without these the run
    // ends on a verdict with nothing behind it.
    case "tool_call_repeated":
      pushActivity(run, { kind: "fail", icon: "⊘", html: escapeHtml(frame.message || "Refused a repeated call") });
      break;
    // DR-039: a retried tool call is visible, not silently absorbed.
    case "tool_call_malformed":
      pushActivity(run, {
        kind: "fail",
        icon: "△",
        html: `The model wrote a tool call the runtime could not parse; asked it to write it again (${escapeHtml(String(data.attempt || 1))}/3)`,
      });
      break;
    case "agent_stalled":
      pushActivity(run, { kind: "fail", icon: "△", html: escapeHtml(frame.message || "Stopped going in circles") });
      break;
    case "agent_context_thrashing":
      pushActivity(run, { kind: "fail", icon: "△", html: escapeHtml(frame.message || "The context window is too small") });
      toast("The context window is too small to hold what it reads — raise it and run this again.", "bad", "ctx-thrash");
      break;
    case "agent_round_cap_reached":
      pushActivity(run, { kind: "fail", icon: "△", html: escapeHtml(frame.message || "Hit the tool-round cap") });
      break;
    // DR-030: the stop shows up in the feed both when it is asked for and when
    // it lands, because a round of inference can sit between the two.
    case "stop_requested":
      pushActivity(run, { icon: "△", html: escapeHtml(frame.message || "Stopping") });
      break;
    case "agent_cancelled":
      pushActivity(run, {
        kind: "fail",
        icon: "△",
        html: escapeHtml(`${data.stage || "It"} stopped after ${data.rounds || 0} round(s) — keeping what it had`),
      });
      break;
    case "context_truncated_by_runtime":
      pushActivity(run, { kind: "fail", icon: "△", html: "The runtime is serving a smaller context window than it reported" });
      toast("The runtime silently trimmed the request — raise its context window (see the machine check).", "bad");
      break;
    case "changes_reverted":
      toast(`Undid changes to ${plural((data.restored || []).length, "file")}.`, "good");
      break;
    case "model_selected":
      reloadPending = true;
      toast(`Model is now ${data.model}.`, "good");
      break;
    case "session_save_failed":
      toast(`This session could not be saved: ${frame.message}`, "bad");
      break;
    default:
      break;
  }
}

/* ---------- state ---------- */

function adoptState() {
  if (!state) return;
  // Sessions the server no longer has open cannot receive frames again, so
  // their cards are dropped rather than kept as a leak.
  const live = new Set((state.open || []).map((entry) => entry.session_id));
  Array.from(desks.keys()).forEach((id) => {
    if (!live.has(id)) {
      const seat = desks.get(id);
      if (seat.run) stopTimer(seat.run);
      desks.delete(id);
    }
  });
  approvals = mergeApprovals(state);

  const seat = desk(focusId());
  if (state.running && !seat.run && focusId()) {
    // Joined mid-turn: rebuild what we can from the snapshot and let the
    // stream fill in the rest.
    const current = state.current || {};
    startRun(focusId(), current);
    seat.run.answer = state.answer || "";
    // DR-033: the snapshot carries what this turn has done so far. Replaying
    // it through the same handler the stream uses draws the card exactly as a
    // tab that watched from the start would have it.
    replaying = true;
    try {
      (current.timeline || []).forEach((frame) => handleEvent(focusId(), frame));
    } finally {
      replaying = false;
    }
    seat.run.narration = current.narration || seat.run.narration;
    seat.run.thinking = current.thinking || seat.run.thinking;
    scheduleRun(seat.run);
  }
  if (!state.running && seat.run && seat.run.running) {
    seat.run.running = false;
    stopTimer(seat.run);
  }
  if (!state.running) stopRequested = false;
  renderHeader();
  renderStatus();
  renderConversation();
  renderRail();
  renderApproval();
  overlays.setup.hidden = Boolean(state.ready);
  if (!state.ready) {
    el("setup-problem").textContent = state.problem || "No runtime is serving a model.";
  }
  renderSetupWarnings();
}

/* Standing configuration problems, shown until they stop being true.
 *
 * These used to be toasts only. A toast for "the runtime is serving 4,096 of
 * the model's 262,144 tokens" is worse than nothing: it fades, and the run it
 * predicted then fails minutes later with a verdict — "Stopped — Stalled" —
 * that reads as the model's fault and gives the user nothing to act on. */
function renderSetupWarnings() {
  const box = el("setup-warnings");
  if (!box) return;
  const messages = state && Array.isArray(state.warnings) ? state.warnings : [];
  box.hidden = messages.length === 0;
  box.innerHTML = messages
    .map(
      (message) =>
        `<div class="setup-warning"><span class="setup-warning-mark">△</span>` +
        `<span>${escapeHtml(String(message))}</span></div>`
    )
    .join("");
}

/* Merged by id, never replaced. A snapshot carries the focused session's parked
 * approvals in full and every other session's only as a count, so treating it
 * as the whole truth would drop two things: what this tab already knows about
 * the unfocused sessions, and — the one that actually loses a call — an
 * approval whose frame arrived after the snapshot was taken. That call would
 * then sit unseen until DR-012's timeout denied it. Removal is the
 * `approval_closed` frame's job, plus the server telling us it is gone when we
 * try to answer it. */
function mergeApprovals(snapshot) {
  const known = new Map(approvals.map((item) => [item.id, item]));
  (snapshot.pending_approvals || []).forEach((item) => {
    known.set(item.id, { ...item, session: snapshot.focus || "" });
  });
  const live = new Set((snapshot.open || []).map((entry) => entry.session_id));
  return Array.from(known.values()).filter((item) => live.has(item.session));
}

/* ---------- header + status ---------- */

/* What each tier does, short enough for the picker; the full summary is its
 * tooltip. */
const TIER_SHORT = {
  spark: "answer, or one small edit",
  craft: "Plan → Execute → Validate",
  forge: "Design, then as craft",
};

function renderHeader() {
  const tiers = state.tiers || [];
  const chosen = state.auto_classify ? "auto" : state.tier;
  // The tier as a switch: Auto, then each tier, with what it does on hover.
  const locked = !state.ready || state.running;
  setHtml(
    dom.tier,
    [
      { name: "auto", label: "Auto", title: "Pick a tier for each request" },
      ...tiers.map((tier) => ({ name: tier.name, label: humanize(tier.name), title: `${tier.name}: ${TIER_SHORT[tier.name] || tier.summary}` })),
    ]
      .map(
        (item) =>
          `<button type="button" data-tier="${escapeHtml(item.name)}" aria-pressed="${item.name === chosen}" title="${escapeHtml(item.title)}"${locked ? " disabled" : ""}>${tierIconMarkup(item.name, 15)}${escapeHtml(item.label)}</button>`
      )
      .join("")
  );

  // DR-036: every usable provider's models, grouped, once /api/providers has
  // answered; until then the focused session's own provider's list.
  const current = { provider: state.provider || "", model: state.model || "" };
  if (providers) {
    dom.model.innerHTML = modelPickerMarkup(providers, current);
  } else {
    const names = (state.models || []).filter(Boolean).map((model) => model.name);
    if (state.model && !names.includes(state.model)) names.unshift(state.model);
    dom.model.innerHTML = names
      .map((name) => `<option value="${escapeHtml(`${current.provider}|${name}`)}">${escapeHtml(name)}</option>`)
      .join("");
  }
  dom.model.value = `${current.provider}|${current.model}`;
  dom.model.disabled = !state.ready || state.running || dom.model.options.length < 2;

  // DR-031: a session running is no reason not to open another one. The only
  // thing that closes this button is the desk being full.
  const full = (state.open || []).length >= (state.max_open || 6);
  dom.newSession.disabled = !state.ready || full;
  dom.newSession.title = full
    ? `${state.max_open} sessions are open — close one first`
    : "Open another session; anything already running keeps going";
  dom.shell.classList.toggle("is-running", Boolean(state.running));
}

/* The page warms as the context window fills: the paper takes on a little
 * heat from about half full, so a session running out of room is felt before
 * it is read. One custom property; the stylesheet does the rest. */
function setWarmth(percent) {
  const warmth = Math.max(0, Math.min(1, (percent - 45) / 50));
  document.documentElement.style.setProperty("--warmth", warmth.toFixed(2));
}

function renderStatus() {
  if (!state) return;
  const usage = state.usage || {};
  dom.modelLabel.textContent = state.model || "none";
  dom.modelLabel.title = `${state.provider || ""} ${state.base_url || ""}`.trim();
  dom.dirLabel.textContent = shortenPath(state.workspace || "");
  dom.dirLabel.title = state.workspace || "";
  dom.sessionLabel.textContent = state.session_id ? shortId(state.session_id) : "—";
  // The session's settled total plus what the running turn has spent so far;
  // the turn's share moves into `usage` when it finishes (DR-033).
  const spending = (focusedRun() && focusedRun().liveTokens) || 0;
  dom.tokensLabel.textContent = formatCount((usage.total_tokens || 0) + spending);

  const limit = state.context_limit || 0;
  if (limit && lastPromptTokens) {
    const percent = Math.min(100, Math.round((lastPromptTokens / limit) * 100));
    dom.ctxFill.style.width = `${percent}%`;
    dom.ctxFill.className = `ctx-fill${percent > 90 ? " hot" : percent > 70 ? " warn" : ""}`;
    dom.ctxLabel.textContent = `${percent}% of ${formatWindow(limit)}`;
    setWarmth(percent);
    dom.ctxLabel.title = `${formatCount(lastPromptTokens)} of ${formatCount(limit)} tokens last round`;
  } else {
    dom.ctxFill.style.width = "0%";
    dom.ctxLabel.textContent = limit ? `${formatWindow(limit)} window` : "—";
    setWarmth(0);
  }
  // DR-034: the mark carries the advice level — calm for a deliberate cap,
  // loud only for a window that will actually hurt.
  const advice = state.context_advice;
  const mark = el("ctx-mark");
  mark.className = `ctx-mark ${advice ? advice.level : ""}`.trim();
  el("status-ctx").title = advice
    ? `${advice.summary} Click for details.`
    : "How full the context window was last round";

  const run = focusedRun();
  const elsewhere = runningElsewhere();
  const waitingHere = approvals.some((item) => item.session === focusId());
  let label = "Ready";
  let mood = "";
  // Fail-safe, as a signal box's diagram is: with the stream down nothing
  // drawn here can be trusted to be current, so every session shows unknown
  // rather than its last known state.
  document.body.classList.toggle("link-lost", !connected);
  if (!connected) {
    label = "Disconnected";
    mood = "offline";
  } else if (waitingHere) {
    label = "Needs you";
    mood = "attention";
  } else if (state.running) {
    label = (run && run.thinkingLabel) || "Working";
    mood = "working";
  } else if (!state.ready) {
    label = "No model";
    mood = "offline";
  }
  if (label === "Ready" && document.body.classList.contains("resting")) label = "Resting";
  // Somebody else's run is not this session's status, but it must be visible:
  // the whole point of DR-031 is that work continues where you are not looking.
  if (elsewhere.length) {
    label += ` · ${elsewhere.length} working elsewhere`;
    if (!mood) mood = "aside";
  }
  dom.presence.className = `presence ${mood}`.trim();
  dom.presenceText.textContent = label;
  dom.presence.title = connected
    ? ""
    : "The page lost its connection to AutoSE and is reconnecting. Until it does, what it shows may be out of date.";

  // DR-012: the posture this run is in belongs on screen, not only in the
  // terminal that started it. An auto-approved dashboard never interrupts,
  // which is exactly why it has to say so.
  // DR-053: YOLO mode takes over the whole page while it is on.
  const yolo = Boolean(state.yolo);
  document.body.classList.toggle("yolo", yolo);
  el("toggle-yolo").setAttribute("aria-pressed", String(yolo));
  el("yolo-word").textContent = yolo ? "YOLO on" : "YOLO";
  el("toggle-yolo").title = yolo
    ? "YOLO mode is on: nothing asks. Click to turn it off and ask again."
    : "YOLO mode: let every command, file change, web call and plan run without asking";
  if (yolo) {
    dom.stamp.textContent = "Nothing asks";
    dom.stamp.classList.add("auto-approve");
    dom.stamp.title = "YOLO mode: every call and plan in every open session runs without asking.";
  } else if (state.auto_approve) {
    dom.stamp.textContent = "Auto-approve on";
    dom.stamp.classList.add("auto-approve");
    dom.stamp.title = `Shell and network calls run without asking, authorised by ${state.authorized_by || "whoever started this"}.`;
    toast(
      `Auto-approve is on: shell and network calls run without asking (authorised by ${state.authorized_by || "the operator"}).`,
      "bad",
      "auto-approve"
    );
  } else {
    dom.stamp.textContent = "Local · no cloud";
    dom.stamp.classList.remove("auto-approve");
    dom.stamp.title = `These tools always ask first: ${(state.gated_tools || []).join(", ")}.`;
    if (state.require_edit_approval) {
      toast("This workspace asks before every file write.", "", "edit-approval");
    }
  }

  // Deliberately *not* disabled while a turn runs: someone mid-sentence should
  // not have the box taken away from them, and `submitPrompt` refuses a second
  // request itself with a reason. Only an unusable dashboard locks it.
  dom.prompt.disabled = !state.ready;
  dom.send.disabled = !state.ready;
  el("review-plans").checked = Boolean(state.review_plans);
  dom.send.textContent = state.running ? "Queue" : "Start";
  // DR-030: a run has no built-in end, so Stop *takes the primary button's
  // place* while one is going rather than sitting beside a dead "Working…"
  // button — the control is where the hand already is. `stopping` comes from
  // the snapshot, so a reopened tab does not offer a button already pressed.
  const stopping = Boolean(state.stopping || stopRequested);
  dom.send.hidden = false;
  dom.stop.hidden = !state.running;
  dom.stop.disabled = stopping;
  dom.stop.textContent = stopping ? "Stopping…" : "Stop";
  dom.hint.textContent = state.running
    ? (stopping
      ? "Stopping after this step; what it produced is kept"
      : "Enter queues it for after this run")
    : "Enter starts · Shift+Enter for a new line";
  renderCardStop();
}

/* ---------- the rail ---------- */

/* The session whose name is being edited in the list, if any. */
let renamingId = "";

function renameRowMarkup(id, title) {
  return `<div class="sheet-row renaming"><form class="rename-form" data-rename-form="${escapeHtml(id)}">` +
    `<input class="rename-input" type="text" maxlength="80" value="${escapeHtml(title)}" aria-label="Session name" />` +
    `<span class="rename-hint">Enter saves · Esc cancels · empty goes back to the first request</span></form></div>`;
}

function startRename(id) {
  renamingId = id;
  renderRail();
  const input = dom.sessionList.querySelector(".rename-input");
  if (input) {
    input.focus();
    input.select();
  }
}

async function finishRename(id, name) {
  if (renamingId !== id) return;
  renamingId = "";
  const result = await post("/api/session", { action: "rename", id, name });
  if (result) {
    stored = result.sessions || stored;
    refreshSessions();
  }
  renderRail();
}

/* ---------- the session bar ---------- */

function renderSessionBar() {
  if (!state) return;
  const id = focusId();
  const entry = openEntry(id);
  const title = entry ? entry.title : "New session";
  setHtml(el("sb-badge"), tierBadgeMarkup((entry && entry.tier) || state.tier || "", entry && entry.running ? "busy" : ""));
  el("sb-no").textContent = id ? sheetNo(id) : "";
  el("sb-title").textContent = title;
  el("sb-name").title = `Rename this session (${title})`;
  const allowed = (state.allowances || []).length;
  el("sb-allowed-count").textContent = allowed ? String(allowed) : "";
  el("open-allowed").classList.toggle("has-items", allowed > 0);
  el("open-allowed").title = allowed
    ? `${plural(allowed, "rule")}: what runs without asking in this session`
    : "Nothing runs without asking yet; add a rule here";
  const files = new Set();
  (state.history || []).forEach((turn) => (turn.changes || []).forEach((change) => files.add(change.path)));
  el("sb-changes-count").textContent = files.size ? String(files.size) : "";
  el("open-diff").disabled = !files.size;
  el("open-diff").title = files.size
    ? `${plural(files.size, "file")} changed in this session: see the diffs`
    : "Nothing changed in this session yet";
  el("clear-history").disabled = !(state.history || []).length || Boolean(state.running);
}

function startBarRename() {
  const entry = openEntry(focusId());
  el("sb-rename-input").value = entry ? entry.title : "";
  el("sb-name").hidden = true;
  el("sb-rename").hidden = false;
  el("sb-rename-input").focus();
  el("sb-rename-input").select();
}

async function finishBarRename(save) {
  if (el("sb-rename").hidden) return;
  el("sb-rename").hidden = true;
  el("sb-name").hidden = false;
  if (!save) return;
  const result = await post("/api/session", { action: "rename", id: focusId(), name: el("sb-rename-input").value.trim() });
  if (result) refreshSessions();
}

el("sb-name").addEventListener("click", startBarRename);
el("sb-rename").addEventListener("submit", (event) => {
  event.preventDefault();
  finishBarRename(true);
});
el("sb-rename-input").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.stopPropagation();
    finishBarRename(false);
  }
});
el("sb-rename-input").addEventListener("blur", () => finishBarRename(true));

function renderRail() {
  renderSessionBar();
  // A name being typed is not repainted away by the rail's clock. Only the
  // name box counts: a real click first focuses the pencil that opened it,
  // and that must not stop the box from being drawn.
  const typing = document.activeElement && document.activeElement.classList.contains("rename-input");
  if (renamingId && typing) return;
  if (!document.hidden) unseen.delete(focusId());
  renderWaitingCount();
  renderInbox();
  renderNeedsYou();
  const open = (state && state.open) || [];
  const openIds = new Set(open.map((entry) => entry.session_id));
  const rest = stored.filter((record) => !openIds.has(record.id));
  const blocks = storedProblems.map((problem) => `<p class="empty-state">${escapeHtml(problem)}</p>`);

  // The set, by what each sheet needs: you first, then what is being drawn,
  // then what is open and quiet, then what is filed away.
  const bays = [
    ["Waiting for you", "you", open.filter(sheetWaits)],
    ["Working", "", open.filter((entry) => !sheetWaits(entry) && entry.running)],
    ["Open", "", open.filter((entry) => !sheetWaits(entry) && !entry.running)],
  ];
  bays.forEach(([label, kind, entries]) => {
    if (!entries.length) return;
    blocks.push(groupHead(label, entries.length, kind));
    entries.forEach((entry) => blocks.push(openRowMarkup(entry)));
  });
  if (rest.length) {
    blocks.push(groupHead("Closed · click to reopen", rest.length, ""));
    rest.forEach((record) => blocks.push(storedRowMarkup(record)));
  }
  if (!open.length && !rest.length) {
    blocks.push(
      `<p class="empty-state">No sessions yet. Each one is saved to <span class="mono">.autose/sessions/</span> and can be reopened.</p>`
    );
  }
  dom.sessionList.innerHTML = blocks.join("");
  scheduleRailTicks(open.some((entry) => entry.running));
}

/* ---------- needs you: everything waiting on the person, from every session ---------- */

let railView = "sessions";

function setRailView(view) {
  railView = view;
  el("show-sessions").classList.toggle("on", view === "sessions");
  el("show-inbox").classList.toggle("on", view === "inbox");
  el("rail-title").textContent = view === "inbox" ? "Needs you" : "Sessions";
  dom.sessionList.hidden = view === "inbox";
  el("inbox-list").hidden = view !== "inbox";
  dom.newSession.hidden = view === "inbox";
  dom.shell.classList.add("rail-open");
  renderInbox();
}

function renderInbox() {
  const open = (state && state.open) || [];
  const entryOf = (id) => open.find((item) => item.session_id === id);
  const from = (id) => {
    const entry = entryOf(id);
    return `<button type="button" class="inbox-from" data-focus="${escapeHtml(id)}" title="Open this session">` +
      `${escapeHtml(sheetNo(id))} · ${escapeHtml(entry ? entry.title : shortId(id))}</button>`;
  };
  const items = approvals.map((request) => {
    const args = request.arguments || {};
    const keys = Object.keys(args);
    const shown = keys.length === 1 && typeof args[keys[0]] === "string" ? args[keys[0]] : request.summary || "";
    return `<article class="inbox-item ask">` +
      `<div class="inbox-kind">${escapeHtml(approvalQuestion(request.tool))}</div>${from(request.session)}` +
      `<pre class="inbox-args"><code>${linkifyPaths(shown)}</code></pre>` +
      (request.allow ? "" : `<p class="inbox-note">Only once: ${escapeHtml(request.allow_note || "")}.</p>`) +
      `<div class="inbox-actions">` +
      `<button type="button" class="primary-button small" data-decide="once" data-id="${escapeHtml(request.id)}">Run it</button>` +
      (request.allow
        ? `<button type="button" class="ghost-button small" data-decide="session" data-id="${escapeHtml(request.id)}" title="Run this, and later ${escapeHtml(request.allow.label)} in that session, without asking">${allowButtonMarkup(request.allow).replace(/ here$/, "")}</button>`
        : "") +
      `<button type="button" class="ghost-button small" data-decide="deny" data-id="${escapeHtml(request.id)}">Don't run</button>` +
      `</div>` +
      (request.session !== focusId()
        ? `<button type="button" class="inline-link inbox-open" data-focus="${escapeHtml(request.session)}">Open this session →</button>`
        : "") +
      `</article>`;
  });
  open
    .filter((entry) => entry.plan_waiting)
    .forEach((entry) =>
      items.push(
        `<article class="inbox-item plan"><div class="inbox-kind">A plan to look over</div>${from(entry.session_id)}` +
        `<p class="inbox-note">Nothing has been changed yet. Approve it, ask for changes, or edit it.</p>` +
        `<div class="inbox-actions"><button type="button" class="primary-button small" data-focus="${escapeHtml(entry.session_id)}">Review the plan</button></div></article>`
      )
    );
  open
    .filter((entry) => entry.queue_held && entry.queued)
    .forEach((entry) =>
      items.push(
        `<article class="inbox-item queue"><div class="inbox-kind">Queue held</div>${from(entry.session_id)}` +
        `<p class="inbox-note">${plural(entry.queued, "request")} waiting: the last run stopped, so they wait for you.</p>` +
        `<div class="inbox-actions"><button type="button" class="primary-button small" data-inbox-resume="${escapeHtml(entry.session_id)}">Resume</button></div></article>`
      )
    );
  const badge = el("inbox-count");
  badge.hidden = !items.length;
  badge.textContent = String(items.length);
  el("show-inbox").classList.toggle("has-items", items.length > 0);
  if (railView !== "inbox") return;
  setHtml(
    el("inbox-list"),
    items.length
      ? items.join("")
      : `<p class="empty-state">Nothing needs you. Commands to approve, plans to review and held queues from every session show up here.</p>`
  );
}

function groupHead(label, count, kind) {
  return `<div class="session-group ${kind || ""}"><span class="lbl">${escapeHtml(label)}</span><span class="lbl">${count}</span></div>`;
}

/* ---------- notifications (DR-043) ---------- */

/* Raise a desktop notification about session `id` when it is news: the page
 * is hidden, or the session is not the one on screen. `always` covers an
 * approval, which is worth raising for a visible page too when the window
 * itself is not focused. */
function notifyAbout(id, { title, body, tag, urgent = false, always = false }) {
  const elsewhere = document.hidden || !isFocused(id) || (always && !document.hasFocus());
  if (!elsewhere) return;
  const entry = openEntry(id);
  raiseNotification({
    title,
    body: entry && entry.title ? `${entry.title}\n${body}` : body,
    tag,
    urgent,
    onOpen: () => {
      if (!isFocused(id)) post("/api/session", { action: "focus", id });
    },
  });
}

/* What waits for the person, as a number in the tab title. */
function renderWaitingCount() {
  const open = (state && state.open) || [];
  const held = open.filter((entry) => entry.queue_held && entry.queued).length;
  const plans = open.filter((entry) => entry.plan_waiting).length;
  setTitleCount(approvals.length + held + plans);
}

function renderNotifyButton() {
  const button = el("toggle-notify");
  const supported = notifySupported();
  const on = notifyActive();
  const denied = supported && Notification.permission === "denied";
  button.setAttribute("aria-pressed", String(on));
  button.classList.toggle("on", on);
  button.hidden = !supported;
  button.title = !supported
    ? "This browser cannot show notifications"
    : denied
      ? "Notifications are blocked for this page in the browser's site settings"
      : on
        ? "Notifications on — click to turn off"
        : "Notify me when something waits for me";
}


/* Whether a sheet is held on the person: a call, a plan, a held queue. */
function sheetWaits(entry) {
  const waiting = Math.max(
    entry.waiting || 0,
    approvals.filter((item) => item.session === entry.session_id).length
  );
  return Boolean(waiting || entry.plan_waiting || (entry.queue_held && entry.queued));
}

/* One sheet in the set: its line figure, number, title, and what it is doing. */
function openRowMarkup(entry) {
  const active = entry.session_id === focusId();
  const strip = entry.strip || {};
  const waiting = Math.max(
    entry.waiting || 0,
    approvals.filter((item) => item.session === entry.session_id).length
  );
  const needsYou = sheetWaits(entry);
  let line = "";
  let tone = "";
  if (waiting) {
    line = waiting === 1 ? "Wants to run something" : `${waiting} calls need you`;
    tone = "you";
  } else if (entry.plan_waiting) {
    line = "A plan to look over";
    tone = "you";
  } else if (needsYou) {
    line = `${plural(entry.queued, "request")} waiting to resume`;
    tone = "you";
  } else if (entry.running) {
    const stage = strip.stage ? strip.stage.title : "Starting";
    const since = entry.current && entry.current.started_at ? formatElapsed(Date.now() - entry.current.started_at * 1000) : "";
    line = `${stage}${since ? ` · ${since}` : ""}${strip.context_pct ? ` · ctx ${strip.context_pct}%` : ""}`;
    tone = "working";
  } else {
    line = strip.requirements
      ? `${strip.requirements.verified} of ${strip.requirements.total} checks hold`
      : `${plural(entry.turns, "run")} · ${formatCount(entry.total_tokens)} tok`;
    if (strip.requirements && strip.requirements.failed) tone = "failed";
  }
  const auto = entry.automation ? (entry.automation.type === "watcher" ? " · watcher" : " · scheduled") : "";
  if (renamingId === entry.session_id) return renameRowMarkup(entry.session_id, entry.title);
  return `<div class="sheet-row${active ? " active" : ""}${needsYou ? " waiting" : ""}">
      ${needsYou ? `<svg class="waitline" aria-hidden="true"><rect x="1" y="1" rx="10" ry="10"></rect></svg>` : ""}
      <button class="sheet-main" type="button" data-focus="${escapeHtml(entry.session_id)}" title="${escapeHtml(entry.title)}">
        ${tierBadgeMarkup(entry.tier, entry.running ? "busy" : "")}${unseenDotMarkup(entry.session_id)}
        <span class="sheet-no">${escapeHtml(sheetNo(entry.session_id))} · ${escapeHtml(entry.tier || "")}${auto}</span>
        <span class="sheet-title">${escapeHtml(entry.title)}</span>
        <span class="sheet-state ${tone}">${escapeHtml(line)}</span>
      </button>
      <button class="session-rename" type="button" data-rename="${escapeHtml(entry.session_id)}" title="Rename this session" aria-label="Rename"><svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 4.5l3 3L7 16H4v-3z"/></svg></button>
      <button class="session-close" type="button" data-close="${escapeHtml(entry.session_id)}" title="${entry.running ? "Stop it before closing it" : "Close this session; it stays on disk"}">×</button>
    </div>`;
}

function storedRowMarkup(record) {
  const armed = armedDelete === record.id;
  const failed = record.stop_reason && record.stop_reason !== "completed";
  if (renamingId === record.id) return renameRowMarkup(record.id, record.title || "");
  return `<div class="sheet-row stored">
      <button class="sheet-main" type="button" data-resume="${escapeHtml(record.id)}" title="${escapeHtml(record.title)}">
        ${tierBadgeMarkup(record.tier, "closed")}
        <span class="sheet-no">${escapeHtml(sheetNo(record.id))} · ${escapeHtml(record.tier || "")} · ${formatWhen(record.updated_at)}</span>
        <span class="sheet-title">${escapeHtml(record.title || "(no title)")}</span>
        <span class="sheet-state${failed ? " failed" : ""}">${plural(record.turns, "run")} · ${formatCount(record.total_tokens)} tok</span>
      </button>
      <button class="session-rename" type="button" data-rename="${escapeHtml(record.id)}" title="Rename this session" aria-label="Rename"><svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 4.5l3 3L7 16H4v-3z"/></svg></button>
      <button class="session-delete${armed ? " armed" : ""}" type="button" data-delete="${escapeHtml(record.id)}" title="${armed ? "Click again to delete" : "Delete this saved session"}">${armed ? "!" : "×"}</button>
    </div>`;
}

/* A running session's row shows how long it has been going, and that only
 * moves if something repaints it. One timer for the whole rail, and only while
 * something is actually running. */
function scheduleRailTicks(needed) {
  if (needed && !railTimer) {
    railTimer = window.setInterval(renderRail, 1000);
  } else if (!needed && railTimer) {
    window.clearInterval(railTimer);
    railTimer = 0;
  }
}

/* ---------- the conversation ---------- */

function renderConversation() {
  const pinned = isPinnedToBottom();
  const history = state.history || [];
  const seat = desk(focusId());
  const nodes = [];
  if (!history.length && !seat.extras.length && !seat.run) nodes.push(greetingNode());
  history.forEach((turn) => nodes.push(receiptNode(turn)));
  seat.extras.forEach((node) => nodes.push(node));
  if (seat.run) nodes.push(seat.run.node);
  const planCard = planNode();
  if (planCard) nodes.push(planCard);
  approvalNodes().forEach((node) => nodes.push(node));
  const queued = queueNode();
  if (queued) nodes.push(queued);
  dom.messages.replaceChildren(...nodes);
  if (seat.run) renderRun();
  else renderInspector();
  if (pinned) scrollToBottom();
}

/* DR-042: what waits for the running turn, editable in place. */
function queueNode() {
  const queue = (state && state.queue) || [];
  if (!queue.length) return null;
  const node = document.createElement("section");
  node.className = `queue-strip${state.queue_held ? " held" : ""}`;
  const head = state.queue_held
    ? `<div class="queue-head"><span class="queue-title">Paused: ${plural(queue.length, "request")} up next</span>
        <span class="queue-note">The last run stopped, so these wait for you.</span>
        <button type="button" class="primary-button small" data-queue="resume">Resume</button></div>`
    : `<div class="queue-head"><span class="queue-title">Up next</span>
        <span class="queue-note">They start one after another when this run ends.</span></div>`;
  const items = queue
    .map(
      (item, index) =>
        `<div class="queue-item"><span class="queue-index">${index + 1}</span>` +
        `<span class="queue-prompt" title="${escapeHtml(item.prompt)}">${escapeHtml(item.prompt)}</span>` +
        `<span class="queue-tools">` +
        `<button type="button" class="icon-button" data-queue="move" data-id="${escapeHtml(item.id)}" data-to="${index - 1}" ${index === 0 ? "disabled" : ""} title="Move up" aria-label="Move up">↑</button>` +
        `<button type="button" class="icon-button" data-queue="move" data-id="${escapeHtml(item.id)}" data-to="${index + 1}" ${index === queue.length - 1 ? "disabled" : ""} title="Move down" aria-label="Move down">↓</button>` +
        `<button type="button" class="icon-button danger" data-queue="remove" data-id="${escapeHtml(item.id)}" title="Remove from the queue" aria-label="Remove">✕</button>` +
        `</span></div>`
    )
    .join("");
  node.innerHTML = head + items;
  return node;
}

function greetingNode() {
  const node = document.createElement("div");
  node.className = "greeting";
  node.innerHTML = `
    <svg class="greeting-nib" aria-hidden="true"><use href="#nib" /></svg>
    <span class="lbl">New session</span>
    <h1>What should I work on?</h1>
    <p>I work in <span class="mono">${escapeHtml(shortenPath(state.workspace || ""))}</span>. I ask before running a command or using the network, and every file I change can be undone.</p>
    <p class="hand">describe a change and I'll plan it first</p>
    <div class="starter-chips">
      ${STARTERS.map((starter, index) => `<button class="starter-chip" type="button" data-starter="${index}">${escapeHtml(starter.label)}</button>`).join("")}
    </div>`;
  return node;
}

function slipNode(prompt) {
  const node = document.createElement("div");
  node.className = "req-slip";
  node.innerHTML = `<span class="eyebrow"><span class="tick"></span>Request</span>
    <div class="req-text">${escapeHtml(prompt)}</div>`;
  return node;
}

/* Titles and explanations for a run that did not finish. Kept beside each
 * other so a new stop reason cannot get one without the other. */
const OUTCOME_HEADLINES = {
  stalled: "Stopped: going in circles",
  context_thrashing: "Stopped: the context window is too small",
  round_cap: "Stopped: round limit reached",
  no_result: "Stopped: nothing was produced",
};

const OUTCOME_REASONS = {
  stalled:
    "It kept re-reading what it had already been given instead of answering, so it was " +
    "stopped. The model is usually too small for the request: try a larger one, or ask " +
    "something narrower.",
  context_thrashing:
    "Every round had to drop what the round before it gathered to fit the context " +
    "window, so the model kept re-reading what it could no longer see. Raise the window " +
    "under CTX and run this again.",
  round_cap: "The round limit was reached before an answer was written.",
  empty_answer: "Its reply was empty. A full context window is the usual cause: raise it under CTX and run again.",
  no_result: "The run ended without producing a result.",
  cancelled: "Stopped before there was anything to show.",
};

/* Finished cards are kept and reused: the conversation is redrawn on many
 * frames, and rebuilding a card would replay its stamp landing each time. */
const receiptCache = new Map();

function receiptNode(turn) {
  const key = `${focusId()}:${turn.index}:${turn.stop_reason}:${(turn.changes || []).length}:${(turn.undone || []).length}`;
  const cached = receiptCache.get(key);
  if (cached) return cached;
  const node = document.createElement("article");
  node.className = `work-card ${receiptTone(turn)}`;
  node.innerHTML = receiptMarkup(turn);
  receiptCache.set(key, node);
  return node;
}

function receiptTone(turn) {
  if (turn.succeeded) return "done";
  return turn.stop_reason === "cancelled" ? "stopped" : "failed";
}

/* The verdict stamp's word. A run with requirements passed or failed them;
 * one without (spark) is simply done. */
function verdictWord(turn) {
  if (turn.stop_reason === "cancelled") return ["stop", "Stopped"];
  if (!turn.succeeded) return ["fail", "Not done"];
  const requirements = turn.requirements || [];
  if (!requirements.length) return ["pass", "Done"];
  return requirements.every((item) => item.status === "Verified") ? ["pass", "Passed"] : ["fail", "Failed"];
}

function receiptMarkup(turn) {
  const usage = turn.usage || {};
  const changes = turn.changes || [];
  const undone = (turn.undone || []).map((change) => ({ ...change, reverted: true }));
  const number = (turn.index >= 0 ? turn.index : 0) + 1;
  const route = routeFromRecord(turn.route, turn.succeeded);
  const notes = Object.entries(turn.artifacts || {})
    .map(
      ([name, text]) =>
        `<details class="worknotes"><summary>${escapeHtml(humanize(name))}</summary><div class="worknotes-body">${escapeHtml(text)}</div></details>`
    )
    .join("");

  // The stamp says how it ended; a headline is only needed when it did not
  // simply finish, to say what happened and what to do.
  const headline = turn.succeeded
    ? ""
    : turn.stop_reason === "empty_answer"
      ? "No answer from the model"
      : turn.stop_reason === "cancelled"
        // DR-030: a run the user ended on purpose is reported as their choice,
        // not as something that went wrong.
        ? "You stopped this run"
        : OUTCOME_HEADLINES[turn.stop_reason] || `Stopped: ${humanize(turn.stop_reason)}`;
  const why =
    turn.succeeded || turn.answer
      ? ""
      : `<p class="receipt-why">${escapeHtml(
          OUTCOME_REASONS[turn.stop_reason] ||
            "The run ended without producing an answer."
        )}</p>`;
  const [tone, word] = verdictWord(turn);
  const files = changes.length + undone.length;

  return `${verdictStampMarkup(tone, word)}
    <div class="work-head">
      <span class="lbl sheet-label">Run ${number}</span>
      <span class="work-request" title="${escapeHtml(turn.prompt)}">${escapeHtml(turn.prompt.split("\n")[0])}</span>
      <span class="work-elapsed" title="Tier, and tokens this run used">${escapeHtml(turn.tier || "")} · ${formatCount(usage.total_tokens || 0)} tok</span>
    </div>
    ${routeMarkup(route)}
    ${stampsMarkup(turn.requirements)}
    <div class="receipt">
      ${headline ? `<div class="receipt-headline">${escapeHtml(headline)}</div>` : ""}
      ${turn.answer ? `<div class="receipt-summary prose">${renderMarkdown(turn.answer)}</div>` : ""}
      ${why}
      ${files ? revisionBlockMarkup([...changes, ...undone], turn.index) : ""}
      ${changes.length ? `<div class="receipt-actions"><button class="inline-button" type="button" data-open-diff="1" data-turn="${turn.index}">See the diff</button></div>` : ""}
      ${notes}
    </div>`;
}

/* ---------- the live card ---------- */

function startRun(id, current) {
  const seat = desk(id);
  if (seat.run) return; // already tracking this session's turn
  const node = document.createElement("article");
  node.className = "work-card";
  const slip = slipNode(current.prompt || "");
  seat.run = {
    session: id,
    node,
    slip,
    /* Filled in by `ensureCard` on the first paint: the card is built once and
     * then updated in place. Rebuilding its innerHTML on every delta — which
     * is what this used to do — destroys and re-creates the streaming answer
     * block a few times a second, and a node that is replaced mid-stream
     * collapses to zero height and takes the scroll position with it. That is
     * the flicker on the last line. */
    dom: null,
    prompt: current.prompt || "",
    tier: current.tier || (state ? state.tier : ""),
    startedAt: (current.started_at ? current.started_at * 1000 : Date.now()),
    stages: [],
    activities: [],
    expanded: false,
    narration: "",
    thinking: "",
    answer: "",
    notes: {},
    requirements: [],
    verdict: null,
    thinkingLabel: "Starting",
    running: true,
    // DR-037: the structured record the inspector draws, one entry per tool
    // call or notable moment, each tagged with the stage that produced it.
    steps: [],
    // DR-038: the ledger as each stage left it, in order, and every loop-back.
    ledger: [],
    loopbacks: [],
    // DR-041: every model round's usage, and when each stage started and ended.
    rounds: [],
    stageTimes: {},
    currentStage: "",
    openSteps: new Set(),
    detailed: false,
    finishedAt: 0,
    timer: window.setInterval(() => tickElapsed(seat.run), 1000),
  };
  if (isFocused(id)) {
    renderConversation();
    renderStatus();
  }
}

function stopTimer(run) {
  if (run && run.timer) {
    window.clearInterval(run.timer);
    run.timer = 0;
  }
}

function setThinking(run, label) {
  if (!run) return;
  run.thinkingLabel = label;
  scheduleRun(run);
  if (isFocused(run.session)) renderStatus();
}

function pushActivity(run, line) {
  if (!run || !line) return;
  if (!line.stepless) {
    run.steps.push({ type: "note", stage: run.currentStage, kind: line.kind || "", icon: line.icon, html: line.html });
  }
  const previous = run.activities[run.activities.length - 1];
  if (previous && previous.html === line.html) {
    previous.count = (previous.count || 1) + 1;
  } else {
    run.activities.push({ kind: "", icon: "·", count: 1, ...line });
  }
  scheduleRun(run);
}

let runFrame = 0;

/* Deltas arrive faster than a screen refreshes; coalesce them into one paint
 * so a streaming stage does not rebuild the card per token. A run in a session
 * nobody is looking at is not painted at all — its state is kept, and drawn
 * when the user switches to it. */
function scheduleRun(run) {
  if (runFrame || !run || !isFocused(run.session)) return;
  runFrame = window.requestAnimationFrame(() => {
    runFrame = 0;
    renderRun();
  });
}

function tickElapsed(run) {
  if (!run || !run.dom || !isFocused(run.session)) return;
  setText(run.dom.elapsed, formatElapsed(Date.now() - run.startedAt));
  // A stalled model is visible as it happens: the line under the head counts
  // the quiet, and the station says "still thinking".
  if (runSlow(run) || run.node.classList.contains("slow")) scheduleRun(run);
  // A stream that went quiet, or a note about the steps taken while you were
  // away that has had its moment, is taken off on the next tick.
  const flowing = run.node.classList.contains("flow-think") || run.node.classList.contains("flow-write");
  if (flowing !== Boolean(streamFlow(run))) scheduleRun(run);
  if (run.sinceNote && Date.now() - run.sinceNote.at > 12000) {
    run.sinceNote = null;
    scheduleRun(run);
  }
  renderRate();
}

/* The live card's frame, written once. Every part that changes as the run goes
 * is addressed by `data-part` and updated in place. */
const CARD_SKELETON = `
  <div class="work-head">
    <span class="lbl sheet-label" data-part="phase"></span>
    <span class="work-request" data-part="title"></span>
    <span class="work-elapsed" data-part="elapsed"></span>
    <button class="card-details" type="button" data-details="1" data-part="details"
      title="Show every step, the stage output and the reasoning here"></button>
    <button class="card-stop" type="button" data-stop="1"
      title="Stop after the current step, keeping everything produced so far">Stop</button>
  </div>
  <div class="work-now" data-part="now"></div>
  <div data-part="stages"></div>
  <div data-part="stamps"></div>
  <div class="activity" data-part="activity"></div>
  <div class="narration prose" data-part="narration" hidden></div>
  <details class="thinking" data-part="thinking" hidden>
    <summary>Its reasoning, as it streams</summary>
    <div class="thinking-body" data-part="reasoning"></div>
  </details>
`;

function ensureCard(run) {
  if (run.dom) return;
  run.node.innerHTML = CARD_SKELETON;
  const part = (name) => run.node.querySelector(`[data-part="${name}"]`);
  run.dom = {
    phase: part("phase"),
    title: part("title"),
    elapsed: part("elapsed"),
    stop: run.node.querySelector("[data-stop]"),
    details: part("details"),
    now: part("now"),
    stages: part("stages"),
    stamps: part("stamps"),
    activity: part("activity"),
    narration: part("narration"),
    thinking: part("thinking"),
    reasoning: part("reasoning"),
  };
}

/* Both return whether they actually wrote anything. An unchanged section is
 * left alone rather than re-parsed, which is what keeps a streaming card from
 * repainting the parts of itself that did not move. */
function setText(node, value) {
  if (!node || node.__value === value) return false;
  node.__value = value;
  node.textContent = value;
  return true;
}

function setHtml(node, html) {
  if (!node || node.__value === html) return false;
  node.__value = html;
  node.innerHTML = html;
  return true;
}

/* Whether this session's run is held on the person right now. */
function runWaiting(run) {
  return (
    approvals.some((item) => item.session === run.session) ||
    Boolean(state && state.pending_plan && isFocused(run.session))
  );
}

/* No new step for this long reads as a stalled model, and says so. */
const SLOW_MS = 75 * 1000;

function runSlow(run) {
  return run.running && !runWaiting(run) && Date.now() - (run.lastEventAt || run.startedAt) > SLOW_MS;
}

/* The live route: the workflow's stages as stations, how long each took,
 * and every time the check sent the work back. */
function liveRoute(run) {
  const waiting = runWaiting(run);
  return {
    ops: run.stages.map((stage) => {
      const status = stage.status === "done" ? "done" : stage.status === "active" ? "active" : "next";
      const times = run.stageTimes[stage.name];
      let note = "";
      if (status === "done" && times && times.end) {
        note = `done · ${formatElapsed((times.end - times.start) * 1000)}`;
      } else if (status === "active") {
        note = waiting ? "waiting for you" : runSlow(run) ? "still thinking" : "working";
      }
      return {
        name: stage.name,
        title: stage.title,
        status,
        passes: (run.passes && run.passes[stage.name]) || 0,
        note,
      };
    }),
    loopbacks: run.loopbacks.map((item) => ({
      to: item.target,
      unmet: (item.unmet || []).join(", "),
      detail: item.feedback || "",
    })),
    pose: waiting ? "waiting" : runSlow(run) ? "slow" : "working",
    drops: run.inkDrops || 0,
  };
}

function runNumber() {
  return ((state && state.history) || []).length + 1;
}

function liveStamps(run) {
  return stampsMarkup(run.requirements);
}

/* The line under the head: what the nib is doing, in plain words. */
function nowMarkup(run) {
  if (!run.running) return `<strong>Finishing</strong>`;
  const away = run.sinceNote
    ? ` <span class="since-note hand">${plural(run.sinceNote.count, "step")} while you were away</span>`
    : "";
  if (!run.firstTokenAt && modelColdness()) {
    return `<strong>Loading the model</strong> · the server is reading it into memory; the first reply waits for it${away}`;
  }
  if (runWaiting(run)) return `<strong>Waiting for you</strong> · nothing runs without your say-so`;
  if (runSlow(run)) {
    return `<strong>${escapeHtml(run.thinkingLabel)}</strong> · no new step for ${formatElapsed(Date.now() - (run.lastEventAt || run.startedAt))}; a slow model, or a long step`;
  }
  const flow = streamFlow(run);
  const doing = flow === "think" ? " · thinking" : flow === "write" ? " · writing" : "";
  return `<strong>${escapeHtml(run.thinkingLabel)}</strong>${run.tier ? ` · ${escapeHtml(run.tier)}` : ""}${doing}${away}`;
}

function activityMarkup(run) {
  // DR-037: compact by default — the latest step only; the inspector and the
  // card's own Details toggle hold the rest.
  if (!run.detailed) {
    const latest = run.activities[run.activities.length - 1];
    return latest
      ? `<div class="activity-line kind-${latest.kind}"><span class="act-icon">${latest.icon}</span><span>${latest.html}${latest.count > 1 ? ` <em>×${latest.count}</em>` : ""}</span></div>`
      : "";
  }
  const visible = run.expanded ? run.activities : run.activities.slice(-7);
  const hidden = run.activities.length - visible.length;
  return (
    (hidden > 0
      ? `<button class="activity-more" type="button" data-expand="1">show ${hidden} earlier step(s)</button>`
      : "") +
    visible
      .map(
        (line) =>
          `<div class="activity-line kind-${line.kind}"><span class="act-icon">${line.icon}</span><span>${line.html}${line.count > 1 ? ` <em>×${line.count}</em>` : ""}</span></div>`
      )
      .join("")
  );
}

function renderRun() {
  const run = focusedRun();
  if (!run) return;
  ensureCard(run);
  const d = run.dom;
  // Read the scroll position before anything grows: content arriving is what
  // pushes the view off the bottom, so asking afterwards would answer "no,
  // they scrolled away" on every single delta.
  const pinned = isPinnedToBottom();

  run.node.classList.toggle("compact", !run.detailed);
  setText(d.details, run.detailed ? "Less" : "Details");
  setText(d.phase, `Run ${runNumber()}`);
  setText(d.title, run.prompt.split("\n")[0]);
  setText(d.elapsed, formatElapsed(Date.now() - run.startedAt));
  run.node.classList.toggle("waiting", runWaiting(run));
  run.node.classList.toggle("slow", runSlow(run));
  const flow = streamFlow(run);
  run.node.classList.toggle("flow-think", flow === "think");
  run.node.classList.toggle("flow-write", flow === "write");
  setHtml(d.now, nowMarkup(run));
  renderCardStop();

  const redrawn = setHtml(d.stages, routeMarkup(liveRoute(run)));
  const restamped = setHtml(d.stamps, liveStamps(run));
  if (redrawn || restamped) animateLiveSheet(run);
  let grew = redrawn || restamped;
  grew = setHtml(d.activity, activityMarkup(run)) || grew;

  const prose = run.narration || run.answer;
  if (setHtml(d.narration, prose ? renderMarkdown(prose) : "")) {
    d.narration.scrollTop = d.narration.scrollHeight;
    grew = true;
  }
  d.narration.hidden = !prose;

  if (setText(d.reasoning, run.thinking)) {
    d.reasoning.scrollTop = d.reasoning.scrollHeight;
    grew = true;
  }
  d.thinking.hidden = !run.thinking;

  if (grew && pinned) scrollToBottom();
  renderInspector();
}

/* ---------- inspector (DR-037) ---------- */

let inspectorTab = "requirements";

/* The run the inspector describes: the one in progress, else the last one this
 * tab watched. A turn finished before the page loaded has no step record, so
 * there is nothing to show for it beyond its receipt in the conversation. */
function inspectedRun() {
  const seat = desk(focusId());
  return seat.run || seat.lastRun || null;
}

/* DR-038: the requirements view — the run this page watched, with each
 * stage's statuses; else the latest stored turn, with its final ledger. */
function requirementsView() {
  const run = inspectedRun();
  if (run && run.requirements && run.requirements.length) {
    return {
      requirements: run.requirements,
      journey: run.ledger,
      verdict: run.verdict,
      loopbacks: run.loopbacks,
      live: Boolean(run.running),
    };
  }
  const turn = [...((state && state.history) || [])]
    .reverse()
    .find((item) => item.requirements && item.requirements.length);
  return turn ? { requirements: turn.requirements, journey: [], verdict: null, loopbacks: [], live: false } : null;
}

/* DR-040: the Changes tab's diffs, per session and turn, fetched on demand. */
const changeDiffs = new Map();
const openChanges = new Set();

function changesView() {
  const session = focusId();
  const history = (state && state.history) || [];
  // `undo` reverts the most recent turn that wrote (DR-024); only that turn
  // offers it, so the button never promises more than undo does.
  let lastWriting = -1;
  history.forEach((turn, index) => {
    if ((turn.changes || []).length) lastWriting = index;
  });
  const turns = history
    .map((turn, index) => {
      const files = [
        ...(turn.changes || []).map((change) => ({ ...change, undone: false })),
        // Reverted by an undo: kept in the list, struck through.
        ...(turn.undone || []).map((change) => ({ ...change, undone: true })),
      ].map((change) => ({
        path: change.path,
        kind: change.kind,
        added: change.added || 0,
        removed: change.removed || 0,
        undone: change.undone,
      }));
      return {
        index,
        prompt: turn.prompt,
        files,
        added: files.filter((file) => !file.undone).reduce((sum, file) => sum + file.added, 0),
        removed: files.filter((file) => !file.undone).reduce((sum, file) => sum + file.removed, 0),
        undoable: index === lastWriting && !(state && state.running),
      };
    })
    .filter((turn) => turn.files.length)
    .reverse();
  const run = focusedRun();
  const liveEdits = run
    ? run.steps.filter((step) => step.type === "tool" && ["write_file", "edit_file"].includes(step.tool) && step.status === "done").length
    : 0;
  const diffs = new Map();
  turns.forEach((turn) => {
    const cached = changeDiffs.get(`${session}:${turn.index}`);
    if (cached) diffs.set(turn.index, cached);
  });
  return { turns, liveEdits, diffs, open: openChanges };
}

async function toggleChange(key, turnIndex) {
  if (openChanges.has(key)) {
    openChanges.delete(key);
    renderInspector();
    return;
  }
  openChanges.add(key);
  renderInspector();
  const cacheKey = `${focusId()}:${turnIndex}`;
  if (changeDiffs.has(cacheKey)) return;
  try {
    const payload = await api(
      `/api/changes?session=${encodeURIComponent(focusId())}&turn=${encodeURIComponent(turnIndex)}`
    );
    changeDiffs.set(cacheKey, payload.changes || []);
  } catch (error) {
    openChanges.delete(key);
    toast(String(error.message || error), "bad");
  }
  renderInspector();
}

/* DR-041: the run this page watched, plus every stored turn's totals. */
function usageView() {
  const history = (state && state.history) || [];
  return {
    run: inspectedRun(),
    turns: history.map((turn, index) => ({
      index,
      prompt: turn.prompt,
      total: (turn.usage && turn.usage.total_tokens) || 0,
    })),
    session: (state && state.usage) || {},
  };
}

function renderInspector() {
  setHtml(el("inspector-tabs"), inspectorTabsMarkup(inspectorTab));
  if (!dom.shell.classList.contains("inspector-open")) return;
  const body =
    inspectorTab === "requirements"
      ? requirementsPanelMarkup(requirementsView())
      : inspectorTab === "changes"
        ? changesPanelMarkup(changesView())
        : inspectorTab === "usage"
          ? usagePanelMarkup(usageView())
          : activityPanelMarkup(inspectedRun());
  setHtml(el("inspector-body"), body);
}

function setInspectorOpen(open) {
  dom.shell.classList.toggle("inspector-open", open);
  try {
    localStorage.setItem("autose.inspector", open ? "open" : "closed");
  } catch (error) {
    // Storage can be unavailable (private window); the toggle still works.
  }
  renderInspector();
}

/* DR-030: the stop control on the card itself, because that is the thing the
 * user is looking at when they decide they have seen enough. The composer has
 * one too, for a run whose card has scrolled out of view. */
function renderCardStop() {
  const run = focusedRun();
  if (!run || !run.dom) return;
  const stopping = Boolean((state && state.stopping) || stopRequested);
  run.dom.stop.hidden = !run.running;
  run.dom.stop.disabled = stopping;
  setText(run.dom.stop, stopping ? "Stopping…" : "Stop");
}

function finishRun(id, turn) {
  const seat = desk(id);
  if (isFocused(id)) {
    // The meter is how full the window was on the last round. A turn's
    // prompt_tokens is the sum over every round, which overstates it many
    // times over; it is only a fallback when no round was seen (DR-041).
    const rounds = seat.run ? seat.run.rounds : [];
    const last = rounds[rounds.length - 1];
    lastPromptTokens = last
      ? last.prompt || last.estimated || lastPromptTokens
      : (turn.usage && turn.usage.prompt_tokens) || lastPromptTokens;
  }
  const run = seat.run;
  if (!run) return;
  stopTimer(run);
  run.running = false;
  run.finishedAt = Date.now();
  seat.lastRun = run;
  // The next state frame carries this turn in `history`, so the live pair is
  // dropped rather than drawn twice.
  seat.run = null;
  if (isFocused(id)) {
    // The card that was live becomes the finished one, and lands its verdict
    // stamp once; the cache keeps this node, so later repaints do not replay it.
    run.node.className = `work-card ${receiptTone(turn)} just-finished`;
    run.node.innerHTML = receiptMarkup(turn);
    receiptCache.set(
      `${id}:${turn.index}:${turn.stop_reason}:${(turn.changes || []).length}:${(turn.undone || []).length}`,
      run.node
    );
    scrollToBottom();
    if (!document.hidden) reactToFinish(turn);
  }
}

function failRun(id, title, message) {
  const seat = desk(id);
  const run = seat.run;
  if (run) {
    stopTimer(run);
    run.running = false;
    run.node.className = "work-card failed";
    run.node.innerHTML = `<div class="work-head">
        <span class="presence-dot"></span>
        <span class="eyebrow">Stopped</span>
        <span class="work-title">${escapeHtml(run.thinkingLabel)}</span>
      </div>
      <div class="receipt">
        <div class="receipt-headline">${escapeHtml(title)}</div>
        <div class="receipt-summary prose">${renderMarkdown(message)}</div>
      </div>`;
    // Nothing landed in history, so the card has to be kept deliberately or
    // the next repaint would erase the only account of what happened.
    seat.extras = seat.extras.concat([run.slip, run.node]);
    run.finishedAt = Date.now();
    seat.lastRun = run;
    seat.run = null;
    if (isFocused(id)) renderConversation();
  } else {
    toast(`${shortId(id)}: ${title}: ${message}`, "bad");
  }
  renderStatus();
}

/* ---------- approvals (DR-012) ---------- */

/* ---------- plan review (DR-045) ---------- */

const planCards = new Map();

/* The plan as written, with each requirement's number drawn as a balloon:
 * the same numbers the checks will carry. */
function planBodyMarkup(plan) {
  return renderMarkdown(plan).replace(
    /(<li>|<p>)\s*(?:<strong>)?R(\d+)(?:<\/strong>)?\s*(?:\[[^\]]*\]\s*)?[:.)\u2014-]?\s*/g,
    (match, tag, number) => `${tag}<span class="balloon open plan-ball">${number}</span> `
  );
}

function planNode() {
  const review = state && state.pending_plan;
  if (!review) return null;
  if (planCards.has(review.id)) return planCards.get(review.id);
  const node = document.createElement("section");
  node.className = "plan-card";
  node.innerHTML =
    // Like a question, a plan waits for you: the same moving outline.
    `<svg class="outline" aria-hidden="true"><rect x="1" y="1" rx="13" ry="13"></rect></svg>` +
    `<div class="approval-head"><svg class="approval-nib" aria-hidden="true"><use href="#nib" /></svg>` +
    `<div><strong>Here is my plan${review.pass_number > 1 ? `, draft ${review.pass_number}` : ""}</strong>` +
    `<p class="approval-lede">Nothing has changed yet. Approve it to start, ask for changes, or edit it yourself.</p></div>` +
    `<span class="plate">no files changed</span></div>` +
    (review.problem ? `<p class="plan-problem">Your edit could not be used: ${escapeHtml(review.problem)}</p>` : "") +
    `<div class="plan-body prose">${planBodyMarkup(review.plan || "")}</div>` +
    `<textarea class="plan-input plan-editor" data-plan-editor rows="14" hidden></textarea>` +
    `<div class="approval-actions">` +
    `<button type="button" class="primary-button" data-plan-action="approve">Approve and start</button>` +
    `<button type="button" class="ghost-button" data-plan-action="edit-open">Edit the plan</button>` +
    `<button type="button" class="ghost-button" data-plan-action="stop">Stop</button>` +
    `</div>` +
    `<input type="text" class="approval-reason plan-feedback" data-plan-feedback placeholder="Or say what should change, and press Enter" />`;
  node.querySelector("[data-plan-editor]").value = review.plan || "";
  planCards.set(review.id, node);
  return node;
}

async function planAction(button) {
  const review = state && state.pending_plan;
  if (!review) return;
  const card = button.closest(".plan-card");
  const action = button.dataset.planAction;
  if (action === "edit-open") {
    const editor = card.querySelector("[data-plan-editor]");
    editor.hidden = !editor.hidden;
    button.textContent = editor.hidden ? "Edit the plan" : "Approve my edit";
    button.dataset.planAction = editor.hidden ? "edit-open" : "edit";
    if (!editor.hidden) editor.focus();
    return;
  }
  const body = { id: review.id, action };
  if (action === "revise") {
    body.feedback = card.querySelector("[data-plan-feedback]").value.trim();
    if (!body.feedback) {
      toast("Say what should change first.", "bad");
      card.querySelector("[data-plan-feedback]").focus();
      return;
    }
  }
  if (action === "edit") body.plan = card.querySelector("[data-plan-editor]").value;
  await post("/api/plan", body);
}

/* ---------- approvals, inline (DR-044) ---------- */

/* A call waiting for a decision is a card in the conversation of the session
 * it belongs to, under the run that asked; every waiting call from every
 * session is also listed under "Needs you" in the rail. Nothing seizes the
 * screen: a decision is something to answer, not something to be held by. */
function renderApproval() {
  renderConversation();
  renderNeedsYou();
  renderStatus();
}

/* Cards are kept by id and reused across repaints: the conversation is
 * rebuilt on many frames, and a fresh card would drop a half-typed reason. */
const approvalCards = new Map();

function approvalNodes() {
  const waiting = new Set(approvals.map((item) => item.id));
  Array.from(approvalCards.keys()).forEach((id) => {
    if (!waiting.has(id)) approvalCards.delete(id);
  });
  const mine = approvals.filter((item) => item.session === focusId());
  if (!mine.length) return [];
  const nodes = mine.map((request) => {
    if (approvalCards.has(request.id)) return withPlate(approvalCards.get(request.id), request);
    const node = document.createElement("section");
    node.className = "approval-card";
    node.dataset.approval = request.id;
    const args = request.arguments || {};
    const keys = Object.keys(args);
    const single = keys.length === 1 && typeof args[keys[0]] === "string" ? args[keys[0]] : "";
    const shown = single
      ? single
      : keys.length
      ? Object.entries(args)
        .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
        .join("\n")
      : request.summary;
    node.innerHTML =
      // The only loop on screen: this outline marches while it waits.
      `<svg class="outline" aria-hidden="true"><rect x="1" y="1" rx="13" ry="13"></rect></svg>` +
      `<span class="approval-stamp" aria-hidden="true"></span>` +
      `<div class="approval-head"><svg class="approval-nib" aria-hidden="true"><use href="#nib" /></svg>` +
      `<div><strong>${escapeHtml(approvalQuestion(request.tool))}</strong>` +
      `<p class="approval-lede">${escapeHtml(approvalLede(request.tool))}</p></div>` +
      // Filled on every paint: after a reload the card exists before the
      // run's stage has been replayed.
      `<span class="plate" data-plate hidden></span>` +
      `</div>` +
      `<pre class="approval-args"><code>${linkifyPaths(shown)}</code></pre>` +
      (request.allow ? "" : `<p class="approval-once">Only once: ${escapeHtml(request.allow_note)}.</p>`) +
      `<div class="approval-actions">` +
      `<button type="button" class="primary-button" data-decide="once" data-id="${escapeHtml(request.id)}">Run it</button>` +
      // DR-048: the standing yes covers calls like this one (the same command
      // words, the same host), never the whole shell; some calls get none.
      (request.allow
        ? `<button type="button" class="ghost-button" data-decide="session" data-id="${escapeHtml(request.id)}" title="Run this call, and later ${escapeHtml(request.allow.label)} in this session, without asking">${allowButtonMarkup(request.allow)}</button>`
        : "") +
      `<button type="button" class="ghost-button" data-decide="deny" data-id="${escapeHtml(request.id)}">Don't run</button>` +
      `</div>` +
      `<input type="text" class="approval-reason" placeholder="Or tell me what to do instead, and press Enter" data-reason-for="${escapeHtml(request.id)}" />`;
    approvalCards.set(request.id, node);
    return withPlate(node, request);
  });
  if (mine.length > 1) {
    const bulk = document.createElement("div");
    bulk.className = "approval-bulk";
    bulk.innerHTML =
      `<span>${mine.length} calls are waiting in this session.</span>` +
      `<button type="button" class="ghost-button small" data-decide-all="deny">Refuse all</button>` +
      `<button type="button" class="primary-button small" data-decide-all="once">Run all once</button>`;
    nodes.unshift(bulk);
  }
  return nodes;
}

/* "Allow pytest … for this session": the command words in mono, since they
 * are what the next command has to start with. */
function allowButtonMarkup(allow) {
  if (allow.tool === "run_command") {
    // DR-052: a chain names each of its parts.
    const parts = (allow.patterns && allow.patterns.length ? allow.patterns : [allow.pattern])
      .map((pattern) => `<code>${escapeHtml(pattern)} …</code>`);
    const listed = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
    return `Always allow ${listed} here`;
  }
  return `Always allow ${escapeHtml(allow.label)} here`;
}

function withPlate(node, request) {
  const plate = node.querySelector("[data-plate]");
  const held = heldAt(request.session);
  if (plate) {
    plate.hidden = !held;
    plate.textContent = held ? `paused at ${held}` : "";
  }
  return node;
}

/* Where a session's run is held, as its operation: "20 Execute". */
function heldAt(sessionId) {
  const run = desk(sessionId).run;
  if (!run || !run.currentStage) return "";
  const index = run.stages.findIndex((stage) => stage.name === run.currentStage);
  const stage = run.stages[index];
  return `${index >= 0 ? `step ${index + 1}: ` : ""}${stage ? stage.title : humanize(run.currentStage)}`;
}

function approvalQuestion(tool) {
  const questions = {
    run_command: "I'd like to run a command",
    fetch_url: "I'd like to fetch a page",
    web_search: "I'd like to search the web",
    write_file: "I'd like to write a file",
    edit_file: "I'd like to make an edit",
  };
  return questions[tool] || `I'd like to use ${humanize(String(tool || "a tool"))}`;
}

function approvalLede(tool) {
  if (tool === "run_command") return "In your shell, in this workspace, exactly as written. Nothing runs without your say-so.";
  if (tool === "fetch_url" || tool === "web_search") return "This reaches the internet and brings back what I did not write. Nothing runs without your say-so.";
  return "This changes a file in your workspace. You can undo it later.";
}

async function decide(id, choice) {
  const request = approvals.find((item) => item.id === id);
  if (!request) return;
  const input = document.querySelector(`[data-reason-for="${CSS.escape(id)}"]`);
  const reason = input ? input.value.trim() : "";
  const approved = choice !== "deny";
  // The answer is stamped on the card, which then goes; it comes back only
  // if the server says the call was no longer waiting.
  const card = approvalCards.get(id);
  if (card) {
    card.classList.add("answered", approved ? "approved" : "refused");
    card.querySelector(".approval-stamp").textContent =
      choice === "deny" ? "Not run" : choice === "session" ? "Allowed here" : "Approved";
  }
  window.setTimeout(() => {
    approvals = approvals.filter((item) => item.id !== id);
    renderApproval();
  }, card && !matchMedia("(prefers-reduced-motion: reduce)").matches ? 520 : 0);
  try {
    await api("/api/approval", { id, approved, reason, scope: choice === "session" ? "session" : "once" });
    if (choice === "session") {
      toast(`${request.allow ? request.allow.label : humanize(String(request.tool))} will run without asking for the rest of this session.`, "good");
    }
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
}

/* The rail's "Needs you": every waiting call, from every session, plus held
 * queues; and what this session runs without asking. */
function renderNeedsYou() {
  const box = el("needs-you");
  if (!box) return;
  // What is waiting lives in the set itself ("Waiting for you"); this box is
  // the standing orders: what runs without asking in this session.
  const allowed = (state && state.allowances) || [];
  // Session-specific: shown on the session bar's Allowed button, not in the
  // global session list.
  box.hidden = true;
  renderSessionBar();
  box.innerHTML = allowed.length
    ? `<div class="needs-allowed"><span>Runs without asking here <button type="button" class="inline-link" data-manage-allowed="1">Manage</button></span>` +
      allowed
        .map((item) => `<button type="button" class="allow-chip" data-revoke="${escapeHtml(item.id)}" title="Ask again before ${escapeHtml(item.label)}">${escapeHtml(item.label)} ×</button>`)
        .join("") +
      `</div>`
    : "";
  if (!overlays.allowed.hidden) renderAllowed();
}

/* ---------- allowed commands (DR-048, DR-050) ---------- */

function openAllowed() {
  renderAllowed();
  overlays.allowed.hidden = false;
  el("allowed-pattern").focus();
}

function renderAllowed() {
  const allowed = (state && state.allowances) || [];
  const entry = openEntry(focusId());
  el("allowed-scope").textContent = `For ${entry ? `${sheetNo(focusId())} · ${entry.title}` : "this session"}. Switch sessions to see theirs.`;
  el("allowed-list").innerHTML = allowed.length
    ? allowed
        .map(
          (item) =>
            `<div class="allowed-row"><span class="allowed-kind">${escapeHtml(item.tool === "run_command" ? "command" : item.tool === "fetch_url" ? "host" : "tool")}</span>` +
            `<span class="allowed-what">${item.tool === "run_command" ? `<code>${escapeHtml(item.pattern)} …</code>` : escapeHtml(item.label)}</span>` +
            `<button type="button" class="ghost-button small" data-revoke-allowed="${escapeHtml(item.id)}" title="Ask again before ${escapeHtml(item.label)}">Remove</button></div>`
        )
        .join("")
    : `<p class="empty-state">Nothing yet: every command, page fetch and search asks first. Add a rule below, or press "Always allow" on a question.</p>`;
}

el("open-allowed").addEventListener("click", openAllowed);
el("allowed-close").addEventListener("click", closeOverlays);
el("allowed-tool").addEventListener("change", (event) => {
  const tool = event.target.value;
  const input = el("allowed-pattern");
  input.hidden = tool === "web_search";
  input.placeholder = tool === "fetch_url" ? "docs.python.org" : "pytest, or npm run test";
});
el("allowed-add").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tool = el("allowed-tool").value;
  const pattern = el("allowed-pattern").value.trim();
  const result = await post("/api/allowances", { action: "add", tool, pattern, session: focusId() });
  if (result) {
    state.allowances = result.allowances || [];
    el("allowed-pattern").value = "";
    toast(`${result.added.label} will run without asking in this session.`, "good");
    renderNeedsYou();
    renderAllowed();
  }
});
el("allowed-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-revoke-allowed]");
  if (!button) return;
  const result = await post("/api/allowances", { action: "revoke", id: button.dataset.revokeAllowed, session: focusId() });
  if (result) {
    state.allowances = result.allowances || [];
    renderNeedsYou();
    renderAllowed();
  }
});

/* ---------- overlays ---------- */

/* The diff viewer lives in diff.js. */

async function openDoctor() {
  el("doctor-body").innerHTML = `<p class="panel-note">Looking for runtimes…</p>`;
  overlays.doctor.hidden = false;
  try {
    const report = await api("/api/doctor");
    el("doctor-body").innerHTML = doctorMarkup(report);
  } catch (error) {
    el("doctor-body").innerHTML = `<p class="form-error">${escapeHtml(String(error.message || error))}</p>`;
  }
}

function doctorMarkup(report) {
  const blocks = [];
  if (report.problems.length) {
    blocks.push(report.problems.map((problem) => `<p class="callout bad">${escapeHtml(problem)}</p>`).join(""));
  }
  if (report.advisories.length) {
    blocks.push(report.advisories.map((note) => `<p class="callout warn">${escapeHtml(note)}</p>`).join(""));
  }
  const same = (a, b) => String(a || "").replace(/\/+$/, "").toLowerCase() === String(b || "").replace(/\/+$/, "").toLowerCase();
  const live = report.runtimes.filter((runtime) => runtime.reachable || same(runtime.base_url, report.configured_url));
  const idle = report.runtimes.filter((runtime) => !live.includes(runtime));
  const modelRows = (runtime) =>
    runtime.models
      .map(
        (model) =>
          `<div class="doctor-model"><span class="mono">${escapeHtml(model.name)}</span>` +
          `<span class="doctor-meta">${model.context_length ? `${formatCount(model.context_length)} tokens served` : "window unknown"}${model.is_context_capped ? ` of ${formatCount(model.max_context_length)}` : ""}</span>` +
          `<span class="doctor-tag ${model.is_tool_capable ? "good" : "idle"}">${model.is_tool_capable ? "tools" : "tools unknown"}</span></div>`
      )
      .join("");
  const cards = live
    .map((runtime) => {
      const inUse = same(runtime.base_url, report.configured_url);
      const state = runtime.usable ? ["good", "serving"] : runtime.reachable ? ["idle", "reachable, no models"] : ["bad", "not reachable"];
      return (
        `<div class="provider-card${inUse ? " in-use" : ""}">` +
        `<div class="provider-head"><span class="provider-name">${escapeHtml(runtime.provider)}</span>` +
        (inUse ? `<span class="choice-tag now">in use</span>` : "") +
        `<span class="doctor-tag ${state[0]}">${state[1]}</span></div>` +
        `<div class="provider-url mono">${escapeHtml(runtime.base_url)}</div>` +
        (runtime.error && !runtime.usable ? `<p class="doctor-error">${escapeHtml(runtime.error)}</p>` : "") +
        modelRows(runtime) +
        `</div>`
      );
    })
    .join("");
  const idleRows = idle
    .map(
      (runtime) =>
        `<div class="provider-off"><span class="provider-name">${escapeHtml(runtime.provider)}</span>` +
        `<span class="provider-url mono">${escapeHtml(runtime.base_url)}</span>` +
        `<span class="provider-state tone-idle">not running</span></div>`
    )
    .join("");
  blocks.push(
    `<div class="doctor-section"><span class="lbl">Model servers</span>` +
    (cards || `<div class="empty-box"><p>Nothing answered on this machine.</p></div>`) +
    (idleRows ? `<div class="provider-off-list">${idleRows}</div>` : "") +
    `</div>`
  );
  blocks.push(
    `<div class="doctor-section"><span class="lbl">This workspace</span>` +
    `<div class="provider-card"><div class="mono doctor-path">${escapeHtml(report.workspace)}</div>` +
    `<div class="doctor-meta">${escapeHtml(report.config_source || "no config file: defaults in use")} · tiers ${escapeHtml(report.implemented_tiers.join(", "))}</div></div>` +
    `</div>`
  );
  return blocks.join("");
}

/* DR-035: the window the dashboard is about to ask for, before Apply. */
let contextPick = 0;

function openContext() {
  const advice = (state && state.context_advice) || null;
  const titles = {
    ok: "The window is fine",
    info: "The window is fine",
    warning: "The window is tight",
    blocking: "The window is too small",
  };
  el("context-title").textContent = advice ? titles[advice.level] || "Context window" : "Context window";
  el("context-summary").textContent = advice ? advice.summary : "The runtime has not reported its window.";
  el("context-detail").textContent = advice ? advice.detail : "";
  const figure = (label, value) =>
    `<div class="context-figure"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
  el("context-figures").innerHTML = advice
    ? [
        figure("Served", advice.served ? formatWindow(advice.served) : "?"),
        figure("Model max", advice.maximum ? formatWindow(advice.maximum) : "?"),
        figure(
          "On GPU",
          typeof advice.gpu_fraction === "number" ? `${Math.round(advice.gpu_fraction * 100)}%` : "after next run"
        ),
      ].join("")
    : "";
  const choices = (advice && advice.adjustable && advice.choices) || [];
  el("context-choices").hidden = choices.length === 0;
  if (choices.length) {
    const current = choices.find((choice) => choice.current);
    contextPick = current ? current.tokens : choices[0].tokens;
    renderContextChoices();
  }
  // Where AutoSE chooses the window, the setting is how to keep a choice for
  // every session; elsewhere it is how to restart the runtime (DR-034).
  const remedy = (advice && advice.remedy) || "";
  el("context-remedy").hidden = !remedy;
  el("context-remedy-label").textContent = advice && advice.adjustable
    ? "To make this size the default for this workspace, add it to .autose/config.yaml:"
    : advice && advice.level === "info"
      ? "Optional — if you want more room"
      : "To change it";
  el("context-remedy-code").textContent = remedy;
  el("context-copy").hidden = !remedy || !navigator.clipboard;
  overlays.context.hidden = false;
}

/* Windows are powers of two and people say "32k", not "32.8k". */
function formatWindow(tokens) {
  const value = Number(tokens || 0);
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}k`;
  return formatCount(value);
}

function renderContextChoices() {
  const advice = (state && state.context_advice) || null;
  const choices = (advice && advice.choices) || [];
  el("context-choice-list").innerHTML = choices
    .map((choice) => {
      const tags = [
        choice.current ? `<span class="choice-tag now">in use</span>` : "",
        choice.recommended && !choice.current ? `<span class="choice-tag">suggested</span>` : "",
      ].filter(Boolean).join(" ");
      return (
        `<button type="button" role="radio" aria-checked="${choice.tokens === contextPick}" ` +
        `class="choice${choice.tokens === contextPick ? " picked" : ""}" data-tokens="${choice.tokens}">` +
        `<span class="choice-size">${escapeHtml(formatWindow(choice.tokens))}</span>` +
        `<span>${escapeHtml(choice.note)}</span>${tags ? `<span>${tags}</span>` : ""}</button>`
      );
    })
    .join("");
  const picked = choices.find((choice) => choice.tokens === contextPick);
  const changing = picked && !picked.current;
  const busy = Boolean(state && state.running);
  el("context-apply").hidden = !changing;
  el("context-apply").disabled = busy;
  el("context-choice-hint").textContent = busy
    ? "A run is working in this session. The window can change when it finishes."
    : changing
      ? "Takes effect on the next request. The model server reloads the model at the new size, which takes a few seconds."
      : "";
}

async function applyContextWindow() {
  if (!contextPick) return;
  // The `context_window_changed` event announces it; no second toast here.
  const result = await post("/api/context", { tokens: contextPick, session: focusId() });
  if (result) {
    state = result;
    adoptState();
    openContext();
  }
}

async function copyContextRemedy() {
  const remedy = (state && state.context_advice && state.context_advice.remedy) || "";
  if (!remedy) return;
  try {
    await navigator.clipboard.writeText(remedy);
    toast(
      state.context_advice.adjustable
        ? "Copied — paste it into .autose/config.yaml in your project."
        : "Copied — run it where the model server runs, then restart that server.",
      "good"
    );
  } catch (error) {
    toast("Could not copy; select the command instead.", "bad");
  }
}

/* ---------- settings: providers and keys (DR-036) ---------- */

let providers = null;

async function loadProviders() {
  try {
    providers = await api("/api/providers");
  } catch (error) {
    providers = providers || null;
  }
  if (state) renderHeader();
  if (!overlays.settings.hidden) renderSettings();
}

function renderSettings() {
  el("settings-body").innerHTML = settingsMarkup(providers, {
    provider: (state && state.provider) || "",
    model: (state && state.model) || "",
  });
}

async function openSettings() {
  overlays.settings.hidden = false;
  renderSettings();
  await loadProviders();
}

async function providerAction(button) {
  const provider = button.dataset.provider;
  const action = button.dataset.providerAction;
  const card = button.closest(".provider-card");
  if (action === "save_key") {
    const input = card.querySelector("[data-key-input]");
    const key = input.value.trim();
    if (!key) {
      toast("Paste the key first.", "bad");
      input.focus();
      return;
    }
    const result = await post("/api/providers", { action, provider, api_key: key }, `Key saved for ${providerTitle(provider)}.`);
    input.value = "";
    if (result) providers = result;
  } else if (action === "delete_key") {
    const result = await post("/api/providers", { action, provider }, `Key removed for ${providerTitle(provider)}.`);
    if (result) providers = result;
  } else if (action === "use" || action === "set_default") {
    const model = card.querySelector("[data-model-input]").value;
    if (action === "use") {
      const result = await post("/api/model", { model, provider, session: focusId() }, `This session now uses ${model}.`);
      if (result) {
        state = result;
        adoptState();
      }
    } else {
      const result = await post("/api/providers", { action, provider, model }, `New sessions will start on ${model}.`);
      if (result) providers = result;
    }
  }
  renderSettings();
  if (state) renderHeader();
}

/* ---------- schedules and watchers (DR-047) ---------- */

let automation = null;
const automationView = { editing: null };

async function loadAutomation() {
  try {
    automation = await api("/api/automation");
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
  renderAutomation();
}

function renderAutomation() {
  // A form being typed into is not repainted from under the person.
  if (automationView.editing && el("automation-body").querySelector("[data-auto-form]")) return;
  const tiers = ((state && state.tiers) || []).map((tier) => tier.name);
  el("automation-body").innerHTML = automationMarkup(automation, automationView, tiers);
}

async function openAutomation() {
  overlays.automation.hidden = false;
  automationView.editing = null;
  renderAutomation();
  await loadAutomation();
}

async function automationRequest(body, note) {
  const result = await post("/api/automation", body, note);
  if (result && result.automation) automation = result.automation;
  return result;
}

async function automationClick(event) {
  const target = event.target.closest("button");
  if (!target) return;
  const data = target.dataset;
  if (data.autoNew) {
    automationView.editing = { kind: data.autoNew, id: "" };
  } else if (data.autoEdit) {
    automationView.editing = { kind: data.autoEdit, id: data.id };
  } else if (data.autoCancel) {
    automationView.editing = null;
  } else if (data.autoSave) {
    const form = target.closest("[data-auto-form]");
    const kind = data.autoSave;
    const id = form.dataset.id;
    const values = automationFormValues(form);
    const result = await automationRequest(
      { action: `${id ? "update" : "add"}_${kind}`, ...(id ? { id } : {}), ...values },
      id ? "Saved." : `${kind === "schedule" ? "Schedule" : "Watcher"} added.`
    );
    if (!result) return;
    automationView.editing = null;
  } else if (data.autoToggle) {
    const enabled = data.enabled !== "true";
    await automationRequest({ action: `update_${data.autoToggle}`, id: data.id, enabled }, enabled ? "Resumed." : "Paused.");
  } else if (data.autoAction === "trust") {
    // Saving the command unchanged is what allows it on this machine.
    const watcher = (automation.watchers || []).find((item) => item.id === data.id);
    if (!watcher) return;
    await automationRequest({ action: "update_watcher", id: data.id, command: watcher.command }, "Allowed on this machine.");
  } else if (data.autoAction) {
    const action = data.autoAction;
    if (action.startsWith("delete_") && target.dataset.armed !== "1") {
      target.dataset.armed = "1";
      target.textContent = "Delete — sure?";
      return;
    }
    const notes = { run_now: "Started. It runs in its own session.", check_now: "", delete_schedule: "Deleted.", delete_watcher: "Deleted." };
    const result = await automationRequest({ action, id: data.id }, notes[action]);
    if (result && action === "check_now" && result.check) {
      const check = result.check;
      toast(
        check.refused
          ? `Refused: ${check.output}`
          : check.baseline
            ? "First check: its output is the baseline to compare against."
            : check.triggered
              ? "It changed, so a run started."
              : `No change (exit ${check.exit_code}).`,
        check.refused ? "bad" : ""
      );
    }
  } else {
    return;
  }
  automationView.editing = automationView.editing && { ...automationView.editing };
  const body = el("automation-body");
  body.innerHTML = automationMarkup(automation, automationView, ((state && state.tiers) || []).map((tier) => tier.name));
  const form = body.querySelector("[data-auto-form]");
  if (form) {
    form.scrollIntoView({ block: "nearest" });
    form.querySelector("[data-field]").focus();
  }
}

function closeOverlays() {
  ["diff", "doctor", "tips", "context", "settings", "automation", "allowed", "files"].forEach((name) => {
    overlays[name].hidden = true;
  });
}

function openTips() {
  el("tips-body").innerHTML = `<ul class="panel-list">${TIPS.map(
    (tip) => `<li class="panel-item"><div class="row"><span class="name">${escapeHtml(tip.title)}</span></div><div>${escapeHtml(tip.text)}</div></li>`
  ).join("")}</ul>`;
  overlays.tips.hidden = false;
}

function showTipPopup() {
  const tip = TIPS[Math.floor(Math.random() * TIPS.length)];
  el("tip-popup-title").textContent = tip.title;
  el("tip-popup-text").textContent = tip.text;
  el("tip-popup").hidden = false;
}

function hideTipPopup() {
  el("tip-popup").hidden = true;
}

/* ---------- actions ---------- */

async function submitPrompt() {
  const text = dom.prompt.value.trim();
  if (!text) {
    dom.composer.classList.remove("nudge");
    window.requestAnimationFrame(() => dom.composer.classList.add("nudge"));
    dom.prompt.focus();
    return;
  }
  // DR-042: while the session works, a request is queued for when it ends
  // rather than refused.
  const queueing = Boolean(state && state.running);
  dom.send.disabled = true;
  try {
    await api("/api/prompt", { prompt: text, session: focusId(), queue: queueing });
    dom.prompt.value = "";
    autosize();
    if (queueing) toast("Queued. Starts after the current run.", "good");
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
  renderStatus();
}

//: Set the moment Stop is clicked, so the button changes before the server
//: answers, and cleared when the turn ends. The snapshot's `stopping` is the
//: authority; this only covers the round trip.
let stopRequested = false;

async function requestStop() {
  if (stopRequested || !(state && state.running)) return;
  stopRequested = true;
  renderStatus();
  const payload = await post("/api/interrupt", { session: focusId() });
  if (!payload) {
    // The turn had already ended, or the request failed: let the next state
    // frame decide what the button should say.
    stopRequested = false;
    renderStatus();
  }
}

async function post(path, body, note) {
  try {
    const payload = await api(path, body);
    if (note) toast(note, "good");
    return payload;
  } catch (error) {
    toast(String(error.message || error), "bad");
    return null;
  }
}

/* ---------- wiring ---------- */

dom.send.addEventListener("click", submitPrompt);
dom.stop.addEventListener("click", requestStop);

dom.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitPrompt();
  }
});

dom.prompt.addEventListener("input", autosize);

dom.tier.addEventListener("click", (event) => {
  const choice = event.target.closest("[data-tier]");
  if (!choice || choice.disabled) return;
  post("/api/tier", { tier: choice.dataset.tier, session: focusId() });
});

dom.model.addEventListener("change", () => {
  const [provider, ...rest] = dom.model.value.split("|");
  post("/api/model", { model: rest.join("|"), provider, session: focusId() });
});

el("toggle-rail").addEventListener("click", () => {
  dom.shell.classList.toggle("rail-open");
});

dom.newSession.addEventListener("click", () => {
  post("/api/session", { action: "new" }, "Opened a session. Anything already running keeps going.");
});

el("clear-history").addEventListener("click", () => {
  if (window.confirm("Clear this session's history? Files stay as they are.")) {
    desk(focusId()).extras = [];
    post("/api/clear", { session: focusId() }, "History cleared.");
  }
});

el("open-diff").addEventListener("click", () => openDiff());
el("open-doctor").addEventListener("click", openDoctor);
el("review-plans").addEventListener("change", (event) => {
  post(
    "/api/plan-review",
    { enabled: event.target.checked, session: focusId() },
    event.target.checked
      ? "Craft and Forge will stop after planning for your review."
      : "Plans go straight to execution."
  );
});
el("open-settings").addEventListener("click", openSettings);
el("toggle-notify").addEventListener("click", async () => {
  const result = await setNotifications(!notifyActive());
  if (result === "on") toast("Notifications on. You will hear when something needs you or a run ends while you are elsewhere.", "good");
  else if (result === "denied") toast("The browser blocked notifications for this page — allow them in its site settings, then try again.", "bad");
  renderNotifyButton();
});
renderNotifyButton();
el("open-settings-header").addEventListener("click", openSettings);
el("settings-close").addEventListener("click", closeOverlays);
el("settings-refresh").addEventListener("click", loadProviders);
el("settings-body").addEventListener("click", (event) => {
  const button = event.target.closest("[data-provider-action]");
  if (button) providerAction(button);
});
// The picker lists every provider's models; fetched once at load, in the
// background — a slow hosted API must not hold up the first paint.
loadProviders();
el("open-automation").addEventListener("click", openAutomation);
el("automation-close").addEventListener("click", closeOverlays);
el("automation-body").addEventListener("click", automationClick);
el("open-tips").addEventListener("click", () => {
  hideTipPopup();
  openTips();
});
el("tips-close").addEventListener("click", closeOverlays);
el("status-ctx").addEventListener("click", openContext);
el("context-close").addEventListener("click", closeOverlays);
el("context-copy").addEventListener("click", copyContextRemedy);
el("context-apply").addEventListener("click", applyContextWindow);
el("context-choice-list").addEventListener("click", (event) => {
  const button = event.target.closest(".choice");
  if (!button) return;
  contextPick = Number(button.dataset.tokens);
  renderContextChoices();
});
el("open-tips").addEventListener("mouseenter", showTipPopup);
el("open-tips").addEventListener("mouseleave", hideTipPopup);
el("open-tips").addEventListener("focus", showTipPopup);
el("open-tips").addEventListener("blur", hideTipPopup);
el("setup-doctor").addEventListener("click", openDoctor);
el("setup-retry").addEventListener("click", () => post("/api/reload", {}));
el("setup-connect").addEventListener("submit", (event) => {
  event.preventDefault();
  post("/api/connect", { base_url: el("setup-url").value, model: el("setup-model").value }, "Connected");
});
el("doctor-refresh").addEventListener("click", openDoctor);
el("diff-close").addEventListener("click", closeOverlays);
el("doctor-close").addEventListener("click", closeOverlays);
el("needs-you").addEventListener("click", (event) => {
  if (event.target.closest("[data-manage-allowed]")) {
    openAllowed();
    return;
  }
  const revoke = event.target.closest("[data-revoke]");
  if (revoke) {
    post("/api/allowances", { action: "revoke", id: revoke.dataset.revoke, session: focusId() }, "It will ask again.");
    return;
  }
  const row = event.target.closest("[data-focus]");
  if (row && row.dataset.focus !== focusId()) post("/api/session", { action: "focus", id: row.dataset.focus });
});

/* One undo for the diff popup and the Changes tab: both revert the most
 * recent turn that wrote, and only that one (DR-024). */
async function undoChanges() {
  if (!window.confirm("Put these files back as they were before the last turn that changed them?")) return;
  const payload = await post("/api/undo", { session: focusId() });
  if (!payload) return;
  closeOverlays();
  changeDiffs.clear();
  openChanges.clear();
  if ((payload.skipped || []).length) {
    payload.skipped.forEach((item) => toast(`Left ${item.path} alone: ${item.reason}`, "bad"));
  }
  renderInspector();
}

el("diff-undo").addEventListener("click", undoChanges);

dom.jump.addEventListener("click", scrollToBottom);

dom.conversation.addEventListener("scroll", () => {
  dom.jump.hidden = isPinnedToBottom();
});

dom.messages.addEventListener("click", (event) => {
  // No intercept for the reasoning disclosure any more: the card is updated in
  // place, so the <details> element survives a paint and the browser keeps its
  // own open state.
  const target = event.target.closest(
    "[data-starter], [data-expand], [data-open-diff], [data-stop], [data-details], [data-queue], [data-decide], [data-decide-all], [data-plan-action]"
  );
  if (!target) return;
  if (target.dataset.starter !== undefined) {
    dom.prompt.value = STARTERS[Number(target.dataset.starter)].text;
    dom.prompt.focus();
    autosize();
  } else if (target.dataset.expand) {
    const run = focusedRun();
    if (!run) return;
    run.expanded = true;
    renderRun();
  } else if (target.dataset.openDiff) {
    openDiff(
      target.dataset.turn !== undefined ? Number(target.dataset.turn) : undefined,
      target.dataset.path
    );
  } else if (target.dataset.stop) {
    requestStop();
  } else if (target.dataset.planAction) {
    planAction(target);
  } else if (target.dataset.decide) {
    decide(target.dataset.id, target.dataset.decide);
  } else if (target.dataset.decideAll) {
    approvals
      .filter((item) => item.session === focusId())
      .forEach((item) => decide(item.id, target.dataset.decideAll));
  } else if (target.dataset.queue) {
    const body = { action: target.dataset.queue, session: focusId() };
    if (target.dataset.id) body.id = target.dataset.id;
    if (target.dataset.to !== undefined) body.to = Number(target.dataset.to);
    post("/api/queue", body);
  } else if (target.dataset.details) {
    const run = focusedRun();
    if (!run) return;
    run.detailed = !run.detailed;
    renderRun();
  }
});

el("toggle-inspector").addEventListener("click", () => {
  setInspectorOpen(!dom.shell.classList.contains("inspector-open"));
});

/* Double-clicking a changed file opens it in the diff viewer. */
el("inspector-body").addEventListener("dblclick", (event) => {
  const change = event.target.closest("[data-toggle-change]");
  if (change) openDiff(Number(change.dataset.turn), change.dataset.path);
});

el("inspector-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-inspector-tab]");
  if (!tab) return;
  inspectorTab = tab.dataset.inspectorTab;
  renderInspector();
});

el("inspector-body").addEventListener("click", (event) => {
  const viewer = event.target.closest("[data-open-diff]");
  if (viewer) {
    openDiff(Number(viewer.dataset.turn), viewer.dataset.path);
    return;
  }
  const change = event.target.closest("[data-toggle-change]");
  if (change) {
    toggleChange(change.dataset.toggleChange, Number(change.dataset.turn));
    return;
  }
  if (event.target.closest("[data-undo-turn]")) {
    undoChanges();
    return;
  }
  const toggle = event.target.closest("[data-toggle-step]");
  if (!toggle) return;
  const run = inspectedRun();
  if (!run) return;
  const id = toggle.dataset.toggleStep;
  if (run.openSteps.has(id)) run.openSteps.delete(id);
  else run.openSteps.add(id);
  renderInspector();
});

// Open by default on a wide screen; a narrow one gets the conversation first.
try {
  const saved = localStorage.getItem("autose.inspector");
  setInspectorOpen(saved ? saved === "open" : window.innerWidth >= 1240);
} catch (error) {
  setInspectorOpen(window.innerWidth >= 1240);
}
/* Drafting paper or cyanotype: the person's choice, else the system's. */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = el("toggle-theme");
  button.setAttribute("aria-pressed", String(theme === "dark"));
  button.title = theme === "dark" ? "Switch to drafting paper" : "Switch to cyanotype";
}

try {
  const saved = localStorage.getItem("autose.theme");
  applyTheme(saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
} catch (error) {
  applyTheme(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

el("toggle-theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem("autose.theme", next);
  } catch (error) {
    // Unavailable storage: the switch still works for this page.
  }
});

// Below this the rail is a drawer over the conversation: start with it shut.
if (window.innerWidth < 980) dom.shell.classList.remove("rail-open");

/* DR-031: the rail stays live while a turn runs. Switching sessions, opening
 * one and closing one are never gated on whether something is generating —
 * that gating was the reason the window felt frozen. */

el("show-sessions").addEventListener("click", () => setRailView("sessions"));
el("show-inbox").addEventListener("click", () => setRailView("inbox"));
dom.presence.addEventListener("click", () => {
  if (approvals.length || ((state && state.open) || []).some(sheetWaits)) setRailView("inbox");
});

el("inbox-list").addEventListener("click", (event) => {
  const target = event.target.closest("[data-decide], [data-focus], [data-inbox-resume]");
  if (!target) return;
  if (target.dataset.decide) {
    decide(target.dataset.id, target.dataset.decide);
  } else if (target.dataset.inboxResume) {
    post("/api/queue", { action: "resume", session: target.dataset.inboxResume });
  } else if (target.dataset.focus && target.dataset.focus !== focusId()) {
    post("/api/session", { action: "focus", id: target.dataset.focus });
  }
});

dom.sessionList.addEventListener("click", (event) => {
  const rename = event.target.closest("[data-rename]");
  if (rename) {
    startRename(rename.dataset.rename);
    return;
  }
  if (event.target.closest(".rename-form")) return;
  const remove = event.target.closest("[data-delete]");
  if (remove) {
    const id = remove.dataset.delete;
    if (armedDelete !== id) {
      // Ask twice before anything stored is lost.
      armedDelete = id;
      renderRail();
      return;
    }
    armedDelete = "";
    post("/api/session", { action: "delete", id }).then(refreshSessions);
    return;
  }
  const close = event.target.closest("[data-close]");
  if (close) {
    armedDelete = "";
    post("/api/session", { action: "close", id: close.dataset.close });
    return;
  }
  const switchTo = event.target.closest("[data-focus]");
  if (switchTo) {
    armedDelete = "";
    if (switchTo.dataset.focus === focusId()) return;
    post("/api/session", { action: "focus", id: switchTo.dataset.focus });
    return;
  }
  const resume = event.target.closest("[data-resume]");
  if (resume) {
    armedDelete = "";
    post("/api/session", { action: "resume", id: resume.dataset.resume });
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeOverlays();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshState();
});

dom.sessionList.addEventListener("dblclick", (event) => {
  const row = event.target.closest(".sheet-title");
  const owner = row && row.closest(".sheet-row");
  const button = owner && owner.querySelector("[data-rename]");
  if (button) startRename(button.dataset.rename);
});

dom.sessionList.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-rename-form]");
  if (!form) return;
  event.preventDefault();
  finishRename(form.dataset.renameForm, form.querySelector("input").value.trim());
});

dom.sessionList.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !event.target.closest(".rename-form")) return;
  event.stopPropagation();
  renamingId = "";
  renderRail();
});

dom.sessionList.addEventListener("focusout", (event) => {
  const form = event.target.closest && event.target.closest("[data-rename-form]");
  if (form && renamingId === form.dataset.renameForm) {
    finishRename(form.dataset.renameForm, form.querySelector("input").value.trim());
  }
});

/* A click on the dimmed backdrop, outside the panel, closes the window.
 * Setup is the exception: it has to be finished, not dismissed. A press
 * that starts inside the panel (selecting text) and ends outside does not
 * count. */
let backdropPress = null;
document.querySelectorAll(".overlay").forEach((overlay) => {
  if (overlay === overlays.setup) return;
  overlay.addEventListener("pointerdown", (event) => {
    backdropPress = event.target === overlay ? overlay : null;
  });
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && backdropPress === overlay) overlay.hidden = true;
    backdropPress = null;
  });
});

/* Enter in a plan's feedback box asks for those changes. */
dom.messages.addEventListener("keydown", (event) => {
  const input = event.target.closest && event.target.closest("[data-plan-feedback]");
  if (!input || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!input.value.trim()) return;
  const card = input.closest(".plan-card");
  const button = document.createElement("button");
  button.dataset.planAction = "revise";
  card.appendChild(button);
  planAction(button).finally(() => button.remove());
});

/* Enter in the reason box answers "Don't run" with what was typed: a
 * suggestion is a refusal that says what to do instead. An empty box does
 * nothing, so a stray Enter refuses nothing. */
dom.messages.addEventListener("keydown", (event) => {
  const input = event.target.closest && event.target.closest(".approval-reason[data-reason-for]");
  if (!input || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!input.value.trim()) return;
  decide(input.dataset.reasonFor, "deny");
});

/* ---------- YOLO mode (DR-053) ---------- */

async function setYolo(on) {
  if (on && !window.confirm(
    "Turn on YOLO mode?\n\nEvery shell command, file change, web call and plan, in every open session, will run without asking you. Anything waiting now runs at once.\n\nIt stays on until you turn it off or the dashboard restarts."
  )) return;
  const result = await post("/api/yolo", { enabled: on });
  if (result) {
    state = result;
    adoptState();
    toast(on ? "YOLO mode is on: nothing will ask." : "YOLO mode is off: risky calls ask again.", on ? "bad" : "good");
  }
}

el("toggle-yolo").addEventListener("click", () => setYolo(!(state && state.yolo)));

/* ---------- resizable panels ---------- */

/* The sessions panel and the details panel take the width you drag them to,
 * kept in this browser. The run takes what is left, never less than MAIN_MIN. */
const PANEL_LIMITS = { rail: [220, 560, 290], inspector: [300, 720, 360] };
const MAIN_MIN = 420;

function setPanelSize(which, width, keep = true) {
  const [min, max, fallback] = PANEL_LIMITS[which];
  const other = which === "rail" ? "inspector" : "rail";
  const otherWidth = dom.shell.querySelector(`.${other}`)?.getBoundingClientRect().width || 0;
  const room = window.innerWidth - 64 - otherWidth - 5 * 12 - MAIN_MIN;
  const size = Math.round(Math.max(min, Math.min(max, room, width || fallback)));
  document.documentElement.style.setProperty(`--${which}-size`, `${size}px`);
  if (keep) {
    try {
      window.localStorage.setItem(`autose.${which}-width`, String(size));
    } catch (error) {
      // Not kept between visits; the drag still works.
    }
  }
}

function restorePanelSizes() {
  ["rail", "inspector"].forEach((which) => {
    let saved = 0;
    try {
      saved = Number(window.localStorage.getItem(`autose.${which}-width`)) || 0;
    } catch (error) {
      saved = 0;
    }
    if (saved) setPanelSize(which, saved, false);
  });
}

document.querySelectorAll(".resize-handle").forEach((handle) => {
  const which = handle.dataset.resize;
  const panel = handle.parentElement;
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    try {
      handle.setPointerCapture(event.pointerId);
    } catch (error) {
      // A synthetic pointer cannot be captured; the drag still follows it.
    }
    const startX = event.clientX;
    const startWidth = panel.getBoundingClientRect().width;
    dom.shell.classList.add("resizing");
    const move = (moved) => {
      const delta = moved.clientX - startX;
      setPanelSize(which, which === "rail" ? startWidth + delta : startWidth - delta);
    };
    const stop = () => {
      dom.shell.classList.remove("resizing");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", stop);
      handle.removeEventListener("pointercancel", stop);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
  });
  handle.addEventListener("dblclick", () => setPanelSize(which, PANEL_LIMITS[which][2]));
  handle.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 48 : 16;
    const grow = which === "rail" ? event.key === "ArrowRight" : event.key === "ArrowLeft";
    const shrink = which === "rail" ? event.key === "ArrowLeft" : event.key === "ArrowRight";
    if (!grow && !shrink) return;
    event.preventDefault();
    setPanelSize(which, panel.getBoundingClientRect().width + (grow ? step : -step));
  });
});

restorePanelSizes();

/* ---------- helpers ---------- */

function autosize() {
  dom.prompt.style.height = "auto";
  dom.prompt.style.height = `${Math.min(dom.prompt.scrollHeight, 180)}px`;
}

function isPinnedToBottom() {
  const gap = dom.conversation.scrollHeight - dom.conversation.scrollTop - dom.conversation.clientHeight;
  return gap < 90;
}

function scrollToBottom() {
  dom.conversation.scrollTop = dom.conversation.scrollHeight;
  dom.jump.hidden = true;
}

const shownToasts = new Set();

function toast(message, tone = "", once = "") {
  if (replaying) return;
  if (once) {
    if (shownToasts.has(once)) return;
    shownToasts.add(once);
  }
  const node = document.createElement("div");
  node.className = `toast ${tone}`.trim();
  node.textContent = message;
  dom.toasts.append(node);
  window.setTimeout(() => node.remove(), tone === "bad" ? 12000 : 6000);
}

function describeTool(tool, args) {
  const spec = TOOL_LABELS[tool];
  if (!spec) return { icon: "·", html: `Using ${escapeHtml(humanize(tool || "a tool"))}` };
  const raw = String(args[spec.key] || "");
  // A path a tool reads or writes is a link into the Files view (DR-051).
  const detail = !spec.key || !raw
    ? ""
    : spec.key === "path"
      ? ` ${fileRefMarkup(raw, elideMiddle(raw, 64))}`
      : ` ${monoSpan(elideMiddle(raw, 64), raw)}`;
  const kind = tool === "run_command" ? "run" : "";
  return { kind, icon: spec.icon, html: `${escapeHtml(spec.verb)}${detail}` };
}

function monoSpan(text, full) {
  const title = full !== text ? ` title="${escapeHtml(full)}"` : "";
  return `<span class="mono"${title}>${escapeHtml(text)}</span>`;
}

/* "1 file", "3 files": counted and pluralised, never "file(s)". */
function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function elideMiddle(value, max) {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) * 0.6);
  return `${value.slice(0, head)}…${value.slice(value.length - (max - 1 - head))}`;
}

function humanize(value) {
  return String(value || "").replace(/_/g, " ").replace(/^\w/, (char) => char.toUpperCase());
}

function shortId(value) {
  return String(value || "").slice(0, 8) || "—";
}

/* A sheet's number: the distinctive tail of its session id, "S-069E". */
function sheetNo(value) {
  const tail = String(value || "").split("-").pop().slice(0, 4).toUpperCase();
  return tail ? `S-${tail}` : "—";
}

function shortenPath(path) {
  const parts = String(path).split("/").filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : path;
}

function formatCount(value) {
  const number = Number(value || 0);
  if (number >= 1000000) return `${(number / 1000000).toFixed(1)}M`;
  if (number >= 1000) return `${(number / 1000).toFixed(1)}k`;
  return String(number);
}

function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, "0")}s` : `${seconds}s`;
}

function formatWhen(timestamp) {
  const then = new Date(Number(timestamp || 0) * 1000);
  const minutes = Math.floor((Date.now() - then.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return then.toLocaleDateString();
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/* A deliberately small markdown subset: headings, lists, fences, tables, and
 * inline emphasis. Everything is escaped before any markup is added. */
function renderMarkdown(value) {
  const lines = String(value || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let inList = false;
  let inCode = false;
  let code = [];

  const closeList = () => {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${code.join("\n")}</code></pre>`);
        code = [];
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(escapeHtml(line));
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      continue;
    }

    if (isTableRow(trimmed) && isTableSeparator(lines[index + 1] || "")) {
      closeList();
      const header = splitRow(trimmed);
      const body = [];
      let cursor = index + 2;
      while (cursor < lines.length && isTableRow(lines[cursor].trim())) {
        body.push(splitRow(lines[cursor].trim()));
        cursor += 1;
      }
      html.push(renderTable(header, body));
      index = cursor - 1;
      continue;
    }

    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      html.push(`<h${heading[1].length}>${renderInline(heading[2])}</h${heading[1].length}>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*+]\s+(.+)$/) || trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (bullet) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }

    closeList();
    html.push(`<p>${renderInline(trimmed)}</p>`);
  }

  closeList();
  if (inCode) html.push(`<pre><code>${code.join("\n")}</code></pre>`);
  return html.join("");
}

function renderInline(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
}

function isTableRow(line) {
  return line.startsWith("|") && line.endsWith("|") && line.length > 2;
}

function isTableSeparator(line) {
  const trimmed = line.trim();
  return isTableRow(trimmed) && /^[|\s:-]+$/.test(trimmed);
}

function splitRow(line) {
  return line.slice(1, -1).split("|").map((cell) => cell.trim());
}

function renderTable(header, rows) {
  const head = header.map((cell) => `<th>${renderInline(cell)}</th>`).join("");
  const body = rows
    .map((row) => `<tr>${header.map((_, index) => `<td>${renderInline(row[index] || "")}</td>`).join("")}</tr>`)
    .join("");
  return `<div class="table-scroll"><table><thead><tr>${head}</tr></thead>${body ? `<tbody>${body}</tbody>` : ""}</table></div>`;
}

/* ---------- start ---------- */

if (!TOKEN) {
  document.body.innerHTML = `<div class="overlay"><div class="overlay-panel cropped">
    <span class="eyebrow"><span class="tick"></span>No token</span>
    <h2>Open the link from your terminal</h2>
    <p class="overlay-lede">This dashboard is reachable only with the token printed when it started, which is what keeps anything else on this machine from driving your workspace.</p>
  </div></div>`;
} else {
  refreshState().then(() => {
    refreshSessions();
    connect();
    autosize();
    dom.prompt.focus();
  });
}
