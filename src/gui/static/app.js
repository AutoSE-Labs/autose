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
    text: "Spark is a single direct pass for a question or a near-trivial edit. Craft plans, executes, and independently validates against a requirements ledger, for anything that needs a plan. A trivial question in Craft just burns time it doesn't need.",
  },
  {
    title: "Let it plan before it writes",
    text: "Craft produces a concrete plan with an ID'd requirements ledger before touching files, then Validate checks the result against that same ledger independently. If it's heading the wrong way, redirect it while the plan is still just text.",
  },
  {
    title: "Approvals are your steering wheel, not just a gate",
    text: "Anything that writes a file or runs a shell command pauses for your say-so. Refusing with a short reason, not just clicking Refuse, goes back to the model, so it tries something different instead of asking the same thing again.",
  },
  {
    title: "Review what changed before you trust it",
    text: "\"Review what it changed\" shows a real diff of every file touched this session, and one click undoes all of it if the run went sideways.",
  },
  {
    title: "Stop keeps the work, a reload doesn't",
    text: "A run with nothing capping its length can be stopped after the current step, keeping everything produced so far, reloading the page instead throws that away.",
  },
  {
    title: "Run several things at once",
    text: "The worklog rail holds more than one session, and switching away from one that's generating doesn't pause it, come back and it's further along, not stuck.",
  },
  {
    title: "Forgetting history doesn't touch your files",
    text: "\"Forget this conversation\" clears what gets sent back to the model as context. Whatever it already wrote to disk is untouched, use Undo for that instead.",
  },
  {
    title: "Watch the context meter, not just the token count",
    text: "The status bar tracks how full the context window was last round. A model running consistently near full starts losing earlier turns, that's when a fresh session helps more than a longer prompt.",
  },
  {
    title: "No model responding? Run Machine check",
    text: "It looks for a runtime already on this machine (Ollama, LM Studio, vLLM, llama.cpp) and reports what's reachable, what's serving, and whether it supports tool calls. Nothing is bundled or proxied.",
  },
  {
    title: "Learned facts stay inert until you approve them",
    text: "A stage can propose a fact it verified while working, but nothing is remembered across sessions until it's approved in Learned facts, and that approval is recorded against your name.",
  },
  {
    title: "Everything here runs local, by default",
    text: "No cloud call is made unless you point it at one. Set AUTOSE_BASE_URL and AUTOSE_MODEL to use any OpenAI-compatible endpoint instead of what was autodetected.",
  },
  {
    title: "Shift+Enter for a real prompt, Enter to send",
    text: "Enter starts the run immediately. Use Shift+Enter first if you want to lay out a longer request across a few lines before sending it.",
  },
  {
    title: "Starter chips are a draft, not a send",
    text: "Clicking one on the greeting screen only fills the composer with a starting prompt. Edit it to fit what you actually need, then press Start work when it's ready.",
  },
  {
    title: "Tier and model are per session",
    text: "Switching either dropdown only changes the session you're currently looking at. Open a second session to compare a different tier or model side by side instead of switching back and forth.",
  },
  {
    title: "Deleting a stored session asks twice",
    text: "The × next to a session turns into a ! on the first click. Only a second click, on that same button, actually deletes it, so a stray click can't lose one.",
  },
  {
    title: "Jump to the latest instead of fighting the scroll",
    text: "Scrolling up to reread earlier output doesn't lose your place. A ↓ button appears the moment you're no longer pinned to the bottom, so getting back is one click.",
  },
  {
    title: "Network calls are approved like writes are",
    text: "web_search and fetch_url pause for your approval too, since they bring back content AutoSE didn't write. The prompt tells you which kind of risk it is: a file write, a shell command, or a network fetch.",
  },
  {
    title: "The workspace path in the status bar is the boundary",
    text: "DIR names the one directory AutoSE can read and write. Nothing outside it is touched, no matter what a prompt asks for.",
  },
  {
    title: "A failed verdict is worth more than confident prose",
    text: "Validate issues a fail-closed verdict: if a requirement can't be independently confirmed, the stage is reported as failed rather than assumed fine. Trust that pass/fail over how convincing the writeup around it sounds.",
  },
  {
    title: "Closing a session isn't deleting it",
    text: "Closing just removes it from the rail for now; a generating session keeps running either way. Delete (the double-click ×) is the only action that actually discards a stored session.",
  },
];

/* Tools, described the way a person would describe them. The names come from
 * logic/common/tools; anything unknown falls through to a readable default. */
