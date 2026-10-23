// AutoSE site: theme switch, OS tabs, click-to-select commands, scroll reveal.
// No script writes to the clipboard: a page that puts 'irm ... | iex' there is
// what blockers rightly treat as ClickFix, so the reader copies it themselves.
(function () {
  "use strict";

  // --- theme switch ------------------------------------------------------------
  // Light unless dark was chosen; a stored dark choice was applied in <head>
  // before first paint. The system's dark setting does not switch it.
  var THEME_KEY = "autose-site-theme";
  var root = document.documentElement;
  var themeBtn = document.getElementById("theme-btn");
  var THEME_COLOR = { light: "#e3e9f6", dark: "#11111b" };

  function currentTheme() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function syncTheme() {
    var theme = currentTheme();
    if (themeBtn) {
      themeBtn.setAttribute("aria-label", theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme");
    }
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (meta) {
      meta.setAttribute("content", THEME_COLOR[theme]);
    });
  }

  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
      syncTheme();
    });
  }
  syncTheme();

  // --- install command: OS tabs ----------------------------------------------
  var tabs = document.querySelectorAll(".install-card .tab");
  var cmds = document.querySelectorAll(".install-cmd");
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var os = tab.dataset.os;
      tabs.forEach(function (t) {
        var active = t === tab;
        t.classList.toggle("is-active", active);
        t.setAttribute("aria-selected", active ? "true" : "false");
      });
      cmds.forEach(function (c) {
        c.classList.toggle("is-hidden", c.dataset.os !== os);
      });
    });
  });

  // --- install commands: one click selects the whole command ----------------
  document.querySelectorAll(".install-cmd code, .panel-cmd code").forEach(function (code) {
    code.addEventListener("click", function () {
      var selection = window.getSelection && window.getSelection();
      if (!selection) return;
      var range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
    });
  });

  // --- scroll reveal -------------------------------------------------------------
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (still || !("IntersectionObserver" in window)) return;
  var reveals = [].slice.call(
    document.querySelectorAll(".section-head, .route-sheet, .card, .pub, .install-detail-panel, .demo-frame")
  );
  reveals.forEach(function (el) { el.classList.add("reveal"); });
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("in");
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08, rootMargin: "0px 0px -6% 0px" });
  reveals.forEach(function (el) { io.observe(el); });

  // Benchmark bars grow in when their chart scrolls into view.
  var charts = [].slice.call(document.querySelectorAll(".bench-chart"));
  charts.forEach(function (el) { el.classList.add("bench-wait"); });
  var chartIo = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.remove("bench-wait");
        chartIo.unobserve(entry.target);
      }
    });
  }, { threshold: 0, rootMargin: "0px 0px -20% 0px" });
  charts.forEach(function (el) { chartIo.observe(el); });
  // Never leave a chart empty if the observer does not report it.
  setTimeout(function () {
    charts.forEach(function (el) { el.classList.remove("bench-wait"); });
  }, 6000);
})();
