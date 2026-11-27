// Portal behavior: mobile navigation, menu groups, confirmations and ballot
// limits. No inline scripts (CSP).
(function () {
  "use strict";
  var shell = document.querySelector(".shell");
  var sidebar = document.getElementById("sidebar");
  var button = document.querySelector("[data-nav-toggle]");
  // The topbar's Menu and the phone tab bar's Menu (D48) work the same way.
  var toggles = document.querySelectorAll("[data-nav-toggle]");
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
    Array.prototype.forEach.call(toggles, function (t) { t.setAttribute("aria-expanded", open ? "true" : "false"); });
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
  Array.prototype.forEach.call(toggles, function (t) {
    t.addEventListener("click", function () {
      button = t;
      setOpen(!shell.classList.contains("nav-open"), true);
    });
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

  // <form data-busy="Checking the file…"> shows that text on its button while
  // the upload and malware check run, and stops a second press.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.getAttribute || !form.hasAttribute("data-busy")) return;
    var button = form.querySelector("button[type=submit], button:not([type])");
    if (!button) return;
    var text = form.getAttribute("data-busy");
    window.setTimeout(function () {
      button.disabled = true;
      button.textContent = text;
      button.setAttribute("aria-busy", "true");
    }, 0);
  });

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

// Uploads: all the files in one submission share a size limit. Warn before
// sending, so nothing typed is lost (review R2-004).
(function () {
  var meta = document.querySelector('meta[name="max-upload-bytes"]');
  var limit = meta ? parseInt(meta.getAttribute("content"), 10) : 0;
  if (!limit) return;
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.querySelectorAll) return;
    var total = 0;
    Array.prototype.forEach.call(form.querySelectorAll("input[type=file]"), function (input) {
      Array.prototype.forEach.call(input.files || [], function (f) { total += f.size; });
    });
    if (total > limit) {
      e.preventDefault();
      window.alert("These files are too large to send together (" + Math.round(limit / 1048576) +
        " MB in all). Please choose fewer, or add them in smaller groups.");
    }
  }, true);
})();

// <details data-collapse-narrow open> starts closed on a phone (board
// controls that would push the page's own content down, WT-044). Without
// JavaScript it stays open.
(function () {
  "use strict";
  if (!window.matchMedia || !window.matchMedia("(max-width: 820px)").matches) return;
  Array.prototype.forEach.call(document.querySelectorAll("details[data-collapse-narrow]"), function (d) {
    d.open = false;
  });
})();

// --- Phone layout and in-page navigation (walkthrough fixes, D48) ----------------

// Filter and tab links (<nav data-swap>) swap only the list region
// ([data-swap-region]) in place: no reload, no jump to the top. The address
// bar follows (back and forward work). Without JavaScript the plain link
// loads the page at #results, which is the list, so it stays in view.
(function () {
  "use strict";
  if (!window.fetch || !window.DOMParser || !window.history || !history.pushState) return;
  function region() { return document.querySelector("[data-swap-region]"); }
  if (!region()) return;
  function plain(url) { var u = new URL(url, window.location.href); u.hash = ""; return u.href; }
  function markCurrent(url) {
    var here = plain(url);
    Array.prototype.forEach.call(document.querySelectorAll("nav[data-swap]"), function (nav) {
      var links = nav.querySelectorAll("a[href]");
      var match = null;
      Array.prototype.forEach.call(links, function (a) { if (plain(a.href) === here) match = a; });
      if (!match) return;
      var attr = nav.classList.contains("filters") ? "true" : "page";
      Array.prototype.forEach.call(links, function (a) { a.removeAttribute("aria-current"); });
      match.setAttribute("aria-current", attr);
    });
  }
  function swap(url, push, focusHref) {
    var current = region();
    if (!current) { window.location.href = url; return; }
    current.setAttribute("aria-busy", "true");
    fetch(plain(url), { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok || r.redirected) throw new Error("reload");
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.querySelector("[data-swap-region]");
        if (!fresh) throw new Error("reload");
        // Navs outside the region come from the new page too (counts, links).
        var oldNavs = document.querySelectorAll("nav[data-swap]");
        var newNavs = doc.querySelectorAll("nav[data-swap]");
        current.replaceWith(document.importNode(fresh, true));
        if (oldNavs.length === newNavs.length) {
          Array.prototype.forEach.call(oldNavs, function (nav, i) {
            if (document.contains(nav)) nav.replaceWith(document.importNode(newNavs[i], true));
          });
        }
        if (doc.title) document.title = doc.title;
        if (push) history.pushState({ swap: true }, "", plain(url));
        markCurrent(url);
        var target = null;
        if (focusHref) {
          Array.prototype.forEach.call(document.querySelectorAll("nav[data-swap] a[href]"), function (a) {
            if (!target && plain(a.href) === plain(focusHref)) target = a;
          });
        }
        if (target) target.focus({ preventScroll: true });
      })
      .catch(function () { window.location.href = url; });
  }
  history.replaceState({ swap: true }, "", window.location.href);
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("nav[data-swap] a[href]");
    if (!a || a.target || a.hasAttribute("download")) return;
    if (new URL(a.href, window.location.href).origin !== window.location.origin) return;
    e.preventDefault();
    swap(a.href, true, a.href);
  });
  window.addEventListener("popstate", function (e) {
    if (e.state && e.state.swap) swap(window.location.href, false, null);
  });
})();

