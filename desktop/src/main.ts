import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles.css";

type Mode = "auto" | "lite" | "standard";

type SessionEvent = {
  type: string;
  message: string;
  data: Record<string, unknown>;
  timestamp: number;
};

type SessionArtifact = {
  kind: string;
  title: string;
  path: string;
  content: string;
  metadata: Record<string, unknown>;
  timestamp: number;
};

type SessionPayload = {
  session_id: string;
  task: string;
  workspace_root: string;
  mode: Mode;
  created_at: number;
  events: SessionEvent[];
  artifacts: SessionArtifact[];
  result: {
    status: string;
    summary: string;
    changed_files: string[];
    tests_run: Array<Record<string, unknown>>;
    warnings: string[];
    followups: string[];
  };
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    energy_joules?: number;
    energy_quality?: string;
    energy_scope?: string;
    energy_display?: string;
    energy_calls?: number;
  };
  messages: Array<Record<string, unknown>>;
};

type TaskResponse = {
  payload: SessionPayload;
  stderr: string;
};

type BootstrapStatus = {
  state: "dev" | "ready" | "needs_setup";
  detail: string;
};

type SetupProgress = {
  stage: string;
  message: string;
};

type InferenceSettings = {
  provider: string;
  base_url: string;
  api_key: string;
  model: string;
  context_limit: number;
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

type SessionHistoryItem = {
  id: string;
  task: string;
  status: string;
  summary: string;
  createdAt: string;
  messages?: ChatMessage[];
  totalTokens?: number;
};

// One ledger line per finished run: when it happened, which chat it belongs
// to, and the input/output token split. Everything on the Usage sheet is
// computed from these.
type UsageRecord = {
  t: number;
  chatId: string;
  prompt: number;
  completion: number;
};

type ActivityKind =
  | "read"
  | "search"
  | "edit"
  | "run"
  | "test-pass"
  | "test-fail"
  | "note"
  | "denied";

type ActivityLine = {
  kind: ActivityKind;
  icon: string;
  html: string;
  count: number;
};

type StageState = {
  id: string;
  title: string;
  status: "active" | "done";
};

type RunState = {
  cardId: string;
  reqNumber: number;
  startedAt: number;
  stages: StageState[];
  activities: ActivityLine[];
  activitiesExpanded: boolean;
  narration: string;
  worknotes: string[];
  totalTokens: number;
  promptTokens: number;
  completionTokens: number;
  energyJoules: number;
  energyDisplay: string;
  thinkingLabel: string;
  commandsDenied: boolean;
  timerId: number | undefined;
};

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

const isTauriRuntime = Boolean(window.__TAURI_INTERNALS__);

const STORAGE_SESSIONS = "autose.desktop.sessions";
const STORAGE_WORKSPACE = "autose.desktop.workspace";
const STORAGE_AUTONOMY = "autose.desktop.autonomy";
const STORAGE_RAIL = "autose.desktop.rail";
const STORAGE_DRAFT = "autose.desktop.draft";
const STORAGE_USAGE = "autose.desktop.usage";
const STORAGE_USAGE_RANGE = "autose.desktop.usageRange";
const STORAGE_USAGE_WEEKENDS = "autose.desktop.usageWeekends";

const STARTERS: Array<{ label: string; template: string }> = [
  {
    label: "Fix a bug",
    template: "Something isn't working: describe what you did, what you expected, and what happened instead.",
  },
  {
    label: "Add a feature",
    template: "I'd like a new feature: describe what it should do and where it fits.",
  },
  {
    label: "Explain this project",
    template: "Walk me through what this project does, in plain language.",
  },
  {
    label: "Tidy up + test",
    template: "Look over the code, clean up anything messy, and make sure the tests pass.",
  },
];

const app = document.querySelector<HTMLDivElement>("#app");
if (!app) {
  throw new Error("Missing #app root");
}

app.innerHTML = `
  <div class="blueprint-grid" aria-hidden="true"></div>
  <div class="app-shell" id="app-shell">
    <aside class="rail" id="rail" aria-label="Previous tasks">
      <div class="rail-head"><span class="eyebrow"><span class="tick"></span>Worklog</span></div>
      <button id="new-task" class="new-task-button" type="button" title="Start a fresh requirement (Ctrl+N)">+ New requirement</button>
      <div class="session-list" id="session-list"></div>
    </aside>

    <header class="app-header">
      <button id="toggle-rail" class="icon-button" type="button" aria-label="Show or hide the worklog" title="Show or hide the worklog">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4.5h12M2 8h12M2 11.5h7"/></svg>
      </button>
      <span class="brand"><span class="brand-mark" aria-hidden="true"></span><span class="brand-name">Auto<span class="brand-accent">SE</span></span></span>
      <button id="open-usage" class="icon-button push-right" type="button" aria-label="Usage" title="Usage (Ctrl+U)">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2.75 13.25V8.5M8 13.25V2.75M13.25 13.25V6"/></svg>
      </button>
      <button id="open-settings" class="icon-button" type="button" aria-label="Settings" title="Settings (Ctrl+,)">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 5h12M2 11h12M10.5 2.75v4.5M5.5 8.75v4.5"/></svg>
      </button>
    </header>

    <div class="main-column">
      <section class="conversation" id="conversation">
        <div class="conversation-inner" id="messages"></div>
      </section>

      <div class="jump-wrap"><button id="jump-latest" class="jump-latest" type="button" hidden>↓ Jump to the latest</button></div>

      <section class="composer-area" aria-label="New requirement">
        <div class="composer cropped">
          <div class="composer-label"><span class="eyebrow"><span class="tick"></span>New requirement</span></div>
          <textarea id="prompt" spellcheck="true" aria-label="Describe your requirement"
            placeholder="Describe a feature, a bug, or a question about your project. Plain English is perfect." rows="2"></textarea>
          <div class="composer-foot">
            <span class="composer-hint" id="composer-hint">ENTER ↵ STARTS · SHIFT+ENTER FOR A NEW LINE</span>
            <button id="send-button" class="send-button" type="button">Start work</button>
          </div>
        </div>
      </section>
    </div>

    <footer class="status-bar">
      <div class="presence status-cell" id="presence" aria-live="polite">
        <span class="presence-dot"></span>
        <span id="presence-text">Starting up</span>
      </div>
      <span class="status-cell" id="status-model" title="The model AutoSE thinks with">MODEL <em id="model-label">not set</em></span>
      <button class="status-cell status-link" id="workspace-chip" type="button" title="Change the project folder">
        DIR <em id="workspace-label">...</em>
      </button>
      <button class="status-cell status-link status-right" id="status-tokens" type="button" title="Tokens used this task. Click for the full usage sheet." hidden>
        TOKENS <em id="tokens-label">0</em>
        <span class="ctx-meter" id="ctx-meter" aria-hidden="true"><span class="ctx-fill" id="ctx-fill"></span></span>
        <em id="ctx-label">CTX 0%</em>
      </button>
      <span class="status-cell status-right" id="status-energy" title="Estimated or measured inference energy for this task" hidden>
        ENERGY <em id="energy-label">—</em>
      </span>
      <span class="status-cell status-stamp">LOCAL / NO CLOUD</span>
    </footer>

    <div class="overlay" id="setup-screen" hidden>
      <div class="overlay-panel cropped">
        <span class="eyebrow"><span class="tick"></span>First run</span>
        <h2>Setting up your workshop</h2>
        <p class="overlay-lede">First launch takes a minute: AutoSE is installing the tools it works with. This happens once and needs an internet connection. After that, everything runs on this machine.</p>
        <p id="setup-stage" class="setup-stage">Preparing…</p>
        <p id="setup-log" class="setup-log"></p>
        <p id="setup-error" class="form-error" hidden></p>
        <div class="overlay-actions">
          <button id="setup-retry" class="primary-button" type="button" hidden>Try again</button>
        </div>
      </div>
    </div>

    <div class="overlay" id="settings-screen" hidden>
      <div class="overlay-panel cropped">
        <span class="eyebrow"><span class="tick"></span>Configuration</span>
        <h2>Settings</h2>
        <p id="settings-notice" class="overlay-lede" hidden>One thing before we start: tell AutoSE where your AI model runs. Any OpenAI-compatible endpoint works (Ollama, LM Studio, OpenAI, and friends).</p>
        <label class="field">
          <span>Model server address<small>Where your AI model runs, e.g. http://localhost:11434/v1</small></span>
          <input id="setting-base-url" class="mono" type="text" placeholder="http://localhost:11434/v1" spellcheck="false" />
        </label>
        <label class="field">
          <span>API key<small>Leave blank if your server doesn't need one</small></span>
          <input id="setting-api-key" class="mono" type="password" spellcheck="false" />
        </label>
        <label class="field">
          <span>Model name<small>e.g. qwen3-coder:30b</small></span>
          <input id="setting-model" class="mono" type="text" spellcheck="false" />
        </label>
        <label class="field">
          <span>Context limit<small>In tokens. The default is fine for most models</small></span>
          <input id="setting-context-limit" class="mono" type="number" min="1024" step="1024" />
        </label>
        <hr class="settings-divider" />
        <label class="field">
          <span>Project folder<small>The folder AutoSE works in</small></span>
          <input id="setting-workspace" class="mono" type="text" spellcheck="false" />
        </label>
        <label class="toggle-field">
          <input id="setting-autonomy" type="checkbox" />
          <span class="toggle-text">Work hands-free
            <small>Let AutoSE run commands (tests, builds, installs) on its own. Turn this off and it will skip any command instead of running it.</small>
          </span>
        </label>
        <p id="settings-error" class="form-error" hidden></p>
        <div class="overlay-actions">
          <button id="settings-cancel" class="ghost-button" type="button">Cancel</button>
          <button id="settings-save" class="primary-button" type="button">Save changes</button>
        </div>
      </div>
    </div>

    <div class="overlay" id="usage-screen" hidden>
      <div class="overlay-panel usage-panel cropped">
        <span class="eyebrow"><span class="tick"></span>Instrumentation</span>
        <h2>Usage</h2>

        <div class="range-row" role="group" aria-label="Time range">
          <button class="range-chip" type="button" data-range="1h">1H</button>
          <button class="range-chip" type="button" data-range="1d">1D</button>
          <button class="range-chip" type="button" data-range="1w">1W</button>
          <button class="range-chip" type="button" data-range="1m">1M</button>
          <button class="range-chip" type="button" data-range="1y">1Y</button>
          <button class="range-chip" type="button" data-range="all">ALL</button>
          <button class="range-chip" type="button" data-range="custom">CUSTOM</button>
        </div>
        <div class="custom-range" id="usage-custom" hidden>
          <label>FROM <input type="date" id="usage-from" class="mono" /></label>
          <label>TO <input type="date" id="usage-to" class="mono" /></label>
        </div>

        <div class="usage-stats" id="usage-stats"></div>
        <div class="usage-costs" id="usage-costs"></div>

        <div class="usage-block">
          <div class="usage-cal-head">
            <h4 class="eyebrow">Days on the board</h4>
            <div class="cal-nav">
              <button class="cal-nav-button" id="cal-prev" type="button" aria-label="Previous month">‹</button>
              <span class="cal-title" id="cal-title"></span>
              <button class="cal-nav-button" id="cal-next" type="button" aria-label="Next month">›</button>
            </div>
          </div>
          <div class="usage-calendar" id="usage-calendar"></div>
        </div>

        <div class="usage-block">
          <h4 class="eyebrow">Streak</h4>
          <div class="usage-streaks" id="usage-streaks"></div>
          <label class="streak-toggle">
            <input id="usage-skip-weekends" type="checkbox" />
            <span>Let weekends slide. Saturday and Sunday never break a streak.</span>
          </label>
        </div>

        <div class="overlay-actions">
          <button id="usage-close" class="ghost-button" type="button">Close</button>
        </div>
      </div>
    </div>
  </div>
`;

const $ = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;

const appShell = $<HTMLDivElement>("#app-shell");
const railToggle = $<HTMLButtonElement>("#toggle-rail");
const presence = $<HTMLDivElement>("#presence");
const presenceText = $<HTMLSpanElement>("#presence-text");
const conversation = $<HTMLElement>("#conversation");
const messages = $<HTMLDivElement>("#messages");
const promptInput = $<HTMLTextAreaElement>("#prompt");
const sendButton = $<HTMLButtonElement>("#send-button");
const composerEl = $<HTMLDivElement>(".composer");
const composerHint = $<HTMLSpanElement>("#composer-hint");
const jumpLatest = $<HTMLButtonElement>("#jump-latest");
const workspaceChip = $<HTMLButtonElement>("#workspace-chip");
const workspaceLabel = $<HTMLElement>("#workspace-label");
const modelLabel = $<HTMLElement>("#model-label");
const statusTokens = $<HTMLElement>("#status-tokens");
const tokensLabel = $<HTMLElement>("#tokens-label");
const ctxFill = $<HTMLElement>("#ctx-fill");
const ctxLabel = $<HTMLElement>("#ctx-label");
const statusEnergy = $<HTMLElement>("#status-energy");
const energyLabel = $<HTMLElement>("#energy-label");
const sessionList = $<HTMLDivElement>("#session-list");
const newTaskButton = $<HTMLButtonElement>("#new-task");
const openSettingsButton = $<HTMLButtonElement>("#open-settings");
const setupScreen = $<HTMLDivElement>("#setup-screen");
const setupStage = $<HTMLParagraphElement>("#setup-stage");
const setupLog = $<HTMLParagraphElement>("#setup-log");
const setupError = $<HTMLParagraphElement>("#setup-error");
const setupRetryButton = $<HTMLButtonElement>("#setup-retry");
const settingsScreen = $<HTMLDivElement>("#settings-screen");
const settingsNotice = $<HTMLParagraphElement>("#settings-notice");
const settingBaseUrl = $<HTMLInputElement>("#setting-base-url");
const settingApiKey = $<HTMLInputElement>("#setting-api-key");
const settingModel = $<HTMLInputElement>("#setting-model");
const settingContextLimit = $<HTMLInputElement>("#setting-context-limit");
const settingWorkspace = $<HTMLInputElement>("#setting-workspace");
const settingAutonomy = $<HTMLInputElement>("#setting-autonomy");
const settingsError = $<HTMLParagraphElement>("#settings-error");
const settingsCancelButton = $<HTMLButtonElement>("#settings-cancel");
const settingsSaveButton = $<HTMLButtonElement>("#settings-save");
const openUsageButton = $<HTMLButtonElement>("#open-usage");
const usageScreen = $<HTMLDivElement>("#usage-screen");
const usageCustom = $<HTMLDivElement>("#usage-custom");
const usageFrom = $<HTMLInputElement>("#usage-from");
const usageTo = $<HTMLInputElement>("#usage-to");
const usageStats = $<HTMLDivElement>("#usage-stats");
const usageCosts = $<HTMLDivElement>("#usage-costs");
const usageCalendar = $<HTMLDivElement>("#usage-calendar");
const usageStreaks = $<HTMLDivElement>("#usage-streaks");
const usageSkipWeekends = $<HTMLInputElement>("#usage-skip-weekends");
const usageCloseButton = $<HTMLButtonElement>("#usage-close");
const calPrev = $<HTMLButtonElement>("#cal-prev");
const calNext = $<HTMLButtonElement>("#cal-next");
const calTitle = $<HTMLSpanElement>("#cal-title");

const STOP_SENTINEL = "__AUTOSE_STOPPED__";

const selectedMode: Mode = "auto";
let defaultWorkspace = "";
let sessionHistory: SessionHistoryItem[] = loadSessionHistory();
let unlistenAutoseEvent: UnlistenFn | null = null;
let isRunning = false;
let stopRequested = false;
let currentChatId: string | null = null;
let currentChatMessages: ChatMessage[] = [];
let backendReady = false;
let endpointConfigured = false;
let currentSettings: InferenceSettings | null = null;
let run: RunState | null = null;
let stickToBottom = true;
let settingsOpener: HTMLElement | null = null;
let overlayPressOnBackdrop = false;
let usagePressOnBackdrop = false;
let usageOpener: HTMLElement | null = null;
let currentChatTokens = 0;
let usageRange = localStorage.getItem(STORAGE_USAGE_RANGE) ?? "1w";
// The month the calendar is showing, as a first-of-month Date.
let calMonth = startOfMonth(new Date());

const tauriWindow = isTauriRuntime ? getCurrentWindow() : null;

/* ---------- boot ---------- */

if (localStorage.getItem(STORAGE_RAIL) !== "closed" && window.innerWidth >= 960) {
  appShell.classList.add("rail-open");
}

renderSessionList();
showGreeting();
updateWorkspaceLabel();

// An unsent requirement survives a restart. Losing typed text is never fine.
const savedDraft = localStorage.getItem(STORAGE_DRAFT);
if (savedDraft) {
  promptInput.value = savedDraft;
  autosizePrompt();
}
promptInput.focus();

if (isTauriRuntime) {
  void hydrateDefaultWorkspace();
  void hydrateSavedSessions();
  void subscribeToAutoseEvents();
  void initializeApp();
} else {
  sendButton.disabled = true;
  setPresence("attention", "Preview only");
}


/* ---------- top-level wiring ---------- */

railToggle.addEventListener("click", () => {
  const open = appShell.classList.toggle("rail-open");
  localStorage.setItem(STORAGE_RAIL, open ? "open" : "closed");
});

newTaskButton.addEventListener("click", () => startNewChat());
sendButton.addEventListener("click", () => {
  if (isRunning) {
    // Absorb the second half of a double-click on Start so it can't
    // immediately stop the task it just started.
    if (run && Date.now() - run.startedAt < 400) {
      return;
    }
    void stopTask();
  } else {
    void runTask();
  }
});
openSettingsButton.addEventListener("click", () => void openSettings(false));
workspaceChip.addEventListener("click", () => void openSettings(false));
settingsCancelButton.addEventListener("click", () => closeSettings());
settingsSaveButton.addEventListener("click", () => void saveSettings());
setupRetryButton.addEventListener("click", () => void runSetup());

// Dismiss settings by clicking the backdrop, but only when the press also
// started there: a drag that ends outside the panel must not close it.
settingsScreen.addEventListener("pointerdown", (event) => {
  overlayPressOnBackdrop = event.target === settingsScreen;
});
settingsScreen.addEventListener("click", (event) => {
  if (event.target === settingsScreen && overlayPressOnBackdrop && !settingsCancelButton.hidden) {
    closeSettings();
  }
});

/* usage sheet wiring */
openUsageButton.addEventListener("click", () => openUsage());
statusTokens.addEventListener("click", () => openUsage());
usageCloseButton.addEventListener("click", () => closeUsage());
usageScreen.addEventListener("pointerdown", (event) => {
  usagePressOnBackdrop = event.target === usageScreen;
});
usageScreen.addEventListener("click", (event) => {
  if (event.target === usageScreen && usagePressOnBackdrop) {
    closeUsage();
  }
});
usageScreen.querySelectorAll<HTMLButtonElement>(".range-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    usageRange = chip.dataset.range ?? "1w";
    localStorage.setItem(STORAGE_USAGE_RANGE, usageRange);
    renderUsageSheet();
  });
});
usageFrom.addEventListener("change", () => renderUsageSheet());
usageTo.addEventListener("change", () => renderUsageSheet());
usageSkipWeekends.addEventListener("change", () => {
  localStorage.setItem(STORAGE_USAGE_WEEKENDS, usageSkipWeekends.checked ? "skip" : "count");
  renderUsageSheet();
});
calPrev.addEventListener("click", () => {
  calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1);
  renderUsageCalendar();
});
calNext.addEventListener("click", () => {
  calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1);
  renderUsageCalendar();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!usageScreen.hidden) {
      closeUsage();
    } else if (!settingsScreen.hidden && !settingsCancelButton.hidden) {
      closeSettings();
    }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey) {
    if (event.key === ",") {
      event.preventDefault();
      void openSettings(false);
    } else if (event.key.toLowerCase() === "n") {
      event.preventDefault();
      startNewChat();
    } else if (event.key.toLowerCase() === "u") {
      event.preventDefault();
      if (usageScreen.hidden) {
        openUsage();
      } else {
        closeUsage();
      }
    }
  }
});

