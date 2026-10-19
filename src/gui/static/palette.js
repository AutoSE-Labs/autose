/* The command menu: Ctrl K (⌘ K on a Mac) reaches anything the page can do.
 *
 * - A search box over the dimmed page. Typing narrows every command with
 *   forgiving matching: letters anywhere in a name, the first letters of its
 *   words ("cw" finds Context window), or its letters in order with a few
 *   gaps ("ctxwin" finds it too). A session answers to its number.
 * - Results sit under small headings, each with its context on the right:
 *   the session it belongs to, what is in use now, what it asks first, or
 *   why it cannot run yet.
 * - Up and Down move, Enter runs, Esc closes; the mouse works too. Focus goes
 *   back to where it was.
 * - The commands used last come first next time, remembered in this browser.
 *
 * Every command calls what the page's own controls call (decide, openDiff,
 * setYolo, …) or presses the control itself, so the menu has no behaviour of
 * its own: what a button asks to confirm is still asked, and what a button
 * refuses while a run works is refused here too. Loaded after app.js,
 * diff.js and files.js; it reads their state only when the menu opens.
 */

const PALETTE_RECENT_KEY = "autose.palette.recent";
const PALETTE_RECENT_MAX = 5;
/* Closed sessions, models and files can run to hundreds: a few are shown
 * before anything is typed, the rest are found by typing. */
const PALETTE_REOPEN_BROWSE = 3;
const PALETTE_FILES_MAX = 8;
const PALETTE_GROUP_MAX = 10;
/* The longest call the menu approves: shown whole, on a row that wraps. */
const PALETTE_ASK_WHOLE = 160;
const PALETTE_GROUPS = ["Waiting for you", "This run", "Sessions", "Go to", "Settings", "Files"];

