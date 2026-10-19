/* The diff viewer: what a run wrote, one file at a time or all of them.
 *
 * - A file list on the left: name, folder, new / edited / deleted, and its
 *   added and removed lines. Clicking a file jumps to it; scrolling marks the
 *   file you are in. A filter narrows the list.
 * - Every file's diff on the right under a sticky head, with old and new line
 *   numbers, the hunks' gaps shown as "N lines unchanged", unified or side by
 *   side. Long files start folded.
 * - Any run with changes can be picked; Undo is offered for the latest one,
 *   which is what the undo endpoint puts back. Each file's head also offers
 *   "Undo this file" (DR-061); a file undone that way stays listed, struck
 *   through, after the run's other files.
 * - Keys while it is open: j / k (or ↓ / ↑) next and previous file, s for
 *   side by side, Esc closes.
 *
 * `app.js` owns the session state and calls `openDiff`; this file reads
 * `/api/changes` itself and draws only inside the diff overlay.
 */

const FOLD_OVER_LINES = 400;

const diffView = {
  turn: -1,
  changes: [],
  split: false,
  filter: "",
  current: 0,
  folded: new Set(),
};

/* Open on run `turn` (an index into history; the latest with changes when
 * omitted), scrolled to `path` if given. */
async function openDiff(turn, path) {
  const runs = runsWithChanges();
  const latest = runs.length ? runs[runs.length - 1].index : -1;
  const chosen = typeof turn === "number" && turn >= 0 ? turn : latest;
  diffView.filter = "";
  el("diff-filter").value = "";
  renderRunPicker(runs, chosen);
  await loadDiffRun(chosen, path);
  overlays.diff.hidden = false;
  el("diff-filter").focus({ preventScroll: true });
}

function runsWithChanges() {
  return ((state && state.history) || [])
    .map((run, index) => ({ index, prompt: run.prompt || "", files: (run.changes || []).length }))
    .filter((run) => run.files);
}

function renderRunPicker(runs, chosen) {
  const picker = el("diff-run");
  picker.innerHTML = runs
    .map(
      (run) =>
        `<option value="${run.index}">Run ${run.index + 1} · ${escapeHtml(elideMiddle(run.prompt.split("\n")[0], 48))} (${plural(run.files, "file")})</option>`
    )
    .join("");
  picker.hidden = runs.length < 2;
  if (runs.length) picker.value = String(chosen);
}

async function loadDiffRun(turn, path) {
  diffView.turn = turn;
  diffView.current = 0;
  diffView.jumpedAt = 0;
  diffView.folded = new Set();
  let payload = { changes: [], added: 0, removed: 0 };
  try {
    const query = `session=${encodeURIComponent(focusId())}${turn >= 0 ? `&turn=${turn}` : ""}`;
    payload = await api(`/api/changes?${query}`);
  } catch (error) {
    toast(String(error.message || error), "bad");
  }
  // DR-061: files put back one by one come last, marked, with no diff.
  diffView.changes = (payload.changes || []).concat(
    (payload.undone || []).map((change) => ({ ...change, undone: true, diff: "" }))
  );
  diffView.changes.forEach((change, index) => {
    const lines = (change.diff || "").split("\n").length;
    if (lines > FOLD_OVER_LINES) diffView.folded.add(index);
  });
  if (path) {
    const at = diffView.changes.findIndex((change) => change.path === path);
    if (at >= 0) {
      diffView.current = at;
      diffView.folded.delete(at);
    }
  }
  const written = (payload.changes || []).length;
  const undone = (payload.undone || []).length;
  el("diff-stat").textContent = diffView.changes.length
    ? `${plural(written, "file")} · +${payload.added} −${payload.removed}${undone ? ` · ${undone} undone` : ""}`
    : "";
  const history = (state && state.history) || [];
  const latest = turn === history.length - 1 || turn < 0;
  el("diff-undo").hidden = !latest;
  el("diff-undo").disabled = !written;
  renderDiffView();
  if (path) window.requestAnimationFrame(() => showDiffFile(diffView.current, false));
}

function renderDiffView() {
  const list = el("diff-file-list");
  const body = el("diff-body");
  const changes = diffView.changes;
  el("diff-split").setAttribute("aria-pressed", String(diffView.split));
  if (!changes.length) {
    list.innerHTML = "";
    body.innerHTML = `<p class="panel-note">Nothing has been written in this session yet.</p>`;
    return;
  }
  const needle = diffView.filter.toLowerCase();
  list.innerHTML = changes
    .map((change, index) => {
      if (needle && !change.path.toLowerCase().includes(needle)) return "";
      const slash = change.path.lastIndexOf("/");
      const name = change.path.slice(slash + 1);
      const folder = slash > 0 ? change.path.slice(0, slash) : "";
      const kind = change.undone ? "undone" : change.kind;
      return (
        `<button type="button" class="diff-file-row${index === diffView.current ? " on" : ""}${change.undone ? " undone" : ""}" data-diff-file="${index}" title="${escapeHtml(change.path)}">` +
        `<span class="diff-kind ${escapeHtml(kind)}">${escapeHtml(kindWord(kind))}</span>` +
        `<span class="diff-name">${escapeHtml(name)}</span>` +
        (folder ? `<span class="diff-folder">${escapeHtml(folder)}</span>` : "") +
        `<span class="diff-counts"><span class="added">+${change.added}</span> <span class="removed">−${change.removed}</span></span>` +
        `</button>`
      );
    })
    .join("") || `<p class="panel-note">No file matches.</p>`;
  body.innerHTML = changes.map((change, index) => diffFileMarkup(change, index)).join("");
}