window.addEventListener("focus", () => {
  if (!isRunning) {
    setWindowTitle();
  }
});

conversation.addEventListener("scroll", () => {
  stickToBottom =
    conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 120;
  if (stickToBottom) {
    jumpLatest.hidden = true;
  }
});

jumpLatest.addEventListener("click", () => scrollToBottom(true));

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void runTask();
  }
});

promptInput.addEventListener("input", () => {
  autosizePrompt();
  persistDraft();
});

function autosizePrompt() {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 180)}px`;
}

function persistDraft() {
  if (promptInput.value.trim()) {
    localStorage.setItem(STORAGE_DRAFT, promptInput.value);
  } else {
    localStorage.removeItem(STORAGE_DRAFT);
  }
}

function setWindowTitle(prefix = "") {
  const title = prefix ? `${prefix} · AutoSE` : "AutoSE";
  document.title = title;
  void tauriWindow?.setTitle(title).catch(() => { });
}

/* ---------- backend bootstrap & settings ---------- */

async function hydrateDefaultWorkspace() {
  try {
    defaultWorkspace = await invoke<string>("default_workspace");
  } catch {
    defaultWorkspace = "";
  }
  updateWorkspaceLabel();
}

async function hydrateSavedSessions() {
  try {
    const savedSessions = await invoke<SessionHistoryItem[]>("list_saved_sessions");
    sessionHistory = mergeSessionHistory(savedSessions, sessionHistory);
    localStorage.setItem(STORAGE_SESSIONS, JSON.stringify(sessionHistory));
  } catch {
    // Local history still renders below.
  }
  renderSessionList();
}

async function subscribeToAutoseEvents() {
  unlistenAutoseEvent = await listen<SessionEvent>("autose-event", (event) => {
    handleLiveEvent(event.payload);
  });
}

async function initializeApp() {
  updateRunGate();
  await listen<SetupProgress>("autose-setup", (event) => {
    setupStage.textContent = stageLabel(event.payload.stage);
    setupLog.textContent = event.payload.message;
  });

  try {
    const status = await invoke<BootstrapStatus>("backend_status");
    if (status.state === "needs_setup") {
      await runSetup();
      return;
    }
    backendReady = true;
  } catch (error) {
    setupScreen.hidden = false;
    showSetupError(error);
    return;
  }

  await refreshSettingsGate();
}

async function runSetup() {
  setupScreen.hidden = false;
  setupError.hidden = true;
  setupRetryButton.hidden = true;
  setupStage.textContent = "Preparing…";
  setupLog.textContent = "";
  setPresence("working", "Setting things up…");

  try {
    await invoke("bootstrap_backend");
  } catch (error) {
    showSetupError(error);
    return;
  }

  backendReady = true;
  setupScreen.hidden = true;
  await refreshSettingsGate();
}

function showSetupError(error: unknown) {
  setupError.textContent = error instanceof Error ? error.message : String(error);
  setupError.hidden = false;
  setupRetryButton.hidden = false;
  setPresence("attention", "Setup hit a snag");
}

function stageLabel(stage: string) {
  switch (stage) {
    case "copy":
      return "Unpacking the toolbox…";
    case "uv":
      return "Fetching the package manager…";
    case "sync":
      return "Preparing the Python environment…";
    case "config":
      return "Writing the starter configuration…";
    case "done":
      return "All set.";
    default:
      return "Working…";
  }
}

async function refreshSettingsGate() {
  try {
    currentSettings = await invoke<InferenceSettings>("get_settings");
  } catch {
    currentSettings = null;
  }
  endpointConfigured = Boolean(currentSettings?.base_url?.trim());
  updateRunGate();
  if (!endpointConfigured) {
    void openSettings(true);
  } else {
    setReadyPresence();
  }
}

function setReadyPresence() {
  const model = currentSettings?.model?.trim() || "not set";
  modelLabel.textContent = model;
  if (model.length > 40) {
    modelLabel.title = model;
  } else {
    modelLabel.removeAttribute("title");
  }
  setPresence("ready", "Ready");
}

async function openSettings(showNotice: boolean) {
  settingsOpener =
    document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null;
  settingsNotice.hidden = !showNotice;
  settingsError.hidden = true;
  settingsCancelButton.hidden = showNotice;

  try {
    currentSettings = await invoke<InferenceSettings>("get_settings");
  } catch {
    // Keep whatever we loaded last; the form falls back to defaults below.
  }
  settingBaseUrl.value = currentSettings?.base_url ?? "";
  settingApiKey.value = currentSettings?.api_key ?? "";
  settingModel.value = currentSettings?.model ?? "";
  settingContextLimit.value = String(currentSettings?.context_limit ?? 262144);
  settingWorkspace.value = workspaceRoot();
  settingAutonomy.checked = autonomyEnabled();
  settingsScreen.hidden = false;
  settingBaseUrl.focus();
}

function closeSettings() {
  settingsScreen.hidden = true;
  // Hand focus back to wherever the person was before the overlay opened.
  (settingsOpener ?? promptInput).focus();
  settingsOpener = null;
}

function normalizeBaseUrl(raw: string): string {
  let value = raw.trim().replace(/\/+$/, "");
  if (!value) {
    return value;
  }
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(value)) {
    value = `http://${value}`;
  }
  if (/:11434$/i.test(value)) {
    value = `${value}/v1`;
  }
  return value.replace(/\/+$/, "");
}

