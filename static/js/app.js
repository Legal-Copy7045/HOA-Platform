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

  // Installable app: the service worker caches static files only (signed-in pages).
  var sw = document.body.getAttribute("data-sw");
  if (sw && "serviceWorker" in navigator) {
    navigator.serviceWorker.register(sw, { scope: "/" }).catch(function () {});
  }
})();
