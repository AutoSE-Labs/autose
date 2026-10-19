/* The page as a living thing: small, quiet signs of what is going on.
 *
 * - Rest: after a while with nothing running and nothing touched, the nib caps
 *   itself and dozes; after a long while the page dims for the night. Any
 *   move wakes it.
 * - Moments: the redline loop draws itself when work is sent back, the nib
 *   scratches at the station it returned to, a check balloon pops when it
 *   comes to hold, and the nib nods (or shakes its head) when a run ends.
 * - Unseen: a dot on a sheet where something happened while you were not
 *   looking, and a "since you looked" line in its steps.
 *
 * `app.js` owns the state and calls in here; these only read it. With reduced
 * motion asked for, nothing here animates: the stylesheet stops its own
 * animations, and `playFrom` stops the scripted ones.
 */

const REST_AFTER_MS = 90 * 1000;
const NIGHT_AFTER_MS = 10 * 60 * 1000;
let lastStir = Date.now();

/* Sessions where something happened while you were not looking at them. */
const unseen = new Set();

function reduceMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* Play a scripted animation as if it had started at `since`: a part of a card
 * that is redrawn mid-way carries on instead of starting over. */
function playFrom(node, keyframes, duration, since, easing = "ease-out") {
  if (!node || !node.animate || reduceMotion()) return;
  const elapsed = Date.now() - since;
  if (elapsed >= duration) return;
  node.animate(keyframes, { duration, delay: -elapsed, easing, fill: "backwards" });
}

/* ---------- rest ---------- */

function somethingGoingOn() {
  if (!state) return false;
  return Boolean(state.running) || (state.open || []).some((entry) => entry.running) || approvals.length > 0;
}

function applyRest() {
  const quiet = somethingGoingOn() || document.hidden ? 0 : Date.now() - lastStir;
  const resting = quiet > REST_AFTER_MS;
  const night = quiet > NIGHT_AFTER_MS;
  const changed = document.body.classList.contains("resting") !== resting;
  document.body.classList.toggle("resting", resting);
  document.body.classList.toggle("night", night);
  document
    .querySelectorAll(".brand-nib use, .greeting-nib use")
    .forEach((use) => use.setAttribute("href", resting ? "#nib-sleep" : "#nib"));
  if (changed && state) renderStatus();
}

function stir() {
  lastStir = Date.now();
  if (document.body.classList.contains("resting")) applyRest();
}

["pointerdown", "pointermove", "keydown", "wheel", "touchstart"].forEach((type) =>
  window.addEventListener(type, stir, { passive: true })
);
window.setInterval(applyRest, 5000);

/* ---------- moments ---------- */

/* After the live route is redrawn: draw a fresh send-back, let the nib
 * scratch where the work went back to, and pop balloons that just changed. */
function animateLiveSheet(run) {
  const route = run.dom && run.dom.stages;
  if (!route) return;
  const back = run.loopbacks[run.loopbacks.length - 1];
  if (back && back.at) {
    const loops = route.querySelectorAll(".redloop");
    const loop = loops[loops.length - 1];
    if (loop && loop.getTotalLength) {
      const length = Math.ceil(loop.getTotalLength());
      playFrom(
        loop,
        [
          { strokeDasharray: `${length}`, strokeDashoffset: length },
          { strokeDasharray: `${length}`, strokeDashoffset: 0 },
        ],
        1100,
        back.at
      );
    }
    route.querySelectorAll(".revtri, .revtxt, .margin-note").forEach((node) =>
      playFrom(node, [{ opacity: 0 }, { opacity: 0 }, { opacity: 1 }], 1500, back.at)
    );
    playFrom(
      route.querySelector(".nib-on-route use"),
      [
        { transform: "translateX(0)" },
        { transform: "translateX(-3px)" },
        { transform: "translateX(3px)" },
        { transform: "translateX(-3px)" },
        { transform: "translateX(3px)" },
        { transform: "translateX(0)" },
      ],
      700,
      back.at,
      "linear"
    );
  }
  const balloons = run.dom.stamps ? run.dom.stamps.querySelectorAll(".balloon") : [];
  (run.requirements || []).forEach((item, index) => {
    const changed = run.reqChangedAt && run.reqChangedAt[item.id];
    if (!changed || !balloons[index]) return;
    const pop =
      item.status === "Verified"
        ? [{ transform: "scale(1)" }, { transform: "scale(1.35)" }, { transform: "scale(1)" }]
        : [
            { transform: "translateX(0)" },
            { transform: "translateX(-3px)" },
            { transform: "translateX(3px)" },
            { transform: "translateX(0)" },
          ];
    playFrom(balloons[index], pop, 600, changed);
  });
}

/* Note when each requirement changes status, so its balloon can mark it. */
function noteRequirementChanges(run, requirements) {
  run.reqSeen = run.reqSeen || {};
  run.reqChangedAt = run.reqChangedAt || {};
  requirements.forEach((item) => {
    const before = run.reqSeen[item.id];
    if (before !== undefined && before !== item.status) run.reqChangedAt[item.id] = Date.now();
    run.reqSeen[item.id] = item.status;
  });
}