const TOOL_LABELS = {
  read_file: { icon: "◉", verb: "Reading", key: "path" },
  list_files: { icon: "▤", verb: "Looking around", key: "directory" },
  find_files: { icon: "⌕", verb: "Hunting for", key: "name_pattern" },
  search_files: { icon: "⌕", verb: "Searching the code for", key: "text" },
  write_file: { icon: "✎", verb: "Writing", key: "path" },
  edit_file: { icon: "✎", verb: "Editing", key: "path" },
  run_command: { icon: "▸", verb: "Running", key: "command" },
  git_status: { icon: "⎇", verb: "Checking what git says", key: "" },
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
  approval: el("approval-overlay"),
  diff: el("diff-overlay"),
  doctor: el("doctor-overlay"),
  tips: el("tips-overlay"),
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
        `${frame.sessions.length} sessions are working in this workspace at once. Their file changes are journalled separately, so Undo only covers the session you run it from.`,
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
        active.answer += frame.text;
        scheduleRun(active);
      }
      break;
    }
    case "turn":
      finishRun(id, frame.turn);
      break;
    case "failed":
      failRun(id, frame.title || "Stopped", frame.message || "");
      break;
    case "approval":
      approvals.push({ ...frame.request, session: id });
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
    case "event":
      handleEvent(id, frame);
      break;
    default:
      break;
  }
}

