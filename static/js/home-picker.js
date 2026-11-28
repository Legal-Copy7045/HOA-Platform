// The home picker (D96): pick a home by its street address with a
// type-ahead. Enhances every [data-home-picker]; without this script each is
// a plain text box that takes an exact address or lot number. No inline
// scripts or handlers (CSP); suggestions come as JSON from the same origin
// and are built with textContent only. ARIA combobox and listbox pattern:
// Up/Down move, Enter picks, Escape closes. Several homes ([data-multiple])
// become removable chips; the original box keeps "address; address" for the
// form, exactly what a person would type without JavaScript.
(function () {
  "use strict";
  var MIN = 2;
  var DELAY = 180;
  var counter = 0;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function split(value) {
    return (value || "").split(/[;\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function setup(box) {
    // An expando, not an attribute: a row copied by "Add another row"
    // (app.js cloneNode) comes without listeners and is set up afresh.
    if (box.homePickerReady) return;
    box.homePickerReady = true;
    Array.prototype.forEach.call(box.querySelectorAll(".home-picker-chips, [data-picker-search]"), function (n) { n.parentNode.removeChild(n); });
    var original = box.querySelector("input");
    if (original && original.type === "hidden") original.type = "text";
    var list = box.querySelector("[role=listbox]");
    var status = box.querySelector("[data-picker-status]");
    var url = box.getAttribute("data-suggest-url");
    var multiple = box.hasAttribute("data-multiple");
    if (!original || !list || !url) return;
    counter += 1;
    var uid = "hp" + counter;
    list.id = uid + "-list";

    var input = original;
    var chips = null;
    var picked = [];
    if (multiple) {
      // The visible box is a new search box; the original keeps the value.
      input = el("input");
      input.type = "text";
      input.setAttribute("data-picker-search", "");
      input.id = original.id ? original.id + "-search" : uid + "-search";
      input.autocomplete = "off";
      input.className = original.className;
      input.placeholder = picked.length ? "Add another home" : (original.placeholder || "Type part of an address");
      var label = original.getAttribute("aria-label");
      if (!label && original.id) {
        var lab = document.querySelector("label[for='" + original.id + "']");
        if (lab) { lab.setAttribute("for", input.id); label = null; }
      }
      if (label) input.setAttribute("aria-label", label);
      if (original.getAttribute("aria-describedby")) input.setAttribute("aria-describedby", original.getAttribute("aria-describedby"));
      if (original.getAttribute("aria-invalid")) input.setAttribute("aria-invalid", original.getAttribute("aria-invalid"));
      original.type = "hidden";
      original.removeAttribute("required");
      chips = el("ul", "home-picker-chips");
      chips.setAttribute("aria-label", (label || "Homes") + ": chosen");
      box.insertBefore(chips, original);
      box.insertBefore(input, list);
      picked = split(original.value);
      renderChips();
    }
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);

    var timer = null;
    var active = -1;
    var options = [];
    var lastQuery = null;
    var controller = null;

    function sync() {
      original.value = picked.join("; ");
      original.dispatchEvent(new Event("input", { bubbles: true }));
      original.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function renderChips() {
      if (!chips) return;
      chips.textContent = "";
      picked.forEach(function (value, i) {
        var li = el("li", "home-picker-chip");
        li.appendChild(el("span", "home-picker-chip-label", value));
        var remove = el("button", "home-picker-remove");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove " + value);
        remove.appendChild(el("span", null, "×")).setAttribute("aria-hidden", "true");
        remove.addEventListener("click", function () {
          picked.splice(i, 1);
          renderChips();
          sync();
          say(value + " removed.");
          input.focus();
        });
        li.appendChild(remove);
        chips.appendChild(li);
      });
      chips.hidden = picked.length === 0;
      input.placeholder = picked.length ? "Add another home" : (original.placeholder || "Type part of an address");
    }

    function say(text) { if (status) status.textContent = text; }

    function close() {
      list.hidden = true;
      list.textContent = "";
      options = [];
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function highlight(i) {
      options.forEach(function (o, j) { o.setAttribute("aria-selected", j === i ? "true" : "false"); });
      active = i;
      if (i >= 0 && options[i]) {
        input.setAttribute("aria-activedescendant", options[i].id);
        if (options[i].scrollIntoView) options[i].scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function choose(i) {
      var o = options[i];
      if (!o) return;
      var value = o.getAttribute("data-value");
      if (multiple) {
        if (picked.indexOf(value) === -1) picked.push(value);
        input.value = "";
        renderChips();
        sync();
        say(value + " added.");
      } else {
        input.value = value;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        say(value + " chosen.");
      }
      lastQuery = null;
      close();
    }

    function show(results) {
      list.textContent = "";
      options = [];
      active = -1;
      if (!results.length) {
        var none = el("li", "home-picker-empty", "No home matches. Type part of the street address.");
        none.setAttribute("role", "option");
        none.setAttribute("aria-disabled", "true");
        list.appendChild(none);
        say("No matching home.");
      } else {
        results.forEach(function (r, i) {
          var li = el("li", "home-picker-option");
          li.id = uid + "-opt-" + i;
          li.setAttribute("role", "option");
          li.setAttribute("aria-selected", "false");
          li.setAttribute("data-value", r.value);
          li.appendChild(el("span", "home-picker-address", r.label));
          if (r.owners) li.appendChild(el("span", "home-picker-owners small muted", r.owners));
          // mousedown, not click: picks before the box loses focus.
          li.addEventListener("mousedown", function (e) { e.preventDefault(); choose(i); });
          list.appendChild(li);
          options.push(li);
        });
        say(results.length + (results.length === 1 ? " home" : " homes") + " found. Use the up and down arrows to choose.");
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function fetchFor(query) {
      if (query === lastQuery) return;
      lastQuery = query;
      if (controller && controller.abort) controller.abort();
      controller = window.AbortController ? new AbortController() : null;
      var opts = { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" } };
      if (controller) opts.signal = controller.signal;
      fetch(url + "?q=" + encodeURIComponent(query), opts)
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (data) {
          if (input.value.trim() !== query) return;
          show((data && data.results) || []);
        })
        .catch(function () { /* offline or aborted: the plain box still works */ });
    }

    input.addEventListener("input", function () {
      var query = input.value.trim();
      clearTimeout(timer);
      if (query.length < MIN) { close(); lastQuery = null; return; }
      timer = setTimeout(function () { fetchFor(query); }, DELAY);
    });

    input.addEventListener("keydown", function (e) {
      var open = !list.hidden && options.length;
      if (e.key === "ArrowDown") {
        if (!open) { if (input.value.trim().length >= MIN) { lastQuery = null; fetchFor(input.value.trim()); } return; }
        e.preventDefault();
        highlight(active + 1 >= options.length ? 0 : active + 1);
      } else if (e.key === "ArrowUp") {
        if (!open) return;
        e.preventDefault();
        highlight(active <= 0 ? options.length - 1 : active - 1);
      } else if (e.key === "Enter") {
        if (open && active >= 0) { e.preventDefault(); choose(active); }
        else if (multiple && input.value.trim()) {
          // Without a highlighted suggestion, Enter in the add box keeps the
          // text as typed (the server checks it), never submits by surprise.
          e.preventDefault();
          if (open && options.length === 1) { choose(0); return; }
          var typed = input.value.trim();
          if (picked.indexOf(typed) === -1) picked.push(typed);
          input.value = "";
          renderChips();
          sync();
          close();
        }
      } else if (e.key === "Escape") {
        if (!list.hidden) { e.preventDefault(); e.stopPropagation(); close(); }
        else if (!multiple) { input.value = ""; }
      } else if (e.key === "Backspace" && multiple && !input.value && picked.length) {
        var gone = picked.pop();
        renderChips();
        sync();
        say(gone + " removed.");
      }
    });

    input.addEventListener("blur", function () {
      setTimeout(close, 120);
      // Text typed in the add box but not picked still counts (server checks it).
      if (multiple && input.value.trim()) {
        var typed = input.value.trim();
        if (picked.indexOf(typed) === -1) picked.push(typed);
        input.value = "";
        renderChips();
        sync();
      }
    });
  }

  function init(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll("[data-home-picker]"), setup);
  }
  init(document);
  // Regions swapped in place by app.js ([data-swap-region]) bring new pickers.
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        Array.prototype.forEach.call(r.addedNodes, function (n) {
          if (n.nodeType === 1) {
            if (n.matches && n.matches("[data-home-picker]")) setup(n);
            else if (n.querySelectorAll) init(n);
          }
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