function kindWord(kind) {
  return { created: "new", deleted: "deleted", modified: "edited" }[kind] || kind || "edited";
}

function diffFileMarkup(change, index) {
  if (change.undone) return undoneFileMarkup(change, index);
  const folded = diffView.folded.has(index);
  const lines = (change.diff || "").split("\n").length;
  const head =
    `<div class="diff-head">` +
    `<button type="button" class="diff-fold" data-diff-fold="${index}" aria-expanded="${!folded}" title="${folded ? "Show this file" : "Fold this file"}">${folded ? "▸" : "▾"}</button>` +
    `<span class="path">${escapeHtml(change.path)}</span>` +
    `<span class="diff-kind ${escapeHtml(change.kind)}">${escapeHtml(kindWord(change.kind))}</span>` +
    `<span class="stat"><span class="added">+${change.added}</span> <span class="removed">−${change.removed}</span></span>` +
    undoFileButtonMarkup(diffView.turn, change, "diff-undo-file") +
    `</div>`;
  const content = folded
    ? `<button type="button" class="diff-unfold" data-diff-fold="${index}">${lines} lines folded · show them</button>`
    : diffView.split
      ? splitMarkup(parseDiff(change.diff || ""))
      : unifiedMarkup(parseDiff(change.diff || ""));
  return `<section class="diff-file" id="diff-file-${index}" data-file-index="${index}">${head}${content}</section>`;
}

/* DR-061: a file of this run that was put back on its own. */
function undoneFileMarkup(change, index) {
  return (
    `<section class="diff-file undone" id="diff-file-${index}" data-file-index="${index}">` +
    `<div class="diff-head"><span class="diff-fold-gap" aria-hidden="true"></span>` +
    `<span class="path">${escapeHtml(change.path)}</span>` +
    `<span class="diff-kind undone">undone</span></div>` +
    `<p class="panel-note diff-none">Put back as it was before this run. The run's other files are kept.</p>` +
    `</section>`
  );
}