const PALETTE_ON_MAC = /Mac|iPhone|iPad/.test(
  (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ""
);
const PALETTE_KEYS = PALETTE_ON_MAC ? "⌘ K" : "Ctrl K";

const paletteView = {
  open: false,
  returnTo: null,
  commands: [],
  shown: [], // the commands on screen, in list order
  active: -1,
  recent: paletteStoredRecent(),
};

/* ---------- icons ---------- */

const paletteSvg = (body) =>
  `<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

/* Only the shapes no control on the page already has; the rest are borrowed
 * from the button that does the same thing, so a command looks like it. */
const PALETTE_ICONS = {
  run: paletteSvg('<path d="M7 4.5l8 5.5-8 5.5z"/>'),
  deny: paletteSvg('<circle cx="10" cy="10" r="6.5"/><path d="M5.4 14.6l9.2-9.2"/>'),
  stop: paletteSvg('<rect x="5" y="5" width="10" height="10" rx="1.5"/>'),
  undo: paletteSvg('<path d="M7.5 5 4 8.5 7.5 12"/><path d="M4 8.5h8a4 4 0 0 1 0 8H9.5"/>'),
  reopen: paletteSvg('<path d="M3.8 10a6.2 6.2 0 1 0 1.9-4.5"/><path d="M3.5 3.5v3.2h3.2"/><path d="M10 6.8V10l2.4 1.5"/>'),
  plus: paletteSvg('<path d="M10 4v12M4 10h12"/>'),
  file: paletteSvg('<path d="M5.5 2.5h6l3 3v12h-9z"/><path d="M11.5 2.5v3h3"/>'),
  plan: paletteSvg('<path d="M7.5 5.5h8M7.5 10h8M7.5 14.5h8"/><path d="M3.8 5.5h.4M3.8 10h.4M3.8 14.5h.4"/>'),
  window: paletteSvg('<path d="M3 13.5h14"/><path d="M5 13.5V11M8 13.5V9.5M11 13.5V11M14 13.5V9.5"/>'),
  go: paletteSvg('<path d="M4.5 10h11M11 5.5l4.5 4.5-4.5 4.5"/>'),
};

const paletteIconCache = new Map();

function paletteIcon(controlId) {
  if (!paletteIconCache.has(controlId)) {
    const svg = el(controlId) && el(controlId).querySelector("svg");
    let markup = PALETTE_ICONS.go;
    if (svg) {
      const copy = svg.cloneNode(true);
      copy.setAttribute("width", "16");
      copy.setAttribute("height", "16");
      markup = copy.outerHTML;
    }
    paletteIconCache.set(controlId, markup);
  }
  return paletteIconCache.get(controlId);
}

/* ---------- the commands ---------- */

/* Every command the page offers right now. A command is a label, the group
 * it is listed under, its context (`hint`), extra words it answers to, and
 * what it runs. `keep` leaves open windows open (an answer, a setting);
 * otherwise they close first so what the command opens is not underneath.
 * `unavailable` says why it cannot run yet, and `remember: false` keeps
 * one-off answers out of the recent list. */
function paletteCommands() {
  const commands = [];
  const add = (group, command) =>
    commands.push({
      group,
      hint: "",
      keywords: "",
      icon: PALETTE_ICONS.go,
      keep: false,
      remember: true,
      unavailable: "",
      // `deliberate`: it answers something that waits (a call, a plan, a
      // held queue) or stops a run, so it is never the row Enter runs before
      // anything is typed.
      deliberate: false,
      wrap: false,
      ...command,
    });
  if (state) {
    paletteWaiting(add);
    paletteThisRun(add);
    paletteSessions(add);
    paletteSettings(add);
  }
  paletteGoTo(add);
  return commands;
}

/* What waits for the person, from every session: the answers the question's
 * own card offers, the plan, and queues held after a stop. */
function paletteWaiting(add) {
  const group = "Waiting for you";
  approvals.forEach((request) => {
    const subject = paletteSubject(request);
    // A call is approved here only when every character of it is on screen:
    // one line, short enough to be shown in full on a row that wraps. A
    // longer one, or one on several lines (each a command of its own), is
    // opened where it waits instead.
    const whole = !/[\r\n]/.test(subject) && subject.length <= PALETTE_ASK_WHOLE;
    const what = whole ? subject : elideMiddle(subject.replace(/\s+/g, " "), 64);
    const base = { hint: paletteWhere(request.session), keep: true, remember: false, deliberate: true };
    const tool = String(request.tool || "");
    if (whole) {
      add(group, {
        ...base,
        id: `ask:${request.id}:once`,
        icon: PALETTE_ICONS.run,
        label: `Run it: ${what}`,
        keywords: `approve yes once ${tool}`,
        wrap: true,
        run: () => decide(request.id, "once"),
      });
      if (request.allow) {
        add(group, {
          ...base,
          id: `ask:${request.id}:session`,
          icon: paletteIcon("open-allowed"),
          label: `Always allow ${request.allow.label} here`,
          keywords: `approve yes session ${tool}`,
          wrap: true,
          run: () => decide(request.id, "session"),
        });
      }
    } else {
      add(group, {
        ...base,
        id: `ask:${request.id}:look`,
        icon: PALETTE_ICONS.go,
        label: `Read it in full: ${what}`,
        keywords: `approve yes question waiting ${tool}`,
        keep: false,
        deliberate: false,
        run: () => paletteShowAsk(request),
      });
    }
    add(group, {
      ...base,
      id: `ask:${request.id}:deny`,
      icon: PALETTE_ICONS.deny,
      label: `Don't run: ${what}`,
      keywords: `refuse deny no ${tool}`,
      run: () => decide(request.id, "deny"),
    });
  });
  // DR-045: the plan on screen can be approved from here; one in another
  // session is opened first, since it should be read before it is approved.
  const review = state.pending_plan;
  if (review) {
    add(group, {
      id: `plan:${review.id}`,
      icon: PALETTE_ICONS.plan,
      label: "Approve the plan and start",
      hint: "nothing has changed yet",
      keywords: "plan review approve yes start",
      keep: true,
      remember: false,
      deliberate: true,
      run: () => post("/api/plan", { id: review.id, action: "approve" }),
    });
  }
  (state.open || []).forEach((entry) => {
    const id = entry.session_id;
    // A question asked before this page loaded, in a session it has not
    // shown, is known only by its count: the session is opened to answer it.
    const known = approvals.filter((request) => request.session === id).length;
    if ((entry.waiting || 0) > known) {
      add(group, {
        id: `asks-in:${id}`,
        icon: PALETTE_ICONS.run,
        label: `Answer what waits in ${entry.title}`,
        hint: sheetNo(id),
        keywords: "approve question waiting run",
        remember: false,
        run: () => paletteFocus(id),
      });
    }
    if (entry.plan_waiting && !(review && isFocused(id))) {
      add(group, {
        id: `plan-in:${id}`,
        icon: PALETTE_ICONS.plan,
        label: `Review the plan in ${entry.title}`,
        hint: sheetNo(id),
        keywords: "plan review approve",
        remember: false,
        run: () => paletteFocus(id),
      });
    }
    if (entry.queue_held && entry.queued) {
      add(group, {
        id: `resume:${id}`,
        icon: PALETTE_ICONS.run,
        label: `Resume ${plural(entry.queued, "waiting request")}`,
        hint: paletteWhere(id),
        keywords: "queue held resume continue",
        keep: true,
        remember: false,
        deliberate: true,
        run: () => post("/api/queue", { action: "resume", session: id }),
      });
    }
  });
}

