/* Terminals: the commands runs started in the background (DR-062).
 *
 * - A rail section with a badge counting what is still running.
 * - A window listing every open session's background commands, newest first,
 *   and the selected one's output in a dark code box that follows the end
 *   until the person scrolls up to read.
 * - Stop for a running command, Clear for a finished one.
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
              `<span class="term-row-meta">${escapeHtml(terminalSession(item))} · started ${escapeHtml(formatWhen(item.started_at))}</span>` +
              `</button>`
          )
          .join("")
      : `<div class="empty-box"><p>Nothing is running in the background. When a run starts a server or a watcher, it shows up here with its output.</p></div>`
  );
  renderTerminalView();
}

function renderTerminalView() {
  const item = terminals.list.find((entry) => entry.id === terminals.selected);
  const head = el("terminals-head");
  const output = el("terminals-output");
  if (!item) {
    setHtml(head, "");
    output.hidden = true;
    el("terminals-follow").hidden = true;
    return;
  }
  output.hidden = false;
  const actions =
    item.status === "running" || item.alive
      ? `<button type="button" class="ghost-button small danger" data-term-action="stop">Stop</button>`
      : `<button type="button" class="ghost-button small" data-term-action="clear">Clear</button>`;
  setHtml(
    head,
    `<div class="term-head-text"><code class="term-full">${escapeHtml(item.command)}</code>` +
      `<span class="term-head-meta">${terminalPill(item)} <span>${escapeHtml(terminalSession(item))}</span>` +
      ` · <span>started ${escapeHtml(formatWhen(item.started_at))}</span>` +
      (item.cwd && item.cwd !== "." ? ` · <span>in ${escapeHtml(item.cwd)}</span>` : "") +
      (item.pid ? ` · <span>pid\u00a0${escapeHtml(item.pid)}</span>` : "") +
      `</span></div><div class="term-actions">${actions}</div>`
  );
  const text =
    (terminals.dropped ? `… ${terminals.dropped} earlier lines are no longer kept\n` : "") +
    terminals.lines.join("\n") +
    (terminals.partial ? `${terminals.lines.length ? "\n" : ""}${terminals.partial}` : "");
  const shown = text || (item.status === "running" ? "Waiting for output…" : "(no output)");
  if (output.__shown !== shown) {
    output.__shown = shown;
    output.textContent = shown;
    if (terminals.follow) output.scrollTop = output.scrollHeight;
  }
  el("terminals-follow").hidden = terminals.follow;
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
  if (payload) adoptTerminalList(payload);
  button.disabled = false;
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