async function saveSettings() {
  const baseUrl = normalizeBaseUrl(settingBaseUrl.value);
  if (!baseUrl) {
    settingsError.textContent = "The model server address is required. AutoSE has nowhere to think without it.";
    settingsError.hidden = false;
    return;
  }

  const contextLimit = Number.parseInt(settingContextLimit.value, 10);
  const settings: InferenceSettings = {
    provider: currentSettings?.provider || "openai",
    base_url: baseUrl,
    api_key: settingApiKey.value.trim(),
    model: settingModel.value.trim(),
    context_limit: Number.isFinite(contextLimit) && contextLimit > 0 ? contextLimit : 262144,
  };

  try {
    await invoke("save_settings", { settings });
  } catch (error) {
    settingsError.textContent = error instanceof Error ? error.message : String(error);
    settingsError.hidden = false;
    return;
  }

  localStorage.setItem(STORAGE_WORKSPACE, settingWorkspace.value.trim());
  localStorage.setItem(STORAGE_AUTONOMY, settingAutonomy.checked ? "on" : "off");
  currentSettings = settings;
  endpointConfigured = true;
  closeSettings();
  updateWorkspaceLabel();
  updateRunGate();
  if (!isRunning) {
    setReadyPresence();
  }
}

function workspaceRoot() {
  return localStorage.getItem(STORAGE_WORKSPACE)?.trim() || defaultWorkspace;
}

function autonomyEnabled() {
  return localStorage.getItem(STORAGE_AUTONOMY) !== "off";
}

function updateWorkspaceLabel() {
  const root = workspaceRoot();
  workspaceLabel.textContent = root ? shortenPath(root) : "not set";
  workspaceChip.title = root ? `Working in ${root}. Click to change.` : "Choose the project folder";
}

function shortenPath(path: string) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) {
    return path;
  }
  return `…${path.includes("\\") ? "\\" : "/"}${parts.slice(-2).join(path.includes("\\") ? "\\" : "/")}`;
}

function updateRunGate() {
  if (isRunning) {
    sendButton.disabled = stopRequested;
    sendButton.textContent = stopRequested ? "Stopping…" : "Stop";
    composerHint.textContent = stopRequested ? "WRAPPING UP" : "WORKING · STOP TO INTERRUPT";
  } else {
    sendButton.disabled = !isTauriRuntime || !backendReady || !endpointConfigured;
    sendButton.textContent = "Start work";
    composerHint.textContent = "ENTER ↵ STARTS · SHIFT+ENTER FOR A NEW LINE";
  }
  sendButton.classList.toggle("stop", isRunning);
  appShell.classList.toggle("is-running", isRunning);
  newTaskButton.disabled = isRunning;
  newTaskButton.title = isRunning
    ? "Available once the current task finishes"
    : "Start a fresh requirement (Ctrl+N)";
}

/* ---------- greeting ---------- */