/* The session on screen and its latest run. */
function paletteThisRun(add) {
  const group = "This run";
  const running = Boolean(state.running);
  const changed = new Set();
  (state.history || []).forEach((turn) => (turn.changes || []).forEach((change) => changed.add(change.path)));
  if (running) {
    const stopping = Boolean(state.stopping || stopRequested);
    add(group, {
      id: "run:stop",
      icon: PALETTE_ICONS.stop,
      label: "Stop the current run",
      hint: "keeps what it produced",
      keywords: "cancel interrupt halt end",
      keep: true,
      deliberate: true,
      unavailable: stopping ? "stopping already" : "",
      run: requestStop,
    });
  }
  add(group, {
    id: "run:diff",
    icon: paletteIcon("open-diff"),
    label: "Open the diff",
    hint: changed.size ? `${plural(changed.size, "file")} changed` : "",
    keywords: "changes diff files written review",
    unavailable: changed.size ? "" : "nothing changed yet",
    run: () => openDiff(),
  });
  // DR-024: undo puts back the most recent run that wrote, and asks first.
  add(group, {
    id: "run:undo",
    icon: PALETTE_ICONS.undo,
    label: "Undo the last run's changes",
    hint: "asks first",
    keywords: "revert restore put back",
    unavailable: !changed.size ? "nothing to undo" : running ? "after this run" : "",
    run: undoChanges,
  });
  const clear = el("clear-history");
  add(group, {
    id: "run:clear",
    icon: paletteIcon("clear-history"),
    label: "Clear this session's history",
    hint: "asks first · files stay",
    keywords: "forget reset history",
    unavailable: clear.disabled ? (running ? "after this run" : "nothing to clear") : "",
    run: () => clear.click(),
  });
}

/* Open sessions to switch to, closed ones to reopen, a new one, a name. */
function paletteSessions(add) {
  const group = "Sessions";
  const open = state.open || [];
  open.forEach((entry) => {
    const id = entry.session_id;
    if (isFocused(id)) return;
    add(group, {
      id: `session:${id}`,
      icon: tierIconMarkup(entry.tier, 16) || paletteIcon("show-sessions"),
      label: `Switch to ${entry.title}`,
      hint: `${sheetNo(id)} · ${paletteSessionState(entry)}`,
      keywords: `${paletteNumberWords(id)} open session ${entry.tier || ""}`,
      run: () => paletteFocus(id),
    });
  });
  const openIds = new Set(open.map((entry) => entry.session_id));
  stored
    .filter((record) => !openIds.has(record.id))
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
    .forEach((record, index) =>
      add(group, {
        id: `reopen:${record.id}`,
        icon: PALETTE_ICONS.reopen,
        label: `Reopen ${record.title || "(no title)"}`,
        hint: `${sheetNo(record.id)} · ${formatWhen(record.updated_at)}`,
        keywords: `${paletteNumberWords(record.id)} closed stored session ${record.tier || ""}`,
        browse: index < PALETTE_REOPEN_BROWSE,
        run: () => post("/api/session", { action: "resume", id: record.id }),
      })
    );
  const full = open.length >= (state.max_open || 6);
  add(group, {
    id: "session:new",
    icon: PALETTE_ICONS.plus,
    label: "New session",
    hint: "anything running keeps going",
    keywords: "open another start fresh",
    unavailable: dom.newSession.disabled ? (full ? "close a session first" : "no model yet") : "",
    run: () => dom.newSession.click(),
  });
  const entry = openEntry(focusId());
  if (entry) {
    add(group, {
      id: "session:rename",
      icon: paletteIcon("sb-name"),
      label: "Rename this session",
      hint: entry.title,
      keywords: "name title",
      run: startBarRename,
    });
  }
}

