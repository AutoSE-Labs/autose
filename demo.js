// AutoSE "Try it": a smaller, scripted copy of the dashboard (src/gui/static
// on main: index.html, app.css, route.js, app.js). The runs are replayed from
// the scripts below; nothing here contacts the network.
(() => {
  "use strict";

  const shell = document.getElementById("demo-shell");
  if (!shell) return;
  const $ = (id) => document.getElementById(id);

  const stream = $("d-stream");
  const conv = $("d-conv");
  const list = $("d-list");
  const promptEl = $("d-prompt");
  const startBtn = $("d-start");
  const stopBtn = $("d-stop");
  const hintEl = $("d-hint");
  const tiersEl = $("d-tiers");
  const reviewEl = $("d-review");
  const composer = $("d-composer");
  const presence = $("d-presence");
  const presenceText = $("d-presence-text");
  const ctxFill = $("d-ctx-fill");
  const ctxLabel = $("d-ctx-label");
  const rateEl = $("d-rate");
  const inboxCount = $("d-inbox-count");
  const overlay = $("d-overlay");
  const panel = $("d-panel");
  const toastEl = $("d-toast");
  const relaunch = $("d-relaunch");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* ---------- small helpers ---------- */

  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const formatCount = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const formatElapsed = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const humanTier = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const inShell = () => shell.contains(document.activeElement);
  const quietFocus = (el) => {
    if (el && el.focus) el.focus({ preventScroll: true });
  };

  /* Inline text: `code`, **bold**, *em* and {file} chips. */
  function rich(text) {
    return esc(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\{([^}]+)\}/g, (_, name) => fileRef(name));
  }

  /* A file name as the dashboard's chip; changed files open their diff. */
  function fileRef(name) {
    return FILES[name]
      ? `<button type="button" class="d-file-ref" data-diff="${name}" title="Open this file's diff">${name}</button>`
      : `<span class="d-file-ref plain">${name}</span>`;
  }

  /* Paragraphs, and lines starting "- " as a list. */
  function prose(text) {
    return text
      .split("\n\n")
      .map((block) =>
        block.startsWith("- ")
          ? `<ul>${block
              .split("\n")
              .map((line) => `<li>${rich(line.replace(/^- /, ""))}</li>`)
              .join("")}</ul>`
          : `<p>${rich(block)}</p>`
      )
      .join("");
  }

  /* ---------- the icons (route.js) ---------- */

  const TIER_ICONS = {
    auto: '<path d="M4.5 9a5.5 5.5 0 0 1 9.6-3.2M15.5 11a5.5 5.5 0 0 1-9.6 3.2"/><path d="M14.5 2.8v3.4h-3.4M5.5 17.2v-3.4h3.4"/>',
    spark: '<path d="M11.2 2.5 4.8 11h4.6l-1 6.5L15.2 9h-4.6z"/>',
    craft: '<path d="M11.8 3.2l5 5-2.3 2.3-5-5z"/><path d="M10.6 7.4 3.4 14.6a1.4 1.4 0 0 0 2 2l7.2-7.2"/>',
    forge: '<path d="M10 2.5c.6 2.8 4.5 4.4 4.5 8.7a4.5 4.5 0 0 1-9 0c0-2.1 1-3.4 2.1-4.3.1 1.6.9 2.6 2.2 2.8-.6-2.4-.5-4.6.2-7.2z"/>',
  };

  const tierIcon = (tier, size) =>
    `<svg class="d-tier-icon" viewBox="0 0 20 20" width="${size}" height="${size}" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${TIER_ICONS[tier]}</svg>`;

  const tierBadge = (tier, busy) =>
    `<span class="d-tier-badge tier-${tier}${busy ? " busy" : ""}" title="${humanTier(tier)} tier">${tierIcon(tier, 17)}</span>`;

  const CHANGE_ICONS = {
    new: '<path d="M10 5v10M5 10h10"/>',
    edited: '<path d="M12.5 4.5l3 3L7 16H4v-3z"/>',
    undone: '<path d="M6.5 7.5H12a4 4 0 0 1 0 8H8"/><path d="M8.5 4.5l-3 3 3 3"/>',
  };

  const changeWord = (change) => (change.reverted ? "undone" : change.kind);
  const changeBadge = (change) => {
    const word = changeWord(change);
    return `<span class="d-change-badge ${word}" title="${word}"><svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${CHANGE_ICONS[word]}</svg></span>`;
  };

  /* ---------- the sample workspace ---------- */

  // Each file's diff as lines: " " kept, "-" removed, "+" added.
  const FILES = {
    "ops.py": {
      kind: "edited",
      lines: [
        " def add(a, b):",
        "     return a + b",
        " ",
        " def sub(a, b):",
        "     return a - b",
        " ",
        " def mul(a, b):",
        "-    return a + b",
        "+    return a * b",
        " ",
        " def div(a, b):",
        "-    return a * b",
        "+    if b == 0:",
        '+        raise ZeroDivisionError("div: cannot divide by zero")',
        "+    return a / b",
      ],
    },
    "test_ops.py": {
      kind: "new",
      lines: [
        "import pytest", "", "from ops import add, sub, mul, div", "", "",
        "def test_add():", "    assert add(2, 3) == 5", "", "",
        "def test_sub():", "    assert sub(5, 3) == 2", "", "",
        "def test_mul():", "    assert mul(4, 3) == 12", "", "",
        "def test_div():", "    assert div(12, 4) == 3", "", "",
        "def test_div_by_zero():", "    with pytest.raises(ZeroDivisionError):", "        div(1, 0)",
      ].map((line) => `+${line}`),
    },
    "text.py": {
      kind: "edited",
      lines: [
        " import re",
        " import unicodedata",
        " ",
        " def slugify(text):",
        '     text = unicodedata.normalize("NFKD", text)',
        '     text = text.encode("ascii", "ignore").decode()',
        '-    return re.sub(r"[^a-z0-9]", "-", text.lower()).strip("-")',
        '+    text = re.sub(r"[^a-z0-9]+", "-", text.lower())',
        '+    return text.strip("-")',
      ],
    },
    "test_text.py": {
      kind: "new",
      lines: [
        "from text import slugify", "", "",
        "def test_empty_input():", '    assert slugify("") == ""', "", "",
        "def test_accents_are_folded():", '    assert slugify("Café Crème") == "cafe-creme"', "", "",
        "def test_repeated_dashes_collapse():", '    assert slugify("a -- b") == "a-b"', "", "",
        "def test_spaces_become_dashes():", '    assert slugify("hello world") == "hello-world"',
      ].map((line) => `+${line}`),
    },
  };
  Object.values(FILES).forEach((file) => {
    file.added = file.lines.filter((l) => l[0] === "+").length;
    file.removed = file.lines.filter((l) => l[0] === "-").length;
  });

  /* ---------- the scripted runs ---------- */

  const SCENARIOS = {
    test: {
      label: "Add a test",
      prompt: "Add tests for slugify in text.py: empty input, accents and repeated dashes.",
      auto: "craft",
      craft: {
        reqs: ["empty input gives an empty slug", "accented letters fold to plain ones", "repeated dashes collapse to one"],
        plan: [
          "Read `slugify` in `text.py` and note what it promises",
          "Write `test_text.py` with one test per case (R1, R2, R3)",
          "Run the tests; change `text.py` only if a test shows a real bug",
        ],
        planActs: [["◉", "Reading {text.py}"], ["⌕", "Searching the code for `slugify(`"]],
        exec1: [["✎", "Writing {test_text.py}", "test_text.py"]],
        cmd: "python -m pytest -q",
        allow: "python -m pytest",
        out1: ["✗", "1 failed, 3 passed in 0.03s", "fail"],
        validate1: [["◉", "Reading {text.py}"]],
        check1: ["Verified", "Verified", "Failed"],
        unmet: "R3",
        exec2: [["✎", "Editing {text.py}", "text.py"]],
        out2: ["✓", "4 passed in 0.02s", "pass"],
        validate2: [["◉", "Reading {test_text.py}"]],
        answer: "Added `test_text.py`. One test caught a real bug, so `text.py` now collapses repeated dashes.",
        tokens: 64900,
        notes: {
          "Execution summary":
            "Wrote test_text.py with four tests for slugify.\nPass 2: the repeated-dash test failed on a real bug; text.py now matches a run of separators as one dash.",
          "Validation report":
            "R1 empty input gives an empty slug: verified (test_empty_input)\nR2 accented letters fold to plain ones: verified (test_accents_are_folded)\nR3 repeated dashes collapse to one: verified on pass 2 (4 passed)",
        },
      },
      spark: {
        acts: [["◉", "Reading {text.py}"], ["✎", "Writing {test_text.py}", "test_text.py"]],
        answer:
          "Wrote `test_text.py` with four tests for `slugify`. I did not run them: Craft runs the tests and checks the work against numbered requirements.",
        tokens: 8100,
      },
    },
    fix: {
      label: "Fix a bug",
      prompt: "Fix mul and div in ops.py, guard division by zero, and add pytest tests.",
      auto: "craft",
      craft: {
        reqs: ["`mul` multiplies and `div` divides", "dividing by zero raises a clear error", "tests cover all four operations and the zero case"],
        plan: [
          "Fix `mul` and `div` in `ops.py` (R1)",
          "Make `div` raise a clear error when dividing by zero (R2)",
          "Add `test_ops.py` for all four operations and the zero case (R3)",
        ],
        planActs: [["▤", "Listing `.`"], ["◉", "Reading {ops.py}"], ["⌕", "Searching the code for `def div`"]],
        exec1: [["✎", "Editing {ops.py}", "ops.py"], ["✎", "Writing {test_ops.py}", "test_ops.py"]],
        cmd: "python -m pytest -q",
        allow: "python -m pytest",
        out1: ["✓", "4 passed in 0.02s", "pass"],
        validate1: [["◉", "Reading {test_ops.py}"]],
        check1: ["Verified", "Verified", "Failed"],
        unmet: "R3",
        exec2: [["✎", "Editing {test_ops.py}", "test_ops.py"]],
        out2: ["✓", "5 passed in 0.02s", "pass"],
        validate2: [["◉", "Reading {test_ops.py}"]],
        answer: "Fixed `ops.py` and wrote the tests.",
        tokens: 80300,
        notes: {
          "Execution summary":
            "ops.py: mul now multiplies and div divides; div raises ZeroDivisionError with a clear message when b is 0.\ntest_ops.py: one test per operation.\nPass 2: added test_div_by_zero after Validate sent the work back.",
          "Validation report":
            "R1 `mul` multiplies and `div` divides: verified (test_mul, test_div pass)\nR2 dividing by zero raises a clear error: verified (ops.py line 11)\nR3 tests cover all four operations and the zero case: verified on pass 2 (5 passed in 0.02s)",
        },
      },
      spark: {
        acts: [["◉", "Reading {ops.py}"], ["✎", "Editing {ops.py}", "ops.py"]],
        answer:
          "Fixed `mul` and `div` in `ops.py`; dividing by zero now raises a clear `ZeroDivisionError`. I did not add tests: Craft plans the change, writes the tests and checks the work against numbered requirements.",
        tokens: 9400,
      },
    },
    explain: {
      label: "Explain this project",
      prompt: "Walk me through what this project does and how it is laid out.",
      auto: "spark",
      spark: {
        acts: [["▤", "Listing `.`"], ["◉", "Reading {README.md}"], ["◉", "Reading {ops.py}"], ["◉", "Reading {pyproject.toml}"]],
        answer:
          "**calc** is a small Python calculator library with a command line on top.\n\n- `ops.py` holds the four operations: `add`, `sub`, `mul` and `div`.\n- `cli.py` reads two numbers and an operator and prints the result.\n- `tests/` is where pytest looks; `pyproject.toml` names the package and its test runner.\n\nThere is no build step: `python -m pytest -q` checks it.",
        tokens: 6200,
      },
      craft: {
        reqs: ["says what the project is for", "walks through each top-level file", "says how to run the tests"],
        plan: ["Read the README and list the files", "Read each module and say what it holds (R1, R2)", "Find how the tests run (R3)"],
        planActs: [["▤", "Listing `.`"], ["◉", "Reading {README.md}"]],
        exec1: [["◉", "Reading {ops.py}"], ["◉", "Reading {cli.py}"], ["◉", "Reading {pyproject.toml}"]],
        cmd: null,
        validate1: [["◉", "Reading {README.md}"]],
        check1: ["Verified", "Verified", "Verified"],
        unmet: null,
        answer:
          "**calc** is a small Python calculator library with a command line on top.\n\n- `ops.py` holds the four operations: `add`, `sub`, `mul` and `div`.\n- `cli.py` reads two numbers and an operator and prints the result.\n- `tests/` is where pytest looks; `pyproject.toml` names the package and its test runner.\n\nThere is no build step: `python -m pytest -q` checks it.",
        tokens: 31800,
        notes: {
          "Validation report":
            "R1 says what the project is for: verified\nR2 walks through each top-level file: verified (ops.py, cli.py, tests/, pyproject.toml)\nR3 says how to run the tests: verified",
        },
      },
    },
  };
  const STARTER_ORDER = ["test", "fix", "explain"];

  function pickScenario(text) {
    if (/\b(explain|what|how|why|walk|describe|tell)\b|\?\s*$/i.test(text)) return "explain";
    if (/\btests?\b/i.test(text)) return "test";
    return "fix";
  }

  const TIPS = [
    "Pick the tier the request needs. Spark answers or makes one small edit; Craft plans, makes the change and checks it against numbered requirements.",
    "Every command asks first. \"Always allow\" covers calls that start with the same words, in this session only.",
    "Changes holds the diff of every file a session touched, and can put them back as they were.",
    "Review the plan stops Craft after planning, before any file is touched.",
  ];

  /* ---------- state ---------- */

  const OLD_SESSIONS = [
    { no: "S-A914", tier: "craft", title: "Rename the CLI flags", line: "3 of 3 checks hold" },
    { no: "S-64A0", tier: "spark", title: "What does parse_args return?", line: "1 run · 4.1k tok" },
  ];

  let sessions = [];
  let focus = null;
  let run = null;
  let tier = "auto";
  let typing = false;
  let typeTimer = 0;
  let toastTimer = 0;
  let stick = true;
  let tipIndex = 0;
  let uid = 0;
  let lastOpener = null;
  const STOP = { stop: true };

  function makeSession(no) {
    return {
      no: no || `S-${Math.floor(Math.random() * 0xffff).toString(16).toUpperCase().padStart(4, "0")}`,
      tier: tier === "spark" ? "spark" : "craft",
      title: "New session",
      runs: [],
      nodes: [],
      allowed: [],
    };
  }

  /* ---------- the route (route.js, at the card's own width) ---------- */

  function stationNote(op) {
    if (op.note) return op.note;
    if (op.status === "done") return op.passes > 1 ? `done · ${op.passes} passes` : "done";
    if (op.status === "active") return "working";
    if (op.status === "halted") return "stopped here";
    return "next";
  }

  function routeMarkup(route, width, id) {
    const ops = route.ops;
    const n = ops.length;
    const left = 34;
    const right = width - 34;
    const y = 54;
    const span = right - left;
    const xs = ops.map((_, i) => (n === 1 ? left : left + (span * i) / (n - 1)));
    const anchor = (i) => (n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle");

    // Ink up to the current station, inking after it, pencil beyond.
    const segments = [];
    for (let i = 0; i < n - 1; i++) {
      const next = ops[i + 1].status;
      const cls =
        next === "done" || next === "active" || next === "halted" ? "r-done" : ops[i].status === "active" ? "r-now" : "r-next";
      segments.push(`<line class="d-${cls}" x1="${xs[i]}" y1="${y}" x2="${xs[i + 1]}" y2="${y}"></line>`);
    }

    const stations = ops
      .map((op, i) => {
        const status = op.status || "next";
        const cls = status === "done" ? "d-wp done" : status === "active" ? "d-wp" : status === "halted" ? "d-wp halted" : "d-wp next";
        const r = status === "active" ? 11 : 9;
        const noteCls = status === "active" ? "d-wp-s ink" : status === "halted" ? "d-wp-s red" : "d-wp-s";
        return (
          `<circle class="${cls}" cx="${xs[i]}" cy="${y}" r="${r}"></circle>` +
          `<text class="d-wp-l" x="${xs[i]}" y="${y + 36}" text-anchor="${anchor(i)}">Step ${i + 1}</text>` +
          `<text class="d-wp-n" x="${xs[i]}" y="${y + 57}" text-anchor="${anchor(i)}">${esc(op.title)}</text>` +
          `<text class="${noteCls}" x="${xs[i]}" y="${y + 77}" text-anchor="${anchor(i)}">${esc(stationNote(op))}</text>`
        );
      })
      .join("");

    // The nib, centred over the station it works at; lifted while it waits.
    const at = ops.findIndex((op) => op.status === "active");
    const drop = at >= 0 ? `<circle class="d-ink-drop" cx="${xs[at]}" cy="${y}" r="11"></circle>` : "";
    const tipY = route.pose === "waiting" ? y - 18 : y - 14;
    const nib =
      at >= 0
        ? `<g class="d-nib-on-route nib-${route.pose}" transform="translate(${xs[at] - 13} ${tipY - 31})"><use href="#d-nib" width="26" height="31"></use></g>`
        : "";

    // The send-back: a redline from Validate down and back to the station it
    // returned to, a numbered circle on its lowest point, a note under it.
    const from = ops.findIndex((op) => op.name === "validate");
    let loops = "";
    let height = y + 92;
    const targets = [...new Set(route.loopbacks.map((item) => item.to))];
    if (from >= 0 && targets.length) {
      const top = y + 88;
      const stem = 12;
      let lowest = top;
      targets.forEach((target, k) => {
        const to = ops.findIndex((op) => op.name === target);
        if (to < 0 || to === from) return;
        const x1 = xs[from];
        const x2 = xs[to];
        const bend = top + stem;
        const dip = bend + 34 + k * 18;
        const midX = (x1 + x2) / 2;
        const midY = (bend + 6 * dip + bend) / 8;
        loops += `<path class="d-redloop" d="M${x1} ${top} V${bend} C ${x1} ${dip}, ${x2} ${dip}, ${x2} ${bend} V${top + 1}" marker-end="url(#d-arrow-${id})"></path>`;
        loops +=
          `<circle class="d-revtri" cx="${midX}" cy="${midY}" r="10"></circle>` +
          `<text class="d-revtxt" x="${midX}" y="${midY + 4}" text-anchor="middle">${k + 1}</text>`;
        lowest = Math.max(lowest, midY + 10);
      });
      const last = route.loopbacks[route.loopbacks.length - 1];
      const times = route.loopbacks.length === 1 ? "once" : `${route.loopbacks.length} times`;
      const note = `sent back ${times}${last.unmet ? `: ${last.unmet} unmet` : ""}`;
      const half = (note.length * 7.5) / 2;
      const centre = (xs[from] + xs[ops.findIndex((op) => op.name === targets[targets.length - 1])]) / 2;
      const noteX = Math.min(width - 6 - half, Math.max(6 + half, centre));
      height = lowest + 30;
      loops += `<text class="d-margin-note" x="${noteX}" y="${lowest + 22}" text-anchor="middle">${esc(note)}</text>`;
    }

    // Narrow: the same route, top to bottom.
    const listMarkup =
      `<ol class="d-route-list" aria-hidden="true">` +
      ops
        .map(
          (op, i) =>
            `<li class="rl-${op.status || "next"}"><span class="d-rl-dot"></span>` +
            `<span class="d-rl-text"><span class="d-lbl">Step ${i + 1}</span> <b>${esc(op.title)}</b> <span class="d-rl-note">${esc(stationNote(op))}</span></span></li>`
        )
        .join("") +
      (route.loopbacks.length
        ? `<li class="rl-back"><span class="d-rl-dot"></span><span class="d-rl-text d-hand">sent back ${route.loopbacks.length === 1 ? "once" : `${route.loopbacks.length} times`}: ${esc(route.loopbacks[route.loopbacks.length - 1].unmet)} unmet</span></li>`
        : "") +
      `</ol>`;

    const label = ops.map((op) => `${op.title}: ${stationNote(op)}`).join(", ") +
      (route.loopbacks.length ? `, sent back ${plural(route.loopbacks.length, "time")}` : "");

    return (
      listMarkup +
      `<svg viewBox="0 0 ${width} ${Math.ceil(height)}" width="${width}" height="${Math.ceil(height)}" role="img" aria-label="${esc(label)}">` +
      `<defs><marker id="d-arrow-${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="d-redhead" d="M0 1 L10 5 L0 9 Z"></path></marker></defs>` +
      segments.join("") +
      loops +
      stations +
      drop +
      nib +
      `</svg>`
    );
  }

  /* Draws a card's route at the width it has; only redrawn when it changes. */
  function paintRoute(node, model) {
    node._route = model;
    const host = node.querySelector(".d-route");
    if (!host) return;
    const width = Math.max(320, Math.round(host.clientWidth || 640));
    const key = JSON.stringify([width, model.pose, model.ops.map((op) => [op.status, stationNote(op)]), model.loopbacks.length]);
    if (key !== node._routeKey) {
      node._routeKey = key;
      host.innerHTML = routeMarkup(model, width, node.dataset.uid);
      const loop = host.querySelector(".d-redloop");
      if (model.draw && loop && loop.getTotalLength) {
        loop.style.setProperty("--len", String(Math.ceil(loop.getTotalLength()) + 2));
        loop.classList.add("d-draw");
      }
      node._drops = model.drops;
    } else if (model.drops !== node._drops) {
      // A drop of ink on the station for every new step.
      node._drops = model.drops;
      const old = host.querySelector(".d-ink-drop");
      if (old) old.replaceWith(old.cloneNode(false));
    }
  }

  /* ---------- checks, files, cards ---------- */

  const STAMP_SHAPES = {
    "Not Started": ["open", "not started"],
    Implemented: ["implemented", "implemented, not yet checked"],
    Verified: ["verified", "holds"],
    Failed: ["failed", "does not hold"],
  };

  function stampsMarkup(reqs, fresh) {
    if (!reqs || !reqs.length) return "";
    let held = 0;
    let failed = 0;
    const balloons = reqs
      .map((item, index) => {
        const [shape, word] = STAMP_SHAPES[item.status];
        if (shape === "verified") held += 1;
        if (shape === "failed") failed += 1;
        const isFresh = fresh && fresh.has(item.id) ? " fresh" : "";
        return `<span class="d-balloon ${shape}${isFresh}" title="${esc(`${item.id}: ${item.desc.replace(/`/g, "")} (${word})`)}">${index + 1}<span class="d-vh"> ${word}</span></span>`;
      })
      .join("");
    const tally = `${held} of ${reqs.length} checks hold${failed ? ` · ${failed} ${failed === 1 ? "does" : "do"} not` : ""}`;
    return `<div class="d-checks">${balloons}<span class="d-tally">${tally}</span></div>`;
  }

  function revisionMarkup(changes) {
    if (!changes.length) return "";
    const rows = changes
      .map(
        (change) =>
          `<tr${change.reverted ? ' class="d-rev-undone" title="Undone: put back as it was"' : ""}>` +
          `<td class="d-rev-sym">${changeBadge(change)}</td>` +
          `<td class="d-rev-file">${change.reverted ? esc(change.path) : `<button type="button" class="d-rev-link" data-diff="${change.path}" title="Open this file's diff">${change.path}</button>`}</td>` +
          `<td class="d-rev-kind">${changeWord(change)}</td>` +
          `<td class="d-rev-lines"><span class="d-added">+${change.added}</span> / <span class="d-removed">−${change.removed}</span></td>` +
          `</tr>`
      )
      .join("");
    return (
      `<table class="d-revision"><caption>Files changed</caption>` +
      `<thead><tr><th scope="col"><span class="d-vh">Change</span></th><th scope="col">File</th><th scope="col">Kind</th><th scope="col">Lines</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`
    );
  }

  function activityMarkup(r) {
    const lines = r.detailed ? r.acts : r.acts.slice(-1);
    return lines
      .map((a) => `<div class="d-act kind-${a.kind || "plain"}"><span class="d-act-icon" aria-hidden="true">${a.icon}</span><span>${a.html}</span></div>`)
      .join("");
  }

  function nowMarkup(r) {
    if (r.waiting === "ask") return "<strong>Waiting for you</strong> · nothing runs without your say-so";
    if (r.waiting === "plan") return "<strong>Waiting for you</strong> · a plan to look over";
    return `<strong>${esc(r.now)}</strong>${r.classified ? ` · ${r.tier}` : ""}`;
  }

  function liveCardMarkup(r) {
    return (
      `<div class="d-head"><span class="d-lbl">Run ${r.n}</span>` +
      `<span class="d-req" title="${esc(r.title)}">${esc(r.title)}</span>` +
      `<span class="d-elapsed">0s</span>` +
      `<button type="button" class="d-card-btn" data-details aria-pressed="false">Details</button>` +
      `<button type="button" class="d-card-btn d-card-stop" data-stop>Stop</button></div>` +
      `<div class="d-now"></div><div class="d-route"></div><div class="d-checks-host"></div><div class="d-activity"></div>`
    );
  }

  function receiptMarkup(r) {
    const c = r.script;
    const done = r.outcome === "done";
    let tone = "stop";
    let word = "Stopped";
    if (done) {
      tone = "pass";
      word = r.reqs ? "Passed" : "Done";
    }
    let headline = "";
    let why = "";
    if (!done) {
      const changed = r.changes.length
        ? "Files it already changed stay changed: Changes shows them and can put them back."
        : "Stopped before there was anything to show.";
      if (r.stopReason === "denied") {
        headline = "Stopped: the checks needed that command";
        why = `You chose not to run \`${c.cmd}\`, so the work could not be checked. ${changed}`;
      } else {
        headline = "You stopped this run";
        why = changed;
      }
    }
    const reqLine = done && r.reqs ? `\n\n${r.reqs.length} requirement(s): ${r.reqs.length} verified` : "";
    const notes = r.tier === "craft"
      ? Object.entries(Object.assign({ Plan: c.plan.map((s, i) => `${i + 1}. ${s.replace(/`/g, "")}`).join("\n") }, done ? c.notes : {}))
          .map(([name, text]) => `<details class="d-notes"><summary>${esc(name)}</summary><div class="d-notes-body">${esc(text).replace(/`/g, "")}</div></details>`)
          .join("")
      : "";
    const live = r.changes.filter((ch) => !ch.reverted).length;
    return (
      `<span class="d-verdict d-verdict-${tone}" aria-hidden="true">${word}</span>` +
      `<div class="d-head"><span class="d-lbl">Run ${r.n}</span>` +
      `<span class="d-req" title="${esc(r.title)}">${esc(r.title)}</span>` +
      `<span class="d-elapsed" title="Tier, and tokens this run used">${r.tier} · ${formatCount(r.tokens)} tok</span>` +
      `<span class="d-vh">Verdict: ${word}.</span></div>` +
      `<div class="d-route"></div>` +
      stampsMarkup(r.reqs) +
      `<div class="d-receipt">` +
      (headline ? `<div class="d-headline">${esc(headline)}</div>` : "") +
      (done ? `<div class="d-prose">${prose(c.answer + reqLine)}</div>` : "") +
      (why ? `<p class="d-why">${rich(why)}</p>` : "") +
      revisionMarkup(r.changes) +
      (live ? `<div class="d-receipt-actions"><button type="button" class="d-inline-btn" data-diff="">See the diff</button></div>` : "") +
      notes +
      `</div>`
    );
  }

  function finalRoute(r) {
    return {
      ops: r.ops.map((op) => ({ name: op.name, title: op.title, status: op.status, passes: op.passes, note: "" })),
      loopbacks: r.loopbacks,
      pose: "",
      drops: 0,
      draw: false,
    };
  }

  function liveRoute(r) {
    return {
      ops: r.ops.map((op) => ({
        name: op.name,
        title: op.title,
        status: op.status,
        passes: op.passes,
        note: op.status === "active" ? (r.waiting ? "waiting for you" : "working") : op.note,
      })),
      loopbacks: r.loopbacks,
      pose: r.waiting ? "waiting" : "working",
      drops: r.drops,
      draw: r.drawLoop,
    };
  }

  /* ---------- painting ---------- */

  function paint(r) {
    if (!r.node || r.done) return;
    const node = r.node;
    node.classList.toggle("waiting", Boolean(r.waiting));
    node.querySelector(".d-now").innerHTML = nowMarkup(r);
    paintRoute(node, liveRoute(r));
    r.drawLoop = false;
    const checks = stampsMarkup(r.reqs, r.fresh);
    const host = node.querySelector(".d-checks-host");
    if (host._html !== checks) {
      host._html = checks;
      host.innerHTML = checks;
    }
    r.fresh = null;
    const activity = node.querySelector(".d-activity");
    const acts = activityMarkup(r);
    if (activity._html !== acts) {
      activity._html = acts;
      activity.innerHTML = acts;
    }
    renderChrome();
    follow();
  }

  function follow() {
    if (stick) conv.scrollTop = conv.scrollHeight;
  }

  function renderChrome() {
    renderList();
    renderBar();
    // Presence: Ready, the current step, or Needs you.
    presence.classList.toggle("working", Boolean(run && !run.waiting));
    presence.classList.toggle("attention", Boolean(run && run.waiting));
    presenceText.textContent = run ? (run.waiting ? "Needs you" : run.now) : "Ready";
    const waiting = run && run.waiting ? 1 : 0;
    inboxCount.hidden = !waiting;
    inboxCount.textContent = waiting ? "1" : "";
    shell.querySelector('[data-sec="inbox"]').classList.toggle("has-items", Boolean(waiting));
    // The context ruler and the model's speed.
    if (run) {
      ctxFill.style.width = `${run.ctx}%`;
      ctxLabel.textContent = `${Math.round(run.ctx)}% of 64k`;
      rateEl.hidden = false;
      rateEl.textContent = `${run.rate} tok/s`;
    } else {
      ctxFill.style.width = "0%";
      ctxLabel.textContent = "64k window";
      rateEl.hidden = true;
    }
    renderComposer();
  }

  function sessionChanges(s) {
    const byPath = new Map();
    s.runs.forEach((r) => r.changes.forEach((ch) => byPath.set(ch.path, ch)));
    return [...byPath.values()];
  }

  function rowLine(s) {
    if (run && run.session === s) {
      if (run.waiting === "ask") return ["Wants to run something", "you"];
      if (run.waiting === "plan") return ["A plan to look over", "you"];
      return [`${run.now} · ${formatElapsed(Date.now() - run.started)} · ctx ${Math.round(run.ctx)}%`, "working"];
    }
    const last = s.runs[s.runs.length - 1];
    if (last && last.reqs && last.done) {
      const held = last.reqs.filter((q) => q.status === "Verified").length;
      return [`${held} of ${last.reqs.length} checks hold`, ""];
    }
    const tokens = s.runs.reduce((sum, r) => sum + r.tokens, 0);
    return [`${plural(s.runs.length, "run")} · ${formatCount(tokens)} tok`, ""];
  }

  function rowMarkup(s) {
    const active = s === focus;
    const busy = Boolean(run && run.session === s);
    const waits = busy && run.waiting;
    const [line, tone] = rowLine(s);
    return (
      `<div class="d-row${active ? " active" : ""}${waits ? " waiting" : ""}">` +
      (waits ? '<svg class="d-waitline" aria-hidden="true"><rect x="1" y="1" rx="10" ry="10"></rect></svg>' : "") +
      `<button class="d-row-main" type="button" data-session="${s.no}"${active ? ' aria-current="true"' : ""} title="${esc(s.title)}">` +
      tierBadge(s.tier, busy && !waits) +
      `<span class="d-row-no">${s.no} · ${s.tier}</span>` +
      `<span class="d-row-title">${esc(s.title)}</span>` +
      `<span class="d-row-state ${tone}">${esc(line)}</span></button></div>`
    );
  }

  function staticRowMarkup(o) {
    return (
      `<div class="d-row static"><div class="d-row-main">${tierBadge(o.tier, false)}` +
      `<span class="d-row-no">${o.no} · ${o.tier}</span><span class="d-row-title">${esc(o.title)}</span>` +
      `<span class="d-row-state">${esc(o.line)}</span></div></div>`
    );
  }

  function renderList() {
    const mine = run ? sessions.filter((s) => s !== run.session) : sessions;
    const groups = [];
    if (run && !run.waiting) groups.push(["Working", "", [rowMarkup(run.session)]]);
    if (run && run.waiting) groups.push(["Waiting for you", "you", [rowMarkup(run.session)]]);
    groups.push(["Open", "", mine.map(rowMarkup).concat(OLD_SESSIONS.map(staticRowMarkup))]);
    const html = groups
      .map(([name, cls, rows]) => `<div class="d-group ${cls}"><span class="d-lbl">${name}</span><span class="d-lbl">${rows.length}</span></div>${rows.join("")}`)
      .join("");
    if (list._html === html) return;
    const had = list.contains(document.activeElement) ? document.activeElement.dataset.session : null;
    list._html = html;
    list.innerHTML = html;
    if (had) quietFocus(list.querySelector(`[data-session="${had}"]`));
  }

  function renderBar() {
    const s = focus;
    $("d-sb-badge").innerHTML = tierBadge(s.tier, false);
    $("d-sb-no").textContent = s.no;
    const name = $("d-sb-name");
    name.textContent = s.title;
    name.title = s.title;
    const allowed = $("d-sb-allowed");
    $("d-sb-allowed-count").textContent = s.allowed.length ? String(s.allowed.length) : "";
    allowed.classList.toggle("has-items", s.allowed.length > 0);
    const live = sessionChanges(s).filter((ch) => !ch.reverted).length;
    $("d-sb-changes-count").textContent = live ? String(live) : "";
    $("d-sb-changes").classList.toggle("has-items", live > 0);
  }

  function renderTiers(locked) {
    const items = [
      ["auto", "Auto", "Pick a tier for each request"],
      ["spark", "Spark", "spark: answer, or one small edit"],
      ["craft", "Craft", "craft: Plan → Execute → Validate"],
      ["forge", "Forge", "Forge is in AutoSE Pro: design, then as craft"],
    ];
    const html = items
      .map(
        ([name, label, title]) =>
          `<button type="button" data-tier="${name}" aria-pressed="${name === tier}" title="${title}"${locked || name === "forge" ? " disabled" : ""}>` +
          `${tierIcon(name, 15)}<span class="d-tier-word">${label}</span>${name === "forge" ? '<span class="d-pro">Pro</span>' : ""}</button>`
      )
      .join("");
    if (tiersEl._html === html) return;
    const had = tiersEl.contains(document.activeElement) ? document.activeElement.dataset.tier : null;
    tiersEl._html = html;
    tiersEl.innerHTML = html;
    if (had) quietFocus(tiersEl.querySelector(`[data-tier="${had}"]`));
  }

  function renderComposer() {
    const busy = Boolean(run) || typing;
    startBtn.hidden = Boolean(run);
    stopBtn.hidden = !run;
    startBtn.disabled = typing;
    promptEl.readOnly = busy;
    reviewEl.disabled = busy;
    hintEl.textContent = run ? "Stop keeps everything produced so far" : "Enter starts · Shift+Enter for a new line";
    $("d-sb-clear").disabled = busy || !focus.runs.length;
    $("d-new").disabled = busy;
    renderTiers(busy);
  }

  function greetingNode() {
    const node = document.createElement("div");
    node.className = "d-greet";
    node.innerHTML =
      `<svg class="d-greet-nib" aria-hidden="true"><use href="#d-nib"></use></svg>` +
      `<span class="d-lbl">New session</span>` +
      `<h3>What should I work on?</h3>` +
      `<p>I work in <span class="d-mono">~/projects/calc</span>. I ask before running a command or using the network, and every file I change can be undone.</p>` +
      `<p class="d-hand">describe a change and I'll plan it first</p>` +
      `<div class="d-chips">${STARTER_ORDER.map((key) => `<button class="d-chip" type="button" data-starter="${key}">${SCENARIOS[key].label}</button>`).join("")}</div>`;
    return node;
  }

  function againNode(lastKey) {
    const node = document.createElement("div");
    node.className = "d-again";
    node.innerHTML =
      `<span class="d-lbl">Try another</span><div class="d-chips">` +
      STARTER_ORDER.filter((key) => key !== lastKey)
        .map((key) => `<button class="d-chip" type="button" data-starter="${key}">${SCENARIOS[key].label}</button>`)
        .join("") +
      `<button class="d-chip plain" type="button" data-newsession>+ New session</button></div>`;
    return node;
  }

  function renderStream() {
    const nodes = focus.nodes.length ? focus.nodes : [greetingNode()];
    stream.replaceChildren(...nodes);
    nodes.forEach((node) => {
      if (node._route) paintRoute(node, node._route);
    });
    stick = true;
    follow();
  }

  function renderAll() {
    renderChrome();
    renderStream();
  }

  function addNode(s, node) {
    s.nodes.push(node);
    if (s === focus) {
      if (stream.querySelector(".d-greet")) stream.replaceChildren();
      stream.appendChild(node);
      stick = true;
      follow();
    }
  }

  function dropNode(s, node) {
    s.nodes = s.nodes.filter((n) => n !== node);
    node.remove();
  }

  /* ---------- running a script ---------- */

  function pause(r, ms) {
    if (r.stopped) return Promise.reject(STOP);
    return new Promise((resolve, reject) => {
      const t = window.setTimeout(() => {
        r.cancel = null;
        resolve();
      }, reduced.matches ? ms * 0.35 : ms);
      r.cancel = () => {
        window.clearTimeout(t);
        r.cancel = null;
        reject(STOP);
      };
    });
  }

  function act(r, icon, text, kind) {
    r.acts.push({ icon, html: rich(text), kind: kind || "" });
    r.drops += 1;
    r.tokens += 1500 + Math.floor(Math.random() * 1500);
    r.ctx = Math.min(r.ctx + 0.8, 18);
    r.rate = 92 + Math.floor(Math.random() * 26);
    paint(r);
  }

  function startStage(r, name) {
    r.ops.forEach((op) => {
      if (op.status === "active") {
        op.status = "done";
        op.note = `done · ${formatElapsed(Date.now() - op.start)}`;
      }
    });
    const op = r.ops.find((o) => o.name === name);
    op.status = "active";
    op.start = Date.now();
    op.passes += 1;
    r.now = op.title;
    if (op.passes > 1) act(r, "↺", `${op.title} again, pass ${op.passes}`);
    else paint(r);
  }

  function finishStage(r, name) {
    const op = r.ops.find((o) => o.name === name);
    op.status = "done";
    op.note = `done · ${formatElapsed(Date.now() - op.start)}`;
    act(r, "☰", `${op.title} done`);
  }

  function setReqs(r, statuses) {
    r.fresh = new Set();
    r.reqs.forEach((q, i) => {
      if (statuses[i] && q.status !== statuses[i]) {
        q.status = statuses[i];
        if (statuses[i] === "Verified" || statuses[i] === "Failed") r.fresh.add(q.id);
      }
    });
  }

  function addChange(r, path) {
    if (r.changes.some((ch) => ch.path === path)) return;
    const file = FILES[path];
    r.changes.push({ path, kind: file.kind, added: file.added, removed: file.removed, reverted: false });
    // Earlier runs' rows for the same file are superseded by this one.
    renderBar();
  }

  async function playActs(r, acts, ms) {
    for (const a of acts) {
      await pause(r, ms);
      act(r, a[0], a[1]);
      if (a[2]) addChange(r, a[2]);
    }
  }

  async function classify(r) {
    if (r.chosen !== "auto") {
      r.classified = true;
      return;
    }
    r.now = "Choosing a tier";
    paint(r);
    await pause(r, 800);
    r.classified = true;
    act(r, "◈", `Classified as *${r.tier}*`);
  }

  /* Waits on a card (a question or a plan) until the visitor answers. */
  function waitFor(r, kind, node) {
    r.waiting = kind;
    const hadFocus = inShell();
    addNode(r.session, node);
    paint(r);
    if (hadFocus) quietFocus(node.querySelector(".d-btn-primary"));
    return new Promise((resolve, reject) => {
      r.answer = (decision, reason) => {
        r.answer = null;
        r.cancel = null;
        r.waiting = null;
        resolve({ decision, reason });
      };
      r.cancel = () => {
        r.answer = null;
        r.cancel = null;
        r.waiting = null;
        dropNode(r.session, node);
        reject(STOP);
      };
    });
  }

  /* The sign-off stamp lands, then the card goes, as on the dashboard. */
  function settle(r, node, word, refused) {
    const hadFocus = node.contains(document.activeElement);
    node.classList.add("answered", refused ? "refused" : "approved");
    node.querySelector(".d-ask-stamp").textContent = word;
    node.inert = true;
    if (hadFocus) quietFocus(r.node);
    window.setTimeout(() => dropNode(r.session, node), reduced.matches ? 700 : 1100);
  }

  function questionNode(r, cmd, allow) {
    const node = document.createElement("section");
    node.className = "d-ask";
    node.setAttribute("aria-label", "I'd like to run a command");
    const at = r.ops.findIndex((op) => op.status === "active");
    node.innerHTML =
      `<svg class="d-outline" aria-hidden="true"><rect x="1" y="1" rx="13" ry="13"></rect></svg>` +
      `<span class="d-ask-stamp" aria-hidden="true"></span>` +
      `<div class="d-ask-head"><svg class="d-ask-nib" aria-hidden="true"><use href="#d-nib"></use></svg>` +
      `<div><strong>I'd like to run a command</strong>` +
      `<p class="d-ask-lede">In your shell, in this workspace, exactly as written. Nothing runs without your say-so.</p></div>` +
      `<span class="d-plate">paused at step ${at + 1}: ${r.ops[at].title}</span></div>` +
      `<pre class="d-args"><code>${esc(cmd)}</code></pre>` +
      `<div class="d-ask-actions">` +
      `<button type="button" class="d-btn d-btn-primary" data-decide="once">Run it</button>` +
      `<button type="button" class="d-btn" data-decide="session" title="Run this call, and later ${esc(allow)} … in this session, without asking">Always allow <code>${esc(allow)} …</code> here</button>` +
      `<button type="button" class="d-btn d-btn-deny" data-decide="deny">Don't run</button>` +
      `</div>` +
      `<input type="text" class="d-reason" aria-label="Or tell me what to do instead" placeholder="Or tell me what to do instead, and press Enter" />`;
    return node;
  }

  function planNode(r) {
    const node = document.createElement("section");
    node.className = "d-ask d-plan";
    node.setAttribute("aria-label", "Here is my plan");
    node.innerHTML =
      `<svg class="d-outline" aria-hidden="true"><rect x="1" y="1" rx="13" ry="13"></rect></svg>` +
      `<span class="d-ask-stamp" aria-hidden="true"></span>` +
      `<div class="d-ask-head"><svg class="d-ask-nib" aria-hidden="true"><use href="#d-nib"></use></svg>` +
      `<div><strong>Here is my plan</strong>` +
      `<p class="d-ask-lede">Nothing has changed yet. Approve it to start, or stop here.</p></div>` +
      `<span class="d-plate">no files changed</span></div>` +
      `<ol class="d-plan-body">${r.script.plan.map((step) => `<li>${rich(step)}</li>`).join("")}</ol>` +
      `<div class="d-ask-actions">` +
      `<button type="button" class="d-btn d-btn-primary" data-plan="approve">Approve and start</button>` +
      `<button type="button" class="d-btn d-btn-deny" data-plan="stop">Stop</button></div>`;
    return node;
  }

  async function runCommand(r, out) {
    const c = r.script;
    await pause(r, 800);
    if (!r.session.allowed.includes(c.allow)) {
      const node = questionNode(r, c.cmd, c.allow);
      const { decision, reason } = await waitFor(r, "ask", node);
      if (decision === "deny") {
        settle(r, node, "Not run", true);
        act(r, "⊘", `Not run: \`${c.cmd}\`${reason ? ` (${reason})` : ""}`, "denied");
        r.stopReason = "denied";
        r.stopped = true;
        throw STOP;
      }
      settle(r, node, decision === "session" ? "Allowed here" : "Approved", false);
      if (decision === "session") {
        r.session.allowed.push(c.allow);
        toast(`${c.allow} … will run without asking for the rest of this session.`);
      }
    }
    act(r, "▸", `Running \`${c.cmd}\``);
    await pause(r, 1200);
    act(r, out[0], out[1], out[2]);
  }

  async function playCraft(r) {
    const c = r.script;
    await classify(r);
    startStage(r, "plan");
    await playActs(r, c.planActs, 900);
    await pause(r, 900);
    r.reqs = c.reqs.map((desc, i) => ({ id: `R${i + 1}`, desc, status: "Not Started" }));
    finishStage(r, "plan");
    if (reviewEl.checked) {
      const node = planNode(r);
      const { decision } = await waitFor(r, "plan", node);
      if (decision === "stop") {
        dropNode(r.session, node);
        r.stopped = true;
        throw STOP;
      }
      settle(r, node, "Approved", false);
    }
    await pause(r, 500);
    startStage(r, "execute");
    await playActs(r, c.exec1, 1000);
    setReqs(r, c.reqs.map(() => "Implemented"));
    paint(r);
    if (c.cmd) await runCommand(r, c.out1);
    await pause(r, 700);
    finishStage(r, "execute");
    await pause(r, 400);
    startStage(r, "validate");
    await playActs(r, c.validate1, 900);
    await pause(r, 900);
    setReqs(r, c.check1);
    if (c.unmet) {
      act(r, "✗", `Checked the work: ${c.unmet} unmet`, "fail");
      await pause(r, 500);
      finishStage(r, "validate");
      await pause(r, 500);
      r.loopbacks.push({ to: "execute", unmet: c.unmet });
      r.drawLoop = true;
      act(r, "↩", `Sent back to Execute: ${c.unmet} unmet`, "fail");
      await pause(r, 1100);
      startStage(r, "execute");
      await playActs(r, c.exec2, 1000);
      setReqs(r, c.check1.map((s) => (s === "Failed" ? "Implemented" : s)));
      paint(r);
      if (c.cmd) await runCommand(r, c.out2);
      await pause(r, 600);
      finishStage(r, "execute");
      await pause(r, 400);
      startStage(r, "validate");
      await playActs(r, c.validate2, 900);
      await pause(r, 900);
    }
    setReqs(r, c.reqs.map(() => "Verified"));
    act(r, "✓", "Checked the work against the plan — it holds", "pass");
    await pause(r, 500);
    finishStage(r, "validate");
  }

  async function playSpark(r) {
    await classify(r);
    startStage(r, "answer");
    await playActs(r, r.script.acts, 950);
    await pause(r, 900);
    finishStage(r, "answer");
  }

  function startRun(text, key) {
    if (run || typing) return;
    const scKey = key || pickScenario(text);
    const sc = SCENARIOS[scKey];
    const effective = tier === "auto" ? sc.auto : tier;
    const s = focus;
    const title = text.split("\n")[0].trim();
    if (!s.runs.length) s.title = title.length > 80 ? `${title.slice(0, 79)}…` : title;
    s.tier = effective;
    const r = {
      session: s,
      key: scKey,
      n: s.runs.length + 1,
      title,
      tier: effective,
      chosen: tier,
      script: sc[effective],
      started: Date.now(),
      ops: (effective === "craft" ? ["plan", "execute", "validate"] : ["answer"]).map((name) => ({
        name,
        title: humanTier(name),
        status: "next",
        passes: 0,
        note: "next",
        start: 0,
      })),
      loopbacks: [],
      reqs: null,
      acts: [],
      changes: [],
      tokens: 0,
      ctx: 2,
      rate: 104,
      drops: 0,
      now: humanTier(effective === "craft" ? "plan" : "answer"),
      waiting: null,
      stopped: false,
      detailed: false,
      classified: false,
    };
    s.runs.push(r);
    s.nodes.filter((n) => n.classList.contains("d-again")).forEach((n) => dropNode(s, n));
    const hadFocus = inShell();
    const node = document.createElement("article");
    node.className = "d-card live";
    node.tabIndex = -1;
    node.dataset.uid = String(++uid);
    node.setAttribute("aria-label", `Run ${r.n}`);
    node.innerHTML = liveCardMarkup(r);
    r.node = node;
    run = r;
    promptEl.value = "";
    addNode(s, node);
    paint(r);
    if (hadFocus && !shell.contains(document.activeElement)) quietFocus(node);
    else if (hadFocus && (document.activeElement === startBtn || document.activeElement === promptEl)) quietFocus(node);
    (effective === "craft" ? playCraft(r) : playSpark(r))
      .then(() => finishRun(r, "done"))
      .catch((error) => {
        if (error !== STOP) throw error;
        finishRun(r, "stopped");
      });
  }

  function finishRun(r, outcome) {
    if (r.discarded || r.done) return;
    r.done = true;
    r.outcome = outcome;
    if (outcome === "done") r.tokens = r.script.tokens;
    r.ops.forEach((op) => {
      if (op.status === "active") op.status = outcome === "done" ? "done" : "halted";
      op.note = "";
    });
    const hadFocus = r.node.contains(document.activeElement);
    r.node.className = `d-card ${outcome === "done" ? "done" : "stopped"} just-finished`;
    r.node.innerHTML = receiptMarkup(r);
    r.node._routeKey = "";
    paintRoute(r.node, finalRoute(r));
    if (run === r) run = null;
    addNode(r.session, againNode(r.key));
    renderChrome();
    // Land on the finished sheet: its stamp, route and answer.
    if (r.session === focus) conv.scrollTop = Math.max(0, r.node.offsetTop - 8);
    if (hadFocus) quietFocus(r.node);
    window.setTimeout(() => r.node.classList.remove("just-finished"), 900);
  }

  function stopRun() {
    if (!run || run.stopped) return;
    run.stopReason = "cancelled";
    run.stopped = true;
    if (run.cancel) run.cancel();
  }

  /* A starter types itself into the request box, then starts. */
  function typeStarter(key) {
    if (run || typing) return;
    const text = SCENARIOS[key].prompt;
    typing = true;
    renderComposer();
    const done = () => {
      typing = false;
      startRun(text, key);
    };
    if (reduced.matches) {
      promptEl.value = text;
      window.setTimeout(done, 150);
      return;
    }
    let i = 0;
    promptEl.value = "";
    const tick = () => {
      if (!typing) return;
      i += 1;
      promptEl.value = text.slice(0, i);
      promptEl.scrollTop = promptEl.scrollHeight;
      typeTimer = window.setTimeout(tick, i < text.length ? 14 + Math.random() * 22 : 0);
      if (i >= text.length) {
        window.clearTimeout(typeTimer);
        typeTimer = window.setTimeout(done, 380);
      }
    };
    tick();
  }

  function submit() {
    if (run || typing) return;
    const text = promptEl.value.trim();
    if (!text) {
      composer.classList.remove("nudge");
      void composer.offsetWidth;
      composer.classList.add("nudge");
      quietFocus(promptEl);
      return;
    }
    startRun(text);
  }

  /* ---------- sessions, panels, toasts ---------- */

  function toast(text) {
    toastEl.textContent = text;
    toastEl.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toastEl.hidden = true;
    }, 3600);
  }

  function newSession() {
    if (run || typing) return;
    const s = makeSession();
    sessions.unshift(s);
    if (sessions.length > 3) sessions = sessions.slice(0, 3);
    focus = s;
    renderAll();
    if (inShell()) quietFocus(promptEl);
  }

  function focusSession(no) {
    const s = sessions.find((x) => x.no === no);
    if (!s || s === focus) return;
    if (run || typing) {
      toast("One run at a time in this demo: this one is still going.");
      return;
    }
    focus = s;
    renderAll();
  }

  function clearSession() {
    if (run || typing || !focus.runs.length) return;
    focus.runs = [];
    focus.nodes = [];
    focus.title = "New session";
    renderAll();
    toast("Cleared this session's history. Files stay as they are.");
    quietFocus(promptEl);
  }

  function section(name) {
    if (name === "sessions") return;
    if (name === "inbox") {
      const card = stream.querySelector(".d-ask:not(.answered)");
      if (run && run.waiting && card) {
        conv.scrollTop = Math.max(0, card.offsetTop - 12);
        quietFocus(card.querySelector(".d-btn-primary"));
      } else {
        toast("Nothing is waiting for you.");
      }
      return;
    }
    if (name === "tips") {
      toast(TIPS[tipIndex % TIPS.length]);
      tipIndex += 1;
      return;
    }
    toast(`${name} is in the full dashboard: install AutoSE to open it.`);
  }

  function openPanel(html, opener) {
    panel.innerHTML = html;
    overlay.hidden = false;
    lastOpener = opener || document.activeElement;
    quietFocus(panel.querySelector("[data-close]"));
  }

  function closePanel() {
    if (overlay.hidden) return;
    overlay.hidden = true;
    panel.innerHTML = "";
    if (lastOpener && document.contains(lastOpener) && shell.contains(lastOpener)) quietFocus(lastOpener);
    lastOpener = null;
  }

  function diffTable(path) {
    const file = FILES[path];
    let oldNo = 0;
    let newNo = 0;
    const rows = file.lines
      .map((line) => {
        const sign = line[0];
        const text = line.slice(1);
        let cls = "";
        let a = "";
        let b = "";
        if (sign === "-") {
          oldNo += 1;
          a = oldNo;
          cls = "del";
        } else if (sign === "+") {
          newNo += 1;
          b = newNo;
          cls = "add";
        } else {
          oldNo += 1;
          newNo += 1;
          a = oldNo;
          b = newNo;
        }
        const mark = sign === " " ? " " : sign === "-" ? "−" : "+";
        return `<tr class="${cls}"><td class="d-no">${a}</td><td class="d-no">${b}</td><td class="d-code"><span class="d-sign" aria-hidden="true">${mark}</span>${esc(text)}</td></tr>`;
      })
      .join("");
    return `<div class="d-diff-scroll"><table class="d-diff-table" aria-label="Diff of ${esc(path)}"><tbody>${rows}</tbody></table></div>`;
  }

  function openDiff(path, opener) {
    const changes = sessionChanges(focus).filter((ch) => !ch.reverted);
    if (!changes.length) {
      toast(path ? `${path} has no changes in this session yet.` : "Nothing changed in this session yet.");
      return;
    }
    const current = changes.find((ch) => ch.path === path) || changes[0];
    if (path && current.path !== path) toast(`${path} has no changes in this session yet.`);
    const added = changes.reduce((n, ch) => n + ch.added, 0);
    const removed = changes.reduce((n, ch) => n + ch.removed, 0);
    const html =
      `<div class="d-panel-top"><div><span class="d-lbl">File changes</span>` +
      `<h4 id="d-panel-title">What it wrote <em class="d-stat"><span class="d-added">+${added}</span> <span class="d-removed">−${removed}</span></em></h4></div>` +
      `</div>` +
      `<div class="d-diff-main"><nav class="d-diff-files" aria-label="Changed files">` +
      changes
        .map(
          (ch) =>
            `<button type="button" class="d-diff-file" data-difffile="${ch.path}" aria-pressed="${ch === current}">${changeBadge(ch)}<span class="d-diff-name">${ch.path}</span>` +
            `<span class="d-diff-counts"><span class="d-added">+${ch.added}</span> <span class="d-removed">−${ch.removed}</span></span></button>`
        )
        .join("") +
      `</nav><div class="d-diff-body" id="d-diff-body" tabindex="0" aria-label="Diff">` +
      `<div class="d-diff-head"><span class="d-diff-path">${current.path}</span><span class="d-diff-kind">${current.kind}</span></div>${diffTable(current.path)}</div></div>` +
      `<div class="d-panel-actions"><button type="button" class="d-btn" data-close>Close</button>` +
      `<button type="button" class="d-btn d-btn-primary" data-undo${run ? " disabled" : ""}>Undo these changes</button></div>`;
    openPanel(html, opener);
  }

  function showDiffFile(path) {
    panel.querySelectorAll("[data-difffile]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.difffile === path)));
    const ch = sessionChanges(focus).find((x) => x.path === path);
    $("d-diff-body").innerHTML =
      `<div class="d-diff-head"><span class="d-diff-path">${path}</span><span class="d-diff-kind">${ch ? ch.kind : ""}</span></div>${diffTable(path)}`;
  }

  function undoChanges() {
    if (run) return;
    let count = 0;
    focus.runs.forEach((r) => {
      r.changes.forEach((ch) => {
        if (!ch.reverted) {
          ch.reverted = true;
          count += 1;
        }
      });
      if (r.done && r.changes.length) {
        r.node.innerHTML = receiptMarkup(r);
        r.node._routeKey = "";
        paintRoute(r.node, finalRoute(r));
      }
    });
    lastOpener = $("d-sb-changes");
    closePanel();
    renderChrome();
    toast(`Put back ${plural(count, "file")} as ${count === 1 ? "it was" : "they were"}.`);
  }

  function openAllowed(opener) {
    const rules = focus.allowed;
    const html =
      `<div class="d-panel-top"><div><span class="d-lbl">Allowed commands</span><h4 id="d-panel-title">What runs without asking</h4></div>` +
      `</div>` +
      `<p class="d-panel-note">Session ${focus.no} only. A rule is a command's leading words: <code>python -m pytest</code> covers <code>python -m pytest -q</code>.</p>` +
      (rules.length
        ? `<div class="d-allowed-list">${rules
            .map(
              (rule) =>
                `<div class="d-allowed-row"><span class="d-allowed-kind">Command starting with</span><code>${esc(rule)}</code>` +
                `<button type="button" class="d-btn d-btn-small" data-unallow="${esc(rule)}">Remove</button></div>`
            )
            .join("")}</div>`
        : `<p class="d-empty">Nothing yet: every command, page fetch and search asks first. Press "Always allow" on a question to add a rule.</p>`) +
      `<div class="d-panel-actions"><button type="button" class="d-btn" data-close>Close</button></div>`;
    openPanel(html, opener);
  }

  function reset() {
    if (run) {
      run.discarded = true;
      if (run.cancel) run.cancel();
      run = null;
    }
    typing = false;
    window.clearTimeout(typeTimer);
    closePanel();
    toastEl.hidden = true;
    promptEl.value = "";
    sessions = [makeSession("S-4E2A")];
    focus = sessions[0];
    renderAll();
  }

  /* ---------- wiring ---------- */

  shell.addEventListener("click", (event) => {
    const b = event.target.closest("button");
    if (!b || !shell.contains(b) || b.disabled) return;
    const d = b.dataset;
    if (d.starter) typeStarter(d.starter);
    else if ("newsession" in d) newSession();
    else if (d.decide) {
      if (run && run.waiting === "ask" && run.answer) {
        const card = b.closest(".d-ask");
        run.answer(d.decide, card ? card.querySelector(".d-reason").value.trim() : "");
      }
    } else if (d.plan) {
      if (run && run.waiting === "plan" && run.answer) run.answer(d.plan === "stop" ? "stop" : "approve");
    } else if ("diff" in d) openDiff(d.diff, b);
    else if ("stop" in d) stopRun();
    else if ("details" in d) {
      if (run && b.closest(".d-card") === run.node) {
        run.detailed = !run.detailed;
        b.setAttribute("aria-pressed", String(run.detailed));
        paint(run);
      }
    } else if (d.session) focusSession(d.session);
    else if (d.sec) section(d.sec);
    else if (d.tier) {
      if (!run && !typing && d.tier !== "forge") {
        tier = d.tier;
        if (!focus.runs.length) focus.tier = tier === "spark" ? "spark" : "craft";
        renderChrome();
      }
    } else if ("close" in d) closePanel();
    else if ("undo" in d) undoChanges();
    else if (d.unallow) {
      focus.allowed = focus.allowed.filter((rule) => rule !== d.unallow);
      renderBar();
      openAllowed(lastOpener);
    } else if (d.difffile) showDiffFile(d.difffile);
  });

  startBtn.addEventListener("click", submit);
  stopBtn.addEventListener("click", stopRun);
  $("d-new").addEventListener("click", newSession);
  $("d-sb-clear").addEventListener("click", clearSession);
  $("d-sb-changes").addEventListener("click", (e) => openDiff("", e.currentTarget));
  $("d-sb-allowed").addEventListener("click", (e) => openAllowed(e.currentTarget));

  promptEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  // Enter in the reason box answers "Don't run" with what was typed.
  stream.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.classList.contains("d-reason")) return;
    event.preventDefault();
    if (run && run.waiting === "ask" && run.answer) run.answer("deny", event.target.value.trim());
  });

  conv.addEventListener("scroll", () => {
    stick = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 48;
  });

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closePanel();
  });

  shell.addEventListener("keydown", (event) => {
    if (overlay.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closePanel();
    } else if (event.key === "Tab") {
      const items = [...panel.querySelectorAll("button:not([disabled]), [tabindex='0']")];
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        quietFocus(last);
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        quietFocus(first);
      }
    }
  });

  // Window caption buttons.
  $("d-cap-min").addEventListener("click", () => {
    shell.classList.toggle("is-min");
    shell.classList.remove("is-max");
  });
  $("d-cap-max").addEventListener("click", () => {
    shell.classList.toggle("is-max");
    shell.classList.remove("is-min");
    window.requestAnimationFrame(renderStream);
  });
  $("d-cap-close").addEventListener("click", () => {
    reset();
    shell.hidden = true;
    relaunch.hidden = false;
    quietFocus($("d-relaunch-btn"));
  });
  $("d-relaunch-btn").addEventListener("click", () => {
    relaunch.hidden = true;
    shell.hidden = false;
    shell.classList.remove("is-min", "is-max");
    reset();
    quietFocus(promptEl);
  });

  // Routes are drawn at their card's width: redraw when the column resizes.
  if ("ResizeObserver" in window) {
    let frame = 0;
    new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        stream.querySelectorAll(".d-card").forEach((node) => {
          if (node._route) paintRoute(node, node._route);
        });
      });
    }).observe(conv);
  }

  // The clock: elapsed time on the live card and its row.
  window.setInterval(() => {
    if (!run || run.done) return;
    const el = run.node.querySelector(".d-elapsed");
    if (el) el.textContent = formatElapsed(Date.now() - run.started);
    renderList();
  }, 1000);

  reset();
})();