function showGreeting() {
  const hour = new Date().getHours();
  const salutation = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  messages.innerHTML = `
    <div class="greeting" id="greeting">
      <span class="eyebrow"><span class="tick"></span>${salutation} · Engineering runtime ready</span>
      <h1>What are we <span class="ink-underline">building</span> today?</h1>
      <p>Describe a feature, a bug, or a question in plain English. AutoSE reads your project, drafts a plan, does the work, and files a report showing exactly what changed. All of it on this machine.</p>
      <div class="starter-chips">
        ${STARTERS.map(
    (starter, index) =>
      `<button class="starter-chip" type="button" data-starter="${index}">${escapeHtml(starter.label)}</button>`,
  ).join("")}
      </div>
    </div>
  `;
  messages.querySelectorAll<HTMLButtonElement>(".starter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const starter = STARTERS[Number(chip.dataset.starter)];
      promptInput.value = starter.template;
      persistDraft();
      promptInput.focus();
      promptInput.select();
    });
  });
}

function clearGreeting() {
  document.querySelector("#greeting")?.remove();
}

/* ---------- running a task ---------- */

async function runTask() {
  if (isRunning || sendButton.disabled) {
    return;
  }

  if (!backendReady || !endpointConfigured) {
    void openSettings(true);
    return;
  }

  const prompt = promptInput.value.trim();
  if (!prompt) {
    promptInput.focus();
    nudgeComposer();
    return;
  }

  if (!currentChatId) {
    currentChatId = createChatId();
  }

  const backendPrompt = buildBackendPrompt(prompt);
  currentChatMessages.push({ role: "user", content: prompt });
  const reqNumber = currentChatMessages.filter((message) => message.role === "user").length;

  clearGreeting();
  appendUserBubble(prompt, reqNumber);
  promptInput.value = "";
  localStorage.removeItem(STORAGE_DRAFT);
  autosizePrompt();

  startWorkCard(reqNumber);
  setBusy(true);

  try {
    const response = await invoke<TaskResponse>("run_autose", {
      request: {
        prompt: backendPrompt,
        mode: selectedMode,
        workspace: workspaceRoot(),
        autoApprove: autonomyEnabled(),
      },
    });
    finishWorkCard(response.payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message.includes(STOP_SENTINEL)) {
      stopWorkCard();
    } else {
      failWorkCard(message);
    }
  } finally {
    stopRequested = false;
    setBusy(false);
    promptInput.focus();
  }
}

async function stopTask() {
  if (!isRunning || stopRequested) {
    return;
  }
  stopRequested = true;
  updateRunGate();
  setPresence("working", "Stopping…");
  try {
    await invoke("stop_autose");
  } catch (error) {
    stopRequested = false;
    updateRunGate();
    pushActivity({
      kind: "note",
      icon: "△",
      html: escapeHtml(
        `Couldn't stop the task: ${error instanceof Error ? error.message : String(error)}`,
      ),
    });
  }
}

function nudgeComposer() {
  composerEl.classList.remove("nudge");
  void composerEl.offsetWidth;
  composerEl.classList.add("nudge");
  window.setTimeout(() => composerEl.classList.remove("nudge"), 260);
}

function setBusy(busy: boolean) {
  isRunning = busy;
  updateRunGate();
  if (!busy) {
    stopRunTimer();
  }
}

function stopRunTimer() {
  if (run?.timerId !== undefined) {
    window.clearInterval(run.timerId);
    run.timerId = undefined;
  }
}

/* ---------- the work card ---------- */

function startWorkCard(reqNumber: number) {
  const cardId = `work-${Date.now()}`;
  run = {
    cardId,
    reqNumber,
    startedAt: Date.now(),
    stages: [],
    activities: [],
    activitiesExpanded: false,
    narration: "",
    worknotes: [],
    totalTokens: 0,
    promptTokens: 0,
    completionTokens: 0,
    energyJoules: 0,
    energyDisplay: "",
    thinkingLabel: "Reading your request",
    commandsDenied: false,
    timerId: undefined,
  };

  messages.insertAdjacentHTML(
    "beforeend",
    `
    <article class="work-card cropped running" id="${cardId}">
      <div class="work-head">
        <span class="presence-dot"></span>
        <span class="eyebrow">Worklog / ${reqLabel(reqNumber)}</span>
        <span class="work-title">Reading your request</span>
        <span class="work-elapsed">0:00</span>
      </div>
      <div class="stages" hidden></div>
      <div class="activity"></div>
      <div class="narration prose" hidden></div>
    </article>
  `,
  );

  setPresence("working", `Working / ${reqLabel(reqNumber)}`);
  setWindowTitle(`Working / ${reqLabel(reqNumber)}`);
  updateContextMeter(0);
  run.timerId = window.setInterval(() => {
    const card = workCardEl();
    if (card && run) {
      card.querySelector(".work-elapsed")!.textContent = formatElapsed(Date.now() - run.startedAt);
    }
  }, 1000);
  scrollToBottom(true);
}

function workCardEl() {
  return run ? document.getElementById(run.cardId) : null;
}

function handleLiveEvent(event: SessionEvent) {
  if (!run) {
    return;
  }
  const data = event.data ?? {};

  switch (event.type) {
    case "stage_started": {
      const id = String(data.stage ?? "stage");
      const title = humanStageTitle(id, String(data.title ?? id));
      const last = run.stages[run.stages.length - 1];
      if (last && last.id === id) {
        last.status = "active";
      } else {
        run.stages.forEach((stage) => (stage.status = "done"));
        const isRepeat = run.stages.some((stage) => stage.id === id);
        run.stages.push({ id, title: isRepeat ? `${title} again` : title, status: "active" });
      }
      setThinking(String(data.thinking_label ?? title) + "…");
      renderStages();
      break;
    }
    case "agent_stage_started":
      setThinking("Thinking it through…");
      break;
    case "tool_called":
      pushActivity(describeTool(String(data.tool ?? ""), (data.arguments ?? {}) as Record<string, unknown>));
      break;
    case "approval_resolved": {
      if (data.kind === "plan_review") {
        pushActivity({ kind: "note", icon: "✓", html: "Plan looks good, moving on" });
      } else if (data.allowed === false) {
        run.commandsDenied = true;
        const command = String(data.command ?? "a command");
        pushActivity({
          kind: "denied",
          icon: "⊘",
          html: `Skipped ${monoSpan(elideMiddle(command, 60), command)} (hands-free mode is off)`,
        });
      }
      break;
    }
    case "file_changed": {
      const changedPath = String(data.path ?? "");
      pushActivity({
        kind: "edit",
        icon: "✎",
        html: `Changed ${monoSpan(fileName(changedPath), changedPath)}`,
      });
      break;
    }
    case "test_recorded": {
      const passed = String(data.status ?? "") !== "failed";
      pushActivity({
        kind: passed ? "test-pass" : "test-fail",
        icon: passed ? "✓" : "✗",
        html: `${passed ? "Check passed" : "Check failed"}: ${escapeHtml(humanizeName(String(data.name ?? "check")))}`,
      });
      break;
    }
    case "artifact_created": {
      const kind = String(data.kind ?? "");
      if (kind === "plan") {
        pushActivity({ kind: "note", icon: "☰", html: "Sketched the plan" });
      } else if (kind === "design") {
        pushActivity({ kind: "note", icon: "◫", html: "Drafted the design" });
      }
      break;
    }
    case "tokens_updated":
      run.totalTokens = Number(data.total_tokens ?? 0);
      run.promptTokens = Number(data.prompt_tokens ?? run.promptTokens);
      run.completionTokens = Number(data.completion_tokens ?? run.completionTokens);
      updateContextMeter(run.totalTokens);
      break;
    case "energy_updated":
      run.energyJoules = Number(data.total_energy_joules ?? run.energyJoules);
      run.energyDisplay = String(data.total_display ?? data.display ?? run.energyDisplay);
      updateEnergyMeter(run.energyDisplay, run.energyJoules, String(data.display ?? ""));
      if (data.display) {
        pushActivity({
          kind: "note",
          icon: "◉",
          html: escapeHtml(`Energy ${String(data.display)}`),
        });
      }
      break;
    case "assistant_chunk":
      appendNarration(String(data.content ?? ""));
      break;
    case "assistant_message_added":
      run.worknotes.push(String(data.content ?? ""));
      break;
    case "warning_emitted":
      pushActivity({ kind: "note", icon: "△", html: escapeHtml(String(data.message ?? "Noted a warning")) });
      break;
    case "validation_verdict":
      pushActivity({ kind: "note", icon: "☑", html: "Reviewed the work against the plan" });
      break;
    case "session_completed":
    case "session_failed":
    case "stream_finished":
    default:
      break;
  }
}

function setThinking(label: string) {
  if (!run) {
    return;
  }
  run.thinkingLabel = label;
  const card = workCardEl();
  if (card?.classList.contains("running")) {
    card.querySelector(".work-title")!.textContent = label;
  }
  setPresence("working", `Working / ${label}`);
}

function renderStages() {
  const card = workCardEl();
  if (!card || !run) {
    return;
  }
  const host = card.querySelector<HTMLElement>(".stages")!;
  host.hidden = run.stages.length === 0;
  host.innerHTML = run.stages
    .map(
      (stage, index) => `
      <span class="stage ${stage.status}">
        <span class="stage-id">PH-${String(index + 1).padStart(2, "0")}</span>
        <span class="stage-name">${escapeHtml(stage.title)}</span>
      </span>`,
    )
    .join("");
}

function pushActivity(line: Omit<ActivityLine, "count">) {
  if (!run) {
    return;
  }
  const last = run.activities[run.activities.length - 1];
  if (last && last.html === line.html && last.kind === line.kind) {
    last.count += 1;
  } else {
    run.activities.push({ ...line, count: 1 });
  }
  renderActivities();
  scrollToBottom();
}