// One control per form: a header button (<a data-opens="panel-id">) opens its
// <details> panel in place, and is hidden while that panel is open.
(function () {
  "use strict";
  var openers = document.querySelectorAll("[data-opens]");
  Array.prototype.forEach.call(openers, function (button) {
    var panel = document.getElementById(button.getAttribute("data-opens"));
    if (!panel || panel.tagName !== "DETAILS") return;
    function sync() {
      button.hidden = panel.open;
      // A panel marked data-hide-closed is shown only while open.
      if (panel.hasAttribute("data-hide-closed")) panel.hidden = !panel.open;
    }
    button.addEventListener("click", function (e) {
      e.preventDefault();
      panel.hidden = false;
      panel.open = true;
      sync();
      panel.scrollIntoView({ block: "start" });
      var field = panel.querySelector("input:not([type=hidden]), select, textarea");
      if (field) field.focus({ preventScroll: true });
    });
    panel.addEventListener("toggle", sync);
    sync();
  });
})();

// Phones: a sideways-scrolling tab bar shows its current tab (WT-046); list
// filters (details.list-filter) start open on wider screens (WT-047).
(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll(".subnav [aria-current=page]"), function (a) {
    var bar = a.parentElement;
    if (bar && bar.scrollWidth > bar.clientWidth) {
      bar.scrollLeft = a.offsetLeft - bar.offsetLeft - (bar.clientWidth - a.offsetWidth) / 2;
    }
  });
  // A link that stands alone in its paragraph or cell (no sentence around
  // it) gets the 44px tap height on phones (WT-036), like p.link-row.
  var main = document.getElementById("main");
  if (main) {
    Array.prototype.forEach.call(main.querySelectorAll("p > a, dd > a, li > a, div > a, span > a"), function (a) {
      var parent = a.parentElement;
      if (a.classList.contains("btn") || parent.children.length !== 1) return;
      var around = parent.textContent.replace(/\s+/g, " ").trim().length - a.textContent.replace(/\s+/g, " ").trim().length;
      if (around <= 3) a.classList.add("tap");
    });
  }
  // A link to #something inside a closed panel or folded section (<details>,
  // e.g. the Community settings index, WT-014) opens it first, on load and on
  // in-page links.
  function openTo(hash) {
    if (!hash || hash.length < 2) return;
    var target = null;
    try { target = document.getElementById(decodeURIComponent(hash.slice(1))); } catch (err) { return; }
    for (var d = target && target.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) {
      d.hidden = false;
      d.open = true;
    }
    if (target) target.scrollIntoView({ block: "start" });
  }
  if (window.location.hash) openTo(window.location.hash);
  window.addEventListener("hashchange", function () { openTo(window.location.hash); });
  var wide = window.matchMedia && window.matchMedia("(min-width: 821px)").matches;
  if (wide) {
    Array.prototype.forEach.call(document.querySelectorAll("details.list-filter"), function (d) {
      d.open = true;
      d.classList.add("js-open");
    });
  }
})();

// Board cue (D64): the "Board" badge rendered by base.html in the strip at
// the top of a board page moves beside the page's heading (an eyebrow above
// the <h1>, so the heading's own text stays the page name).
(function () {
  "use strict";
  var badge = document.querySelector("[data-board-badge]");
  var h1 = document.querySelector("main .page-head h1") || document.querySelector("main h1");
  if (!badge || !h1 || !h1.parentNode) return;
  var tag = badge.cloneNode(true);
  tag.removeAttribute("data-board-badge");
  tag.classList.add("board-badge-head");
  tag.setAttribute("aria-hidden", "true"); // the strip already says it
  h1.parentNode.insertBefore(tag, h1);
  document.documentElement.classList.add("board-badge-moved");
})();