/* The page's own windows and panels. */
function paletteGoTo(add) {
  const group = "Go to";
  const go = (id, label, hint, keywords, icon, run) => add(group, { id: `go:${id}`, label, hint, keywords, icon, run });
  go("files", "Files", "every file in the workspace", "browse read open tree", paletteIcon("open-files"), () => openFilesView());
  go("changes", "Changes", "details panel", "files changed this session undo", paletteIcon("open-diff"), () => paletteDetails("changes"));
  go("steps", "Steps", "details panel", "activity tools calls timeline", paletteIcon("toggle-inspector"), () => paletteDetails("activity"));
  go("checks", "Checks", "details panel", "requirements verdict validate", paletteIcon("toggle-inspector"), () => paletteDetails("requirements"));
  go("usage", "Usage", "details panel", "tokens time spent", paletteIcon("toggle-inspector"), () => paletteDetails("usage"));
  go("allowed", "Allowed", "what runs without asking here", "allowances rules permissions", paletteIcon("open-allowed"), openAllowed);
  go("schedules", "Schedules", "and watchers", "automation timers watchers", paletteIcon("open-automation"), openAutomation);
  go("machine", "Machine check", "model server, GPU and config", "doctor runtime ollama gpu", paletteIcon("open-doctor"), openDoctor);
  go("settings", "Settings", "providers, models and keys", "preferences api keys providers", paletteIcon("open-settings"), openSettings);
  go("context", "Context window", el("ctx-label").textContent, "ctx tokens size window", PALETTE_ICONS.window, openContext);
  go("tips", "Tips", "", "help hints advice", paletteIcon("open-tips"), openTips);
  const waiting = Number(el("inbox-count").textContent) || 0;
  go("inbox", "Needs you", waiting ? `${waiting} waiting` : "nothing waiting", "inbox waiting questions approvals", paletteIcon("show-inbox"), () => setRailView("inbox"));
  go("sessions", "Sessions list", "", "rail open closed", paletteIcon("show-sessions"), () => setRailView("sessions"));
}

/* Tier, model, theme and the switches in the header and the composer. */
function paletteSettings(add) {
  const group = "Settings";
  const chosen = state.auto_classify ? "auto" : state.tier;
  const locked = !state.ready || state.running;
  const lockedWhy = state.ready ? "after this run" : "no model yet";
  [
    { name: "auto", label: "Use Auto", hint: "picks a tier for each request" },
    ...(state.tiers || []).map((tier) => ({
      name: tier.name,
      label: `Use the ${humanize(tier.name)} tier`,
      hint: TIER_SHORT[tier.name] || tier.summary || "",
    })),
  ].forEach((item) =>
    add(group, {
      id: `tier:${item.name}`,
      icon: tierIconMarkup(item.name, 16),
      label: item.label,
      hint: item.name === chosen ? "in use" : item.hint,
      keywords: `tier ${item.name}`,
      keep: true,
      unavailable: locked && item.name !== chosen ? lockedWhy : "",
      run: () => paletteTier(item.name),
    })
  );
  // DR-036: the header picker's own options, grouped by provider.
  Array.from(dom.model.options).forEach((option) => {
    if (!option.value) return;
    const provider = option.parentElement && option.parentElement.tagName === "OPTGROUP" ? option.parentElement.label : "";
    const current = option.value === dom.model.value;
    add(group, {
      id: `model:${option.value}`,
      icon: paletteIcon("status-model"),
      label: `Use ${option.textContent}`,
      hint: current ? "in use" : provider || "model",
      keywords: `model ${provider}`,
      keep: true,
      browse: current || dom.model.options.length <= 6,
      unavailable: !current && dom.model.disabled ? lockedWhy : "",
      run: () => paletteModel(option.value),
    });
  });
  const dark = document.documentElement.dataset.theme === "dark";
  add(group, {
    id: "theme",
    icon: paletteIcon("toggle-theme"),
    label: dark ? "Switch to the light theme" : "Switch to the dark theme",
    hint: dark ? "dark now" : "light now",
    keywords: "theme dark light night colours appearance",
    keep: true,
    run: () => el("toggle-theme").click(),
  });
  if (notifySupported()) {
    const on = notifyActive();
    add(group, {
      id: "notify",
      icon: paletteIcon("toggle-notify"),
      label: on ? "Turn notifications off" : "Turn notifications on",
      hint: on ? "on now" : "off now",
      keywords: "notifications desktop alerts bell",
      keep: true,
      run: () => el("toggle-notify").click(),
    });
  }
  const reviewing = Boolean(state.review_plans);
  add(group, {
    id: "plan-review",
    icon: PALETTE_ICONS.plan,
    label: reviewing ? "Turn plan review off" : "Turn plan review on",
    hint: reviewing ? "on now" : "Craft and Forge stop after planning",
    keywords: "review the plan approve before",
    keep: true,
    run: () => el("review-plans").click(),
  });
  // DR-053: turning it on still goes through setYolo's confirmation.
  const yolo = Boolean(state.yolo);
  add(group, {
    id: "yolo",
    icon: paletteIcon("toggle-yolo"),
    label: yolo ? "Turn YOLO mode off" : "Turn YOLO mode on",
    hint: yolo ? "nothing asks now" : "asks first",
    keywords: "yolo approve everything without asking",
    keep: true,
    run: () => setYolo(!yolo),
  });
  const details = dom.shell.classList.contains("inspector-open");
  add(group, {
    id: "details",
    icon: paletteIcon("toggle-inspector"),
    label: details ? "Hide the details panel" : "Show the details panel",
    keywords: "inspector panel details",
    keep: true,
    run: () => el("toggle-inspector").click(),
  });
}