function renderActivities() {
  const card = workCardEl();
  if (!card || !run) {
    return;
  }
  const host = card.querySelector<HTMLElement>(".activity")!;
  const visibleCount = run.activitiesExpanded ? run.activities.length : 5;
  const hidden = Math.max(0, run.activities.length - visibleCount);
  const visible = run.activities.slice(-visibleCount);

  host.innerHTML =
    (hidden > 0
      ? `<button class="activity-more" type="button">Show ${hidden} earlier step${hidden === 1 ? "" : "s"}</button>`
      : "") +
    visible
      .map(
        (line) =>
          `<div class="activity-line kind-${line.kind}"><span class="act-icon">${line.icon}</span><span>${line.html}${line.count > 1 ? ` <em>×${line.count}</em>` : ""}</span></div>`,
      )
      .join("");

  host.querySelector<HTMLButtonElement>(".activity-more")?.addEventListener("click", () => {
    if (run) {
      run.activitiesExpanded = true;
      renderActivities();
    }
  });
}

function appendNarration(chunk: string) {
  const card = workCardEl();
  if (!card || !run || !chunk) {
    return;
  }
  run.narration += chunk;
  const narration = card.querySelector<HTMLElement>(".narration")!;
  narration.hidden = false;
  narration.innerHTML = renderMarkdown(run.narration);
  narration.scrollTop = narration.scrollHeight;
  scrollToBottom();
}

/* ---------- finishing ---------- */

function finishWorkCard(payload: SessionPayload) {
  const card = workCardEl();
  if (!card || !run) {
    return;
  }

  stopRunTimer();
  const result = payload.result;
  const completed = result.status === "completed";
  const elapsed = formatElapsed(Date.now() - run.startedAt);
  const summary =
    result.summary?.trim() ||
    (completed
      ? "Done. Nothing new to report."
      : "The run ended without a final answer. Try again or check the model endpoint.");

  // The final payload is the authoritative count; live events are a preview.
  if (payload.usage) {
    run.promptTokens = Number(payload.usage.prompt_tokens ?? run.promptTokens);
    run.completionTokens = Number(payload.usage.completion_tokens ?? run.completionTokens);
    run.totalTokens = Number(payload.usage.total_tokens ?? run.promptTokens + run.completionTokens);
    run.energyJoules = Number(payload.usage.energy_joules ?? run.energyJoules);
    run.energyDisplay = String(payload.usage.energy_display ?? run.energyDisplay);
    if (run.energyDisplay || run.energyJoules > 0) {
      updateEnergyMeter(run.energyDisplay, run.energyJoules);
    }
  }
  recordUsage(run.promptTokens, run.completionTokens);

  card.classList.remove("running");
  card.classList.add(completed ? "done" : "failed");
  card.querySelector(".work-head")!.remove();
  card.querySelector<HTMLElement>(".narration")!.hidden = true;
  run.stages.forEach((stage) => (stage.status = "done"));
  if (!completed) {
    // Leave the last stage visibly unfinished on failure.
    const last = run.stages[run.stages.length - 1];
    if (last) {
      last.status = "active";
    }
  }
  renderStages();
  run.activitiesExpanded = false;
  renderActivities();

  const testsPassed = result.tests_run.filter((test) => String(test.status ?? "") !== "failed").length;
  const testsFailed = result.tests_run.length - testsPassed;
  const planArtifact = payload.artifacts.find((artifact) => artifact.kind === "plan");
  const worknotes = buildWorknotes(summary, planArtifact);

  const facts: string[] = [];
  const fact = (label: string, tone = "") => `<span class="fact ${tone}"><span class="dot"></span>${label}</span>`;
  if (result.changed_files.length) {
    facts.push(fact(`FILES ${result.changed_files.length}`));
  }
  if (result.tests_run.length) {
    facts.push(
      testsFailed > 0
        ? fact(`CHECKS ${testsPassed}/${result.tests_run.length}`, "warn")
        : fact(`CHECKS ${testsPassed}/${result.tests_run.length}`, "good"),
    );
  }
  facts.push(fact(`TIME ${elapsed}`, "good"));
  if (run.totalTokens > 0) {
    facts.push(fact(`TOKENS ${run.totalTokens.toLocaleString()}`));
  }
  if (run.energyDisplay || run.energyJoules > 0) {
    facts.push(fact(`ENERGY ${run.energyDisplay || `${run.energyJoules.toFixed(1)} J`}`));
  }
  if (result.warnings.length) {
    facts.push(fact(`NOTES ${result.warnings.length}`, "warn"));
  }
  if (run.commandsDenied) {
    facts.push(fact("CMDS SKIPPED", "warn"));
  }

  const sections: string[] = [];
  if (result.changed_files.length) {
    sections.push(receiptSection("What changed", result.changed_files.map(
      (file) => `<li><span class="li-mark">✎</span><span class="file">${escapeHtml(file)}</span></li>`,
    )));
  }
  if (result.tests_run.length) {
    sections.push(receiptSection("Checks", result.tests_run.map((test) => {
      const passed = String(test.status ?? "") !== "failed";
      return `<li><span class="li-mark ${passed ? "pass" : "fail"}">${passed ? "✓" : "✗"}</span><span>${escapeHtml(humanizeName(String(test.name ?? "check")))}</span></li>`;
    })));
  }
  if (result.warnings.length) {
    sections.push(receiptSection("Worth knowing", result.warnings.map(
      (warning) => `<li><span class="li-mark">△</span><span>${escapeHtml(warning)}</span></li>`,
    )));
  }
  const followups = [...result.followups];
  if (run.commandsDenied) {
    followups.push("I skipped some commands because hands-free mode is off. Turn on “Work hands-free” in Settings and ask me again for a fully finished job.");
  }
  if (followups.length) {
    sections.push(receiptSection("What you might want next", followups.map(
      (item) => `<li><span class="li-mark">→</span><span>${escapeHtml(item)}</span></li>`,
    )));
  }

  const headline = completed ? "Work complete." : "Stopped. This needs your attention.";

  card.insertAdjacentHTML(
    "beforeend",
    `
    <div class="receipt">
      <span class="eyebrow"><span class="tick"></span>Report / ${reqLabel(run.reqNumber)}</span>
      <div class="receipt-headline">${headline}</div>
      ${facts.length ? `<div class="receipt-facts">${facts.join("")}</div>` : ""}
      ${summary ? `<div class="receipt-summary prose">${renderMarkdown(summary)}</div>` : ""}
      ${sections.join("")}
      ${worknotes ? `<details class="worknotes"><summary>Working notes</summary><div class="worknotes-body">${escapeHtml(worknotes)}</div></details>` : ""}
    </div>
  `,
  );

  currentChatMessages.push({ role: "assistant", content: summary || headline });
  saveCurrentChat(result.status);
  setReadyPresence();
  // If the person is in another window, let the taskbar deliver the news.
  if (document.hasFocus()) {
    setWindowTitle();
  } else {
    setWindowTitle(completed ? "✓ Done" : "△ Needs your attention");
  }
  scrollToBottom();
  run = null;
}

function failWorkCard(message: string) {
  const card = workCardEl();
  if (!card || !run) {
    return;
  }
  stopRunTimer();
  recordUsage(run.promptTokens, run.completionTokens);
  card.classList.remove("running");
  card.classList.add("failed");
  card.querySelector(".work-head")!.remove();
  card.querySelector<HTMLElement>(".narration")!.hidden = true;

  card.insertAdjacentHTML(
    "beforeend",
    `
    <div class="receipt">
      <span class="eyebrow"><span class="tick"></span>Report / ${reqLabel(run.reqNumber)}</span>
      <div class="receipt-headline">Stopped. This needs your attention.</div>
      <div class="receipt-facts"><span class="fact warn"><span class="dot"></span>STOPPED AFTER ${formatElapsed(Date.now() - run.startedAt)}</span></div>
      <div class="receipt-summary prose">${renderMarkdown(friendlyError(message))}</div>
    </div>
  `,
  );

  currentChatMessages.push({ role: "assistant", content: friendlyError(message) });
  saveCurrentChat("failed");
  setPresence("attention", "That last task didn't finish");
  if (document.hasFocus()) {
    setWindowTitle();
  } else {
    setWindowTitle("△ Needs your attention");
  }
  scrollToBottom();
  run = null;
}

function stopWorkCard() {
  const card = workCardEl();
  if (!card || !run) {
    return;
  }
  stopRunTimer();
  recordUsage(run.promptTokens, run.completionTokens);
  card.classList.remove("running");
  card.classList.add("stopped");
  card.querySelector(".work-head")!.remove();
  card.querySelector<HTMLElement>(".narration")!.hidden = true;
  run.stages.forEach((stage) => (stage.status = "done"));
  renderStages();
  run.activitiesExpanded = false;
  renderActivities();

  const note =
    "You stopped this task before it finished. Any files it already changed stay changed. Start a new requirement to pick the work back up or to undo it.";
  card.insertAdjacentHTML(
    "beforeend",
    `
    <div class="receipt">
      <span class="eyebrow"><span class="tick"></span>Report / ${reqLabel(run.reqNumber)}</span>
      <div class="receipt-headline">Stopped at your request.</div>
      <div class="receipt-facts"><span class="fact warn"><span class="dot"></span>STOPPED AT ${formatElapsed(Date.now() - run.startedAt)}</span></div>
      <div class="receipt-summary prose">${renderMarkdown(note)}</div>
    </div>
  `,
  );

  currentChatMessages.push({ role: "assistant", content: "Stopped at your request." });
  saveCurrentChat("stopped");
  setReadyPresence();
  setWindowTitle();
  scrollToBottom(true);
  run = null;
}

