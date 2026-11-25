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
})();
