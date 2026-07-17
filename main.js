// AutoSE site. OS tabs, copy-to-clipboard, scroll reveal.
(function () {
  "use strict";

  // --- install command: OS tabs ---------------------------------------------
  var tabs = document.querySelectorAll(".tab");
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

  // --- copy buttons ----------------------------------------------------------
  function flash(btn) {
    var label = btn.querySelector(".copy-label");
    var prev = label ? label.textContent : "";
    btn.classList.add("is-done");
    if (label) label.textContent = "Copied";
    setTimeout(function () {
      btn.classList.remove("is-done");
      if (label) label.textContent = prev;
    }, 1600);
  }

  function copyText(text, btn) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash(btn); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); flash(btn); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  // hero copy button: copies whichever command is visible
  var heroCopy = document.getElementById("copy-btn");
  if (heroCopy) {
    heroCopy.addEventListener("click", function () {
      var visible = document.querySelector(".install-cmd:not(.is-hidden) code");
      if (visible) copyText(visible.textContent.trim(), heroCopy);
    });
  }

  // panel mini copy buttons: copy a referenced element
  document.querySelectorAll(".copy-mini").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.querySelector(btn.dataset.copy);
      if (target) copyText(target.textContent.trim(), btn);
    });
  });

  // --- scroll reveal ---------------------------------------------------------
  var reveals = [].slice.call(
    document.querySelectorAll(".section-title, .section-lede, .stage, .spec, .pub, .install-card, .hero-sub, .hero-meta, .install-detail-panel, .demo-frame")
  );
  reveals.forEach(function (el) { el.classList.add("reveal"); });

  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("in"); });
  }
})();
