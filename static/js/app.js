// Portal behaviour: mobile navigation and confirmations. No inline scripts (CSP).
(function () {
  "use strict";
  var shell = document.querySelector(".shell");
  var button = document.querySelector("[data-nav-toggle]");
  var scrim = document.querySelector(".scrim");
  function setOpen(open) {
    if (!shell || !button) return;
    shell.classList.toggle("nav-open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (button) button.addEventListener("click", function () {
    setOpen(!shell.classList.contains("nav-open"));
  });
  if (scrim) scrim.addEventListener("click", function () { setOpen(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });

  // <form data-confirm="Are you sure?"> asks before submitting.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) e.preventDefault();
  }, true);
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
