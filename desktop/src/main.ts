import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
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
  };
  messages: Array<Record<string, unknown>>;
};

type TaskResponse = {
  payload: SessionPayload;
  stderr: string;
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
};

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

const isTauriRuntime = Boolean(window.__TAURI_INTERNALS__);

const app = document.querySelector<HTMLDivElement>("#app");
if (!app) {
  throw new Error("Missing #app root");
}

app.innerHTML = `
  <main class="app-shell" id="app-shell">
    <aside class="session-drawer" id="session-drawer" aria-label="Previous sessions">
      <div class="drawer-head">
        <strong>Chats</strong>
        <button id="close-sidebar" class="icon-button" type="button" aria-label="Close chats">×</button>
      </div>
      <button id="new-chat" class="new-chat-button" type="button">New chat</button>
      <div class="session-list" id="session-list">
        <p class="empty-state">No previous chats yet.</p>
      </div>
    </aside>

    <header class="app-header">
      <button id="open-sidebar" class="icon-button" type="button" aria-label="Open chats">☰</button>
      <strong class="brand">AutoSE</strong>
      <div class="status-pill" id="status-pill" hidden></div>
    </header>

    <section class="conversation" aria-live="polite">
      <div id="messages" class="messages">
        <p class="empty-chat">What would you like AutoSE to do?</p>
      </div>
    </section>

    <section class="composer" aria-label="Task composer">
      <textarea id="prompt" spellcheck="true" aria-label="Prompt" placeholder="Message AutoSE..."></textarea>
      <button id="run-button" class="run-button" type="button">Run</button>
    </section>
  </main>
`;

const promptInput = document.querySelector<HTMLTextAreaElement>("#prompt")!;
const runButton = document.querySelector<HTMLButtonElement>("#run-button")!;
const statusPill = document.querySelector<HTMLDivElement>("#status-pill")!;
const messages = document.querySelector<HTMLDivElement>("#messages")!;
const appShell = document.querySelector<HTMLElement>("#app-shell")!;
const sessionList = document.querySelector<HTMLDivElement>("#session-list")!;
const openSidebarButton = document.querySelector<HTMLButtonElement>("#open-sidebar")!;
const closeSidebarButton = document.querySelector<HTMLButtonElement>("#close-sidebar")!;
const newChatButton = document.querySelector<HTMLButtonElement>("#new-chat")!;

const selectedMode: Mode = "auto";
const sessionStorageKey = "autose.desktop.sessions";
let workspaceRoot = "";
let liveEvents: SessionEvent[] = [];
let sessionHistory: SessionHistoryItem[] = loadSessionHistory();
let unlistenAutoseEvent: UnlistenFn | null = null;
let isRunning = false;
let currentChatId: string | null = null;
let currentChatMessages: ChatMessage[] = [];

renderSessionList();

if (isTauriRuntime) {
  void hydrateDefaultWorkspace();
  void hydrateSavedSessions();
  void subscribeToAutoseEvents();
} else {
  runButton.disabled = true;
  showStatus("Preview", "");
  renderNotice("Open the desktop app to run AutoSE.");
}

openSidebarButton.addEventListener("click", () => {
  appShell.classList.add("drawer-open");
});

closeSidebarButton.addEventListener("click", () => {
  appShell.classList.remove("drawer-open");
});

newChatButton.addEventListener("click", () => {
  startNewChat();
});

runButton.addEventListener("click", () => {
  void runTask();
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    void runTask();
  }
});

async function hydrateDefaultWorkspace() {
  try {
    workspaceRoot = await invoke<string>("default_workspace");
  } catch {
    workspaceRoot = "";
  }
}

async function hydrateSavedSessions() {
  try {
    const savedSessions = await invoke<SessionHistoryItem[]>("list_saved_sessions");
    sessionHistory = mergeSessionHistory(savedSessions, sessionHistory);
    localStorage.setItem(sessionStorageKey, JSON.stringify(sessionHistory));
    renderSessionList();
  } catch {
    renderSessionList();
  }
}

async function subscribeToAutoseEvents() {
  unlistenAutoseEvent = await listen<SessionEvent>("autose-event", (event) => {
    appendLiveEvent(event.payload);
  });
}

