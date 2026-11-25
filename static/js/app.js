// Portal behaviour: mobile navigation, menu groups, confirmations and ballot
// limits. No inline scripts (CSP).
(function () {
  "use strict";
  var shell = document.querySelector(".shell");
  var sidebar = document.getElementById("sidebar");
  var button = document.querySelector("[data-nav-toggle]");
  var scrim = document.querySelector(".scrim");
  var phone = window.matchMedia ? window.matchMedia("(max-width: 820px)") : null;

  function isPhone() { return !!(phone && phone.matches); }

  // The off-canvas menu: hidden from keyboard and screen readers while
  // closed (inert + visibility in CSS), focus moves in on open and back to
  // the Menu button on close.
  function setInert(closed) {
    if (!sidebar) return;
    if (closed && isPhone()) sidebar.setAttribute("inert", "");
    else sidebar.removeAttribute("inert");
  }
  function setOpen(open, returnFocus) {
    if (!shell || !button) return;
    shell.classList.toggle("nav-open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    setInert(!open);
    if (open) {
      var target = sidebar && (sidebar.querySelector("[aria-current=page]") || sidebar.querySelector("a, summary"));
      if (target) target.focus();
    } else if (returnFocus) {
      button.focus();
    }
  }
  setInert(true);
  if (phone && phone.addEventListener) {
    phone.addEventListener("change", function () {
      if (!isPhone() && shell) shell.classList.remove("nav-open");
      setInert(!(shell && shell.classList.contains("nav-open")));
    });
  }
  if (button) button.addEventListener("click", function () {
    setOpen(!shell.classList.contains("nav-open"), true);
  });
  if (scrim) scrim.addEventListener("click", function () { setOpen(false, true); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && shell && shell.classList.contains("nav-open")) setOpen(false, true);
  });

  // Menu groups remember being opened or closed in this browser. The group
  // holding the current page always opens.
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (err) { /* private mode or blocked storage: defaults apply */ }
    return null;
  }
  var groups = document.querySelectorAll("details[data-nav-group]");
  Array.prototype.forEach.call(groups, function (group) {
    var key = "nav-group:" + group.getAttribute("data-nav-group");
    if (!group.hasAttribute("data-nav-current")) {
      var saved = store(key);
      if (saved === "1") group.open = true;
      if (saved === "0") group.open = false;
    }
    group.addEventListener("toggle", function () { store(key, group.open ? "1" : "0"); });
  });

  // <form data-confirm="Are you sure?"> asks before submitting. A form with
  // data-confirm-ballot builds the question from the boxes ticked.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.getAttribute) return;
    var message = form.getAttribute("data-confirm");
    if (form.hasAttribute("data-confirm-ballot")) {
      var names = [];
      Array.prototype.forEach.call(form.querySelectorAll("input[type=checkbox]:checked"), function (box) {
        names.push(box.getAttribute("data-name") || box.value);
      });
      var lot = form.querySelector("select[name=lot]");
      var forLot = lot && lot.options.length > 1 ? " for " + lot.options[lot.selectedIndex].text : "";
      message = "Cast your ballot" + forLot + " for " + (names.join(" and ") || "nobody") + "? It can't be changed afterwards.";
    }
    if (message && !window.confirm(message)) e.preventDefault();
  }, true);

  // <input type="checkbox" data-show-password="id1 id2"> shows or hides those passwords.
  document.addEventListener("change", function (e) {
    var ids = e.target.getAttribute && e.target.getAttribute("data-show-password");
    if (!ids) return;
    ids.split(" ").forEach(function (id) {
      var input = document.getElementById(id);
      if (input) input.type = e.target.checked ? "text" : "password";
    });
  });

  // <button data-print> prints the page (recovery codes).
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-print]");
    if (el) { e.preventDefault(); window.print(); }
  });
  // Installable app: the service worker caches static files only (signed-in pages).
  var sw = document.body.getAttribute("data-sw");
  if (sw && "serviceWorker" in navigator) {
    navigator.serviceWorker.register(sw, { scope: "/" }).catch(function () {});
  }
  // Ballots that allow up to N choices: once N are ticked the rest are
  // disabled, and the count is shown.
  Array.prototype.forEach.call(document.querySelectorAll("[data-max-choices]"), function (box) {
    var max = parseInt(box.getAttribute("data-max-choices"), 10) || 1;
    var inputs = box.querySelectorAll("input[type=checkbox]");
    var status = box.querySelector("[data-choice-count]");
    function update() {
      var ticked = box.querySelectorAll("input[type=checkbox]:checked").length;
      Array.prototype.forEach.call(inputs, function (input) {
        input.disabled = !input.checked && ticked >= max;
      });
      if (status) status.textContent = ticked + " of " + max + " chosen";
    }
    box.addEventListener("change", update);
    update();
  });
})();

