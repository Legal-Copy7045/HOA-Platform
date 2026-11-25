// My profile: "Install the app" button and push notifications for this device.
// No inline scripts (CSP). Works without it: the page explains how to install.
(function () {
  "use strict";
  var installButton = document.querySelector("[data-install-app]");
  var deferred = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    if (installButton) installButton.hidden = false;
  });
  if (installButton) installButton.addEventListener("click", function () {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.finally(function () { deferred = null; installButton.hidden = true; });
  });

  var panel = document.querySelector("[data-push]");
  if (!panel) return;
  var status = panel.querySelector("[data-push-status]");
  var onButton = panel.querySelector("[data-push-on]");
  var offButton = panel.querySelector("[data-push-off]");
  var csrf = document.querySelector("input[name=csrfmiddlewaretoken]");

  function say(text) { if (status) status.textContent = text; }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    say("This browser can't show notifications. On iPhone, install the app first, then open it from the home screen.");
    return;
  }

  function keyBytes(base64url) {
    var padded = (base64url + "===".slice((base64url.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(padded);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf ? csrf.value : "" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok || !data.ok) throw new Error(data.error || "Something went wrong. Please try again.");
        return data;
      });
    });
  }

  function show(subscribed) {
    onButton.hidden = subscribed;
    offButton.hidden = !subscribed;
  }

  navigator.serviceWorker.ready.then(function (reg) {
    return reg.pushManager.getSubscription().then(function (sub) {
      show(!!sub);
      if (sub) say("On for this device.");
      onButton.addEventListener("click", function () {
        onButton.disabled = true;
        Notification.requestPermission().then(function (permission) {
          if (permission !== "granted") throw new Error("Notifications are blocked for this site in your browser settings.");
          return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(panel.getAttribute("data-vapid-key")) });
        }).then(function (newSub) {
          return post(panel.getAttribute("data-subscribe-url"), newSub.toJSON()).then(function () {
            show(true); say("On for this device.");
          }, function (err) { newSub.unsubscribe(); throw err; });
        }).catch(function (err) { say(err.message); }).finally(function () { onButton.disabled = false; });
      });
      offButton.addEventListener("click", function () {
        offButton.disabled = true;
        reg.pushManager.getSubscription().then(function (current) {
          if (!current) return null;
          var endpoint = current.endpoint;
          return current.unsubscribe().then(function () {
            return post(panel.getAttribute("data-unsubscribe-url"), { endpoint: endpoint });
          });
        }).then(function () { show(false); say("Off for this device."); })
          .catch(function (err) { say(err.message); })
          .finally(function () { offButton.disabled = false; });
      });
    });
  });
})();