function handleEvent(id, frame) {
  const data = frame.data || {};
  const run = desk(id).run;
  if (!run && !["session_save_failed", "model_selected", "history_cleared"].includes(frame.event)) {
    // Events that belong to a turn we are not tracking (another tab started
    // it, or this one reloaded); the next state frame will catch us up.
    return;
  }
  switch (frame.event) {
    case "classification_started":
      setThinking(run, "Reading the request");
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
      setThinking(run, data.title || humanize(String(data.stage || "")));
      if (data.iteration > 1) {
        pushActivity(run, { icon: "↺", html: `${escapeHtml(data.title || humanize(String(data.stage)))} again, pass ${data.iteration}` });
      }
      break;
    case "stage_output_delta":
      run.narration += frame.message || "";
      scheduleRun(run);
      break;
    case "stage_retry":
      pushActivity(run, { kind: "fail", icon: "△", html: `Retrying ${escapeHtml(humanize(String(data.stage || "")))}: ${escapeHtml(frame.message || "")}` });
      break;
    case "stage_finished":
      run.stages.forEach((stage) => {
        if (stage.name === data.stage) stage.status = "done";
      });
      if (data.content) run.notes[data.artifact || data.stage] = data.content;
      if (Array.isArray(data.requirements) && data.requirements.length) run.requirements = data.requirements;
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
    case "loopback":
      pushActivity(run, { icon: "↺", html: `Back to ${escapeHtml(humanize(String(data.target || "")))} (${data.iteration}/${data.cap})` });
      break;
    case "tool_called":
      pushActivity(run, describeTool(String(data.tool || ""), data.arguments || {}));
      break;
    case "tool_completed":
      if (data.truncated) {
        pushActivity(run, { icon: "▭", html: `${escapeHtml(humanize(String(data.tool || "tool")))} returned more than fits; kept the head and the tail` });
      }
      break;
    case "approval_requested":
      setThinking(run, "Waiting for you");
      break;
    case "approval_resolved":
      pushActivity(run, {
        kind: data.approved ? "pass" : "denied",
        icon: data.approved ? "✓" : "⊘",
        html: `${data.approved ? "Approved" : "Refused"} ${escapeHtml(String(data.tool || ""))}${data.reason ? ` — ${escapeHtml(String(data.reason))}` : ""}`,
      });
      break;
    case "agent_reasoning_delta":
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
      toast(`Restored ${(data.restored || []).length} file(s).`, "good");
      break;
    case "model_selected":
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
    startRun(focusId(), state.current || {});
    seat.run.answer = state.answer || "";
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

function renderHeader() {
  const tiers = state.tiers || [];
  const chosen = state.auto_classify ? "auto" : state.tier;
  dom.tier.innerHTML = [
    `<option value="auto">Auto — classify each request</option>`,
    ...tiers.map((tier) => `<option value="${tier.name}">${escapeHtml(tier.name)} — ${escapeHtml(tier.summary)}</option>`),
  ].join("");
  dom.tier.value = chosen;
  dom.tier.disabled = !state.ready || state.running;

  const models = (state.models || []).filter(Boolean);
  const names = models.map((model) => model.name);
  if (state.model && !names.includes(state.model)) names.unshift(state.model);
  dom.model.innerHTML = names
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join("");
  dom.model.value = state.model || "";
  dom.model.disabled = !state.ready || state.running || names.length < 2;

  // DR-031: a session running is no reason not to open another one. The only
  // thing that closes this button is the desk being full.
  const full = (state.open || []).length >= (state.max_open || 6);
  dom.newSession.disabled = !state.ready || full;
  dom.newSession.title = full
    ? `${state.max_open} sessions are open — close one first`
    : "Open another session; anything already running keeps going";
  dom.shell.classList.toggle("is-running", Boolean(state.running));
}

function renderStatus() {
  if (!state) return;
  const usage = state.usage || {};
  dom.modelLabel.textContent = state.model || "none";
  dom.modelLabel.title = `${state.provider || ""} ${state.base_url || ""}`.trim();
  dom.dirLabel.textContent = shortenPath(state.workspace || "");
  dom.dirLabel.title = state.workspace || "";
  dom.sessionLabel.textContent = state.session_id ? shortId(state.session_id) : "—";
  dom.tokensLabel.textContent = formatCount(usage.total_tokens || 0);

  const limit = state.context_limit || 0;
  if (limit && lastPromptTokens) {
    const percent = Math.min(100, Math.round((lastPromptTokens / limit) * 100));
    dom.ctxFill.style.width = `${percent}%`;
    dom.ctxFill.className = `ctx-fill${percent > 90 ? " hot" : percent > 70 ? " warn" : ""}`;
    dom.ctxLabel.textContent = `CTX ${percent}%`;
    dom.ctxLabel.title = `${formatCount(lastPromptTokens)} of ${formatCount(limit)} tokens last round`;
  } else {
    dom.ctxFill.style.width = "0%";
    dom.ctxLabel.textContent = limit ? `CTX ${formatCount(limit)}` : "CTX —";
  }

  const run = focusedRun();
  const elsewhere = runningElsewhere();
  const waitingHere = approvals.some((item) => item.session === focusId());
  let label = "Ready";
  let mood = "";
  if (!connected) {
    label = "Reconnecting to AutoSE";
    mood = "offline";
  } else if (waitingHere) {
    label = "Waiting for your decision";
    mood = "attention";
  } else if (state.running) {
    label = (run && run.thinkingLabel) || "Working";
    mood = "working";
  } else if (!state.ready) {
    label = "No model";
    mood = "offline";
  }
  // Somebody else's run is not this session's status, but it must be visible:
  // the whole point of DR-031 is that work continues where you are not looking.
  if (elsewhere.length) {
    label += ` · ${elsewhere.length} working elsewhere`;
    if (!mood) mood = "aside";
  }
  dom.presence.className = `presence status-cell ${mood}`.trim();
  dom.presenceText.textContent = label;

  // DR-012: the posture this run is in belongs on screen, not only in the
  // terminal that started it. An auto-approved dashboard never interrupts,
  // which is exactly why it has to say so.
  if (state.auto_approve) {
    dom.stamp.textContent = "AUTO-APPROVE ON";
    dom.stamp.title = `Shell and network calls run without asking, authorized by ${state.authorized_by || "whoever started this"}.`;
    toast(
      `Auto-approve is on: shell and network calls run without asking (authorized by ${state.authorized_by || "the operator"}).`,
      "bad",
      "auto-approve"
    );
  } else {
    dom.stamp.textContent = "LOCAL / NO CLOUD";
    dom.stamp.title = `These tools always ask first: ${(state.gated_tools || []).join(", ")}.`;
    if (state.require_edit_approval) {
      toast("This workspace asks before every file write.", "", "edit-approval");
    }
  }

  // Deliberately *not* disabled while a turn runs: someone mid-sentence should
  // not have the box taken away from them, and `submitPrompt` refuses a second
  // request itself with a reason. Only an unusable dashboard locks it.
  dom.prompt.disabled = !state.ready;
  dom.send.disabled = state.running || !state.ready;
  dom.send.textContent = "Start work";
  // DR-030: a run has no built-in end, so Stop *takes the primary button's
  // place* while one is going rather than sitting beside a dead "Working…"
  // button — the control is where the hand already is. `stopping` comes from
  // the snapshot, so a reopened tab does not offer a button already pressed.
  const stopping = Boolean(state.stopping || stopRequested);
  dom.send.hidden = Boolean(state.running);
  dom.stop.hidden = !state.running;
  dom.stop.disabled = stopping;
  dom.stop.textContent = stopping ? "Stopping…" : "Stop";
  dom.hint.textContent = state.running
    ? (stopping
      ? "STOPPING AFTER THE CURRENT STEP — WHAT IT PRODUCED IS KEPT"
      : "STOP ENDS IT AND KEEPS WHAT IT HAS PRODUCED · OR OPEN ANOTHER SESSION AND WORK ON")
    : "ENTER ↵ STARTS · SHIFT+ENTER FOR A NEW LINE";
  renderCardStop();
}

/* ---------- the rail ---------- */

function renderRail() {
  const open = (state && state.open) || [];
  const openIds = new Set(open.map((entry) => entry.session_id));
  const rest = stored.filter((record) => !openIds.has(record.id));
  const blocks = storedProblems.map((problem) => `<p class="empty-state">${escapeHtml(problem)}</p>`);

  if (open.length) {
    blocks.push(groupHead(`Open · ${open.length}`));
    open.forEach((entry) => blocks.push(openRowMarkup(entry)));
  }
  if (rest.length) {
    blocks.push(groupHead(`Stored · ${rest.length}`));
    rest.forEach((record) => blocks.push(storedRowMarkup(record)));
  }
  if (!open.length && !rest.length) {
    blocks.push(
      `<p class="empty-state">Nothing stored yet. Every turn is written to <span class="mono">.autose/sessions/</span>, so you can pick this up later.</p>`
    );
  }
  dom.sessionList.innerHTML = blocks.join("");
  scheduleRailTicks(open.some((entry) => entry.running));
}

function groupHead(label) {
  return `<div class="session-group"><span class="eyebrow">${escapeHtml(label)}</span></div>`;
}

function openRowMarkup(entry) {
  const active = entry.session_id === focusId();
  // The count the server sent covers a tab that has only just loaded and never
  // saw the `approval` frame; what this tab knows covers the other direction.
  const waiting = Math.max(
    entry.waiting || 0,
    approvals.filter((item) => item.session === entry.session_id).length
  );
  const detail = entry.running
    ? `${entry.stopping ? "stopping" : "working"} · ${formatElapsed(Date.now() - (entry.current && entry.current.started_at ? entry.current.started_at * 1000 : Date.now()))}`
    : `${entry.turns} turn(s) · ${formatCount(entry.total_tokens)} tok`;
  const status = entry.running ? (entry.stopping ? "stopping" : "running") : "open";
  return `<div class="session-row open${active ? " active" : ""}">
      <button class="session-item" type="button" data-focus="${escapeHtml(entry.session_id)}" title="${escapeHtml(entry.title)}">
        <span class="dot" data-status="${status}"></span>
        <span>${escapeHtml(entry.title)}</span>
        <small>${escapeHtml(entry.tier || "")} · ${escapeHtml(detail)}</small>
      </button>
      ${waiting ? `<span class="session-badge" title="This session is waiting for a decision">!</span>` : ""}
      <button class="session-close" type="button" data-close="${escapeHtml(entry.session_id)}" title="${entry.running ? "Stop it before closing it" : "Close this session — it stays on disk"}">−</button>
    </div>`;
}

function storedRowMarkup(record) {
  const status = record.stop_reason === "completed" ? "done" : record.stop_reason ? "failed" : "stopped";
  const armed = armedDelete === record.id;
  return `<div class="session-row">
      <button class="session-item" type="button" data-resume="${escapeHtml(record.id)}" title="${escapeHtml(record.title)}">
        <span class="dot" data-status="${status}"></span>
        <span>${escapeHtml(record.title || "(no title)")}</span>
        <small>${escapeHtml(record.tier || "")} · ${record.turns} turn(s) · ${formatCount(record.total_tokens)} tok · ${formatWhen(record.updated_at)}</small>
      </button>
      <button class="session-delete${armed ? " armed" : ""}" type="button" data-delete="${escapeHtml(record.id)}" title="${armed ? "Click again to delete" : "Delete this stored session"}">${armed ? "!" : "×"}</button>
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
  history.forEach((turn) => nodes.push(slipNode(turn.prompt), receiptNode(turn)));
  seat.extras.forEach((node) => nodes.push(node));
  if (seat.run) nodes.push(seat.run.slip, seat.run.node);
  dom.messages.replaceChildren(...nodes);
  if (seat.run) renderRun();
  if (pinned) scrollToBottom();
}

function greetingNode() {
  const node = document.createElement("div");
  node.className = "greeting";
  node.innerHTML = `
    <span class="eyebrow"><span class="tick"></span>Workshop</span>
    <h1>Describe the work.<br /><span class="ink-underline">AutoSE does it here, on this machine.</span></h1>
    <p>It reads and writes files in <span class="mono">${escapeHtml(shortenPath(state.workspace || ""))}</span>, asks before it touches your shell or the network, and journals every change so you can review or undo it.</p>
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
  stalled: "Stopped — it went in circles",
  context_thrashing: "Stopped — the context window is too small",
  round_cap: "Stopped — hit the round limit",
  no_result: "Stopped — nothing was produced",
};

const OUTCOME_REASONS = {
  stalled:
    "It kept re-reading what it had already been given instead of answering, so it was " +
    "stopped. This usually means the model is too small for the request — try a larger " +
    "one, or ask something narrower.",
  context_thrashing:
    "Every round had to discard the evidence the round before it gathered, to fit the " +
    "served context window — so the model kept re-reading what it could no longer see. " +
    "The window is the problem, not the model: raise it and run this again.",
  round_cap: "The round limit was reached before an answer was written.",
  empty_answer: "The model returned nothing at all.",
  no_result: "The run ended without producing a result.",
  cancelled: "Stopped before there was anything to show.",
};

function receiptNode(turn) {
  const node = document.createElement("article");
  node.className = `work-card ${turn.succeeded ? "done" : "failed"}`;
  node.innerHTML = receiptMarkup(turn);
  return node;
}

function receiptMarkup(turn) {
  const usage = turn.usage || {};
  const facts = [
    fact(turn.tier, "good"),
    fact(`${formatCount(usage.total_tokens || 0)} tokens`),
    turn.files_changed.length ? fact(`${turn.files_changed.length} file(s) changed`) : "",
    turn.succeeded ? "" : fact(humanize(turn.stop_reason), "warn"),
    turn.classification ? fact(`classified by ${turn.classification.source}`) : "",
  ]
    .filter(Boolean)
    .join("");

  const changes = (turn.changes || []).length
    ? section(
      "Files it wrote",
      turn.changes.map(
        (change) =>
          `<li><span class="li-mark">✎</span><span class="file">${escapeHtml(change.path)}</span><span class="stat">+${change.added} −${change.removed}</span></li>`
      )
    )
    : "";

  const notes = Object.entries(turn.artifacts || {})
    .map(
      ([name, text]) =>
        `<details class="worknotes"><summary>${escapeHtml(humanize(name))}</summary><div class="worknotes-body">${escapeHtml(text)}</div></details>`
    )
    .join("");

  const headline = turn.succeeded
    ? "Done"
    : turn.stop_reason === "empty_answer"
      ? "It ran out of room to answer"
      : turn.stop_reason === "cancelled"
        // DR-030: a run the user ended on purpose is reported as their choice,
        // not as something that went wrong.
        ? "You stopped this run"
        : OUTCOME_HEADLINES[turn.stop_reason] || `Stopped — ${humanize(turn.stop_reason)}`;

  // A run that stopped without writing an answer renders a headline and
  // nothing else, which is how "Stopped — Stalled" came to be the entire
  // report. Every unsuccessful outcome says what happened and what to do.
  const why =
    turn.succeeded || turn.answer
      ? ""
      : `<p class="receipt-why">${escapeHtml(
          OUTCOME_REASONS[turn.stop_reason] ||
            "The run ended without producing an answer."
        )}</p>`;

  return `<div class="work-head">
      <span class="presence-dot"></span>
      <span class="eyebrow">Report</span>
      <span class="work-title">${escapeHtml(turn.prompt.split("\n")[0].slice(0, 80))}</span>
    </div>
    <div class="receipt">
      <div class="receipt-headline">${escapeHtml(headline)}</div>
      ${turn.answer ? `<div class="receipt-summary prose">${renderMarkdown(turn.answer)}</div>` : ""}
      ${why}
      <div class="receipt-facts">${facts}</div>
      ${changes}
      ${(turn.changes || []).length ? `<div class="receipt-actions"><button class="inline-button" type="button" data-open-diff="1">Review the diff</button></div>` : ""}
      ${notes}
    </div>`;
}

function fact(label, tone = "") {
  return `<span class="fact ${tone}"><span class="dot"></span>${escapeHtml(String(label || ""))}</span>`;
}

function section(title, items) {
  if (!items.length) return "";
  return `<div class="receipt-section"><h4 class="eyebrow">${escapeHtml(title)}</h4><ul>${items.join("")}</ul></div>`;
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
    thinkingLabel: "Thinking",
    running: true,
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
}

/* The live card's frame, written once. Every part that changes as the run goes
 * is addressed by `data-part` and updated in place. */
const CARD_SKELETON = `
  <div class="work-head">
    <span class="presence-dot"></span>
    <span class="eyebrow" data-part="phase"></span>
    <span class="work-title" data-part="title"></span>
    <span class="work-elapsed" data-part="elapsed"></span>
    <button class="card-stop" type="button" data-stop="1"
      title="Stop after the current step, keeping everything produced so far">Stop</button>
  </div>
  <div class="stages" data-part="stages"></div>
  <div class="activity" data-part="activity"></div>
  <div class="narration prose" data-part="narration" hidden></div>
  <details class="thinking" data-part="thinking" hidden>
    <summary>Its reasoning, as it streams</summary>
    <div class="thinking-body" data-part="reasoning"></div>
  </details>`;

function ensureCard(run) {
  if (run.dom) return;
  run.node.innerHTML = CARD_SKELETON;
  const part = (name) => run.node.querySelector(`[data-part="${name}"]`);
  run.dom = {
    phase: part("phase"),
    title: part("title"),
    elapsed: part("elapsed"),
    stop: run.node.querySelector("[data-stop]"),
    stages: part("stages"),
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

function stagesMarkup(run) {
  return run.stages
    .map(
      (stage, index) =>
        `<div class="stage ${stage.status}"><span class="stage-id">${String(index + 1).padStart(2, "0")}</span><span class="stage-name">${escapeHtml(stage.title)}</span></div>`
    )
    .join("");
}

function activityMarkup(run) {
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

  setText(d.phase, run.running ? "At work" : "Finishing");
  setText(d.title, run.thinkingLabel + (run.tier ? ` · ${run.tier}` : ""));
  setText(d.elapsed, formatElapsed(Date.now() - run.startedAt));
  renderCardStop();

  let grew = setHtml(d.stages, stagesMarkup(run));
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
    lastPromptTokens = (turn.usage && turn.usage.prompt_tokens) || lastPromptTokens;
  }
  const run = seat.run;
  if (!run) return;
  stopTimer(run);
  // The next state frame carries this turn in `history`, so the live pair is
  // dropped rather than drawn twice.
  seat.run = null;
  if (isFocused(id)) {
    run.node.className = `work-card ${turn.succeeded ? "done" : "failed"}`;
    run.node.innerHTML = receiptMarkup(turn);
    scrollToBottom();
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
    seat.run = null;
    if (isFocused(id)) renderConversation();
  } else {
    toast(`${shortId(id)}: ${title}: ${message}`, "bad");
  }
  renderStatus();
}

/* ---------- approvals (DR-012) ---------- */

function renderApproval() {
  // Only the focused session's decision takes the screen. One arriving in a
  // session the user is not looking at is announced in the rail instead —
  // seizing the window would undo the whole point of running two things.
  const request = approvals.find((item) => item.session === focusId());
  overlays.approval.hidden = !request;
  renderStatus();
  if (!request) return;
  const args = request.arguments || {};
  const mine = approvals.filter((item) => item.session === focusId());
  el("approval-title").textContent = `Run ${request.tool}?`;
  el("approval-lede").textContent = approvalLede(request.tool);
  el("approval-args").textContent = Object.keys(args).length
    ? Object.entries(args)
      .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
      .join("\n")
    : request.summary;
  const more = el("approval-more");
  const elsewhere = approvals.length - mine.length;
  const notes = [
    mine.length > 1 ? `${mine.length - 1} more call(s) waiting behind this one` : "",
    elsewhere ? `${elsewhere} call(s) waiting in other sessions` : "",
  ].filter(Boolean);
  more.hidden = !notes.length;
  more.textContent = notes.join(" · ");
  el("approval-reason").value = "";
  el("approval-allow").focus();
}

function approvalLede(tool) {
  if (tool === "run_command") return "This runs in your shell, in the workspace, exactly as written below.";
  if (tool === "fetch_url" || tool === "web_search") return "This reaches the network and brings back content AutoSE did not write.";
  return "This changes a file in your workspace.";
}

async function resolveApproval(approved) {
  const request = approvals.find((item) => item.session === focusId());
  if (!request) return;
  const reason = el("approval-reason").value.trim();
  overlays.approval.hidden = true;
  try {
    await api("/api/approval", { id: request.id, approved, reason });
  } catch (error) {
    // The server refuses an approval it is no longer holding — answered in
    // another tab, or timed out while this one was deciding. Drop it rather
    // than leaving a decision on screen that nothing is waiting on.
    approvals = approvals.filter((item) => item.id !== request.id);
    toast(String(error.message || error), "bad");
    renderApproval();
  }
}

/* ---------- overlays ---------- */

async function openDiff() {
  try {
    const payload = await api("/api/changes");
    const changes = payload.changes || [];
    el("diff-stat").textContent = changes.length
      ? `${changes.length} file(s) · +${payload.added} −${payload.removed}`
      : "";
    el("diff-body").innerHTML = changes.length
      ? changes.map(diffMarkup).join("")
      : `<p class="panel-note">Nothing has been written in this session yet.</p>`;
    el("diff-undo").disabled = !changes.length;
    overlays.diff.hidden = false;
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
}

function diffMarkup(change) {
  const rows = (change.diff || "")
    .split("\n")
    .map((line) => {
      const kind = line.startsWith("+") && !line.startsWith("+++")
        ? "add"
        : line.startsWith("-") && !line.startsWith("---")
          ? "del"
          : line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")
            ? "meta"
            : "";
      return `<span class="row ${kind}">${escapeHtml(line) || " "}</span>`;
    })
    .join("");
  return `<div class="diff-file">
      <div class="diff-head">
        <span class="path">${escapeHtml(change.path)}</span>
        <span class="meta">${escapeHtml(change.kind)}${change.tool ? ` · ${escapeHtml(change.tool)}` : ""}</span>
        <span class="stat"><span class="added">+${change.added}</span> <span class="removed">−${change.removed}</span></span>
      </div>
      <pre class="diff-lines">${rows || `<span class="row meta">(no textual diff)</span>`}</pre>
    </div>`;
}

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
    blocks.push(
      `<div class="panel-block"><h4 class="eyebrow">Problems</h4>${report.problems
        .map((problem) => `<p class="form-error">${escapeHtml(problem)}</p>`)
        .join("")}</div>`
    );
  }
  if (report.advisories.length) {
    blocks.push(
      `<div class="panel-block"><h4 class="eyebrow">Worth knowing</h4><ul class="panel-list">${report.advisories
        .map((note) => `<li class="panel-item">${escapeHtml(note)}</li>`)
        .join("")}</ul></div>`
    );
  }
  blocks.push(
    `<div class="panel-block"><h4 class="eyebrow">Runtimes</h4><ul class="panel-list">${report.runtimes.length
      ? report.runtimes
        .map(
          (runtime) => `<li class="panel-item">
                <div class="row">
                  <span class="name">${escapeHtml(runtime.provider)}</span>
                  <span class="meta">${escapeHtml(runtime.base_url)}</span>
                  <span class="meta ${runtime.usable ? "good" : "warn"}">${runtime.usable ? "serving" : runtime.reachable ? "reachable, no models" : "not reachable"}</span>
                </div>
                ${runtime.error ? `<div class="meta warn">${escapeHtml(runtime.error)}</div>` : ""}
                ${runtime.models
              .map(
                (model) =>
                  `<div class="row"><span class="mono">${escapeHtml(model.name)}</span><span class="meta">${model.context_length ? `${formatCount(model.context_length)} tok served` : "window unknown"}${model.is_context_capped ? ` of ${formatCount(model.max_context_length)}` : ""}</span><span class="meta ${model.is_tool_capable ? "good" : "warn"}">${model.is_tool_capable ? "tools" : "tool support unknown"}</span></div>`
              )
              .join("")}
              </li>`
        )
        .join("")
      : `<li class="panel-item">Nothing answered on this machine.</li>`
    }</ul></div>`
  );
  blocks.push(
    `<div class="panel-block"><h4 class="eyebrow">This workspace</h4><ul class="panel-list">
      <li class="panel-item"><div class="row"><span class="mono">${escapeHtml(report.workspace)}</span></div>
      <div class="meta">${escapeHtml(report.config_source || "no config file — defaults in use")}</div>
      <div class="meta">tiers: ${escapeHtml(report.implemented_tiers.join(", "))}</div></li>
    </ul></div>`
  );
  return blocks.join("");
}

function closeOverlays() {
  ["diff", "doctor", "tips"].forEach((name) => {
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
  if (state && state.running) {
    // Answered here rather than by a 409 from the server, because the useful
    // half of the answer is what to do about it (DR-030, DR-031).
    toast("This session is already working — Stop it, wait, or open another session to work in.");
    return;
  }
  const text = dom.prompt.value.trim();
  if (!text) {
    dom.composer.classList.remove("nudge");
    window.requestAnimationFrame(() => dom.composer.classList.add("nudge"));
    dom.prompt.focus();
    return;
  }
  dom.send.disabled = true;
  try {
    await api("/api/prompt", { prompt: text, session: focusId() });
    dom.prompt.value = "";
    autosize();
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

dom.tier.addEventListener("change", () => {
  post("/api/tier", { tier: dom.tier.value, session: focusId() });
});

dom.model.addEventListener("change", () => {
  post("/api/model", { model: dom.model.value, session: focusId() });
});

el("toggle-rail").addEventListener("click", () => {
  dom.shell.classList.toggle("rail-open");
});

dom.newSession.addEventListener("click", () => {
  post("/api/session", { action: "new" }, "Opened a session. Anything already running keeps going.");
});

el("clear-history").addEventListener("click", () => {
  if (window.confirm("Forget this conversation? The files it wrote are untouched.")) {
    desk(focusId()).extras = [];
    post("/api/clear", { session: focusId() }, "History cleared.");
  }
});

el("open-diff").addEventListener("click", openDiff);
el("open-doctor").addEventListener("click", openDoctor);
el("open-tips").addEventListener("click", () => {
  hideTipPopup();
  openTips();
});
el("tips-close").addEventListener("click", closeOverlays);
el("open-tips").addEventListener("mouseenter", showTipPopup);
el("open-tips").addEventListener("mouseleave", hideTipPopup);
el("open-tips").addEventListener("focus", showTipPopup);
el("open-tips").addEventListener("blur", hideTipPopup);
el("setup-doctor").addEventListener("click", openDoctor);
el("setup-retry").addEventListener("click", () => post("/api/reload", {}));
el("doctor-refresh").addEventListener("click", openDoctor);
el("diff-close").addEventListener("click", closeOverlays);
el("doctor-close").addEventListener("click", closeOverlays);
el("approval-allow").addEventListener("click", () => resolveApproval(true));
el("approval-deny").addEventListener("click", () => resolveApproval(false));

el("diff-undo").addEventListener("click", async () => {
  if (!window.confirm("Put these files back as they were before the last turn?")) return;
  const payload = await post("/api/undo", { session: focusId() });
  if (!payload) return;
  closeOverlays();
  if ((payload.skipped || []).length) {
    payload.skipped.forEach((item) => toast(`Left ${item.path} alone: ${item.reason}`, "bad"));
  }
});

dom.jump.addEventListener("click", scrollToBottom);

dom.conversation.addEventListener("scroll", () => {
  dom.jump.hidden = isPinnedToBottom();
});

dom.messages.addEventListener("click", (event) => {
  // No intercept for the reasoning disclosure any more: the card is updated in
  // place, so the <details> element survives a paint and the browser keeps its
  // own open state.
  const target = event.target.closest(
    "[data-starter], [data-expand], [data-open-diff], [data-stop]"
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
    openDiff();
  } else if (target.dataset.stop) {
    requestStop();
  }
});

/* DR-031: the rail stays live while a turn runs. Switching sessions, opening
 * one and closing one are never gated on whether something is generating —
 * that gating was the reason the window felt frozen. */
dom.sessionList.addEventListener("click", (event) => {
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
  if (!overlays.approval.hidden) return; // an approval is answered, not dismissed
  closeOverlays();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshState();
});

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
  const detail = spec.key && raw ? ` ${monoSpan(elideMiddle(raw, 64), raw)}` : "";
  const kind = tool === "run_command" ? "run" : "";
  return { kind, icon: spec.icon, html: `${escapeHtml(spec.verb)}${detail}` };
}

function monoSpan(text, full) {
  const title = full !== text ? ` title="${escapeHtml(full)}"` : "";
  return `<span class="mono"${title}>${escapeHtml(text)}</span>`;
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