async function runTask() {
  if (isRunning) {
    return;
  }

  if (!isTauriRuntime) {
    setError(
      "Task execution requires the Tauri desktop runtime. Start the app with `npm run dev` and use the desktop window, not the browser preview.",
    );
    return;
  }

  const prompt = promptInput.value.trim();
  if (!prompt) {
    setError("Task prompt is required.");
    return;
  }

  if (!currentChatId) {
    currentChatId = createChatId();
  }
  currentChatMessages.push({ role: "user", content: prompt });
  const backendPrompt = buildBackendPrompt(prompt);
  currentChatMessages.push({
    role: "assistant",
    content: "AutoSE is working on your request...",
  });

  setBusy(true);
  liveEvents = [];
  renderMessages(currentChatMessages);
  promptInput.value = "";

  try {
    const response = await invoke<TaskResponse>("run_autose", {
      request: {
        prompt: backendPrompt,
        mode: selectedMode,
        workspace: workspaceRoot,
        autoApprove: false,
      },
    });
    renderSession(response.payload);
  } catch (error) {
    setError(error instanceof Error ? error.message : String(error));
  } finally {
    setBusy(false);
  }
}

function appendLiveEvent(event: SessionEvent) {
  liveEvents.push(event);

  if (event.type === "assistant_chunk") {
    const content = String(event.data?.content ?? "");
    appendAssistantContent(content);
  }

  if (event.type === "tokens_updated") {
    return;
  }

  if (event.type === "session_completed") {
    showStatus("Done", "completed");
  }

  if (event.type === "session_failed") {
    showStatus("Failed", "failed");
  }
}

function renderSession(payload: SessionPayload) {
  const isCompleted = payload.result.status === "completed";
  showStatus(isCompleted ? "Done" : humanize(payload.result.status), payload.result.status);
  setAssistantContent(payload.result.summary || "Completed without a summary.");
  saveCurrentChat(payload.result.status);
}

function setBusy(isBusy: boolean) {
  isRunning = isBusy;
  runButton.disabled = isBusy;
  runButton.classList.toggle("loading", isBusy);
  runButton.setAttribute("aria-busy", String(isBusy));
  runButton.textContent = isBusy ? "" : "Run";
  if (isBusy) {
    hideStatus();
  }
}

function setError(message: string) {
  showStatus("Failed", "failed");
  setAssistantContent(message);
}

function humanize(value: string) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function saveCurrentChat(status: string) {
  if (!currentChatId || !currentChatMessages.length) {
    return;
  }
  const firstUserMessage =
    currentChatMessages.find((message) => message.role === "user")?.content ?? "Untitled chat";
  const lastAssistantMessage =
    [...currentChatMessages].reverse().find((message) => message.role === "assistant")?.content ??
    "Completed without a summary.";
  const existing = sessionHistory.find((session) => session.id === currentChatId);
  const item: SessionHistoryItem = {
    id: currentChatId,
    task: firstUserMessage,
    status,
    summary: lastAssistantMessage,
    createdAt: new Date().toISOString(),
    messages: currentChatMessages.map((message) => ({ ...message })),
  };

  sessionHistory = mergeSessionHistory([item], sessionHistory);
  localStorage.setItem(sessionStorageKey, JSON.stringify(sessionHistory));
  renderSessionList();
}

function loadSessionHistory() {
  try {
    const raw = localStorage.getItem(sessionStorageKey);
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
    sessionList.innerHTML = `<p class="empty-state">No previous chats yet.</p>`;
    return;
  }

  sessionList.innerHTML = sessionHistory.map(renderSessionButton).join("");
  sessionList.querySelectorAll<HTMLButtonElement>(".session-item").forEach((button) => {
    button.addEventListener("click", () => {
      const item = sessionHistory.find((session) => session.id === button.dataset.sessionId);
      if (!item) {
        return;
      }
      currentChatId = item.id;
      currentChatMessages = getSessionMessages(item);
      renderMessages(currentChatMessages);
      statusPill.textContent = item.status === "completed" ? "Done" : humanize(item.status);
      statusPill.dataset.status = item.status;
      statusPill.hidden = false;
      appShell.classList.remove("drawer-open");
    });
  });
}

function startNewChat() {
  currentChatId = null;
  currentChatMessages = [];
  promptInput.value = "";
  messages.innerHTML = `<p class="empty-chat">What would you like AutoSE to do?</p>`;
  hideStatus();
  appShell.classList.remove("drawer-open");
  promptInput.focus();
}

