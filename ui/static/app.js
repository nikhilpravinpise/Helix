/* Helix operator UI - minimal vanilla JS, no build step (D11).
   Three behaviors, each gated on the elements its page actually renders:
   1. Incident detail: while the incident is non-terminal, poll the JSON
      read endpoint (GET /incidents/{id}) every few seconds; when the
      status changes reload so the timeline re-renders, and stop polling
      once a terminal status arrives.
   2. Approvals queue and incident page: Approve/Reject buttons POST the
      decision (data-decide-kind="review" for a review, else an approval)
      to the 1.8 / 2.6 JSON endpoints with the operator name (and the intake token as
      a Bearer header when the deployment gates decisions); the page
      reloads to show the recorded decision.
   3. Sidebar: drag its right edge (or use the arrow keys, Home, End on the
      focused handle) to resize it; the width lives in the --sidebar-w custom
      property on .app, is clamped to 200-440px, double-click resets it, and
      it persists in localStorage (base.html applies it before first paint). */
(function () {
  "use strict";

  var TERMINAL = { recovered: true, escalated: true, failed: true };
  var POLL_MS = 3000;

  /* ---- incident detail: live status polling ---- */

  function initIncidentPolling() {
    var el = document.querySelector("[data-incident-status]");
    if (!el) return;
    var status = el.getAttribute("data-incident-status");
    if (TERMINAL[status]) return;
    var id = el.getAttribute("data-incident-id");
    var live = document.getElementById("live-check");
    var timer = window.setInterval(function () {
      fetch("/incidents/" + encodeURIComponent(id), {
        headers: { Accept: "application/json" },
      })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (data) {
          if (live) {
            live.textContent = "live - checked " + new Date().toLocaleTimeString();
          }
          if (data.status && data.status !== status) {
            if (TERMINAL[data.status]) window.clearInterval(timer);
            window.location.reload();
          }
        })
        .catch(function () {
          if (live) live.textContent = "live - poll failed, retrying";
        });
    }, POLL_MS);
  }

  /* ---- approvals queue: decision POSTs ---- */

  function field(id) {
    return document.getElementById(id);
  }

  function decide(btn) {
    var id = btn.getAttribute("data-approval-id");
    var action = btn.getAttribute("data-decide");
    var actorEl = field("actor");
    var tokenEl = field("intake-token");
    var kind = btn.getAttribute("data-decide-kind") === "review" ? "review" : "";
    var suffix = kind ? kind + "-" + id : id;
    var noteEl = field("note-" + suffix);
    var errEl = field("decide-error-" + suffix);
    var headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (tokenEl && tokenEl.value) {
      headers.Authorization = "Bearer " + tokenEl.value;
    }
    btn.disabled = true;
    fetch((kind ? "/reviews/" : "/approvals/") + encodeURIComponent(id) + "/" + action, {
      method: "POST",
      headers: headers,
      body: JSON.stringify({
        actor: (actorEl && actorEl.value.trim()) || "ui-operator",
        note: (noteEl && noteEl.value) || "",
      }),
    })
      .then(function (res) {
        if (res.ok) {
          window.location.reload();
          return null;
        }
        return res.json().then(function (body) {
          var detail = body && body.detail ? body.detail : "HTTP " + res.status;
          throw new Error(detail);
        });
      })
      .catch(function (err) {
        btn.disabled = false;
        if (errEl) errEl.textContent = String(err && err.message ? err.message : err);
      });
  }

  function initApprovals() {
    var actorEl = field("actor");
    if (actorEl) {
      var saved = window.sessionStorage.getItem("helix.actor");
      if (saved) actorEl.value = saved;
      actorEl.addEventListener("change", function () {
        window.sessionStorage.setItem("helix.actor", actorEl.value);
      });
    }
    var tokenEl = field("intake-token");
    if (tokenEl) {
      tokenEl.value = window.sessionStorage.getItem("helix.token") || "";
      tokenEl.addEventListener("change", function () {
        window.sessionStorage.setItem("helix.token", tokenEl.value);
      });
    }
    var buttons = document.querySelectorAll("[data-decide]");
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener("click", function () {
        decide(btn);
      });
    });
  }

  /* ---- sidebar resize ---- */

  var SIDEBAR_KEY = "helix.sidebar-w";
  var SIDEBAR_MIN = 200;
  var SIDEBAR_MAX = 440;
  var SIDEBAR_DEFAULT = 264;
  var SIDEBAR_STEP = 16;

  function initSidebarResize() {
    var handle = document.querySelector(".sidebar-handle");
    var app = document.querySelector(".app");
    var side = document.querySelector(".sidebar");
    if (!handle || !app || !side) return;
    var width = Math.round(side.getBoundingClientRect().width);
    if (!(width >= SIDEBAR_MIN && width <= SIDEBAR_MAX)) width = SIDEBAR_DEFAULT;

    function apply(next, save) {
      width = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(next)));
      app.style.setProperty("--sidebar-w", width + "px");
      handle.setAttribute("aria-valuenow", String(width));
      if (save) {
        try {
          window.localStorage.setItem(SIDEBAR_KEY, String(width));
        } catch (e) {
          /* storage can be blocked; the width just will not persist */
        }
      }
    }

    apply(width, false);

    handle.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      handle.setPointerCapture(ev.pointerId);
      handle.classList.add("is-dragging");
      document.body.classList.add("sidebar-resizing");
    });
    handle.addEventListener("pointermove", function (ev) {
      if (!handle.classList.contains("is-dragging")) return;
      apply(ev.clientX, false);
    });
    function stop(ev) {
      if (!handle.classList.contains("is-dragging")) return;
      handle.classList.remove("is-dragging");
      document.body.classList.remove("sidebar-resizing");
      if (ev.type === "pointerup") apply(width, true);
    }
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
    handle.addEventListener("dblclick", function () {
      apply(SIDEBAR_DEFAULT, true);
    });
    handle.addEventListener("keydown", function (ev) {
      var next = null;
      if (ev.key === "ArrowLeft") next = width - SIDEBAR_STEP;
      else if (ev.key === "ArrowRight") next = width + SIDEBAR_STEP;
      else if (ev.key === "Home") next = SIDEBAR_MIN;
      else if (ev.key === "End") next = SIDEBAR_MAX;
      if (next === null) return;
      ev.preventDefault();
      apply(next, true);
    });
  }

  function init() {
    initSidebarResize();
    initIncidentPolling();
    initApprovals();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
