// Kept in a file (not inline) so the Content-Security-Policy can forbid inline scripts.
(function () {
  "use strict";

  // <form data-confirm="Are you sure?"> asks in a modal before submitting (never window.confirm).
  // Optional: data-confirm-title, data-confirm-ok (button text), data-confirm-variant="danger".
  var confirmed = new WeakSet();

  function confirmModal() {
    var el = document.getElementById("confirm-modal");
    if (el) return el;
    el = document.createElement("div");
    el.className = "modal fade";
    el.id = "confirm-modal";
    el.tabIndex = -1;
    el.setAttribute("aria-hidden", "true");
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
      '<div class="modal-header"><h5 class="modal-title"></h5>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>' +
      '<div class="modal-body"></div>' +
      '<div class="modal-footer"><button type="button" class="btn btn-light" data-bs-dismiss="modal">Cancel</button>' +
      '<button type="button" class="btn" data-confirm-ok></button></div></div></div>';
    document.body.appendChild(el);
    return el;
  }

  function askFirst(form, submitter) {
    var el = confirmModal();
    var danger = form.getAttribute("data-confirm-variant") === "danger";
    el.querySelector(".modal-title").textContent = form.getAttribute("data-confirm-title") || "Please confirm";
    el.querySelector(".modal-body").textContent = form.getAttribute("data-confirm");
    var ok = el.querySelector("[data-confirm-ok]");
    ok.textContent = form.getAttribute("data-confirm-ok") || (danger ? "Delete" : "Confirm");
    ok.className = "btn " + (danger ? "btn-danger" : "btn-primary");
    var modal = bootstrap.Modal.getOrCreateInstance(el);
    ok.onclick = function () {
      confirmed.add(form);
      modal.hide();
      if (form.requestSubmit) form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      else form.submit();
    };
    modal.show();
  }

  document.addEventListener("submit", function (e) {
    if (e.target.getAttribute("data-confirm") && !confirmed.has(e.target)) {
      e.preventDefault();
      askFirst(e.target, e.submitter);
      return;
    }
    // Prevent double submission of money-moving forms.
    var btn = e.target.querySelector("button[type=submit]:not([data-allow-repeat])");
    if (btn) {
      setTimeout(function () { btn.disabled = true; }, 0);
    }
  });

  // <button data-details="template-id" data-details-title="..."> shows that <template>'s content in a modal.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-details]");
    if (!btn) return;
    var tpl = document.getElementById(btn.getAttribute("data-details"));
    if (!tpl) return;
    var el = document.getElementById("details-modal");
    if (!el) {
      el = document.createElement("div");
      el.className = "modal fade";
      el.id = "details-modal";
      el.tabIndex = -1;
      el.setAttribute("aria-hidden", "true");
      el.innerHTML =
        '<div class="modal-dialog modal-dialog-centered modal-dialog-scrollable modal-lg"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title"></h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>' +
        '<div class="modal-body"></div>' +
        '<div class="modal-footer"><button type="button" class="btn btn-light" data-bs-dismiss="modal">Close</button></div>' +
        '</div></div>';
      document.body.appendChild(el);
    }
    el.querySelector(".modal-title").textContent = btn.getAttribute("data-details-title") || "Details";
    var body = el.querySelector(".modal-body");
    body.replaceChildren(tpl.content.cloneNode(true));
    bootstrap.Modal.getOrCreateInstance(el).show();
  });

  // <button data-copy="text"> copies to the clipboard.
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-copy]");
    if (!el) return;
    navigator.clipboard.writeText(el.getAttribute("data-copy")).then(function () {
      var old = el.innerHTML;
      el.innerHTML = "Copied";
      setTimeout(function () { el.innerHTML = old; }, 1200);
    });
  });

  // Mobile sidebar toggle.
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-sidebar-toggle]")) {
      document.querySelector(".crm-sidebar, .a-side").classList.toggle("show");
    }
  });

  // The sidebar keeps its scroll position from page to page, and the current page's entry is always in view
  // (picking an entry low in a long menu doesn't send you back to the top).
  (function () {
    var side = document.querySelector(".crm-sidebar, .a-side");
    if (!side) return;
    var key = "sidebar-scroll:" + (side.classList.contains("crm-sidebar") ? "admin" : "portal");
    try {
      var saved = sessionStorage.getItem(key);
      if (saved !== null) side.scrollTop = parseInt(saved, 10) || 0;
    } catch (err) { /* storage blocked: the active entry is still brought into view below */ }
    var current = [].find.call(side.querySelectorAll(".nav-link.active"), function (a) { return !a.closest(".nav-flyout"); });
    if (current) {
      var top = side.getBoundingClientRect().top, brand = side.querySelector(".brand");
      var visibleFrom = top + (brand ? brand.offsetHeight : 0), r = current.getBoundingClientRect();
      if (r.top < visibleFrom || r.bottom > top + side.clientHeight) {
        side.scrollTop += r.top - visibleFrom - (side.clientHeight - (visibleFrom - top)) / 3;
      }
    }
    window.addEventListener("pagehide", function () {
      try { sessionStorage.setItem(key, String(side.scrollTop)); } catch (err) { /* ignore */ }
    });
  })();

  // Tabs: <div data-tabs> whose direct children <section data-tab="Title" id="..."> become tabs (without
  // JavaScript every section simply shows). The open tab follows the URL hash (#id, also an id inside the tab)
  // and is remembered per page, so saving a form in a tab comes back to that tab, error or not.
  document.querySelectorAll("[data-tabs]").forEach(function (box, n) {
    var panes = [].filter.call(box.children, function (el) { return el.hasAttribute("data-tab"); });
    if (panes.length < 2) return;
    var storeKey = "tab:" + location.pathname + ":" + n;
    var nav = document.createElement("ul");
    nav.className = "nav nav-tabs crm-tabs";
    nav.setAttribute("role", "tablist");
    var buttons = panes.map(function (pane, i) {
      pane.id = pane.id || "tab-" + (pane.getAttribute("data-tab").toLowerCase().replace(/[^a-z0-9]+/g, "-") || i);
      pane.classList.add("crm-tab-pane");
      pane.setAttribute("role", "tabpanel");
      var li = document.createElement("li"), b = document.createElement("button");
      li.className = "nav-item";
      li.setAttribute("role", "presentation");
      b.type = "button";
      b.className = "nav-link";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", pane.id);
      b.textContent = pane.getAttribute("data-tab");
      var count = pane.getAttribute("data-tab-count");
      if (count) {
        var c = document.createElement("span");
        c.className = "badge rounded-pill text-bg-light border ms-2";
        c.textContent = count;
        b.appendChild(c);
      }
      b.addEventListener("click", function () { show(pane.id, true); });
      li.appendChild(b);
      nav.appendChild(li);
      return b;
    });
    box.insertBefore(nav, panes[0]);
    function show(id, user) {
      panes.forEach(function (pane, i) {
        var on = pane.id === id;
        pane.hidden = !on;
        buttons[i].classList.toggle("active", on);
        buttons[i].setAttribute("aria-selected", on ? "true" : "false");
      });
      try { sessionStorage.setItem(storeKey, id); } catch (err) { /* ignore */ }
      if (user && history.replaceState) history.replaceState(null, "", "#" + id);
    }
    function paneFor(id) {
      if (!id) return null;
      var target = document.getElementById(id);
      return target && panes.find(function (p) { return p === target || p.contains(target); });
    }
    var saved = null;
    try { saved = sessionStorage.getItem(storeKey); } catch (err) { /* ignore */ }
    var first = paneFor(location.hash.slice(1)) || paneFor(saved) || panes[0];
    show(first.id, false);
    // A form posted from a tab comes back to it: the redirect keeps the action's #hash.
    box.addEventListener("submit", function (e) {
      var pane = paneFor(e.target.id) || panes.find(function (p) { return p.contains(e.target); });
      if (pane && e.target.method && e.target.method.toLowerCase() === "post") {
        e.target.action = e.target.action.split("#")[0] + "#" + pane.id;
        try { sessionStorage.setItem(storeKey, pane.id); } catch (err) { /* ignore */ }
      }
    }, true);
  });

  // [data-show-when="field=value"] is shown only while the form's `field` has that value.
  function applyShowWhen(form) {
    form.querySelectorAll("[data-show-when]").forEach(function (el) {
      var rule = el.getAttribute("data-show-when").split("=");
      var input = form.querySelector("[name='" + rule[0] + "']:checked") || form.querySelector("select[name='" + rule[0] + "']");
      var hidden = !input || input.value !== rule[1];
      el.classList.toggle("d-none", hidden);
      // fields of hidden sections are not submitted (several sections may share field names)
      el.querySelectorAll("input, select, textarea").forEach(function (f) { f.disabled = hidden; });
    });
  }
  document.addEventListener("change", function (e) {
    var form = e.target.closest("[data-toggle-scope]");
    if (form) applyShowWhen(form);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-toggle-scope]").forEach(applyShowWhen);
  });

  // <input type="checkbox" data-check-all="login"> ticks or clears every checkbox named "login" on the page.
  document.addEventListener("change", function (e) {
    var name = e.target.getAttribute && e.target.getAttribute("data-check-all");
    if (!name) return;
    document.querySelectorAll("input[type=checkbox][name='" + name + "']").forEach(function (box) {
      box.checked = e.target.checked;
    });
  });

  // Nested menus (.nav-group): a group opens on hover or focus, beside the sidebar and inside the window.
  // Leaving it closes it only after CLOSE_MS, and hovering another group while one is open switches after
  // SWITCH_MS, so the pointer can cross other entries on its way to the open menu.
  var CLOSE_MS = 400, SWITCH_MS = 250, menuTimer = null;
  function desktopMenu() { return !window.matchMedia("(max-width: 991.98px)").matches; }
  function openGroup() { return document.querySelector(".nav-group.open"); }
  function showGroup(group) {
    clearTimeout(menuTimer);
    var current = openGroup();
    if (current === group) return;
    if (current) current.classList.remove("open");
    if (!group) return;
    group.classList.add("open");
    var flyout = group.querySelector(".nav-flyout");
    var top = group.getBoundingClientRect().top;
    flyout.style.top = Math.max(8, Math.min(top, window.innerHeight - flyout.offsetHeight - 8)) + "px";
  }
  function later(group, ms) {
    clearTimeout(menuTimer);
    menuTimer = setTimeout(function () { showGroup(group); }, ms);
  }
  document.addEventListener("mouseover", function (e) {
    if (!desktopMenu() || !e.target.closest) return;
    var group = e.target.closest(".nav-group"), current = openGroup();
    if (group === current) { clearTimeout(menuTimer); return; }  // still inside the open menu
    if (group) later(group, current ? SWITCH_MS : 0);
    else if (current) later(null, CLOSE_MS);
  });
  document.addEventListener("focusin", function (e) {
    if (desktopMenu() && e.target.closest) showGroup(e.target.closest(".nav-group"));
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && openGroup()) showGroup(null);
  });
  window.addEventListener("scroll", function () { if (desktopMenu()) showGroup(null); }, true);

  // Symbol sessions: each <input data-sessions="kind" data-day="0..6"> holding e.g. "01:00-22:15, 22:30-24:00"
  // gets a picker (a day bar and from / to times); the input stays the submitted value.
  function toMinutes(t) { var p = t.split(":"); return parseInt(p[0], 10) * 60 + parseInt(p[1], 10); }
  function hm(m) { return ("0" + Math.floor(m / 60)).slice(-2) + ":" + ("0" + (m % 60)).slice(-2); }
  function parseDay(text) {
    return text.split(",").map(function (p) { return p.trim(); }).filter(Boolean).map(function (p) {
      var ends = p.split("-");
      return [ends[0].trim(), (ends[1] || "").trim()];
    });
  }

  function sessionRow(editor, open, close) {
    var row = document.createElement("div");
    row.className = "session-row";
    row.innerHTML = '<input type="time" class="form-control form-control-sm" aria-label="From">' +
      '<span class="text-muted">–</span><input type="time" class="form-control form-control-sm" aria-label="To">' +
      '<button type="button" class="btn btn-sm btn-link text-danger p-0" data-session-remove aria-label="Remove">' +
      '<i class="bi bi-x-lg"></i></button>';
    var times = row.querySelectorAll("input");
    times[0].value = open;
    times[1].value = close === "24:00" ? "00:00" : close;  // a time field can't hold 24:00: 00:00 as the end = midnight
    editor.querySelector(".session-rows").appendChild(row);
  }

  function writeSessions(editor) {
    var input = editor.previousElementSibling, ranges = [], ok = true;
    editor.querySelectorAll(".session-row").forEach(function (row) {
      var t = row.querySelectorAll("input"), open = t[0].value, close = t[1].value;
      if (!open || !close) return;
      var a = toMinutes(open), b = close === "00:00" ? 1440 : toMinutes(close);
      row.classList.toggle("is-invalid", a >= b);
      if (a >= b) ok = false;
      ranges.push([a, b]);
    });
    ranges.sort(function (x, y) { return x[0] - y[0]; });
    ranges.forEach(function (r, i) { if (i && r[0] < ranges[i - 1][1]) ok = false; });
    editor.classList.toggle("has-error", !ok);
    input.value = ranges.map(function (r) { return hm(r[0]) + "-" + hm(r[1]); }).join(", ");
    var bar = editor.querySelector(".session-bar");
    bar.innerHTML = "";
    ranges.forEach(function (r) {
      var seg = document.createElement("span");
      seg.className = "session-seg";
      seg.style.left = (r[0] / 14.4) + "%";
      seg.style.width = (Math.max(0, r[1] - r[0]) / 14.4) + "%";
      seg.title = hm(r[0]) + "–" + hm(r[1]);
      bar.appendChild(seg);
    });
    editor.querySelector(".session-closed").classList.toggle("d-none", ranges.length > 0);
  }

  function fillSessions(editor, text) {
    editor.querySelector(".session-rows").innerHTML = "";
    parseDay(text).forEach(function (r) { sessionRow(editor, r[0], r[1]); });
    writeSessions(editor);
  }

  function buildSessionEditor(input) {
    var editor = document.createElement("div");
    editor.className = "session-editor";
    editor.innerHTML = '<div class="session-bar" aria-hidden="true"></div><div class="session-rows"></div>' +
      '<div class="d-flex gap-2 align-items-center small"><span class="session-closed text-muted">closed</span>' +
      '<button type="button" class="btn btn-sm btn-link p-0" data-session-add>+ add</button>' +
      '<button type="button" class="btn btn-sm btn-link p-0 text-muted" data-session-clear>close day</button></div>';
    input.classList.add("d-none");
    input.after(editor);
    fillSessions(editor, input.value);
  }

  function editorFor(kind, day) {
    var input = document.querySelector("[data-sessions='" + kind + "'][data-day='" + day + "']");
    return input && input.nextElementSibling;
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[data-sessions]").forEach(buildSessionEditor);
  });
  document.addEventListener("input", function (e) {
    var editor = e.target.closest && e.target.closest(".session-editor");
    if (editor) writeSessions(editor);
  });
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-session-add], [data-session-remove], [data-session-clear], [data-session-fill]");
    if (!el) return;
    if (el.hasAttribute("data-session-fill")) {  // column tools: copy Monday, 24 hours, same as quotes
      var kind = el.getAttribute("data-session-fill"), how = el.getAttribute("data-fill");
      var monday = editorFor(kind, 1).previousElementSibling.value;
      for (var day = 0; day < 7; day++) {
        var weekday = day >= 1 && day <= 5, text = null;
        if (how === "weekdays" && weekday && day !== 1) text = monday;
        if (how === "24x5") text = weekday ? "00:00-24:00" : "";
        if (how === "24x7") text = "00:00-24:00";
        if (how === "quote") text = editorFor("quote", day).previousElementSibling.value;
        if (text !== null) fillSessions(editorFor(kind, day), text);
      }
      return;
    }
    var editor = el.closest(".session-editor");
    if (el.hasAttribute("data-session-add")) {
      var first = !editor.querySelector(".session-row");  // the first one: the whole day, to narrow down
      sessionRow(editor, first ? "00:00" : "", first ? "24:00" : "");
    } else if (el.hasAttribute("data-session-remove")) {
      el.closest(".session-row").remove();
    } else {
      editor.querySelector(".session-rows").innerHTML = "";
    }
    writeSessions(editor);
  });

  // <select data-autosubmit> submits its form on change (filters).
  document.addEventListener("change", function (e) {
    if (e.target.matches("[data-autosubmit]")) e.target.form.submit();
  });

  // Commission-plan editor: add another rule row.
  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add-row]");
    if (!add) return;
    var tpl = document.getElementById(add.getAttribute("data-add-row"));
    var body = document.getElementById(add.getAttribute("data-target"));
    body.appendChild(tpl.content.cloneNode(true));
    var input = body.lastElementChild && body.lastElementChild.querySelector("input, select");
    if (input) input.focus();  // the button sits in the card header, away from the new row
  });
  // <tbody data-sortable>: rows are reordered by dragging their [data-drag-handle] (the form posts them in order).
  (function () {
    var dragging = null;
    document.addEventListener("mousedown", function (e) {
      var handle = e.target.closest("[data-sortable] [data-drag-handle]");
      if (handle) handle.closest("tr").draggable = true;
    });
    document.addEventListener("dragstart", function (e) {
      var row = e.target.closest && e.target.closest("[data-sortable] > tr");
      if (!row || !row.draggable) return;
      dragging = row;
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", "");
    });
    document.addEventListener("dragover", function (e) {
      if (!dragging) return;
      var over = e.target.closest && e.target.closest("[data-sortable] > tr");
      if (!over || over === dragging || over.parentNode !== dragging.parentNode) return;
      e.preventDefault();
      var box = over.getBoundingClientRect();
      over.parentNode.insertBefore(dragging, e.clientY > box.top + box.height / 2 ? over.nextSibling : over);
    });
    document.addEventListener("dragend", function () {
      if (!dragging) return;
      dragging.classList.remove("dragging");
      dragging.draggable = false;
      dragging = null;
    });
  })();
  // <button data-remove-row> removes its table row.
  document.addEventListener("click", function (e) {
    var remove = e.target.closest("[data-remove-row]");
    if (remove) remove.closest("tr").remove();
  });
  // <select data-unique-from="id">: its choices come from <template id> (one list, however many rows), and what
  // another select of that list has chosen is not offered. A saved value missing from the list stays selectable.
  (function () {
    function fill(select) {
      var wanted = select.value, names = {}, list = document.getElementById(select.getAttribute("data-unique-from")).content;
      list.querySelectorAll("option").forEach(function (o) { names[o.value] = true; });
      [].slice.call(select.options).forEach(function (o) { if (names[o.value]) o.remove(); });
      select.appendChild(list.cloneNode(true));
      select.value = wanted;
      select.setAttribute("data-filled", "1");
    }
    function refresh() {
      var selects = document.querySelectorAll("select[data-unique-from]"), taken = {};
      selects.forEach(function (select) {
        if (!select.hasAttribute("data-filled")) fill(select);
        if (select.value) taken[select.value] = (taken[select.value] || 0) + 1;
      });
      selects.forEach(function (select) {
        [].forEach.call(select.options, function (o) {
          var other = o.value && o.value !== select.value && taken[o.value];
          o.disabled = o.hidden = !!other;
        });
      });
    }
    document.addEventListener("DOMContentLoaded", refresh);
    document.addEventListener("change", function (e) {
      if (e.target.matches && e.target.matches("select[data-unique-from]")) refresh();
    });
    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-add-row], [data-remove-row]")) refresh();
    });
  })();
  // Live prices: elements with data-q="SYMBOL" data-f="bid|ask|spread" follow the /ws/quotes stream; the price
  // turns green / red (and data-qc carets point up / down) as it moves. Without WebSockets (blocked by a proxy),
  // /quotes.json is polled instead. A widget that had no prices yet ([data-quotes-view]) is redrawn once they come.
  (function () {
    var views = document.querySelectorAll("[data-quotes-view]");
    if (!views.length) return;
    var last = {}, socket = null, failures = 0, pollTimer = null;

    function num(text) { return parseFloat(String(text).replace(/,/g, "")); }

    function apply(data) {
      var quotes = (data && data.quotes) || [];
      if (!quotes.length) return;  // MT5 unreachable for a moment: keep the last prices
      // A widget missing a streamed symbol (no prices yet, or the list changed) is redrawn, at most every 10 s.
      views.forEach(function (view) {
        var name = view.getAttribute("data-quotes-view");
        var shown = quotes.slice(0, name === "hero" || name === "mini" ? 6 : quotes.length);
        var missing = shown.some(function (q) { return !view.querySelector('[data-q="' + q.symbol + '"]'); });
        var now = Date.now();
        if (missing && window.htmx && now - (view._redrawn || 0) > 10000) {
          view._redrawn = now;
          htmx.ajax("GET", "/quotes?view=" + name, { target: view, swap: "innerHTML" });
        }
      });
      quotes.forEach(function (q) {
        var before = last[q.symbol], dir = null;
        if (before && before.bid !== q.bid) dir = num(q.bid) > num(before.bid) ? "up" : "down";
        last[q.symbol] = q;
        document.querySelectorAll('[data-q="' + q.symbol + '"]').forEach(function (el) {
          var value = q[el.getAttribute("data-f")];
          if (value === undefined || el.textContent === value) return;
          el.textContent = value;
          if (dir && el.classList.contains("s-price")) {
            el.classList.toggle("q-up", dir === "up");
            el.classList.toggle("q-down", dir === "down");
            el.classList.remove("q-tick"); void el.offsetWidth; el.classList.add("q-tick");
          }
        });
        if (dir) document.querySelectorAll('[data-qc="' + q.symbol + '"]').forEach(function (el) {
          el.classList.toggle("bi-caret-up-fill", dir === "up");
          el.classList.toggle("bi-caret-down-fill", dir === "down");
          el.classList.toggle("q-up", dir === "up");
          el.classList.toggle("q-down", dir === "down");
        });
      });
    }

    function poll() {
      if (pollTimer) return;
      pollTimer = setInterval(function () {
        fetch("/quotes.json", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; })
          .then(apply).catch(function () {});
      }, 3000);
    }

    function connect() {
      if (!("WebSocket" in window)) { poll(); return; }
      socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/quotes");
      socket.onopen = function () {
        failures = 0;
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      };
      socket.onmessage = function (e) { try { apply(JSON.parse(e.data)); } catch (err) { /* ignore a bad frame */ } };
      socket.onclose = function () {
        socket = null;
        failures += 1;
        if (failures >= 3) poll();  // keep prices moving while the socket is down
        setTimeout(connect, Math.min(30000, 1000 * Math.pow(2, failures)));
      };
    }
    // Pause while the tab is hidden; reconnect when it is shown again.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (socket) { socket.onclose = null; socket.close(); socket = null; } }
      else if (!socket) connect();
    });
    connect();
  })();

  // <input type="color" data-color-for="id"> fills the text field it belongs to (and the other way round).
  document.addEventListener("input", function (e) {
    var picker = e.target.closest("[data-color-for]");
    if (picker) { document.getElementById(picker.getAttribute("data-color-for")).value = picker.value; return; }
    var linked = e.target.id && document.querySelector('[data-color-for="' + e.target.id + '"]');
    if (linked && /^#[0-9a-fA-F]{6}$/.test(e.target.value)) linked.value = e.target.value;
  });
})();
