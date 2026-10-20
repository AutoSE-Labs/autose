/* Terminals: the commands runs started in the background (DR-062).
 *
 * - A rail section with a badge counting what is still running.
 * - A window listing every open session's background commands, newest first,
 *   and the selected one's output in a dark code box that follows the end
 *   until the person scrolls up to read.
 * - Stop for a running command; Run again and Clear for a finished one.
 * - Who started each (a run, or the person), when, for how long, its session
 *   with a way back to it, and any local address it printed, to open
 *   (DR-064).
 * - "Start something": in words, which a Spark run in the chosen session
 *   turns into a command (asked about as usual), or as a command the person
 *   types, which runs as typed (DR-064).
 * - In a run's steps, a command that went to the background says so and
 *   links here (`[data-open-terminal]`).
 *
 * The list is fetched again whenever a `process_*` event arrives; the output
 * of the selected command is read from a cursor once a second while the
 * window is open, so only new lines cross the wire. Loaded after app.js.
 */

const TERMINAL_POLL_MS = 1000;
const TERMINAL_KEEP_LINES = 4000;

const terminals = {
  list: [],
  running: 0,
  selected: "",
  lines: [],
  partial: "",
  cursor: 0,
  dropped: 0,
  follow: true,
  timer: 0,
  reading: false,
  mode: "ask",
  // The session picked for "Start something"; empty means the focused one.
  startSession: "",
  // What the last "In words" request was sent to, to say where to follow it.
  asked: null,
};

const TERM_MODES = {
  ask: {
    placeholder: "Start the dev server",
    help: "A Spark run in this session works out the command and starts it. It asks before running anything, as any run does.",
    go: "Ask",
  },
  start: {
    placeholder: "npm run dev",
    help: "Runs exactly as typed, in the workspace, without asking: you wrote it. It stops when its session closes.",
    go: "Start",
  },
};

/* ---------- reading ---------- */

async function refreshTerminals() {
  try {
    const payload = await api("/api/processes");
    adoptTerminalList(payload);
  } catch (error) {
    // The badge keeps its last count; the next event tries again.
  }
}

function adoptTerminalList(payload) {
  terminals.list = payload.processes || [];
  terminals.running = payload.running || 0;
  if (terminals.selected && !terminals.list.some((item) => item.id === terminals.selected)) {
    selectTerminal("");
  }
  renderTerminalBadge();
  if (!overlays.terminals.hidden) renderTerminals();
}

async function readTerminalOutput() {
  const id = terminals.selected;
  if (!id || terminals.reading) return;
  terminals.reading = true;
  try {
    const payload = await api(`/api/processes?id=${encodeURIComponent(id)}&since=${terminals.cursor}`);
    if (terminals.selected !== id) return;
    const output = payload.output || {};
    if (output.skipped) terminals.dropped += output.skipped;
    terminals.lines.push(...(output.lines || []));
    if (terminals.lines.length > TERMINAL_KEEP_LINES) {
      terminals.dropped += terminals.lines.length - TERMINAL_KEEP_LINES;
      terminals.lines.splice(0, terminals.lines.length - TERMINAL_KEEP_LINES);
    }
    terminals.cursor = output.cursor || terminals.cursor;
    terminals.partial = output.partial || "";
    adoptTerminalList(payload);
  } catch (error) {
    // Cleared or its session closed: the list refresh drops the selection.
    refreshTerminals();
  } finally {
    terminals.reading = false;
  }
}

function selectTerminal(id) {
  terminals.selected = id;
  terminals.lines = [];
  terminals.partial = "";
  terminals.cursor = 0;
  terminals.dropped = 0;
  terminals.follow = true;
  el("terminals-output").__shown = undefined;
}

/* ---------- opening ---------- */

async function openTerminals(id) {
  overlays.terminals.hidden = false;
  renderTermStart();
  if (id && id !== terminals.selected) selectTerminal(id);
  await refreshTerminals();
  if (!terminals.selected && terminals.list.length) {
    selectTerminal((terminals.list.find((item) => item.status === "running") || terminals.list[0]).id);
  }
  renderTerminals();
  await readTerminalOutput();
  renderTerminals();
  startTerminalPolling();
}

