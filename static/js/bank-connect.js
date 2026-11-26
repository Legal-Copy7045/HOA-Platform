// Teller Connect on the "Connect a bank" page only (that page's CSP allows
// cdn.teller.io and api.teller.io; no other page does). On success the access
// token goes to the server in the page's CSRF-protected form POST: never in a
// URL, never stored in the browser.
(function () {
  "use strict";
  var box = document.getElementById("teller-connect");
  var button = document.getElementById("teller-open");
  var form = document.getElementById("teller-enrollment");
  var status = document.getElementById("teller-status");
  if (!box || !button || !form) return;
  if (!window.TellerConnect) {
    status.textContent = "Teller could not be loaded. Check the connection, or import statement files instead.";
    button.disabled = true;
    return;
  }
  var connect = window.TellerConnect.setup({
    applicationId: box.getAttribute("data-application-id"),
    environment: box.getAttribute("data-environment"),
    products: ["transactions", "balance"],
    selectAccount: "multiple",
    onSuccess: function (enrollment) {
      status.textContent = "Connected at the bank. Saving...";
      button.disabled = true;
      form.elements.access_token.value = enrollment && enrollment.accessToken ? enrollment.accessToken : "";
      form.submit();
    },
    onExit: function () {
      status.textContent = "Closed without connecting.";
    },
    onFailure: function () {
      status.textContent = "The bank connection did not complete. Try again, or import statement files.";
    }
  });
  button.addEventListener("click", function () {
    connect.open();
  });
})();