function friendlyError(message: string) {
  if (/base_url|connect|connection|refused|timed? ?out/i.test(message)) {
    return `I couldn't reach your model server. Check that it's running and that the address in Settings is right.\n\nDetails:\n\`\`\`\n${truncate(message, 600)}\n\`\`\``;
  }
  return `Something went wrong before I could finish.\n\n\`\`\`\n${truncate(message, 800)}\n\`\`\``;
}

function buildWorknotes(summary: string, plan: SessionArtifact | undefined) {
  if (!run) {
    return "";
  }
  const parts: string[] = [];
  if (plan?.content?.trim()) {
    parts.push(`[ THE PLAN ]\n${plan.content.trim()}`);
  }
  const narration = run.narration.trim();
  if (narration && narration !== summary.trim()) {
    parts.push(`[ NOTES ALONG THE WAY ]\n${narration}`);
  }
  for (const note of run.worknotes) {
    if (note.trim() && note.trim() !== summary.trim()) {
      parts.push(note.trim());
    }
  }
  return parts.join("\n\n");
}

function receiptSection(title: string, items: string[]) {
  return `<div class="receipt-section"><h4 class="eyebrow">${escapeHtml(title)}</h4><ul>${items.join("")}</ul></div>`;
}

/* ---------- humanizing the machine ---------- */

function describeTool(tool: string, args: Record<string, unknown>): Omit<ActivityLine, "count"> {
  const fullPath = String(args.path ?? args.file ?? "");
  const path = fullPath ? monoSpan(fileName(fullPath), fullPath) : "";
  switch (tool) {
    case "read_file":
      return { kind: "read", icon: "◉", html: path ? `Reading ${path}` : "Reading a file" };
    case "list_files":
      return { kind: "read", icon: "▤", html: "Looking around the project" };
    case "search_files": {
      const pattern = String(args.pattern ?? args.query ?? "").trim();
      return {
        kind: "search",
        icon: "⌕",
        html: pattern ? `Searching the code for ${monoSpan(elideMiddle(pattern, 40), pattern)}` : "Searching the code",
      };
    }
    case "find_files":
      return { kind: "search", icon: "⌕", html: "Hunting for the right files" };
    case "write_file":
      return { kind: "edit", icon: "✎", html: path ? `Writing ${path}` : "Writing a file" };
    case "edit_file":
      return { kind: "edit", icon: "✎", html: path ? `Editing ${path}` : "Editing a file" };
    case "run_command": {
      const command = String(args.command ?? "").trim();
      return {
        kind: "run",
        icon: "▸",
        html: command ? `Running ${monoSpan(elideMiddle(command, 60), command)}` : "Running a command",
      };
    }
    default:
      return { kind: "note", icon: "·", html: `Using ${escapeHtml(humanizeName(tool || "a tool"))}` };
  }
}

function humanStageTitle(id: string, fallback: string) {
  switch (id) {
    case "plan":
      return "Plan";
    case "design":
      return "Design";
    case "code":
    case "execute":
      return "Build";
    case "test":
    case "validate":
      return "Check";
    default:
      return fallback;
  }
}

function humanizeName(value: string) {
  return value.replaceAll("_", " ").replace(/^\w/, (char) => char.toUpperCase());
}

