// AutoSE interactive demo: a faithful miniature of the desktop app
// (desktop/src/main.ts on main), replayed with scripted sessions.
// Every control works: rail, settings, usage, sessions, window caps.
(function () {
  "use strict";

  var shell = document.getElementById("demo-shell");
  if (!shell) return;

  var stream = document.getElementById("demo-stream");
  var sessions = document.getElementById("d-sessions");
  var newTaskBtn = document.getElementById("d-newtask");
  var typedEl = document.getElementById("composer-text");
  var placeholderEl = document.getElementById("d-placeholder");
  var caretEl = document.getElementById("d-caret");
  var hintEl = document.getElementById("d-hint");
  var sendBtn = document.getElementById("d-send");
  var composerEl = shell.querySelector(".d-composer");
  var presenceEl = document.getElementById("d-presence");
  var presenceText = document.getElementById("d-presence-text");
  var tokensCell = document.getElementById("d-tokens-cell");
  var tokensEl = document.getElementById("d-tokens");
  var ctxFill = document.getElementById("d-ctx-fill");
  var ctxLabel = document.getElementById("d-ctx-label");
  var modelEl = document.getElementById("d-model");
  var dirEl = document.getElementById("d-dir");

  var settingsScreen = document.getElementById("d-settings");
  var usageScreen = document.getElementById("d-usage");
  var usageStats = document.getElementById("d-usage-stats");
  var relaunch = document.getElementById("d-relaunch");

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var CONTEXT_LIMIT = 262144;
  var HINT_IDLE = "ENTER ↵ STARTS · SHIFT+ENTER FOR A NEW LINE";
  var HINT_READY = "READY · PRESS START WORK";
  var HINT_WORKING = "WORKING · STOP TO INTERRUPT";

  /* ---------- settings state ---------- */
  var settings = {
    baseUrl: "http://localhost:11434/v1",
    model: "qwen3-coder:30b",
    dir: "C:\\Users\\you\\projects\\bookstore",
    handsFree: false
  };

  function shortenPath(path) {
    var parts = path.split(/[\\/]/).filter(Boolean);
    if (parts.length <= 2) return path;
    return "…\\" + parts.slice(-2).join("\\");
  }

  function applySettings() {
    modelEl.textContent = settings.model || "not set";
    dirEl.textContent = shortenPath(settings.dir);
  }

  /* ---------- the scripted sessions ---------- */
  // Event shapes: ["stage", name, thinkingLabel], ["act", kind, icon, html],
  // ["narr", text], ["tok", total]. Delays are per-event, in ms.
  var SCENARIOS = [
    {
      label: "Build a book API",
      prompt: "Add a small REST API for the book library: create, update, search and lend books.",
      events: [
        [500, "stage", "Plan", "Sketching the plan…"],
        [700, "act", "read", "▤", "Looking around the project"],
        [800, "act", "read", "◉", "Reading <code>pyproject.toml</code>"],
        [800, "act", "search", "⌕", "Searching the code for <code>router</code>"],
        [900, "act", "note", "☰", "Sketched the plan"],
        [600, "act", "note", "✓", "Plan looks good, moving on"],
        [400, "narr", "Three thin layers (routes, service, store) so the web framework stays at the edge and storage can swap freely later."],
        [300, "tok", 3900],
        [700, "stage", "Design", "Drafting the design…"],
        [900, "act", "note", "◫", "Drafted the design"],
        [300, "tok", 6800],
        [700, "stage", "Build", "Building…"],
        [900, "act", "edit", "✎", "Writing <code>api/routes_books.py</code>"],
        [900, "act", "edit", "✎", "Writing <code>services/books.py</code>"],
        [800, "act", "edit", "✎", "Writing <code>store/repository.py</code>"],
        [800, "act", "edit", "✎", "Writing <code>tests/test_books.py</code>"],
        [300, "tok", 13400],
        [700, "stage", "Check", "Checking the work…"],
        [900, "act", "run", "▸", "Running <code>pytest -q</code>"],
        [1300, "act", "test-pass", "✓", "Check passed: Pytest"],
        [700, "act", "note", "☑", "Reviewed the work against the plan"],
        [300, "tok", 18204]
      ],
      receipt: {
        summary: "Your library has a small REST API now: create, update, search and lend books. Search covers title and author, and the tests exercise every route.",
        changed: ["api/routes_books.py", "services/books.py", "store/repository.py", "tests/test_books.py"],
        checks: [["Pytest", true]],
        next: [
          "Add pagination to the search endpoint.",
          "Point AutoSE at a real database when you outgrow SQLite."
        ],
        notes: "[ THE PLAN ]\n1. Routes at the edge (FastAPI router).\n2. BookService owns the rules.\n3. BookRepository hides SQLite.\n4. Tests per route, search first."
      }
    },
    {
      label: "Rate-limit payments",
      prompt: "Add rate limiting to the payments service: throttle each client past 20 requests a second.",
      // This one listens to Settings: with "Work hands-free" off, commands
      // are skipped instead of run, exactly like the real app.
      events: function (handsFree) {
        var events = [
          [500, "stage", "Plan", "Sketching the plan…"],
          [700, "act", "read", "▤", "Looking around the project"],
          [800, "act", "read", "◉", "Reading <code>middleware/__init__.py</code>"],
          [900, "act", "note", "☰", "Sketched the plan"],
          [600, "act", "note", "✓", "Plan looks good, moving on"],
          [400, "narr", "A token bucket in middleware, keyed by API token. No new services: the org profile rules out Redis at POC stage."],
          [300, "tok", 4300],
          [700, "stage", "Build", "Building…"],
          [900, "act", "edit", "✎", "Writing <code>middleware/rate_limit.py</code>"],
          [800, "act", "edit", "✎", "Writing <code>config/limits.yaml</code>"],
          [800, "act", "edit", "✎", "Writing <code>tests/test_rate_limit.py</code>"],
          [300, "tok", 11200],
          [700, "stage", "Check", "Checking the work…"]
        ];
        if (handsFree) {
          events.push(
            [900, "act", "run", "▸", "Running <code>pytest -q tests/test_rate_limit.py</code>"],
            [1200, "act", "test-pass", "✓", "Check passed: Rate limit window"],
            [700, "act", "test-pass", "✓", "Check passed: Retry-After header"],
            [700, "act", "note", "☑", "Reviewed the work against the plan"],
            [300, "tok", 15650]
          );
        } else {
          events.push(
            [900, "act", "denied", "⊘", "Skipped <code>pytest -q tests/test_rate_limit.py</code> (hands-free mode is off)"],
            [800, "act", "note", "☑", "Reviewed the work against the plan"],
            [300, "tok", 13100]
          );
        }
        return events;
      },
      receipt: function (handsFree) {
        return {
          summary: handsFree
            ? "Requests to /payments beyond 20 per second now get a 429 with a Retry-After header. Both checks pass. The bucket lives in process, one less moving part, and it resets on deploy."
            : "Requests to /payments beyond 20 per second now get a 429 with a Retry-After header. The bucket lives in process, one less moving part, and it resets on deploy.",
          changed: ["middleware/rate_limit.py", "config/limits.yaml", "tests/test_rate_limit.py"],
          checks: handsFree ? [["Rate limit window", true], ["Retry-After header", true]] : [],
          cmdsSkipped: !handsFree,
          next: handsFree
            ? ["Watch p95 latency on /payments for a day before tightening the limit."]
            : ["I skipped a command because hands-free mode is off. Turn on “Work hands-free” in Settings and ask me again for a fully finished job."],
          notes: "[ THE PLAN ]\n1. TokenBucket per client key.\n2. Middleware before the payments router.\n3. Limits in config/limits.yaml.\n4. Tests for the window and the header."
        };
      }
    },
    {
      label: "Test the CSV parser",
      prompt: "Write tests for the CSV import parser. Cover quoting, BOM and malformed rows.",
      events: [
        [500, "stage", "Plan", "Sketching the plan…"],
        [800, "act", "read", "◉", "Reading <code>data/csv_parser.py</code>"],
        [800, "act", "search", "⌕", "Searching the code for <code>quotechar</code>"],
        [900, "act", "note", "☰", "Sketched the plan"],
        [600, "act", "note", "✓", "Plan looks good, moving on"],
        [400, "narr", "Table-driven tests, one fixture per failure family: quoting, BOM, malformed rows."],
        [300, "tok", 2900],
        [700, "stage", "Build", "Building…"],
        [1000, "act", "edit", "✎", "Writing <code>tests/test_csv_parser.py</code>"],
        [800, "act", "edit", "✎", "Writing <code>tests/fixtures/malformed.csv</code>"],
        [300, "tok", 6400],
        [700, "stage", "Check", "Checking the work…"],
        [900, "act", "run", "▸", "Running <code>pytest -q tests/test_csv_parser.py</code>"],
        [1200, "act", "test-fail", "✗", "Check failed: Quoted commas"],
        [800, "act", "edit", "✎", "Editing <code>tests/test_csv_parser.py</code>"],
        [800, "act", "run", "▸", "Running <code>pytest -q tests/test_csv_parser.py</code>"],
        [1200, "act", "test-pass", "✓", "Check passed: Pytest (12 tests)"],
        [700, "act", "note", "☑", "Reviewed the work against the plan"],
        [300, "tok", 9820]
      ],
      receipt: {
        summary: "The parser has twelve tests now. One expectation was wrong on the first pass (quoted commas), so I fixed the case and re-ran until everything held.",
        changed: ["tests/test_csv_parser.py", "tests/fixtures/malformed.csv"],
        checks: [["Quoted commas", true], ["BOM handling", true], ["Malformed rows", true]],
        next: ["Wire these tests into your CI so regressions get caught on push."],
        notes: "[ THE PLAN ]\n1. Parametrized cases per failure family.\n2. One fixture per family under tests/fixtures/.\n3. Re-run until green."
      }
    }
  ];

  /* ---------- state ---------- */
  var running = false;
  var stopFlag = false;
  var reqCounter = 0;
  var currentTokens = 0;
  var run = null;
  var pendingScenario = null;
  var chats = [];
  var currentChat = null;
  var stats = { tasks: 0, tokens: 0, ms: 0 };

  /* ---------- helpers ---------- */
  function wait(ms) {
    return new Promise(function (r) { setTimeout(r, reduced ? Math.min(ms, 40) : ms); });
  }

  function scrollDown() {
    stream.scrollTop = stream.scrollHeight;
  }

  function addEntry(cls, html) {
    var el = document.createElement("article");
    el.className = "d-entry " + cls;
    el.innerHTML = html;
    stream.appendChild(el);
    scrollDown();
    return el;
  }

  function reqLabel(n) {
    return "REQ-" + String(n).padStart(2, "0");
  }

  function formatElapsed(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function setPresence(state, text) {
    presenceEl.classList.toggle("working", state === "working");
    presenceText.textContent = text;
  }

  function updateTokens(total) {
    currentTokens = total;
    tokensCell.hidden = false;
    tokensEl.textContent = total.toLocaleString();
    var pct = Math.min(100, (total / CONTEXT_LIMIT) * 100);
    ctxFill.style.width = pct + "%";
    ctxLabel.textContent = "CTX " + pct.toFixed(pct >= 10 ? 0 : 1) + "%";
  }

  function eyebrow(text) {
    return '<span class="d-eyebrow"><span class="d-tick"></span>' + text + "</span>";
  }

  /* ---------- greeting & chips ---------- */
  function chipsHtml(exceptIndex) {
    return SCENARIOS.map(function (s, i) {
      if (i === exceptIndex) return "";
      return '<button class="d-chip" type="button" data-scenario="' + i + '">' + s.label + "</button>";
    }).join("");
  }

  function wireChips(scope) {
    scope.querySelectorAll(".d-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        if (running) return;
        stageScenario(SCENARIOS[Number(chip.dataset.scenario)]);
      });
    });
  }

  function showGreeting() {
    var hour = new Date().getHours();
    var salutation = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    stream.innerHTML = "";
    var g = addEntry("d-greeting", eyebrow(salutation + " · Engineering runtime ready") +
      '<h3>What are we <span class="d-underline">building</span> today?</h3>' +
      "<p>Describe a feature, a bug, or a question in plain English. AutoSE reads your project, drafts a plan, does the work, and files a report showing exactly what changed. All of it on this machine.</p>" +
      '<div class="d-chips">' + chipsHtml(-1) + "</div>");
    wireChips(g);
  }

  /* ---------- the work card ---------- */
  function startWorkCard(reqNumber) {
    var card = addEntry("d-work d-cropped running",
      '<div class="d-workhead">' +
      '<span class="d-presence-dot"></span>' +
      '<span class="d-eyebrow">Worklog / ' + reqLabel(reqNumber) + "</span>" +
      '<span class="d-worktitle">Reading your request</span>' +
      '<span class="d-elapsed">0:00</span>' +
      "</div>" +
      '<div class="d-stages" hidden></div>' +
      '<div class="d-activity"></div>' +
      '<div class="d-narration" hidden></div>');

    run = { card: card, stages: [], startedAt: Date.now(), timerId: undefined, reqNumber: reqNumber };
    run.timerId = window.setInterval(function () {
      var el = card.querySelector(".d-elapsed");
      if (el) el.textContent = formatElapsed(Date.now() - run.startedAt);
    }, 1000);
  }

  function setThinking(label) {
    var title = run.card.querySelector(".d-worktitle");
    if (title) title.textContent = label;
    setPresence("working", "Working / " + label);
  }

  function renderStages() {
    var host = run.card.querySelector(".d-stages");
    host.hidden = run.stages.length === 0;
    host.innerHTML = run.stages.map(function (stage, i) {
      return '<span class="d-stage ' + stage.status + '">' +
        '<span class="d-stage-id">PH-' + String(i + 1).padStart(2, "0") + "</span>" +
        '<span class="d-stage-name">' + stage.name + "</span></span>";
    }).join("");
  }

  function pushStage(name, thinking) {
    run.stages.forEach(function (s) { s.status = "done"; });
    run.stages.push({ name: name, status: "active" });
    renderStages();
    setThinking(thinking);
  }

  function pushActivity(kind, icon, html) {
    var host = run.card.querySelector(".d-activity");
    var line = document.createElement("div");
    line.className = "d-actline kind-" + kind;
    line.innerHTML = '<span class="d-acticon">' + icon + "</span><span>" + html + "</span>";
    host.appendChild(line);
    scrollDown();
  }

  function streamNarration(text) {
    var narr = run.card.querySelector(".d-narration");
    narr.hidden = false;
    if (reduced) {
      narr.textContent = text;
      scrollDown();
      return Promise.resolve();
    }
    var words = text.split(" ");
    var i = 0;
    return new Promise(function (resolve) {
      (function tick() {
        if (i < words.length) {
          narr.textContent += (i ? " " : "") + words[i++];
          scrollDown();
          setTimeout(tick, 34);
        } else {
          resolve();
        }
      })();
    });
  }

  function stopRunTimer() {
    if (run && run.timerId !== undefined) {
      window.clearInterval(run.timerId);
      run.timerId = undefined;
    }
  }

  /* ---------- finishing ---------- */
  function fact(label, tone) {
    return '<span class="d-fact ' + (tone || "") + '"><span class="d-fdot"></span>' + label + "</span>";
  }

  function receiptSection(title, items) {
    return '<div class="d-rsection">' + eyebrow(title) + "<ul>" + items.join("") + "</ul></div>";
  }

  function settleCard(state) {
    stopRunTimer();
    run.card.classList.remove("running");
    run.card.classList.add(state);
    var head = run.card.querySelector(".d-workhead");
    if (head) head.remove();
    run.card.querySelector(".d-narration").hidden = true;
    run.stages.forEach(function (s) { s.status = "done"; });
    renderStages();
  }

  function finishWorkCard(receipt) {
    settleCard("done");
    var elapsed = formatElapsed(Date.now() - run.startedAt);

    var passed = receipt.checks.filter(function (c) { return c[1]; }).length;
    var facts = [fact("FILES " + receipt.changed.length)];
    if (receipt.checks.length) {
      facts.push(fact("CHECKS " + passed + "/" + receipt.checks.length, passed === receipt.checks.length ? "good" : "warn"));
    }
    facts.push(fact("TIME " + elapsed, "good"));
    facts.push(fact("TOKENS " + currentTokens.toLocaleString()));
    if (receipt.cmdsSkipped) facts.push(fact("CMDS SKIPPED", "warn"));

    var sections = [
      receiptSection("What changed", receipt.changed.map(function (file) {
        return '<li><span class="d-limark">✎</span><span class="d-file">' + file + "</span></li>";
      }))
    ];
    if (receipt.checks.length) {
      sections.push(receiptSection("Checks", receipt.checks.map(function (check) {
        return '<li><span class="d-limark ' + (check[1] ? "pass" : "fail") + '">' + (check[1] ? "✓" : "✗") +
          "</span><span>" + check[0] + "</span></li>";
      })));
    }
    if (receipt.next && receipt.next.length) {
      sections.push(receiptSection("What you might want next", receipt.next.map(function (item) {
        return '<li><span class="d-limark">→</span><span>' + item + "</span></li>";
      })));
    }

    run.card.insertAdjacentHTML("beforeend",
      '<div class="d-receipt">' +
      eyebrow("Report / " + reqLabel(run.reqNumber)) +
      '<div class="d-headline">Work complete.</div>' +
      '<div class="d-facts">' + facts.join("") + "</div>" +
      '<div class="d-summary">' + receipt.summary + "</div>" +
      sections.join("") +
      (receipt.notes ? '<details class="d-worknotes"><summary>Working notes</summary><div class="d-worknotes-body">' + receipt.notes + "</div></details>" : "") +
      "</div>");
    scrollDown();
  }

  function stopWorkCard() {
    settleCard("stopped");
    run.card.insertAdjacentHTML("beforeend",
      '<div class="d-receipt">' +
      eyebrow("Report / " + reqLabel(run.reqNumber)) +
      '<div class="d-headline">Stopped at your request.</div>' +
      '<div class="d-facts">' + fact("STOPPED AT " + formatElapsed(Date.now() - run.startedAt), "warn") + "</div>" +
      '<div class="d-summary">You stopped this task before it finished. Any files it already changed stay changed. Start a new requirement to pick the work back up or to undo it.</div>' +
      "</div>");
    scrollDown();
  }

  /* ---------- session history (the rail) ---------- */
  function setActiveRow(row) {
    sessions.querySelectorAll(".d-session").forEach(function (r) {
      r.classList.toggle("active", r === row);
    });
  }

  function upsertSessionRow(chat) {
    var empty = sessions.querySelector(".d-empty");
    if (empty) empty.remove();

    if (!chat.row) {
      var row = document.createElement("div");
      row.className = "d-session";
      row.innerHTML =
        '<button class="d-sessionbtn" type="button">' +
        '<span class="d-dot"></span>' +
        '<span class="d-task"></span>' +
        "<small></small></button>" +
        '<button class="d-sessiondel" type="button" aria-label="Delete this task" title="Delete">✕</button>';
      sessions.insertBefore(row, sessions.firstChild);
      chat.row = row;

      row.querySelector(".d-sessionbtn").addEventListener("click", function () {
        if (running) return;
        restoreChat(chat);
      });
      var del = row.querySelector(".d-sessiondel");
      del.addEventListener("click", function () {
        if (running) return;
        if (!del.classList.contains("armed")) {
          del.classList.add("armed");
          window.setTimeout(function () { del.classList.remove("armed"); }, 2000);
          return;
        }
        deleteChat(chat);
      });
    }

    chat.row.querySelector(".d-dot").dataset.status = chat.status;
    chat.row.querySelector(".d-task").textContent = chat.task;
    chat.row.querySelector("small").textContent =
      "JUST NOW · " + chat.tokens.toLocaleString() + " TOKENS";
    setActiveRow(chat.row);
  }

  function restoreChat(chat) {
    currentChat = chat;
    reqCounter = chat.reqCount;
    stream.innerHTML = chat.html;
    wireChips(stream);
    setActiveRow(chat.row);
    scrollDown();
  }

  function deleteChat(chat) {
    chats = chats.filter(function (c) { return c !== chat; });
    chat.row.remove();
    if (currentChat === chat) {
      currentChat = null;
      reqCounter = 0;
      showGreeting();
    }
    if (!chats.length) {
      sessions.innerHTML = '<p class="d-empty">Nothing on the board yet. Finished tasks land here.</p>';
    }
  }

  /* ---------- composer ---------- */
  function typePrompt(text) {
    placeholderEl.hidden = true;
    caretEl.hidden = false;
    typedEl.textContent = "";
    if (reduced) {
      typedEl.textContent = text;
      return wait(150);
    }
    var i = 0;
    return new Promise(function (resolve) {
      (function tick() {
        if (i < text.length) {
          typedEl.textContent += text.charAt(i++);
          setTimeout(tick, 12 + Math.random() * 20);
        } else {
          resolve();
        }
      })();
    });
  }

  function clearComposer() {
    typedEl.textContent = "";
    caretEl.hidden = true;
    placeholderEl.hidden = false;
  }

  function nudgeComposer() {
    composerEl.classList.remove("nudge");
    void composerEl.offsetWidth;
    composerEl.classList.add("nudge");
    window.setTimeout(function () { composerEl.classList.remove("nudge"); }, 260);
  }

  function setRunGate() {
    if (running) {
      sendBtn.textContent = "Stop";
      sendBtn.classList.add("stop");
      hintEl.textContent = HINT_WORKING;
    } else {
      sendBtn.textContent = "Start work";
      sendBtn.classList.remove("stop");
      hintEl.textContent = pendingScenario ? HINT_READY : HINT_IDLE;
    }
    sendBtn.disabled = false;
    newTaskBtn.disabled = running;
    shell.classList.toggle("is-running", running);
    shell.querySelectorAll(".d-chip").forEach(function (chip) {
      chip.disabled = running;
    });
  }

  /* ---------- staging & running a session ---------- */
  function stageScenario(scenario) {
    if (running) return;
    pendingScenario = scenario;
    typePrompt(scenario.prompt).then(function () {
      if (!running) setRunGate();
    });
  }

  function startScenario(scenario) {
    running = true;
    stopFlag = false;
    pendingScenario = null;
    setRunGate();
    reqCounter += 1;
    var reqNumber = reqCounter;
    var startedAt = Date.now();

    if (!currentChat) {
      currentChat = { task: scenario.prompt, status: "done", tokens: 0, reqCount: 0, html: "", row: null };
      chats.push(currentChat);
    }

    // remove any greeting or "next requirement" nudge
    clearComposer();
    stream.querySelectorAll(".d-greeting, .d-again").forEach(function (el) { el.remove(); });

    addEntry("d-slip",
      '<span class="d-eyebrow">' + reqLabel(reqNumber) + " / You</span>" +
      '<div class="d-slip-text">' + scenario.prompt + "</div>");
    startWorkCard(reqNumber);
    updateTokens(0);
    setPresence("working", "Working / " + reqLabel(reqNumber));

    var events = typeof scenario.events === "function" ? scenario.events(settings.handsFree) : scenario.events;
    var receipt = typeof scenario.receipt === "function" ? scenario.receipt(settings.handsFree) : scenario.receipt;

    wait(700)
      .then(function () {
        return playEvents(events, 0);
      })
      .then(function (completed) {
        if (completed) {
          finishWorkCard(receipt);
          currentChat.status = "done";
        } else {
          stopWorkCard();
          currentChat.status = "stopped";
        }
        setPresence("ready", "Ready");
        stats.tasks += 1;
        stats.tokens += currentTokens;
        stats.ms += Date.now() - startedAt;

        running = false;
        // offer the remaining requirements
        var idx = SCENARIOS.indexOf(scenario);
        var again = addEntry("d-again", eyebrow("Next requirement") +
          '<div class="d-chips">' + chipsHtml(idx) + "</div>");
        wireChips(again);
        scrollDown();
        setRunGate();

        currentChat.tokens += currentTokens;
        currentChat.reqCount = reqCounter;
        currentChat.html = stream.innerHTML;
        upsertSessionRow(currentChat);
      });
  }

  function playEvents(events, i) {
    if (stopFlag) return Promise.resolve(false);
    if (i >= events.length) return Promise.resolve(true);
    var ev = events[i];
    return wait(ev[0]).then(function () {
      if (stopFlag) return false;
      switch (ev[1]) {
        case "stage":
          pushStage(ev[2], ev[3]);
          break;
        case "act":
          pushActivity(ev[2], ev[3], ev[4]);
          break;
        case "narr":
          return streamNarration(ev[2]).then(function () {
            return playEvents(events, i + 1);
          });
        case "tok":
          updateTokens(ev[2]);
          break;
      }
      return playEvents(events, i + 1);
    });
  }

  /* ---------- overlays ---------- */
  function openSettings() {
    document.getElementById("ds-baseurl").value = settings.baseUrl;
    document.getElementById("ds-model").value = settings.model;
    document.getElementById("ds-dir").value = settings.dir;
    document.getElementById("ds-handsfree").checked = settings.handsFree;
    settingsScreen.hidden = false;
  }

  function saveSettings() {
    settings.baseUrl = document.getElementById("ds-baseurl").value.trim() || settings.baseUrl;
    settings.model = document.getElementById("ds-model").value.trim() || settings.model;
    settings.dir = document.getElementById("ds-dir").value.trim() || settings.dir;
    settings.handsFree = document.getElementById("ds-handsfree").checked;
    applySettings();
    settingsScreen.hidden = true;
  }

  function openUsage() {
    var mins = Math.floor(stats.ms / 60000);
    var secs = Math.floor((stats.ms % 60000) / 1000);
    var statTile = function (label, value, sub) {
      return '<div class="d-stat"><span class="d-stat-label">' + label + '</span><span class="d-stat-value">' + value + "</span>" +
        (sub ? '<span class="d-stat-sub">' + sub + "</span>" : "") + "</div>";
    };
    usageStats.innerHTML = stats.tasks
      ? statTile("Tasks", String(stats.tasks), "this visit") +
      statTile("Tokens", stats.tokens.toLocaleString(), "all local") +
      statTile("Time", (mins ? mins + "m " : "") + secs + "s", "at the bench") +
      statTile("Cloud calls", "0", "always")
      : '<p class="d-usage-empty">Nothing on the ledger yet. Run a requirement and come back.</p>';
    usageScreen.hidden = false;
  }

  /* ---------- wiring ---------- */
  sendBtn.addEventListener("click", function () {
    if (running) {
      stopFlag = true;
    } else if (pendingScenario) {
      startScenario(pendingScenario);
    } else {
      nudgeComposer();
    }
  });

  newTaskBtn.addEventListener("click", function () {
    if (running) return;
    currentChat = null;
    reqCounter = 0;
    pendingScenario = null;
    clearComposer();
    setActiveRow(null);
    showGreeting();
    setRunGate();
  });

  document.getElementById("d-railtoggle").addEventListener("click", function () {
    shell.classList.toggle("rail-closed");
  });

  document.getElementById("d-settingsbtn").addEventListener("click", openSettings);
  document.getElementById("d-dirbtn").addEventListener("click", openSettings);
  document.getElementById("ds-cancel").addEventListener("click", function () { settingsScreen.hidden = true; });
  document.getElementById("ds-save").addEventListener("click", saveSettings);

  document.getElementById("d-usagebtn").addEventListener("click", openUsage);
  tokensCell.addEventListener("click", openUsage);
  document.getElementById("du-close").addEventListener("click", function () { usageScreen.hidden = true; });

  [settingsScreen, usageScreen].forEach(function (overlay) {
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) overlay.hidden = true;
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (!usageScreen.hidden) usageScreen.hidden = true;
    else if (!settingsScreen.hidden) settingsScreen.hidden = true;
  });

  // window caption buttons
  document.getElementById("d-cap-min").addEventListener("click", function () {
    shell.classList.toggle("is-min");
    shell.classList.remove("is-max");
  });
  document.getElementById("d-cap-max").addEventListener("click", function () {
    shell.classList.toggle("is-max");
    shell.classList.remove("is-min");
  });
  document.getElementById("d-cap-close").addEventListener("click", function () {
    shell.hidden = true;
    relaunch.hidden = false;
  });
  document.getElementById("d-relaunch-btn").addEventListener("click", function () {
    relaunch.hidden = true;
    shell.hidden = false;
    shell.classList.remove("is-min", "is-max");
  });

  /* ---------- boot ---------- */
  sessions.innerHTML = '<p class="d-empty">Nothing on the board yet. Finished tasks land here.</p>';
  applySettings();
  showGreeting();
  setPresence("ready", "Ready");
  setRunGate();
})();