/* The brand nib reacts to a run ending in front of you: a nod for work that
 * holds, a shake of the head for work that does not. A stop is your call, so
 * it gets neither. */
function reactToFinish(turn) {
  const nib = document.querySelector(".brand-nib");
  if (!nib || turn.stop_reason === "cancelled") return;
  const frames = turn.succeeded
    ? [
        { transform: "rotate(0)" },
        { transform: "rotate(12deg)" },
        { transform: "rotate(0)" },
        { transform: "rotate(12deg)" },
        { transform: "rotate(0)" },
      ]
    : [
        { transform: "rotate(0)" },
        { transform: "rotate(-10deg)" },
        { transform: "rotate(10deg)" },
        { transform: "rotate(-8deg)" },
        { transform: "rotate(0)" },
      ];
  playFrom(nib, frames, 900, Date.now(), "ease-in-out");
}

/* ---------- unseen ---------- */

/* Something happened in session `id`; mark it if you were not looking. */
function markUnseen(id) {
  if (id && (document.hidden || !isFocused(id))) unseen.add(id);
}

function unseenDotMarkup(id) {
  return unseen.has(id)
    ? `<span class="unseen-dot" title="Something happened here since you last looked"></span>`
    : "";
}

/* Leaving the tab notes how far each run had got; coming back marks the steps
 * taken since, and lights the focused sheet if it finished meanwhile. */
document.addEventListener("visibilitychange", () => {
  desks.forEach((seat) => {
    const run = seat.run;
    if (!run) return;
    if (document.hidden) {
      run.seenSteps = run.steps.length;
    } else if (run.seenSteps !== undefined && run.steps.length > run.seenSteps) {
      run.sinceIndex = run.seenSteps;
      run.sinceNote = { count: run.steps.length - run.seenSteps, at: Date.now() };
      scheduleRun(run);
    }
  });
  if (!document.hidden) {
    lastStir = Date.now();
    applyRest();
    const focus = focusId();
    if (unseen.has(focus)) {
      const last = dom.messages.querySelector(".work-card:last-of-type");
      if (last) {
        last.classList.remove("news");
        window.requestAnimationFrame(() => last.classList.add("news"));
      }
    }
    if (state) renderRail();
  }
});

/* ---------- live readings ---------- */

/* How fast the model wrote its last reply, beside the model's name. */
function renderRate() {
  const node = el("tok-rate");
  if (!node) return;
  const run = focusedRun();
  const rate = run && run.running ? run.rate : 0;
  node.hidden = !rate;
  node.textContent = rate ? `${rate < 10 ? rate.toFixed(1) : Math.round(rate)} tok/s` : "";
  // The model is being read into memory: a new window or a new model, or the
  // first run since the page opened while the server holds nothing.
  const machine = el("status-model");
  const warming = Boolean(run && run.running && !run.firstTokenAt && modelColdness());
  machine.classList.toggle("warming", warming);
  machine.title = warming
    ? "Loading the model into memory; the first reply waits for it"
    : "The model AutoSE thinks with";
}

let reloadPending = false;
let modelWarm = false;

function modelColdness() {
  if (reloadPending) return true;
  const advice = state && state.context_advice;
  return !modelWarm && Boolean(advice) && advice.gpu_fraction == null && /ollama/i.test(state.provider || "");
}

/* A token arrived: the model is loaded, and the stream has a direction. */
function noteStream(run, kind) {
  const now = Date.now();
  if (!run.firstTokenAt) run.firstTokenAt = now;
  if (!run.roundFirstAt) run.roundFirstAt = now;
  run.streamKind = kind;
  run.streamAt = now;
  if (reloadPending || !modelWarm) {
    reloadPending = false;
    modelWarm = true;
    if (isFocused(run.session)) renderRate();
  }
}

/* A round finished: its speed, and the slice of the window it added. */
function noteRound(run, completion, promptBefore, promptNow, limit) {
  const now = Date.now();
  const since = run.roundFirstAt || run.lastRoundAt || run.startedAt;
  // A round that came back at once was not timed by its stream; skip it.
  if (completion > 0 && now - since > 400) run.rate = completion / ((now - since) / 1000);
  run.lastRoundAt = now;
  run.roundFirstAt = 0;
  if (!run.firstTokenAt) run.firstTokenAt = now;
  modelWarm = true;
  reloadPending = false;
  if (!isFocused(run.session)) return;
  renderRate();
  const step = el("ctx-step");
  if (!step || !limit) return;
  const grew = promptNow - promptBefore;
  step.hidden = !(promptBefore && grew > 0);
  if (step.hidden) return;
  step.style.setProperty("--from", `${Math.min(100, (promptBefore / limit) * 100)}%`);
  step.style.setProperty("--to", `${Math.min(100, (promptNow / limit) * 100)}%`);
  el("status-ctx").title = `This step added ${formatCount(grew)} tokens. Click for what the window means here.`;
  playFrom(step, [{ opacity: 1, transform: "scaleY(2.2)" }, { opacity: 0.55, transform: "scaleY(1)" }], 1600, now);
}

/* Whether the stream is flowing, and which way: thinking or writing. */
function streamFlow(run) {
  return run.running && run.streamAt && Date.now() - run.streamAt < 2500 ? run.streamKind : "";
}