function fileName(path: string) {
  if (!path) {
    return "";
  }
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

function truncate(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

// Elide from the middle so both the start and the telling end of a command
// or pattern stay readable.
function elideMiddle(value: string, max: number) {
  if (value.length <= max) {
    return value;
  }
  const head = Math.ceil((max - 1) * 0.6);
  const tail = max - 1 - head;
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

// The full text rides along as a tooltip, but only when something was cut.
function monoSpan(text: string, full = text) {
  const title = full !== text ? ` title="${escapeHtml(full)}"` : "";
  return `<span class="mono"${title}>${escapeHtml(text)}</span>`;
}

function formatElapsed(ms: number) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/* ---------- context meter (mirrors the TUI status bar) ---------- */

function updateContextMeter(totalTokens: number) {
  const limit = currentSettings?.context_limit ?? 0;
  statusTokens.hidden = false;
  tokensLabel.textContent = totalTokens.toLocaleString();

  const fraction = limit > 0 ? Math.min(1, totalTokens / limit) : 0;
  const pct = fraction * 100;
  ctxFill.style.width = `${pct}%`;
  ctxFill.classList.toggle("warn", pct >= 70 && pct < 90);
  ctxFill.classList.toggle("hot", pct >= 90);
  ctxLabel.textContent = `CTX ${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
  statusTokens.title = limit > 0
    ? `${totalTokens.toLocaleString()} of ${limit.toLocaleString()} context tokens used this task`
    : `${totalTokens.toLocaleString()} tokens used this task`;
}

function updateEnergyMeter(display: string, totalJoules: number, lastCall = "") {
  statusEnergy.hidden = false;
  energyLabel.textContent = display || `${totalJoules.toFixed(1)} J`;
  statusEnergy.title = lastCall
    ? `Session energy: ${display || `${totalJoules.toFixed(1)} J`}. Last call: ${lastCall}`
    : `Session inference energy: ${display || `${totalJoules.toFixed(1)} J`}`;
}

/* ---------- presence ---------- */

function setPresence(state: "ready" | "working" | "attention", text: string) {
  presence.classList.toggle("working", state === "working");
  presence.classList.toggle("attention", state === "attention");
  presenceText.textContent = text;
}

/* ---------- messages & history ---------- */

function appendUserBubble(content: string, reqNumber: number) {
  messages.insertAdjacentHTML(
    "beforeend",
    `<article class="req-slip">
      <span class="eyebrow">${reqLabel(reqNumber)} / You</span>
      <div class="req-text">${escapeHtml(content)}</div>
    </article>`,
  );
  scrollToBottom(true);
}

function appendAssistantBubble(content: string, reqNumber: number) {
  messages.insertAdjacentHTML(
    "beforeend",
    `<article class="report-block">
      <span class="eyebrow"><span class="tick"></span>Report / ${reqLabel(reqNumber)}</span>
      <div class="prose">${renderMarkdown(content)}</div>
    </article>`,
  );
}

function reqLabel(reqNumber: number) {
  return `REQ-${String(reqNumber).padStart(2, "0")}`;
}

function renderChat(chatMessages: ChatMessage[]) {
  messages.innerHTML = "";
  let reqNumber = 0;
  for (const message of chatMessages) {
    if (message.role === "user") {
      reqNumber += 1;
      appendUserBubble(message.content, reqNumber);
    } else {
      appendAssistantBubble(message.content, Math.max(reqNumber, 1));
    }
  }
  scrollToBottom(true);
}

// Follow new content only when the person is already at the bottom.
// If they scrolled up to read, never yank the view; offer a way down instead.
function scrollToBottom(force = false) {
  if (force || stickToBottom) {
    conversation.scrollTop = conversation.scrollHeight;
    jumpLatest.hidden = true;
  } else {
    jumpLatest.hidden = false;
  }
}

function startNewChat() {
  if (isRunning) {
    return;
  }
  currentChatId = null;
  currentChatMessages = [];
  currentChatTokens = 0;
  stickToBottom = true;
  jumpLatest.hidden = true;
  // Anything already typed is a draft, not debris. Leave it in place.
  showGreeting();
  renderSessionList();
  if (window.innerWidth < 960) {
    appShell.classList.remove("rail-open");
  }
  promptInput.focus();
}

function saveCurrentChat(status: string) {
  if (!currentChatId || !currentChatMessages.length) {
    return;
  }
  const firstUserMessage =
    currentChatMessages.find((message) => message.role === "user")?.content ?? "Untitled task";
  const lastAssistantMessage =
    [...currentChatMessages].reverse().find((message) => message.role === "assistant")?.content ?? "";
  const item: SessionHistoryItem = {
    id: currentChatId,
    task: firstUserMessage,
    status,
    summary: lastAssistantMessage,
    createdAt: new Date().toISOString(),
    messages: currentChatMessages.map((message) => ({ ...message })),
    totalTokens: currentChatTokens,
  };

  sessionHistory = mergeSessionHistory([item], sessionHistory);
  localStorage.setItem(STORAGE_SESSIONS, JSON.stringify(sessionHistory));
  renderSessionList();
}

function loadSessionHistory() {
  try {
    const raw = localStorage.getItem(STORAGE_SESSIONS);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isSessionHistoryItem) : [];
  } catch {
    return [];
  }
}

function renderSessionList() {
  if (!sessionHistory.length) {
    sessionList.innerHTML = `<p class="empty-state">Nothing yet. Your finished tasks will collect here.</p>`;
    return;
  }

  sessionList.innerHTML = sessionHistory.map(renderSessionRow).join("");
  sessionList.querySelectorAll<HTMLButtonElement>(".session-item").forEach((button) => {
    button.addEventListener("click", () => {
      if (isRunning) {
        return;
      }
      const item = sessionHistory.find((session) => session.id === button.dataset.sessionId);
      if (!item) {
        return;
      }
      currentChatId = item.id;
      currentChatMessages = getSessionMessages(item);
      currentChatTokens = item.totalTokens ?? 0;
      renderChat(currentChatMessages);
      renderSessionList();
      if (window.innerWidth < 960) {
        appShell.classList.remove("rail-open");
      }
    });
  });
  sessionList.querySelectorAll<HTMLButtonElement>(".session-delete").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (isRunning) {
        return;
      }
      // First click arms the button; the second, while it's red, deletes.
      // A stray click should never cost anyone a whole conversation.
      if (!button.classList.contains("armed")) {
        button.classList.add("armed");
        button.title = "Click again to delete for good";
        window.setTimeout(() => {
          button.classList.remove("armed");
          button.title = "Delete this task";
        }, 2600);
        return;
      }
      void deleteSession(button.dataset.sessionId ?? "");
    });
  });
}

function renderSessionRow(item: SessionHistoryItem) {
  const title = item.task.trim() || "Untitled task";
  const titleAttr = title.length > 40 ? ` title="${escapeHtml(title)}"` : "";
  const tokens = item.totalTokens && item.totalTokens > 0 ? ` · ${formatCompactTokens(item.totalTokens)} tok` : "";

  return `
    <div class="session-row${item.id === currentChatId ? " active" : ""}">
      <button class="session-item" type="button" data-session-id="${escapeHtml(item.id)}"${titleAttr}>
        <span class="dot" data-status="${escapeHtml(item.status)}"></span>
        <span>${escapeHtml(title)}</span>
        <small>${escapeHtml(formatSessionTime(item.createdAt) + tokens)}</small>
      </button>
      <button class="session-delete" type="button" data-session-id="${escapeHtml(item.id)}" aria-label="Delete “${escapeHtml(truncate(title, 60))}”" title="Delete this task">
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4.5h10M6.25 4.5V3a1 1 0 0 1 1-1h1.5a1 1 0 0 1 1 1v1.5M4.5 4.5v8.75a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V4.5M6.5 7.25v3.5M9.5 7.25v3.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>
  `;
}

async function deleteSession(id: string) {
  if (!id) {
    return;
  }
  sessionHistory = sessionHistory.filter((item) => item.id !== id);
  localStorage.setItem(STORAGE_SESSIONS, JSON.stringify(sessionHistory));
  if (isTauriRuntime) {
    try {
      await invoke("delete_saved_session", { id });
    } catch {
      // The rail entry is already gone; a leftover backend file only means
      // the chat could reappear after a restart, which beats blocking delete.
    }
  }
  if (currentChatId === id) {
    currentChatId = null;
    currentChatMessages = [];
    currentChatTokens = 0;
    showGreeting();
  }
  renderSessionList();
}

// Today's work reads as a clock time; older work as a date. The year only
// appears once it actually disambiguates.
function formatSessionTime(iso: string) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const options: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  if (date.getFullYear() !== now.getFullYear()) {
    options.year = "numeric";
  }
  return date.toLocaleDateString([], options);
}

function isSessionHistoryItem(value: unknown): value is SessionHistoryItem {
  if (!value || typeof value !== "object") {
    return false;
  }
  const item = value as Record<string, unknown>;
  const hasRequiredFields =
    typeof item.id === "string" &&
    typeof item.task === "string" &&
    typeof item.status === "string" &&
    typeof item.summary === "string" &&
    typeof item.createdAt === "string";
  if (!hasRequiredFields) {
    return false;
  }
  if (item.totalTokens !== undefined && typeof item.totalTokens !== "number") {
    return false;
  }
  return item.messages === undefined || isChatMessageArray(item.messages);
}

function mergeSessionHistory(...groups: SessionHistoryItem[][]) {
  const byId = new Map<string, SessionHistoryItem>();
  groups.flat().forEach((item) => {
    if (item.id) {
      byId.set(item.id, item);
    }
  });

  return Array.from(byId.values())
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
    .slice(0, 50);
}

function getSessionMessages(item: SessionHistoryItem) {
  if (item.messages?.length) {
    return item.messages.map((message) => ({ ...message }));
  }
  return [
    { role: "user", content: item.task },
    { role: "assistant", content: item.summary },
  ] satisfies ChatMessage[];
}

function buildBackendPrompt(prompt: string) {
  if (!currentChatMessages.length) {
    return prompt;
  }

  const transcript = currentChatMessages
    .map((message) => `${message.role === "user" ? "User" : "AutoSE"}: ${message.content}`)
    .join("\n\n");
  return `Continue this chat. Use the previous conversation for context, then answer the user's new message.\n\nPrevious conversation:\n${transcript}\n\nNew user message:\n${prompt}`;
}

function createChatId() {
  return `desktop-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function isChatMessageArray(value: unknown): value is ChatMessage[] {
  return (
    Array.isArray(value) &&
    value.every(
      (message) =>
        message &&
        typeof message === "object" &&
        ((message as Record<string, unknown>).role === "user" ||
          (message as Record<string, unknown>).role === "assistant") &&
        typeof (message as Record<string, unknown>).content === "string",
    )
  );
}

/* ---------- the usage sheet ---------- */

// What the same tokens would have cost on hosted models, per million tokens.
// Sonnet 5 uses the Sep 2026 rate card so the comparison stays future-proof.
const RATES = {
  sonnet: () => ({ input: 3, output: 15 }),
  luna: () => ({ input: 1, output: 6 }),
};

function loadUsageRecords(): UsageRecord[] {
  try {
    const raw = localStorage.getItem(STORAGE_USAGE);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (record): record is UsageRecord =>
        record &&
        typeof record === "object" &&
        typeof record.t === "number" &&
        typeof record.prompt === "number" &&
        typeof record.completion === "number",
    );
  } catch {
    return [];
  }
}

function recordUsage(promptTokens: number, completionTokens: number) {
  const prompt = Math.max(0, Math.round(promptTokens));
  const completion = Math.max(0, Math.round(completionTokens));
  if (prompt + completion === 0) {
    return;
  }
  currentChatTokens += prompt + completion;
  const records = loadUsageRecords();
  records.push({ t: Date.now(), chatId: currentChatId ?? "", prompt, completion });
  localStorage.setItem(STORAGE_USAGE, JSON.stringify(records));
  if (!usageScreen.hidden) {
    renderUsageSheet();
  }
}

function modelCost(records: UsageRecord[], rate: () => { input: number; output: number }) {
  let input = 0;
  let output = 0;
  const { input: inRate, output: outRate } = rate();
  for (const record of records) {
    input += (record.prompt / 1_000_000) * inRate;
    output += (record.completion / 1_000_000) * outRate;
  }
  return { input, output, total: input + output };
}

function formatMoney(value: number) {
  if (value === 0) {
    return "$0.00";
  }
  if (value < 0.01) {
    return "< $0.01";
  }
  if (value >= 1000) {
    return `$${Math.round(value).toLocaleString()}`;
  }
  return `$${value.toFixed(2)}`;
}

function formatCompactTokens(value: number) {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`;
  }
  return String(value);
}

function startOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

// Local-time day key, so a late evening run lands on the day you remember.
function dayKey(at: number) {
  const date = new Date(at);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function openUsage() {
  usageOpener =
    document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null;
  usageSkipWeekends.checked = localStorage.getItem(STORAGE_USAGE_WEEKENDS) === "skip";
  calMonth = startOfMonth(new Date());
  if (!usageFrom.value || !usageTo.value) {
    const today = dayKey(Date.now());
    usageFrom.value = usageFrom.value || today;
    usageTo.value = usageTo.value || today;
  }
  renderUsageSheet();
  usageScreen.hidden = false;
  usageCloseButton.focus();
}

function closeUsage() {
  usageScreen.hidden = true;
  (usageOpener ?? promptInput).focus();
  usageOpener = null;
}

function rangeBounds(range: string): { from: number; to: number } {
  const now = Date.now();
  const HOUR = 3_600_000;
  switch (range) {
    case "1h":
      return { from: now - HOUR, to: now };
    case "1d":
      return { from: now - 24 * HOUR, to: now };
    case "1w":
      return { from: now - 7 * 24 * HOUR, to: now };
    case "1m":
      return { from: now - 30 * 24 * HOUR, to: now };
    case "1y":
      return { from: now - 365 * 24 * HOUR, to: now };
    case "custom": {
      const from = usageFrom.value ? new Date(`${usageFrom.value}T00:00:00`).getTime() : 0;
      const toDay = usageTo.value ? new Date(`${usageTo.value}T00:00:00`).getTime() : now;
      // The "to" day counts in full, up to its last millisecond.
      return { from, to: toDay + 24 * HOUR - 1 };
    }
    default:
      return { from: 0, to: now };
  }
}

function rangeLabel(range: string) {
  switch (range) {
    case "1h":
      return "the last hour";
    case "1d":
      return "the last 24 hours";
    case "1w":
      return "the last 7 days";
    case "1m":
      return "the last 30 days";
    case "1y":
      return "the last year";
    case "custom":
      return "the chosen dates";
    default:
      return "all time";
  }
}

function renderUsageSheet() {
  usageScreen.querySelectorAll<HTMLButtonElement>(".range-chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.range === usageRange);
  });
  usageCustom.hidden = usageRange !== "custom";

  const records = loadUsageRecords();
  const { from, to } = rangeBounds(usageRange);
  const inRange = records.filter((record) => record.t >= from && record.t <= to);

  const prompt = inRange.reduce((sum, record) => sum + record.prompt, 0);
  const completion = inRange.reduce((sum, record) => sum + record.completion, 0);
  const total = prompt + completion;
  const allTimeTotal = records.reduce((sum, record) => sum + record.prompt + record.completion, 0);

  const stat = (label: string, value: string, sub = "") =>
    `<div class="stat"><span class="stat-label">${label}</span><span class="stat-value">${value}</span>${sub ? `<span class="stat-sub">${sub}</span>` : ""}</div>`;

  if (records.length === 0) {
    usageStats.innerHTML = `<p class="usage-empty">Nothing on the meter yet. It starts ticking with your first requirement.</p>`;
    usageCosts.innerHTML = "";
  } else if (total === 0) {
    usageStats.innerHTML =
      `<p class="usage-empty">Quiet. No tokens in ${rangeLabel(usageRange)}. All time: ${Number(allTimeTotal).toLocaleString()} tokens.</p>`;
    usageCosts.innerHTML = "";
  } else {
    usageStats.innerHTML =
      stat("Tokens", total.toLocaleString(), rangeLabel(usageRange)) +
      stat("Input", prompt.toLocaleString(), "sent to the model") +
      stat("Output", completion.toLocaleString(), "written back") +
      stat("Runs", inRange.length.toLocaleString(), allTimeTotal !== total ? `of ${records.length} all time` : "all of them");

    const sonnet = modelCost(inRange, RATES.sonnet);
    const luna = modelCost(inRange, RATES.luna);
    const costRow = (name: string, cost: { input: number; output: number; total: number }) => `
      <div class="cost-row">
        <span class="cost-model">${name}</span>
        <span class="cost-split">IN ${formatMoney(cost.input)} · OUT ${formatMoney(cost.output)}</span>
        <span class="cost-total">${formatMoney(cost.total)}</span>
      </div>`;
    usageCosts.innerHTML = `
      <h4 class="eyebrow">If these tokens had a bill</h4>
      ${costRow("Claude Sonnet 5", sonnet)}
      ${costRow("GPT 5.6 Luna", luna)}
    `;
  }

  renderUsageCalendar();
  renderUsageStreaks();
}

function renderUsageCalendar() {
  const records = loadUsageRecords();
  const byDay = new Map<string, { tokens: number; runs: number }>();
  for (const record of records) {
    const key = dayKey(record.t);
    const entry = byDay.get(key) ?? { tokens: 0, runs: 0 };
    entry.tokens += record.prompt + record.completion;
    entry.runs += 1;
    byDay.set(key, entry);
  }

  calTitle.textContent = calMonth.toLocaleDateString([], { month: "long", year: "numeric" });
  const nextMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1);
  calNext.disabled = nextMonth > new Date();

  const daysInMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0).getDate();
  let monthMax = 0;
  for (let day = 1; day <= daysInMonth; day += 1) {
    const key = dayKey(new Date(calMonth.getFullYear(), calMonth.getMonth(), day).getTime());
    monthMax = Math.max(monthMax, byDay.get(key)?.tokens ?? 0);
  }

  const todayKey = dayKey(Date.now());
  const cells: string[] = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"].map(
    (name) => `<span class="cal-dow">${name}</span>`,
  );
  // Weeks start on Monday; getDay() has Sunday at 0.
  const lead = (calMonth.getDay() + 6) % 7;
  for (let pad = 0; pad < lead; pad += 1) {
    cells.push(`<span class="cal-day blank"></span>`);
  }
  for (let day = 1; day <= daysInMonth; day += 1) {
    const date = new Date(calMonth.getFullYear(), calMonth.getMonth(), day);
    const key = dayKey(date.getTime());
    const entry = byDay.get(key);
    const classes = ["cal-day"];
    let title = date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    if (entry) {
      const level = monthMax > 0 ? Math.min(3, Math.ceil((entry.tokens / monthMax) * 3)) : 1;
      classes.push(`used lv${level}`);
      title += ` · ${entry.tokens.toLocaleString()} tokens · ${entry.runs} run${entry.runs === 1 ? "" : "s"}`;
    } else if (key < todayKey || (key === todayKey && !entry)) {
      title += " · idle";
    }
    if (key === todayKey) {
      classes.push("today");
    }
    if (key > todayKey) {
      classes.push("future");
    }
    cells.push(`<span class="${classes.join(" ")}" title="${escapeHtml(title)}">${day}</span>`);
  }
  usageCalendar.innerHTML = cells.join("");
}