// --- Finance and board settings pages (UX review) -------------------------------

// Unsaved-changes warning for pages with one Save per section (Letters).
// Forms marked data-warn-unsaved="Section name" are tracked: a changed form
// shows its "Unsaved changes" note, saving one section asks before losing
// edits in another, and leaving the page asks first.
function portalWarnUnsaved() {
  "use strict";
  var forms = document.querySelectorAll("form[data-warn-unsaved]");
  if (!forms.length) return;
  var dirty = [];
  var submitting = false;
  function markDirty(form) {
    if (dirty.indexOf(form) === -1) dirty.push(form);
    var note = form.querySelector("[data-unsaved-note]");
    if (note) note.hidden = false;
  }
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("input", function () { markDirty(form); });
    form.addEventListener("change", function () { markDirty(form); });
    form.addEventListener("submit", function (e) {
      var others = dirty.filter(function (f) { return f !== form; });
      if (others.length) {
        var names = others.map(function (f) { return f.getAttribute("data-warn-unsaved"); });
        var ok = window.confirm("Unsaved changes in: " + names.join(", ") +
          ". Saving this section will lose them. Save this section anyway?");
        if (!ok) { e.preventDefault(); return; }
      }
      if (!e.defaultPrevented) submitting = true;
    });
  });
  window.addEventListener("beforeunload", function (e) {
    if (submitting || !dirty.length) return;
    e.preventDefault();
    e.returnValue = "";
  });
}

// Money typed in dollars shows what it will be saved as: an input with
// data-money-preview="per month" fills its sibling [data-money-out] with
// "= $75.00 per month" (the server parses the same way and checks again).
function portalMoneyPreview() {
  "use strict";
  function show(input) {
    var out = input.parentNode.querySelector("[data-money-out]");
    if (!out) return;
    var raw = input.value.replace(/[$,\s]/g, "");
    if (!/^\d+(\.\d{0,2})?$/.test(raw)) { out.textContent = raw ? "Enter dollars, e.g. 75.00" : ""; return; }
    var parts = raw.split(".");
    var cents = (parts[1] || "") + "00";
    var dollars = String(parseInt(parts[0], 10)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var unit = input.getAttribute("data-money-preview");
    out.textContent = "= $" + dollars + "." + cents.slice(0, 2) + (unit ? " " + unit : "");
  }
  Array.prototype.forEach.call(document.querySelectorAll("input[data-money-preview]"), function (input) {
    input.addEventListener("input", function () { show(input); });
  });
}

// <button data-confirm="..."> in a form with several step buttons (one shared
// note field, e.g. a collection case) asks before that button submits.
function portalButtonConfirm() {
  "use strict";
  document.addEventListener("submit", function (e) {
    var button = e.submitter;
    var message = button && button.getAttribute && button.getAttribute("data-confirm");
    if (message && !window.confirm(message)) e.preventDefault();
  }, true);
}

portalWarnUnsaved();
portalMoneyPreview();
portalButtonConfirm();