/* A workspace file, opened in the Files view (DR-051). */
function paletteFileCommand(path) {
  const slash = path.lastIndexOf("/");
  return {
    group: "Files",
    id: `file:${path}`,
    icon: PALETTE_ICONS.file,
    label: path.slice(slash + 1),
    hint: slash > 0 ? path.slice(0, slash) : "",
    keywords: path,
    keep: false,
    remember: true,
    unavailable: "",
    run: () => openFilesView(path),
  };
}

/* ---------- what the commands call ---------- */

function paletteSubject(request) {
  const args = request.arguments || {};
  const keys = Object.keys(args);
  const shown = keys.length === 1 && typeof args[keys[0]] === "string" ? args[keys[0]] : request.summary;
  // Its line breaks are kept: in a command each one starts another command.
  return String(shown || humanize(String(request.tool || "a call"))).trim();
}

/* A call too long to approve from a row, shown where it waits: its card in
 * the session on screen, or the "Needs you" list, which shows every waiting
 * call in full. Looking answers nothing. */
function paletteShowAsk(request) {
  const card = isFocused(request.session) && approvalCards.get(request.id);
  if (card && card.isConnected) card.scrollIntoView({ block: "center" });
  else setRailView("inbox");
}

function paletteWhere(sessionId) {
  return isFocused(sessionId) ? "this session" : sheetNo(sessionId);
}

function paletteSessionState(entry) {
  if (sheetWaits(entry)) return "waiting for you";
  if (entry.running) return "working";
  return plural(entry.turns || 0, "run");
}

/* A session answers to its number with or without the dash, and its id. */
function paletteNumberWords(id) {
  const number = sheetNo(id);
  return `${number} ${number.replace("-", "")} ${id}`;
}

function paletteFocus(id) {
  if (!isFocused(id)) post("/api/session", { action: "focus", id });
}

function paletteDetails(tab) {
  inspectorTab = tab;
  setInspectorOpen(true);
}

function paletteTier(name) {
  const button = dom.tier.querySelector(`[data-tier="${CSS.escape(name)}"]`);
  if (button) button.click();
}

function paletteModel(value) {
  if (dom.model.value === value) return;
  dom.model.value = value;
  dom.model.dispatchEvent(new Event("change"));
}

/* ---------- matching ---------- */

/* How well `query` (lower case, one word) fits `text`: null when it does not,
 * else a score and the positions in `text` it matched, to be marked. Only a
 * name is matched `loose`ly, by initials and scattered letters: a command's
 * other words are a bag, and their initials spell nothing anyone types. */
