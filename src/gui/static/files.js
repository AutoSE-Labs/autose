/* The Files view: the workspace as a read-only editor (DR-051).
 *
 * - A tree of every file on the left, folders folding, a filter on top; files
 *   this session changed carry a dot.
 * - Opened files as tabs, each shown with line numbers and light colouring.
 * - Watch re-reads the open file every few seconds and marks the lines that
 *   changed, so a run can be followed as it writes.
 * - Anywhere else on the page, a file name that exists in the workspace is a
 *   link (`.file-ref`) that opens it here: in a command waiting for approval,
 *   in the step line, in Needs you.
 *
 * Reads `/api/files` and `/api/file`; nothing here writes.
 */

const WATCH_MS = 3000;
const PLAIN_OVER_LINES = 6000;

const filesView = {
  index: new Set(), // every listed path, for linking names elsewhere
  list: [],
  truncated: false,
  root: "",
  expanded: new Set(),
  filter: "",
  tabs: [], // [{path, text, size, binary, truncated, fresh: Set<line>}]
  active: "",
  watch: false,
  timer: 0,
  marked: 0,
};

/* ---------- the index, for linking file names anywhere ---------- */

async function refreshFileIndex() {
  try {
    const payload = await api("/api/files");
    filesView.list = payload.files || [];
    filesView.truncated = Boolean(payload.truncated);
    filesView.root = payload.root || "";
    filesView.index = new Set(filesView.list.map((item) => item.path));
    if (!overlays.files.hidden) renderFileTree();
  } catch (error) {
    // The page works without links; the next turn tries again.
  }
}