function startTerminalPolling() {
  if (terminals.timer) return;
  terminals.timer = window.setInterval(async () => {
    if (overlays.terminals.hidden) {
      window.clearInterval(terminals.timer);
      terminals.timer = 0;
      return;
    }
    const chosen = terminals.list.find((item) => item.id === terminals.selected);
    // A finished command's output no longer changes once it has been read.
    if (chosen && (chosen.status === "running" || chosen.alive || chosen.lines > terminals.cursor)) {
      await readTerminalOutput();
    }
    renderTerminals();
  }, TERMINAL_POLL_MS);
}

/* ---------- drawing ---------- */

function renderTerminalBadge() {
  const badge = el("terminals-badge");
  badge.hidden = !terminals.running;
  badge.textContent = String(terminals.running);
  el("open-terminals").classList.toggle("has-items", terminals.running > 0);
}

function terminalStatus(item) {
  if (item.status === "running") return { tone: "running", text: "running" };
  // The command ended, but something it started is still going.
  if (item.alive) return { tone: "running", text: `exited ${item.exit_code}, still running` };
  if (item.status === "stopped") return { tone: "stopped", text: "stopped" };
  return { tone: item.exit_code === 0 ? "ok" : "failed", text: `exited ${item.exit_code}` };
}

function terminalPill(item) {
  const status = terminalStatus(item);
  return `<span class="term-pill ${status.tone}">${escapeHtml(status.text)}</span>`;
}

function terminalSession(item) {
  return `${sheetNo(item.session)}${item.session_title ? ` · ${item.session_title}` : ""}`;
}

/* Who started it, in a word or two: "Run 2", "You". */
function terminalBy(item) {
  const origin = item.origin || {};
  if (origin.by === "person") return origin.again_of ? "You, again" : "You";
  if (origin.by === "run" && origin.run) return `Run ${origin.run}`;
  return "A run";
}

function terminalClock(timestamp, seconds) {
  const when = new Date(Number(timestamp || 0) * 1000);
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined });
}