/* A unified diff as hunks of typed lines with their old and new numbers. */
function parseDiff(text) {
  const hunks = [];
  let hunk = null;
  let oldNo = 0;
  let newNo = 0;
  text.split("\n").forEach((line) => {
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) return;
    const head = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/.exec(line);
    if (head) {
      oldNo = Number(head[1]);
      newNo = Number(head[3]);
      hunk = { oldStart: oldNo, newStart: newNo, label: head[5].trim(), lines: [] };
      hunks.push(hunk);
      return;
    }
    if (!hunk) {
      if (!line) return;
      hunk = { oldStart: 1, newStart: 1, label: "", lines: [] };
      hunks.push(hunk);
    }
    if (line.startsWith("\\")) {
      hunk.lines.push({ kind: "note", text: line.slice(1).trim() });
    } else if (line.startsWith("+")) {
      hunk.lines.push({ kind: "add", text: line.slice(1), newNo: newNo++ });
    } else if (line.startsWith("-")) {
      hunk.lines.push({ kind: "del", text: line.slice(1), oldNo: oldNo++ });
    } else if (line.startsWith(" ") || line === "") {
      hunk.lines.push({ kind: "same", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
  });
  // A trailing empty line is the diff's last newline, not a line of the file.
  hunks.forEach((item) => {
    const last = item.lines[item.lines.length - 1];
    if (last && last.kind === "same" && !last.text) item.lines.pop();
  });
  return hunks;
}

/* Between hunks: how many lines the diff left out. */
function gapMarkup(hunks, index, columns) {
  const hunk = hunks[index];
  const previous = hunks[index - 1];
  let gap = 0;
  if (previous) {
    const lastOld = [...previous.lines].reverse().find((line) => line.oldNo !== undefined);
    gap = hunk.oldStart - (lastOld ? lastOld.oldNo + 1 : previous.oldStart);
  } else {
    gap = hunk.oldStart - 1;
  }
  const words = gap > 0 ? `${plural(gap, "line")} unchanged` : "";
  const label = hunk.label ? `<span class="diff-gap-label">${escapeHtml(hunk.label)}</span>` : "";
  return words || label ? `<tr class="diff-gap"><td colspan="${columns}">⋯ ${words}${label}</td></tr>` : "";
}

function unifiedMarkup(hunks) {
  if (!hunks.length) return `<p class="panel-note diff-none">No text to compare (a binary file, or an empty one).</p>`;
  const rows = hunks
    .map(
      (hunk, index) =>
        gapMarkup(hunks, index, 3) +
        hunk.lines
          .map((line) =>
            line.kind === "note"
              ? `<tr class="note"><td></td><td></td><td>${escapeHtml(line.text)}</td></tr>`
              : `<tr class="${line.kind}"><td class="no">${line.oldNo ?? ""}</td><td class="no">${line.newNo ?? ""}</td>` +
                `<td class="code"><span class="sign">${line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}</span>${escapeHtml(line.text) || " "}</td></tr>`
          )
          .join("")
    )
    .join("");
  return `<div class="diff-scroll"><table class="diff-table unified"><tbody>${rows}</tbody></table></div>`;
}

/* Side by side: each run of removed lines paired with the added run after it. */
function splitMarkup(hunks) {
  if (!hunks.length) return `<p class="panel-note diff-none">No text to compare (a binary file, or an empty one).</p>`;
  const cell = (line, side) =>
    line
      ? `<td class="no">${side === "old" ? line.oldNo : line.newNo}</td><td class="code ${line.kind}">${escapeHtml(line.text) || " "}</td>`
      : `<td class="no empty"></td><td class="code empty"></td>`;
  const rows = hunks
    .map((hunk, index) => {
      const out = [gapMarkup(hunks, index, 4)];
      const lines = hunk.lines.filter((line) => line.kind !== "note");
      for (let i = 0; i < lines.length; ) {
        if (lines[i].kind === "same") {
          out.push(`<tr>${cell(lines[i], "old")}${cell(lines[i], "new")}</tr>`);
          i += 1;
          continue;
        }
        const dels = [];
        const adds = [];
        while (i < lines.length && lines[i].kind === "del") dels.push(lines[i++]);
        while (i < lines.length && lines[i].kind === "add") adds.push(lines[i++]);
        for (let k = 0; k < Math.max(dels.length, adds.length); k++) {
          out.push(`<tr>${cell(dels[k], "old")}${cell(adds[k], "new")}</tr>`);
        }
      }
      return out.join("");
    })
    .join("");
  return `<div class="diff-scroll"><table class="diff-table split"><tbody>${rows}</tbody></table></div>`;
}

/* Bring file `index` into view and mark it in the list. */
function showDiffFile(index, smooth = true) {
  if (index < 0 || index >= diffView.changes.length) return;
  diffView.current = index;
  // A jump is the person's choice: the scroll it causes does not re-mark the
  // list (the last files can never reach the top to be "scrolled to").
  diffView.jumpedAt = Date.now();
  markCurrentFile();
  const node = el(`diff-file-${index}`);
  if (node) node.scrollIntoView({ block: "start", behavior: smooth && !reduceMotion() ? "smooth" : "auto" });
}

function markCurrentFile() {
  el("diff-file-list")
    .querySelectorAll(".diff-file-row")
    .forEach((row) => {
      const on = Number(row.dataset.diffFile) === diffView.current;
      row.classList.toggle("on", on);
      if (on) row.scrollIntoView({ block: "nearest" });
    });
}

el("diff-file-list").addEventListener("click", (event) => {
  const row = event.target.closest("[data-diff-file]");
  if (!row) return;
  const index = Number(row.dataset.diffFile);
  if (diffView.folded.delete(index)) renderDiffView();
  showDiffFile(index);
});

el("diff-body").addEventListener("click", (event) => {
  const fold = event.target.closest("[data-diff-fold]");
  if (!fold) return;
  const index = Number(fold.dataset.diffFold);
  if (!diffView.folded.delete(index)) diffView.folded.add(index);
  renderDiffView();
  showDiffFile(index, false);
});

/* Scrolling marks the file whose head has most recently passed the top. */
el("diff-body").addEventListener("scroll", () => {
  if (Date.now() - (diffView.jumpedAt || 0) < 900) return;
  const body = el("diff-body");
  const top = body.getBoundingClientRect().top + 8;
  let current = 0;
  body.querySelectorAll(".diff-file").forEach((node) => {
    if (node.getBoundingClientRect().top <= top) current = Number(node.dataset.fileIndex);
  });
  if (current !== diffView.current) {
    diffView.current = current;
    markCurrentFile();
  }
});

el("diff-filter").addEventListener("input", (event) => {
  diffView.filter = event.target.value.trim();
  renderDiffView();
});

el("diff-run").addEventListener("change", (event) => loadDiffRun(Number(event.target.value)));

el("diff-split").addEventListener("click", () => {
  diffView.split = !diffView.split;
  renderDiffView();
  showDiffFile(diffView.current, false);
});

document.addEventListener("keydown", (event) => {
  if (overlays.diff.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
  const typing = event.target.closest && event.target.closest("input, textarea, select");
  if (typing && !["ArrowDown", "ArrowUp"].includes(event.key)) return;
  if (event.key === "j" || event.key === "ArrowDown") {
    event.preventDefault();
    showDiffFile(Math.min(diffView.changes.length - 1, diffView.current + 1));
  } else if (event.key === "k" || event.key === "ArrowUp") {
    event.preventDefault();
    showDiffFile(Math.max(0, diffView.current - 1));
  } else if (event.key === "s") {
    el("diff-split").click();
  }
});