function renderUsageStreaks() {
  const skipWeekends = usageSkipWeekends.checked;
  const records = loadUsageRecords();
  const usedDays = new Set(records.map((record) => dayKey(record.t)));
  const { current, best } = computeStreaks(usedDays, skipWeekends);

  const dayWord = (count: number) =>
    skipWeekends ? `working day${count === 1 ? "" : "s"}` : `day${count === 1 ? "" : "s"}`;
  const stat = (label: string, value: string, sub: string) =>
    `<div class="stat"><span class="stat-label">${label}</span><span class="stat-value">${value}</span><span class="stat-sub">${sub}</span></div>`;

  if (usedDays.size === 0) {
    usageStreaks.innerHTML = `<p class="usage-empty">No streak to speak of. Day one is one requirement away.</p>`;
    return;
  }
  usageStreaks.innerHTML =
    stat("Current", String(current), current > 0 ? `${dayWord(current)} and counting` : "the chain is open") +
    stat("Best", String(best), `${dayWord(best)} in a row`) +
    stat("Days used", String(usedDays.size), "since the first run");
}

function computeStreaks(usedDays: Set<string>, skipWeekends: boolean) {
  if (usedDays.size === 0) {
    return { current: 0, best: 0 };
  }
  const isWeekend = (date: Date) => date.getDay() === 0 || date.getDay() === 6;
  const counts = (date: Date) => !(skipWeekends && isWeekend(date));

  const firstKey = [...usedDays].sort()[0];
  const first = new Date(`${firstKey}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  let best = 0;
  let streak = 0;
  for (const cursor = new Date(first); cursor <= today; cursor.setDate(cursor.getDate() + 1)) {
    if (!counts(cursor)) {
      continue;
    }
    if (usedDays.has(dayKey(cursor.getTime()))) {
      streak += 1;
      best = Math.max(best, streak);
    } else {
      streak = 0;
    }
  }

  // The current streak forgives an unused today: the day is not over yet.
  let current = 0;
  const cursor = new Date(today);
  if (counts(cursor) && !usedDays.has(dayKey(cursor.getTime()))) {
    cursor.setDate(cursor.getDate() - 1);
  }
  for (; ;) {
    if (!counts(cursor)) {
      cursor.setDate(cursor.getDate() - 1);
      continue;
    }
    if (!usedDays.has(dayKey(cursor.getTime()))) {
      break;
    }
    current += 1;
    cursor.setDate(cursor.getDate() - 1);
  }
  return { current, best };
}

/* ---------- markdown ---------- */

function renderMarkdown(value: string) {
  const lines = value.replaceAll("\r\n", "\n").split("\n");
  const html: string[] = [];
  let inList = false;
  let inCode = false;
  let codeLines: string[] = [];

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
        html.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }

    if (inCode) {
      codeLines.push(escapeHtml(line));
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      continue;
    }

    // A table starts with a header row followed by a |---|---| separator.
    if (isTableRow(trimmed) && isTableSeparator(lines[index + 1] ?? "")) {
      closeList();
      const header = splitTableRow(trimmed);
      const aligns = parseTableAligns(lines[index + 1].trim(), header.length);
      const bodyRows: string[][] = [];
      let cursor = index + 2;
      while (cursor < lines.length && isTableRow(lines[cursor].trim())) {
        bodyRows.push(splitTableRow(lines[cursor].trim()));
        cursor += 1;
      }
      html.push(renderTable(header, aligns, bodyRows));
      index = cursor - 1;
      continue;
    }

    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
      continue;
    }

    closeList();
    html.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
  }

  closeList();
  if (inCode) {
    html.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
  }

  return html.join("");
}

function renderInlineMarkdown(value: string) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return html;
}

/* ---------- markdown tables ---------- */

function isTableRow(line: string) {
  return line.startsWith("|") && line.includes("|", 1);
}

function isTableSeparator(line: string) {
  const trimmed = line.trim();
  if (!isTableRow(trimmed)) {
    return false;
  }
  const cells = splitTableRow(trimmed);
  return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell.replaceAll(" ", "")));
}

function splitTableRow(line: string) {
  let body = line.trim();
  if (body.startsWith("|")) {
    body = body.slice(1);
  }
  if (body.endsWith("|")) {
    body = body.slice(0, -1);
  }
  // Split on unescaped pipes so `a \| b` stays one cell.
  return body
    .split(/(?<!\\)\|/)
    .map((cell) => cell.trim().replaceAll("\\|", "|"));
}

function parseTableAligns(separator: string, columns: number): Array<"left" | "center" | "right"> {
  const cells = splitTableRow(separator);
  return Array.from({ length: columns }, (_, column) => {
    const cell = (cells[column] ?? "").replaceAll(" ", "");
    if (cell.startsWith(":") && cell.endsWith(":")) {
      return "center";
    }
    if (cell.endsWith(":")) {
      return "right";
    }
    return "left";
  });
}

function renderTable(
  header: string[],
  aligns: Array<"left" | "center" | "right">,
  rows: string[][],
) {
  const alignAttr = (column: number) =>
    aligns[column] && aligns[column] !== "left" ? ` class="align-${aligns[column]}"` : "";
  const head = header
    .map((cell, column) => `<th${alignAttr(column)}>${renderInlineMarkdown(cell)}</th>`)
    .join("");
  const body = rows
    .map(
      (row) =>
        `<tr>${header
          .map((_, column) => `<td${alignAttr(column)}>${renderInlineMarkdown(row[column] ?? "")}</td>`)
          .join("")}</tr>`,
    )
    .join("");
  return `<div class="table-scroll"><table><thead><tr>${head}</tr></thead>${body ? `<tbody>${body}</tbody>` : ""}</table></div>`;
}

function escapeHtml(value: unknown) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/* ---------- deep links ---------- */

// Last on purpose: openUsage touches consts declared through the module,
// and running any earlier would hit them before initialization.
if (window.location.hash === "#usage" || new URLSearchParams(window.location.search).has("usage")) {
  openUsage();
}