/* How long it has run, or ran: "45s", "12m", "2h 05m". */
function terminalSpan(item) {
  const end = item.ended_at && !item.alive ? item.ended_at : Date.now() / 1000;
  const seconds = Math.max(0, Math.round(end - Number(item.started_at || 0)));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/* "localhost:8080", from the address the command printed. */
function terminalLinkLabel(link) {
  return String(link).replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function renderTerminals() {
  el("terminals-count").textContent = terminals.list.length
    ? `${terminals.running} running · ${terminals.list.length} in all`
    : "";
  setHtml(
    el("terminals-list"),
    terminals.list.length
      ? terminals.list
          .map(
            (item) =>
              `<button type="button" class="term-row${item.id === terminals.selected ? " on" : ""}" data-term="${escapeHtml(item.id)}">` +
              `<span class="term-row-top"><span class="term-command">${escapeHtml(elideMiddle(item.command, 60))}</span>${terminalPill(item)}</span>` +
              `<span class="term-row-meta"><span class="term-sheet">${escapeHtml(sheetNo(item.session))}</span>` +
              `<span>${escapeHtml(terminalBy(item))}</span><span>${escapeHtml(terminalClock(item.started_at))}</span>` +
              `<span>${escapeHtml(item.status === "running" || item.alive ? `up ${terminalSpan(item)}` : `ran ${terminalSpan(item)}`)}</span></span>` +
              `</button>`
          )
          .join("")
      : `<div class="empty-box"><p>Nothing is running in the background. Start something above, or ask a run to start a server or a watcher: it shows up here with its output.</p></div>`
  );
  renderTerminalView();
}

function renderTerminalView() {
  const item = terminals.list.find((entry) => entry.id === terminals.selected);
  const head = el("terminals-head");
  const facts = el("terminals-facts");
  const output = el("terminals-output");
  if (!item) {
    setHtml(head, "");
    facts.hidden = true;
    output.hidden = true;
    el("terminals-follow").hidden = true;
    return;
  }
  output.hidden = false;
  const live = item.status === "running" || item.alive;
  const opens = (item.links || [])
    .map(
      (link) =>
        `<a class="ghost-button small term-open" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer" title="Open ${escapeHtml(link)} in a new tab">Open ${escapeHtml(terminalLinkLabel(link))} ↗</a>`
    )
    .join("");
  const actions =
    (live ? opens : "") +
    (live
      ? `<button type="button" class="ghost-button small danger" data-term-action="stop">Stop</button>`
      : `<button type="button" class="ghost-button small" data-term-action="again">Run again</button>` +
        `<button type="button" class="ghost-button small" data-term-action="clear">Clear</button>`);
  setHtml(
    head,
    `<div class="term-head-text"><code class="term-full">${escapeHtml(item.command)}</code>` +
      `<span class="term-head-meta">${terminalPill(item)}</span></div>` +
      `<div class="term-actions">${actions}</div>`
  );
  facts.hidden = false;
  setHtml(facts, terminalFacts(item, live));
  const text =
    (terminals.dropped ? `… ${terminals.dropped} earlier lines are no longer kept\n` : "") +
    terminals.lines.join("\n") +
    (terminals.partial ? `${terminals.lines.length ? "\n" : ""}${terminals.partial}` : "");
  const shown =
    text ||
    (live
      ? "It is running and has not printed anything yet. A server often prints only when something connects to it."
      : "It printed nothing.");
  output.classList.toggle("quiet", !text);
  if (output.__shown !== shown) {
    output.__shown = shown;
    output.textContent = shown;
    if (terminals.follow) output.scrollTop = output.scrollHeight;
  }
  el("terminals-follow").hidden = terminals.follow;
}

function terminalFact(label, value, sub) {
  return `<div class="term-fact"><dt>${escapeHtml(label)}</dt><dd>${value}${sub ? `<span class="term-fact-sub">${sub}</span>` : ""}</dd></div>`;
}

function terminalFacts(item, live) {
  const origin = item.origin || {};
  const request = origin.by === "run" && origin.request ? escapeHtml(elideMiddle(String(origin.request), 90)) : "";
  const by =
    origin.by === "person"
      ? terminalFact("Started by", "You", origin.again_of ? "run again from this panel" : "from this panel")
      : terminalFact("Started by", escapeHtml(terminalBy(item)), request ? `“${request}”` : "");
  return (
    terminalFact("Started", escapeHtml(terminalClock(item.started_at, true)), escapeHtml(formatWhen(item.started_at))) +
    terminalFact(live ? "Running for" : "Ran for", escapeHtml(terminalSpan(item)), live ? "" : escapeHtml(item.ended_at ? `until ${terminalClock(item.ended_at, true)}` : "")) +
    by +
    terminalFact(
      "Session",
      `<button type="button" class="term-goto" data-term-goto="${escapeHtml(item.session)}" title="Go to this session">${escapeHtml(sheetNo(item.session))} →</button>`,
      escapeHtml(elideMiddle(item.session_title || "", 48))
    ) +
    terminalFact("Folder", escapeHtml(item.cwd && item.cwd !== "." ? item.cwd : "the workspace"), "") +
    terminalFact("Process", item.pid ? `pid ${escapeHtml(item.pid)}` : "—", escapeHtml(`${formatCount(item.lines)} ${item.lines === 1 ? "line" : "lines"} of output`))
  );
}

/* ---------- starting something (DR-064) ---------- */

function renderTermStart() {
  const mode = TERM_MODES[terminals.mode];
  const input = el("term-start-input");
  input.placeholder = mode.placeholder;
  el("term-start-go").textContent = mode.go;
  document.querySelectorAll("[data-term-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.termMode === terminals.mode));
  });
  const open = (state && state.open) || [];
  const chosen = open.some((entry) => entry.session_id === terminals.startSession) ? terminals.startSession : focusId();
  setHtml(
    el("term-start-session"),
    open.length
      ? open
          .map(
            (entry) =>
              `<option value="${escapeHtml(entry.session_id)}"${entry.session_id === chosen ? " selected" : ""}>` +
              `${escapeHtml(sheetNo(entry.session_id))} · ${escapeHtml(elideMiddle(entry.title || "New session", 34))}</option>`
          )
          .join("")
      : `<option value="">No session is open</option>`
  );
  const asked = terminals.asked;
  setHtml(
    el("term-start-help"),
    asked
      ? `${escapeHtml(asked.queued ? `Queued in ${sheetNo(asked.session)}: it starts when the run there ends.` : `Asked ${sheetNo(asked.session)} to start it.`)} ` +
          `<button type="button" class="term-goto" data-term-goto="${escapeHtml(asked.session)}">Follow it in the session →</button>`
      : escapeHtml(mode.help)
  );
}

async function submitTermStart(event) {
  event.preventDefault();
  const input = el("term-start-input");
  const text = input.value.trim();
  if (!text) {
    input.focus();
    return;
  }
  const session = el("term-start-session").value || "";
  const go = el("term-start-go");
  go.disabled = true;
  const body =
    terminals.mode === "ask" ? { action: "ask", request: text, session } : { action: "start", command: text, session };
  const payload = await post("/api/processes", body);
  go.disabled = false;
  if (!payload) return;
  input.value = "";
  if (payload.asked) {
    terminals.asked = { session: payload.asked.session || session || focusId(), queued: Boolean(payload.asked.queued) };
  } else {
    terminals.asked = null;
  }
  adoptTerminalList(payload);
  if (payload.started) {
    selectTerminal(payload.started);
    await readTerminalOutput();
  }
  renderTermStart();
  renderTerminals();
}

/* To the session a command belongs to: the window closes on the way. */
function goToTerminalSession(id) {
  if (!id) return;
  overlays.terminals.hidden = true;
  if (!isFocused(id)) post("/api/session", { action: "focus", id });
}

/* ---------- the run's steps ---------- */

/* `run_command` just sent its command to the background: the step that
 * called it says so, and links to its terminal. Also runs on replay. */
function markBackgroundStep(run, data) {
  if (!run) return;
  const step = [...run.steps]
    .reverse()
    .find((item) => item.type === "tool" && item.tool === "run_command" && item.status === "running");
  if (step) step.background = String(data.id || "");
  // Stepless: in the steps the tool's own step already says it, with the link.
  pushActivity(run, {
    stepless: Boolean(step),
    kind: "run",
    icon: "▸",
    html:
      `Started in the background: ${monoSpan(elideMiddle(String(data.command || ""), 64), String(data.command || ""))} ` +
      terminalLink(String(data.id || "")),
  });
}

function terminalLink(id) {
  return `<button type="button" class="term-ref" data-open-terminal="${escapeHtml(id)}" title="Open this command's terminal">open its terminal</button>`;
}

/* Every `process_*` event: the list and the badge are fetched again. */
function noteProcessEvent(frame) {
  if (!String(frame.event || "").startsWith("process_")) return;
  refreshTerminals();
  if (frame.event === "process_exited" && !replaying) {
    const data = frame.data || {};
    if (data.status === "exited" && data.exit_code !== 0) {
      toast(`A background command ended with exit code ${data.exit_code}: ${elideMiddle(String(data.command || ""), 60)}`, "bad");
    }
  }
}

/* ---------- events ---------- */

el("open-terminals").addEventListener("click", () => openTerminals());
el("terminals-close").addEventListener("click", () => {
  overlays.terminals.hidden = true;
});

el("terminals-list").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-term]");
  if (!row || row.dataset.term === terminals.selected) return;
  selectTerminal(row.dataset.term);
  renderTerminals();
  await readTerminalOutput();
  renderTerminals();
});