function paletteMatch(query, text, loose = true) {
  const lower = String(text).toLowerCase();
  const at = lower.indexOf(query);
  if (at >= 0) {
    const wordStart = at === 0 || !/[a-z0-9]/.test(lower[at - 1]);
    return { score: at === 0 ? 100 : wordStart ? 85 : 60, marks: paletteRange(at, query.length) };
  }
  if (!loose || query.length < 2) return null;
  // The first letters of the words: "cw" for Context window.
  const words = [...lower.matchAll(/[a-z0-9]+/g)];
  const initials = words.map((word) => word[0][0]).join("");
  const from = initials.indexOf(query);
  if (from >= 0) {
    return { score: 75 - from, marks: words.slice(from, from + query.length).map((word) => word.index) };
  }
  // Its letters in order: forgiving of a missed letter or a dash, not of a
  // scattering across the whole line.
  const marks = [];
  let next = 0;
  for (const char of query) {
    const found = lower.indexOf(char, next);
    if (found < 0) return null;
    marks.push(found);
    next = found + 1;
  }
  const gaps = marks[marks.length - 1] - marks[0] + 1 - query.length;
  if (gaps > query.length * 3) return null;
  return { score: Math.max(10, 50 - gaps * 3), marks };
}

function paletteRange(start, length) {
  return Array.from({ length }, (_, offset) => start + offset);
}

/* Every word of the query has to fit the label or the command's other words;
 * a fit in the label counts for more, and only it is marked. */
function paletteFit(tokens, whole, label, words) {
  let score = 0;
  const marks = new Set();
  for (const token of tokens) {
    const inLabel = paletteMatch(token, label);
    const inWords = paletteMatch(token, words, false);
    const labelScore = inLabel ? inLabel.score : 0;
    const wordsScore = inWords ? inWords.score * 0.7 : 0;
    if (!labelScore && !wordsScore) return null;
    if (labelScore >= wordsScore) inLabel.marks.forEach((index) => marks.add(index));
    score += Math.max(labelScore, wordsScore);
  }
  if (tokens.length > 1 && label.toLowerCase().includes(whole)) score += 40;
  // Of two equal fits, the shorter name is the closer one.
  return { score: score - label.length / 100, marks };
}

function paletteSearch(query) {
  const tokens = query.split(/\s+/).filter(Boolean);
  const scored = [];
  paletteView.commands.forEach((command) => {
    const fit = paletteFit(tokens, query, command.label, `${command.keywords} ${command.group}`);
    if (fit) scored.push({ command, ...fit });
  });
  if (typeof filesView !== "undefined") {
    const files = [];
    filesView.list.forEach((item) => {
      const name = item.path.slice(item.path.lastIndexOf("/") + 1);
      const fit = paletteFit(tokens, query, name, item.path);
      if (fit) files.push({ path: item.path, ...fit });
    });
    files
      .sort((a, b) => b.score - a.score)
      .slice(0, PALETTE_FILES_MAX)
      .forEach((file) => scored.push({ command: paletteFileCommand(file.path), score: file.score, marks: file.marks }));
  }
  scored.forEach((item) => {
    const recent = paletteView.recent.indexOf(item.command.id);
    if (recent >= 0) item.score += 12 - recent * 2;
    if (item.command.unavailable) item.score -= 20;
  });
  scored.sort((a, b) => b.score - a.score);
  // Grouped, with the group holding the best fit first.
  const sections = new Map();
  scored.forEach((item) => {
    const group = item.command.group;
    if (!sections.has(group)) sections.set(group, []);
    if (sections.get(group).length < PALETTE_GROUP_MAX) sections.get(group).push(item);
  });
  return Array.from(sections, ([title, items]) => ({ title, items }));
}

/* Before anything is typed: what waits first, then what was used last, then
 * every group in order. */
function paletteBrowse() {
  const byId = new Map(paletteView.commands.map((command) => [command.id, command]));
  const recent = paletteView.recent
    .map((id) => byId.get(id) || paletteRecentFile(id))
    .filter(Boolean);
  const taken = new Set(recent.map((command) => command.id));
  const section = (title) => ({
    title,
    items: paletteView.commands
      .filter((command) => command.group === title && command.browse !== false && !taken.has(command.id))
      .map((command) => ({ command })),
  });
  const sections = [section("Waiting for you")];
  if (recent.length) sections.push({ title: "Recent", items: recent.map((command) => ({ command })) });
  PALETTE_GROUPS.slice(1).forEach((title) => sections.push(section(title)));
  return sections.filter((item) => item.items.length);
}

function paletteRecentFile(id) {
  if (!id.startsWith("file:") || typeof filesView === "undefined") return null;
  const path = id.slice(5);
  return filesView.index.has(path) ? paletteFileCommand(path) : null;
}

/* ---------- recently used ---------- */

