/* Helix operator UI - minimal vanilla JS, no build step (D11).
   Three behaviors, each gated on the elements its page actually renders:
   1. Incident detail: while the incident is non-terminal, poll the JSON
      read endpoint (GET /incidents/{id}) every few seconds; when the
      status changes reload so the timeline re-renders, and stop polling
      once a terminal status arrives.
   2. Approvals queue and incident page: Approve/Reject buttons POST the
      decision (data-decide-kind="review" for a review, else an approval)
      to the 1.8 / 2.6 JSON endpoints with the operator name (and the intake token as
      a Bearer header when the deployment gates decisions). The button says
      "Approving..." at once; with ?wait=false the server saves the decision
      and resumes the run after answering, so the page polls the incident
      and reloads once its status moves (or after two minutes).
   3. Team page: Copy link puts an invite's link on the clipboard. Without
      clipboard support (or if copying fails) the link field shows instead.
   4. Services form: reviewer and path-rule rows get Move up / Move down /
      Remove buttons and an Add button that clones the form's <template> row;
      rule rows renumber their owner checkboxes (rule_owners_<i>) to match the
      row order. Without this the server-rendered rows submit as they are.
   5. Team page: picking a role in a row's role dropdown saves it at once
      (its Change role button stays for browsers without JavaScript).
   6. Sidebar: drag its right edge (or use the arrow keys, Home, End on the
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
    var row = btn.closest(".decide-row");
    var pair = row ? row.querySelectorAll("[data-decide]") : [btn];
    var label = btn.textContent;
    Array.prototype.forEach.call(pair, function (b) {
      b.disabled = true;
    });
    btn.textContent = action === "approve" ? "Approving..." : "Rejecting...";
    btn.classList.add("is-busy");
    if (errEl) errEl.textContent = "";
    var url = (kind ? "/reviews/" : "/approvals/") + encodeURIComponent(id) + "/" + action;
    fetch(url + "?wait=false", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({
        actor: (actorEl && actorEl.value.trim()) || "ui-operator",
        note: (noteEl && noteEl.value) || "",
      }),
    })
      .then(function (res) {
        if (res.ok) {
          return res.json().then(function (body) {
            waitForRun(body, errEl);
          });
        }
        return res.json().then(function (body) {
          var detail = body && body.detail ? body.detail : "HTTP " + res.status;
          throw new Error(detail);
        });
      })
      .catch(function (err) {
        btn.textContent = label;
        btn.classList.remove("is-busy");
        Array.prototype.forEach.call(pair, function (b) {
          b.disabled = false;
        });
        if (errEl) errEl.textContent = String(err && err.message ? err.message : err);
      });
  }

  /* The decision is saved; the run carries on in the background. Reload
     when the incident's status moves, or after two minutes regardless. */
  function waitForRun(body, note) {
    var record = body.approval || body.review || {};
    var from = body.status;
    if (note) {
      note.classList.add("decide-progress");
      note.textContent = "Saved. Helix is carrying on in the background - this page updates when it is done.";
    }
    if (!record.incident_id || !body.scheduled) {
      window.location.reload();
      return;
    }
    var polls = 0;
    var timer = window.setInterval(function () {
      polls += 1;
      fetch("/incidents/" + encodeURIComponent(record.incident_id), {
        headers: { Accept: "application/json" },
      })
        .then(function (res) {
          return res.ok ? res.json() : null;
        })
        .then(function (data) {
          if ((data && data.status && data.status !== from) || polls >= 40) {
            window.clearInterval(timer);
            window.location.reload();
          }
        })
        .catch(function () {
          if (polls >= 40) {
            window.clearInterval(timer);
            window.location.reload();
          }
        });
    }, POLL_MS);
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

  /* ---- team page: copy an invite link ---- */

  function initCopyLinks() {
    var buttons = document.querySelectorAll("[data-copy-link]");
    if (!buttons.length || !navigator.clipboard) return;
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.hidden = false;
      var row = btn.closest(".team-row");
      var field = row && row.querySelector(".team-link");
      if (field) field.hidden = true;
      btn.addEventListener("click", function () {
        navigator.clipboard.writeText(btn.getAttribute("data-copy-link")).then(
          function () {
            btn.textContent = "Copied";
            window.setTimeout(function () {
              btn.textContent = "Copy link";
            }, 2000);
          },
          function () {
            if (field) field.hidden = false;
            var box = row && row.querySelector("[data-link-field]");
            if (box) {
              box.focus();
              box.select();
            }
          }
        );
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

  /* ---- services form: ordered rows ---- */

  function initServiceForm() {
    var form = document.querySelector("[data-service-form]");
    if (!form) return;

    function sync() {
      form.querySelectorAll("[data-js-only]").forEach(function (el) {
        el.hidden = false;
      });
      form.querySelectorAll('[data-rows="rules"] > [data-row]').forEach(function (row, i) {
        row.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
          box.name = "rule_owners_" + i;
        });
      });
    }

    form.addEventListener("click", function (ev) {
      var add = ev.target.closest("[data-add]");
      if (add) {
        var kind = add.getAttribute("data-add");
        var list = form.querySelector('[data-rows="' + kind + '"]');
        var tpl = form.querySelector('template[data-for="' + kind + '"]');
        list.appendChild(tpl.content.firstElementChild.cloneNode(true));
        sync();
        return;
      }
      var btn = ev.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-row]");
      var act = btn.getAttribute("data-act");
      if (act === "remove") row.remove();
      else if (act === "up" && row.previousElementSibling) {
        row.parentNode.insertBefore(row, row.previousElementSibling);
      } else if (act === "down" && row.nextElementSibling) {
        row.parentNode.insertBefore(row.nextElementSibling, row);
      }
      sync();
    });
    sync();
  }

  /* ---- team page: a role dropdown saves on change ---- */

  function initAutosubmit() {
    document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
      var form = select.form;
      form.querySelectorAll("[data-js-hide]").forEach(function (el) {
        el.hidden = true;
      });
      select.addEventListener("change", function () {
        form.submit();
      });
    });
  }

  function init() {
    initAutosubmit();
    initSidebarResize();
    initServiceForm();
    initIncidentPolling();
    initApprovals();
    initCopyLinks();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