el("terminals-head").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-term-action]");
  if (!button || !terminals.selected) return;
  button.disabled = true;
  const payload = await post("/api/processes", { action: button.dataset.termAction, id: terminals.selected });
  button.disabled = false;
  if (!payload) return;
  adoptTerminalList(payload);
  if (payload.started) {
    selectTerminal(payload.started);
    await readTerminalOutput();
    renderTerminals();
  }
});

el("term-start").addEventListener("submit", submitTermStart);

el("term-start").addEventListener("click", (event) => {
  const mode = event.target.closest("[data-term-mode]");
  if (!mode || mode.dataset.termMode === terminals.mode) return;
  terminals.mode = mode.dataset.termMode;
  terminals.asked = null;
  renderTermStart();
  el("term-start-input").focus();
});

el("term-start-session").addEventListener("change", (event) => {
  terminals.startSession = event.target.value;
});

el("terminals-overlay").addEventListener("click", (event) => {
  const goto = event.target.closest("[data-term-goto]");
  if (goto) goToTerminalSession(goto.dataset.termGoto);
});

el("terminals-output").addEventListener("scroll", () => {
  const output = el("terminals-output");
  const atEnd = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
  if (atEnd !== terminals.follow) {
    terminals.follow = atEnd;
    el("terminals-follow").hidden = atEnd;
  }
});

el("terminals-follow").addEventListener("click", () => {
  terminals.follow = true;
  const output = el("terminals-output");
  output.scrollTop = output.scrollHeight;
  el("terminals-follow").hidden = true;
});

/* A terminal link anywhere on the page opens it here. */
document.addEventListener(
  "click",
  (event) => {
    const link = event.target.closest("[data-open-terminal]");
    if (!link) return;
    event.preventDefault();
    event.stopPropagation();
    closeOverlays();
    openTerminals(link.dataset.openTerminal);
  },
  true
);

refreshTerminals();
