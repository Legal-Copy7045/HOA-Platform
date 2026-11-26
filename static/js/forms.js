// Small form helpers for owner pages (no inline scripts: CSP). Every page
// works without them; they only make long forms shorter and clearer.
(function () {
  "use strict";

  // <div data-guidance-for="id_project_type"><p data-type="Fence">...</p></div>
  // shows only the guidance for the kind of project chosen.
  document.querySelectorAll("[data-guidance-for]").forEach(function (box) {
    var select = document.getElementById(box.getAttribute("data-guidance-for"));
    if (!select) return;
    function update() {
      var any = false;
      box.querySelectorAll("[data-type]").forEach(function (p) {
        var match = p.getAttribute("data-type") === select.value;
        p.hidden = !match;
        any = any || match;
      });
      box.hidden = !any;
    }
    select.addEventListener("change", update);
    update();
  });

  // <div data-show-if="location=other"> is shown only while the field named
  // "location" in the same form has that value.
  // <textarea data-required-if="position=objected"> is required only then.
  function refresh(form) {
    form.querySelectorAll("[data-show-if], [data-required-if]").forEach(function (el) {
      var rule = el.getAttribute("data-show-if") || el.getAttribute("data-required-if");
      var parts = rule.split("=");
      var field = form.elements[parts[0]];
      if (!field) return;
      // A checkbox counts as its value only while ticked ("opt_in=on").
      var value = field.type === "checkbox" ? (field.checked ? field.value : "") : field.value;
      var on = value === parts[1];
      if (el.hasAttribute("data-show-if")) {
        el.hidden = !on;
      } else {
        el.required = on;
        var mark = form.querySelector("[data-required-mark-for='" + el.id + "']");
        if (mark) mark.hidden = !on;
      }
    });
  }
  document.querySelectorAll("form").forEach(function (form) {
    if (!form.querySelector("[data-show-if], [data-required-if]")) return;
    form.addEventListener("change", function () { refresh(form); });
    refresh(form);
  });

  // A link to #some-panel opens that collapsed panel.
  function openTarget() {
    if (!location.hash || location.hash.length < 2) return;
    var target = document.getElementById(location.hash.slice(1));
    for (var el = target; el; el = el.parentElement) {
      if (el.tagName === "DETAILS") el.open = true;
    }
  }
  window.addEventListener("hashchange", openTarget);
  openTarget();
})();