function paletteStoredRecent() {
  try {
    const saved = JSON.parse(localStorage.getItem(PALETTE_RECENT_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((id) => typeof id === "string").slice(0, PALETTE_RECENT_MAX) : [];
  } catch (error) {
    return [];
  }
}

function paletteRemember(command) {
  if (!command.remember) return;
  paletteView.recent = [command.id, ...paletteView.recent.filter((id) => id !== command.id)].slice(0, PALETTE_RECENT_MAX);
  try {
    localStorage.setItem(PALETTE_RECENT_KEY, JSON.stringify(paletteView.recent));
  } catch (error) {
    // Unavailable storage: remembered for this page only.
  }
}

/* ---------- drawing ---------- */

function renderPalette() {
  const input = el("palette-input");
  const query = input.value.trim().toLowerCase();
  const sections = query ? paletteSearch(query) : paletteBrowse();
  paletteView.shown = [];
  const markup = sections
    .map((section, number) => {
      const items = section.items
        .map((item) => paletteItemMarkup(item.command, paletteView.shown.push(item.command) - 1, item.marks))
        .join("");
      return (
        `<div class="palette-group" role="group" aria-labelledby="palette-group-${number}">` +
        `<div class="palette-head lbl" id="palette-group-${number}" role="presentation">${escapeHtml(section.title)}</div>` +
        `${items}</div>`
      );
    })
    .join("");
  el("palette-list").innerHTML =
    markup || `<p class="palette-empty">Nothing matches. Try fewer letters, a session's number or a file name.</p>`;
  // Enter before anything is typed answers nothing that waits and stops
  // nothing: a reflex Ctrl K, Enter must not run a command or end a run.
  // Typed, the best fit is chosen.
  const first = paletteView.shown.findIndex((command) => !command.unavailable && (query || !command.deliberate));
  setPaletteActive(first >= 0 ? first : 0);
  el("palette-list").scrollTop = 0;
}

function paletteItemMarkup(command, index, marks) {
  const off = Boolean(command.unavailable);
  return (
    `<div class="palette-item${off ? " off" : ""}${command.wrap ? " wrap" : ""}" role="option" id="palette-option-${index}" ` +
    `data-palette-index="${index}" aria-selected="false"${off ? ' aria-disabled="true"' : ""}>` +
    `<span class="palette-icon">${command.icon || ""}</span>` +
    `<span class="palette-label">${paletteMarked(command.label, marks)}</span>` +
    `<span class="palette-hint">${escapeHtml(command.unavailable || command.hint || "")}</span></div>`
  );
}

/* The label with the letters the query matched in bold. */
function paletteMarked(text, marks) {
  const marked = marks instanceof Set ? marks : new Set(marks || []);
  if (!marked.size) return escapeHtml(text);
  let out = "";
  let open = false;
  Array.from(text).forEach((char, index) => {
    if (marked.has(index) !== open) {
      out += open ? "</mark>" : "<mark>";
      open = !open;
    }
    out += escapeHtml(char);
  });
  return open ? `${out}</mark>` : out;
}

function setPaletteActive(index, scroll = true) {
  const list = el("palette-list");
  const count = paletteView.shown.length;
  const previous = list.querySelector(".palette-item.on");
  if (previous) {
    previous.classList.remove("on");
    previous.setAttribute("aria-selected", "false");
  }
  paletteView.active = count ? Math.max(0, Math.min(count - 1, index)) : -1;
  const node = count ? el(`palette-option-${paletteView.active}`) : null;
  // On the combobox, which holds focus and is what a screen reader follows,
  // and on the list it points into.
  [el("palette-input"), list].forEach((target) => {
    if (node) target.setAttribute("aria-activedescendant", node.id);
    else target.removeAttribute("aria-activedescendant");
  });
  if (!node) return;
  node.classList.add("on");
  node.setAttribute("aria-selected", "true");
  if (!scroll) return;
  // The first of a group brings its heading along.
  const head = node.previousElementSibling;
  if (head && head.classList.contains("palette-head")) head.scrollIntoView({ block: "nearest" });
  node.scrollIntoView({ block: "nearest" });
}

function movePalette(step) {
  const count = paletteView.shown.length;
  if (!count) return;
  const at = paletteView.active < 0 ? 0 : paletteView.active;
  // One step wraps around the ends; a page's worth stops at them.
  const next = Math.abs(step) === 1 ? (at + step + count) % count : Math.max(0, Math.min(count - 1, at + step));
  setPaletteActive(next);
}

/* ---------- opening, closing, running ---------- */

function openPalette() {
  if (paletteView.open) return;
  paletteView.returnTo = document.activeElement;
  paletteView.open = true;
  paletteView.commands = paletteCommands();
  const input = el("palette-input");
  input.value = "";
  renderPalette();
  overlays.palette.hidden = false;
  input.focus();
  // The file index is fetched at load and after each run; if it is empty, a
  // name typed in a moment can still find its file.
  if (typeof filesView !== "undefined" && !filesView.list.length) {
    refreshFileIndex().then(() => {
      if (paletteView.open && el("palette-input").value.trim()) renderPalette();
    });
  }
}

function closePalette() {
  if (!paletteView.open) return;
  paletteView.open = false;
  overlays.palette.hidden = true;
  paletteReturnFocus();
}

function paletteReturnFocus() {
  const back = paletteView.returnTo;
  paletteView.returnTo = null;
  if (back && back !== document.body && back.isConnected && typeof back.focus === "function") {
    back.focus({ preventScroll: true });
  }
}

function runPaletteCommand(index) {
  const command = paletteView.shown[index];
  if (!command) return;
  if (command.unavailable) {
    // Its row already says why; the row shakes its head instead of a toast.
    const node = el(`palette-option-${index}`);
    if (node) {
      node.classList.remove("nudge");
      window.requestAnimationFrame(() => node.classList.add("nudge"));
    }
    return;
  }
  paletteRemember(command);
  closePalette();
  if (!command.keep) closeOverlays();
  try {
    const result = command.run();
    if (result && typeof result.catch === "function") {
      result.catch((error) => toast(String(error.message || error), "bad"));
    }
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
}

/* ---------- wiring ---------- */

if (el("palette-input")) {
  el("palette-keycap").textContent = PALETTE_KEYS;
  el("open-palette").title = `Find a command: switch sessions, answer what waits, open files, change settings (${PALETTE_KEYS})`;
  el("open-palette").setAttribute("aria-keyshortcuts", PALETTE_ON_MAC ? "Meta+K" : "Control+K");
  el("open-palette").addEventListener("click", openPalette);

  // Captured, so a field that handles its own keys cannot swallow it.
  document.addEventListener(
    "keydown",
    (event) => {
      // ⌘ K on a Mac, where Control-K deletes to the end of the line in any
      // text field; Ctrl K elsewhere.
      const held = PALETTE_ON_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
      if (!held || event.altKey || event.shiftKey) return;
      const key = /^[a-z]$/i.test(event.key) ? event.key.toLowerCase() : event.code === "KeyK" ? "k" : "";
      if (key !== "k") return;
      event.preventDefault();
      event.stopPropagation();
      if (paletteView.open) closePalette();
      else openPalette();
    },
    true
  );

  el("palette-input").addEventListener("input", () => {
    paletteView.commands = paletteCommands();
    renderPalette();
  });

  // Handled here and stopped: with the diff open underneath, its own arrow
  // keys and app.js's Esc would otherwise act on it as well.
  el("palette-input").addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: 6, PageUp: -6 };
    if (event.key in moves) movePalette(moves[event.key]);
    else if (event.key === "Enter") runPaletteCommand(paletteView.active);
    else if (event.key === "Escape") closePalette();
    else if (event.key !== "Tab") return; // Tab stays in the box.
    event.preventDefault();
    event.stopPropagation();
  });

  const list = el("palette-list");
  // The pointer picks without taking focus from the box.
  el("palette-overlay").addEventListener("mousedown", (event) => {
    if (event.target !== el("palette-input") && event.target.closest(".palette-panel")) event.preventDefault();
  });
  list.addEventListener("mousemove", (event) => {
    const item = event.target.closest("[data-palette-index]");
    if (item && Number(item.dataset.paletteIndex) !== paletteView.active) {
      setPaletteActive(Number(item.dataset.paletteIndex), false);
    }
  });
  list.addEventListener("click", (event) => {
    const item = event.target.closest("[data-palette-index]");
    if (item) runPaletteCommand(Number(item.dataset.paletteIndex));
  });

  // Closed some other way (a click on the backdrop, closeOverlays): focus
  // still goes back to where it was.
  new MutationObserver(() => {
    if (overlays.palette.hidden && paletteView.open) {
      paletteView.open = false;
      paletteReturnFocus();
    }
  }).observe(overlays.palette, { attributes: true, attributeFilter: ["hidden"] });
}