function renderMessages(chatMessages: ChatMessage[]) {
  messages.innerHTML = chatMessages.map((message) => renderMessage(message.role, message.content)).join("");
  scrollConversationToBottom();
}

function renderNotice(message: string) {
  messages.innerHTML = renderMessage("assistant", message);
}

function renderMessage(role: "user" | "assistant", content: string) {
  const renderedContent = role === "assistant" ? renderMarkdown(content) : escapeHtml(content);
  return `
    <article class="message ${role}">
      <div class="message-role">${role === "user" ? "You" : "AutoSE"}</div>
      <div class="message-content">${renderedContent}</div>
    </article>
  `;
}

function appendAssistantContent(content: string) {
  const assistantMessage = lastAssistantElement();
  if (!assistantMessage) {
    messages.insertAdjacentHTML("beforeend", renderMessage("assistant", content));
    currentChatMessages.push({ role: "assistant", content });
    scrollConversationToBottom();
    return;
  }
  if (assistantMessage.textContent === "AutoSE is working on your request...") {
    assistantMessage.dataset.rawContent = "";
  }
  const rawContent = `${assistantMessage.dataset.rawContent ?? assistantMessage.textContent ?? ""}${content}`;
  assistantMessage.dataset.rawContent = rawContent;
  assistantMessage.innerHTML = renderMarkdown(rawContent);
  const lastAssistant = findLastAssistantMessage();
  if (lastAssistant) {
    lastAssistant.content = rawContent;
  }
  scrollConversationToBottom();
}

function setAssistantContent(content: string) {
  const assistantMessage = lastAssistantElement();
  if (!assistantMessage) {
    messages.insertAdjacentHTML("beforeend", renderMessage("assistant", content));
    currentChatMessages.push({ role: "assistant", content });
    scrollConversationToBottom();
    return;
  }
  assistantMessage.dataset.rawContent = content;
  assistantMessage.innerHTML = renderMarkdown(content);
  const lastAssistant = findLastAssistantMessage();
  if (lastAssistant) {
    lastAssistant.content = content;
  }
  scrollConversationToBottom();
}

function scrollConversationToBottom() {
  const conversation = messages.closest<HTMLElement>(".conversation");
  if (conversation) {
    conversation.scrollTop = conversation.scrollHeight;
  }
}

function showStatus(label: string, status: string) {
  statusPill.textContent = label;
  statusPill.dataset.status = status;
  statusPill.hidden = false;
}

function hideStatus() {
  statusPill.textContent = "";
  statusPill.removeAttribute("data-status");
  statusPill.hidden = true;
}

function renderSessionButton(item: SessionHistoryItem) {
  const title = item.task.trim() || "Untitled chat";
  const time = new Date(item.createdAt).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return `
    <button class="session-item" type="button" data-session-id="${escapeHtml(item.id)}">
      <span>${escapeHtml(title)}</span>
      <small>${escapeHtml(time)}</small>
    </button>
  `;
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
  const previousMessages = currentChatMessages.slice(0, -1);
  if (!previousMessages.length) {
    return prompt;
  }

  const transcript = previousMessages
    .map((message) => `${message.role === "user" ? "User" : "AutoSE"}: ${message.content}`)
    .join("\n\n");
  return `Continue this chat. Use the previous conversation for context, then answer the user's new message.\n\nPrevious conversation:\n${transcript}\n\nNew user message:\n${prompt}`;
}

function lastAssistantElement() {
  const assistantMessages = messages.querySelectorAll<HTMLElement>(".message.assistant .message-content");
  return assistantMessages.item(assistantMessages.length - 1) || null;
}

function findLastAssistantMessage() {
  for (let index = currentChatMessages.length - 1; index >= 0; index -= 1) {
    if (currentChatMessages[index].role === "assistant") {
      return currentChatMessages[index];
    }
  }
  return null;
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
        (message as Record<string, unknown>).role !== undefined &&
        ((message as Record<string, unknown>).role === "user" ||
          (message as Record<string, unknown>).role === "assistant") &&
        typeof (message as Record<string, unknown>).content === "string",
    )
  );
}

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

  for (const line of lines) {
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

function escapeHtml(value: unknown) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