function knownFile(token) {
  const path = token.replace(/^\.\//, "");
  return filesView.index.has(path) ? path : "";
}

/* `text` with every word that names a workspace file made a link to it. */
function linkifyPaths(text) {
  return String(text)
    .split(/([\s'"`=:,;()[\]{}<>|&]+)/)
    .map((part) => {
      const path = part && knownFile(part);
      return path
        ? `<button type="button" class="file-ref" data-open-file="${escapeHtml(path)}" title="Open ${escapeHtml(path)}">${escapeHtml(part)}</button>`
        : escapeHtml(part);
    })
    .join("");
}

/* A path a tool named, as a link whether or not it exists yet: a file being
 * written is worth opening once it is. */
function fileRefMarkup(path, shown) {
  return `<button type="button" class="file-ref" data-open-file="${escapeHtml(path.replace(/^\.\//, ""))}" title="Open ${escapeHtml(path)}">${escapeHtml(shown || path)}</button>`;
}

/* Paths this session's runs changed, for the tree's dots. */
function changedPaths() {
  const paths = new Set();
  ((state && state.history) || []).forEach((turn) => (turn.changes || []).forEach((change) => paths.add(change.path)));
  const run = focusedRun();
  if (run) {
    run.steps
      .filter((step) => step.type === "tool" && ["write_file", "edit_file"].includes(step.tool) && step.args && step.args.path)
      .forEach((step) => paths.add(String(step.args.path).replace(/^\.\//, "")));
  }
  return paths;
}

/* ---------- opening ---------- */

async function openFilesView(path, line) {
  overlays.files.hidden = false;
  if (!filesView.list.length) await refreshFileIndex();
  renderFileTree();
  if (path) await openFile(path, line);
  else renderFileTabs();
  if (!path) el("files-filter").focus({ preventScroll: true });
}

async function openFile(path, line) {
  let tab = filesView.tabs.find((item) => item.path === path);
  if (!tab) {
    tab = { path, text: "", size: 0, binary: false, truncated: false, fresh: new Set(), error: "" };
    filesView.tabs.push(tab);
    await loadTab(tab);
  }
  filesView.active = path;
  // Reveal it in the tree: every folder above it opens.
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) filesView.expanded.add(parts.slice(0, i).join("/"));
  renderFileTree();
  renderFileTabs();
  renderOpenFile(line);
}

async function loadTab(tab) {
  try {
    const shown = await api(`/api/file?path=${encodeURIComponent(tab.path)}`);
    const before = tab.text;
    Object.assign(tab, shown, { error: "" });
    tab.fresh = before && before !== tab.text ? changedLines(before, tab.text) : new Set();
    tab.freshAt = Date.now();
  } catch (error) {
    tab.error = String(error.message || error);
  }
}

/* Lines of `after` that differ from the same line of `before`. Cheap, and
 * honest enough for "what moved since the last look". */
function changedLines(before, after) {
  const old = before.split("\n");
  const fresh = new Set();
  after.split("\n").forEach((line, index) => {
    if (old[index] !== line) fresh.add(index + 1);
  });
  return fresh;
}

/* ---------- the tree ---------- */

function renderFileTree() {
  const tree = el("files-tree");
  const changed = changedPaths();
  const needle = filesView.filter.toLowerCase();
  el("files-root").textContent = shortenPath(filesView.root || (state && state.workspace) || "");
  el("files-count").textContent = `${filesView.list.length}${filesView.truncated ? "+" : ""} files`;
  if (needle) {
    const hits = filesView.list.filter((item) => item.path.toLowerCase().includes(needle)).slice(0, 400);
    tree.innerHTML = hits.length
      ? hits.map((item) => fileRowMarkup(item.path, item.path, 0, changed.has(item.path))).join("")
      : `<p class="panel-note">No file matches.</p>`;
    return;
  }
  // Folders first, then files, at every level.
  const root = { dirs: new Map(), files: [] };
  filesView.list.forEach((item) => {
    const parts = item.path.split("/");
    let node = root;
    parts.slice(0, -1).forEach((part) => {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    });
    node.files.push(item.path);
  });
  const rows = [];
  const walk = (node, prefix, depth) => {
    [...node.dirs.keys()].sort().forEach((name) => {
      const path = prefix ? `${prefix}/${name}` : name;
      const open = filesView.expanded.has(path);
      const dirty = [...changed].some((item) => item.startsWith(`${path}/`));
      rows.push(
        `<button type="button" class="tree-row folder${open ? " open" : ""}" data-folder="${escapeHtml(path)}">` +
        `<span class="tree-indent" data-depth="${Math.min(depth, 8)}"></span><span class="tree-caret">${open ? "▾" : "▸"}</span>` +
        `<span class="tree-name">${escapeHtml(name)}</span>${dirty ? `<span class="tree-dot" title="Changed in this session"></span>` : ""}</button>`
      );
      if (open) walk(node.dirs.get(name), path, depth + 1);
    });
    node.files.sort().forEach((path) => rows.push(fileRowMarkup(path, path.split("/").pop(), depth, changed.has(path))));
  };
  walk(root, "", 0);
  tree.innerHTML = rows.join("") || `<p class="panel-note">This workspace has no files yet.</p>`;
}

function fileRowMarkup(path, shown, depth, dirty) {
  return (
    `<button type="button" class="tree-row file${path === filesView.active ? " on" : ""}" data-tree-file="${escapeHtml(path)}" title="${escapeHtml(path)}">` +
    `<span class="tree-indent" data-depth="${Math.min(depth, 8)}"></span><span class="tree-icon ${escapeHtml(fileLanguage(path))}"></span>` +
    `<span class="tree-name">${escapeHtml(shown)}</span>${dirty ? `<span class="tree-dot" title="Changed in this session"></span>` : ""}</button>`
  );
}

/* ---------- tabs and the open file ---------- */

function renderFileTabs() {
  el("files-tabs").innerHTML = filesView.tabs
    .map(
      (tab) =>
        `<div class="file-tab${tab.path === filesView.active ? " on" : ""}">` +
        `<button type="button" class="file-tab-name" data-tab="${escapeHtml(tab.path)}" title="${escapeHtml(tab.path)}">${escapeHtml(tab.path.split("/").pop())}</button>` +
        `<button type="button" class="file-tab-close" data-close-tab="${escapeHtml(tab.path)}" aria-label="Close ${escapeHtml(tab.path)}">×</button></div>`
    )
    .join("");
  if (!filesView.tabs.length) {
    el("files-head").innerHTML = "";
    el("files-code").innerHTML = `<div class="files-empty"><svg aria-hidden="true"><use href="#nib" /></svg><p>Pick a file on the left, or click a file name anywhere on the page.</p></div>`;
  }
}

function renderOpenFile(line) {
  const tab = filesView.tabs.find((item) => item.path === filesView.active);
  if (!tab) return renderFileTabs();
  const parts = tab.path.split("/");
  const lines = tab.text ? tab.text.split("\n") : [];
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const changed = changedPaths().has(tab.path);
  el("files-head").innerHTML =
    `<span class="files-crumbs">${parts.map((part, i) => `<span class="${i === parts.length - 1 ? "leaf" : ""}">${escapeHtml(part)}</span>`).join(`<span class="sep">/</span>`)}</span>` +
    (changed ? `<span class="diff-kind">changed this session</span>` : "") +
    `<span class="files-meta">${tab.binary ? "binary" : `${plural(lines.length, "line")}`} · ${formatBytes(tab.size)}${tab.truncated ? " · first 1 MB shown" : ""}</span>` +
    `<button type="button" class="ghost-button small" data-copy-path="${escapeHtml(tab.path)}">Copy path</button>`;
  const code = el("files-code");
  if (tab.error) {
    code.innerHTML = `<p class="panel-note files-note">${escapeHtml(tab.error)}</p>`;
    return;
  }
  if (tab.binary) {
    code.innerHTML = `<p class="panel-note files-note">This is a binary file; there is no text to show.</p>`;
    return;
  }
  const language = fileLanguage(tab.path);
  const plain = lines.length > PLAIN_OVER_LINES;
  const fresh = Date.now() - (tab.freshAt || 0) < WATCH_MS * 2 ? tab.fresh : new Set();
  code.innerHTML =
    `<table class="code-table"><tbody>` +
    lines
      .map(
        (text, index) =>
          `<tr id="code-line-${index + 1}"${fresh.has(index + 1) ? ` class="fresh"` : ""}><td class="ln" data-line="${index + 1}">${index + 1}</td>` +
          `<td class="src">${plain ? escapeHtml(text) : highlightLine(text, language)}${text ? "" : " "}</td></tr>`
      )
      .join("") +
    `</tbody></table>` +
    (lines.length ? "" : `<p class="panel-note files-note">This file is empty.</p>`);
  if (line) {
    const row = el(`code-line-${line}`);
    if (row) {
      row.classList.add("marked");
      row.scrollIntoView({ block: "center" });
    }
  } else if (!fresh.size) {
    code.scrollTop = 0;
  }
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

/* ---------- colouring ---------- */

const LANGUAGES = {
  py: "python", pyi: "python",
  js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js",
  json: "json", css: "css", scss: "css", html: "html", htm: "html", xml: "html", svg: "html",
  sh: "shell", bash: "shell", zsh: "shell",
  yml: "yaml", yaml: "yaml", toml: "toml", ini: "toml", cfg: "toml",
  md: "markdown", txt: "text", rst: "markdown",
  go: "c", rs: "c", java: "c", c: "c", h: "c", cpp: "c", hpp: "c", cs: "c", kt: "c", swift: "c",
  rb: "python", sql: "sql",
};

function fileLanguage(path) {
  const name = path.split("/").pop().toLowerCase();
  if (name === "dockerfile" || name === "makefile") return "shell";
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return LANGUAGES[ext] || "text";
}

const KEYWORDS = {
  python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
  js: "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements",
  c: "break case char class const continue default do double else enum extern false float fn for func go if impl import int let long match mod mut new nil null package pub return self static struct switch true type use var void while",
  shell: "case do done elif else esac export fi for function if in local readonly return then until while echo cd",
  sql: "select from where and or insert into values update set delete create table drop alter join left right inner outer on group by order having limit as null not primary key",
};

const KEYWORD_SETS = Object.fromEntries(Object.entries(KEYWORDS).map(([name, words]) => [name, new Set(words.split(" "))]));

const COMMENT_START = { python: "#", shell: "#", yaml: "#", toml: "#", js: "//", c: "//", css: "/*", sql: "--" };

/* One line, coloured: comments, strings, numbers and keywords. A line at a
 * time keeps it fast and never wrong by more than a line. */
function highlightLine(text, language) {
  if (language === "text" || language === "markdown" && !/^\s*(#|```|[-*] )/.test(text)) return escapeHtml(text);
  if (language === "markdown") return `<span class="tok-key">${escapeHtml(text)}</span>`;
  const comment = COMMENT_START[language];
  const keywords = KEYWORD_SETS[language] || new Set();
  const pattern = /("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`(?:[^`\\]|\\.)*`?)|(\b\d[\d_.]*(?:e[+-]?\d+)?\b)|([A-Za-z_$][\w$-]*)|(\s+)|(.)/g;
  let out = "";
  let match;
  let index = 0;
  while ((match = pattern.exec(text))) {
    const [token, string, number, word] = match;
    if (comment && text.startsWith(comment, match.index)) {
      out += `<span class="tok-comment">${escapeHtml(text.slice(match.index))}</span>`;
      index = text.length;
      break;
    }
    if (string) out += `<span class="tok-string">${escapeHtml(token)}</span>`;
    else if (number) out += `<span class="tok-number">${escapeHtml(token)}</span>`;
    else if (word && keywords.has(language === "sql" ? word.toLowerCase() : word)) out += `<span class="tok-key">${escapeHtml(token)}</span>`;
    else if (word && (language === "yaml" || language === "toml" || language === "json") && /^\s*:|^\s*=/.test(text.slice(match.index + token.length))) out += `<span class="tok-key">${escapeHtml(token)}</span>`;
    else if (word && text[match.index + token.length] === "(") out += `<span class="tok-call">${escapeHtml(token)}</span>`;
    else out += escapeHtml(token);
    index = match.index + token.length;
  }
  return out + escapeHtml(text.slice(index));
}

/* ---------- watching ---------- */

function setWatch(on) {
  filesView.watch = on;
  el("files-watch").setAttribute("aria-pressed", String(on));
  window.clearInterval(filesView.timer);
  filesView.timer = on ? window.setInterval(watchTick, WATCH_MS) : 0;
}

async function watchTick() {
  if (overlays.files.hidden || document.hidden) return;
  const tab = filesView.tabs.find((item) => item.path === filesView.active);
  if (!tab) return;
  const before = tab.text;
  await loadTab(tab);
  if (tab.text !== before || tab.error) {
    const code = el("files-code");
    const keep = code.scrollTop;
    renderOpenFile();
    code.scrollTop = keep;
  }
}

/* ---------- events ---------- */

el("open-files").addEventListener("click", () => openFilesView());
el("files-close").addEventListener("click", () => {
  overlays.files.hidden = true;
});
el("files-reload").addEventListener("click", async () => {
  await refreshFileIndex();
  const tab = filesView.tabs.find((item) => item.path === filesView.active);
  if (tab) {
    await loadTab(tab);
    renderOpenFile();
  }
  renderFileTree();
});
el("files-watch").addEventListener("click", () => setWatch(!filesView.watch));
el("files-filter").addEventListener("input", (event) => {
  filesView.filter = event.target.value.trim();
  renderFileTree();
});

el("files-tree").addEventListener("click", (event) => {
  const folder = event.target.closest("[data-folder]");
  if (folder) {
    const path = folder.dataset.folder;
    if (!filesView.expanded.delete(path)) filesView.expanded.add(path);
    renderFileTree();
    return;
  }
  const file = event.target.closest("[data-tree-file]");
  if (file) openFile(file.dataset.treeFile);
});

el("files-tabs").addEventListener("click", (event) => {
  const close = event.target.closest("[data-close-tab]");
  if (close) {
    const path = close.dataset.closeTab;
    const at = filesView.tabs.findIndex((tab) => tab.path === path);
    filesView.tabs.splice(at, 1);
    if (filesView.active === path) {
      const next = filesView.tabs[Math.max(0, at - 1)];
      filesView.active = next ? next.path : "";
    }
    renderFileTabs();
    renderFileTree();
    if (filesView.active) renderOpenFile();
    return;
  }
  const tab = event.target.closest("[data-tab]");
  if (tab) openFile(tab.dataset.tab);
});

el("files-head").addEventListener("click", async (event) => {
  const copy = event.target.closest("[data-copy-path]");
  if (!copy || !navigator.clipboard) return;
  await navigator.clipboard.writeText(copy.dataset.copyPath);
  toast("Path copied.", "good");
});

el("files-code").addEventListener("click", (event) => {
  const number = event.target.closest("[data-line]");
  if (!number) return;
  el("files-code").querySelectorAll("tr.marked").forEach((row) => row.classList.remove("marked"));
  number.parentElement.classList.add("marked");
});

/* A file name anywhere on the page opens it here. */
document.addEventListener("click", (event) => {
  const ref = event.target.closest(".file-ref[data-open-file]");
  if (!ref) return;
  event.preventDefault();
  event.stopPropagation();
  openFilesView(ref.dataset.openFile);
}, true);

refreshFileIndex();
