/* Helix operator UI - minimal vanilla JS, no build step (D11).
   Two behaviors, each gated on the elements its page actually renders:
   1. Incident detail: while the incident is non-terminal, poll the JSON
      read endpoint (GET /incidents/{id}) every few seconds; when the
      status changes reload so the timeline re-renders, and stop polling
      once a terminal status arrives.
   2. Approvals queue: Approve/Reject buttons POST the decision to the
      1.8 JSON endpoints with the operator name (and the intake token as
      a Bearer header when the deployment gates decisions); the page
      reloads to show the recorded decision. */
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
    var noteEl = field("note-" + id);
    var errEl = field("decide-error-" + id);
    var headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (tokenEl && tokenEl.value) {
      headers.Authorization = "Bearer " + tokenEl.value;
    }
    btn.disabled = true;
    fetch("/approvals/" + encodeURIComponent(id) + "/" + action, {
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

  function init() {
    initIncidentPolling();
    initApprovals();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
