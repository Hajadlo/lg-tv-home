/*
 * Packaged shell of the TV Home app.
 *
 * Probes the hosted UI on the TV Home server and, when it answers, replaces
 * itself with it (top-level navigation: the hosted page keeps luna access, and
 * webOSUserScripts/userScript.js gives it window.tvHome). When the server is
 * down it shows a packaged fallback with the app launchers and keeps retrying.
 *
 * Launch params (for tests): {"uiUrl": "...", "mode": "navigate"|"iframe"|"fallback"}.
 */
(function () {
  'use strict';
  var tv = window.tvHome;
  var params = tv.launchParams();
  var uiUrl = params.uiUrl || tv.config.uiUrl;
  var mode = params.mode || 'navigate';
  var PROBE_MS = 2500;
  var RETRY_MS = 10000;
  var retryTimer = null;
  var leaving = false;

  function probe(url, done) {
    var xhr = new XMLHttpRequest();
    var finished = false;
    function end(ok) { if (!finished) { finished = true; done(ok); } }
    xhr.open('GET', url + (url.indexOf('?') < 0 ? '?' : '&') + 'probe=' + Date.now());
    xhr.timeout = PROBE_MS;
    xhr.onload = function () { end(xhr.status >= 200 && xhr.status < 400); };
    xhr.onerror = xhr.ontimeout = xhr.onabort = function () { end(false); };
    try { xhr.send(); } catch (err) { end(false); }
  }

  function enterHosted() {
    leaving = true;
    clearTimeout(retryTimer);
    if (mode === 'iframe') {
      var frame = document.createElement('iframe');
      frame.name = 'tvhome-shell';  // bridge.js proxies to us only in this frame
      frame.src = uiUrl;
      frame.setAttribute('allow', 'autoplay');
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;border:0;background:#0b0d10';
      document.body.appendChild(frame);
      frame.addEventListener('load', function () { frame.focus(); });
      return;
    }
    window.location.replace(uiUrl);
  }

  function attempt() {
    if (mode === 'fallback') { showFallback(); return; }
    probe(uiUrl, function (ok) {
      if (ok) { enterHosted(); return; }
      showFallback();
      retryTimer = setTimeout(attempt, RETRY_MS);
    });
  }

  // iframe mode: run tvHome calls for the hosted frame when its own bridge
  // has no PalmServiceBridge (see bridge.js proxy).
  window.addEventListener('message', function (ev) {
    var msg = ev.data;
    if (!msg || msg.tvHome !== 'call') { return; }
    var fn = tv[msg.fn];
    var reply = function (ok, value) {
      ev.source.postMessage({ tvHome: 'result', id: msg.id, ok: ok, value: value }, '*');
    };
    if (typeof fn !== 'function') { reply(false, { code: 'unknown-fn', message: msg.fn }); return; }
    fn.apply(tv, msg.args || []).then(function (v) { reply(true, v); }, function (e) {
      reply(false, { code: e.code, message: e.message, detail: e.detail });
    });
  });

  // ---- fallback UI ----
  var shown = false;
  function showFallback() {
    document.getElementById('splash').hidden = true;
    if (shown) { return; }
    shown = true;
    // Only the apps configured in config.toml get a tile.
    Array.prototype.slice.call(document.querySelectorAll('#fallback [data-app]')).forEach(function (el) {
      if (!tv.config.apps[el.dataset.app]) { el.parentNode.removeChild(el); }
    });
    document.getElementById('fallback').hidden = false;
    tick();
    setInterval(tick, 10000);
    focusables()[0].focus();
  }

  function tick() {
    var d = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    document.getElementById('clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
    document.getElementById('date').textContent = d.toLocaleDateString('en-GB',
      { weekday: 'long', day: 'numeric', month: 'long' });
  }

  function focusables() {
    return Array.prototype.slice.call(document.querySelectorAll('#fallback button'));
  }

  function activate(el) {
    var p;
    if (el.dataset.app) { p = tv.launch(el.dataset.app, {}); }
    else if (el.dataset.settings) { p = tv.openSettings(el.dataset.settings); }
    else if (el.dataset.lghome) { p = tv.lgHome(); }
    if (p) {
      p.catch(function (err) {
        document.getElementById('status').textContent = 'Could not open: ' + err.message;
      });
    }
  }

  function move(dx, dy) {
    var items = focusables();
    var cur = document.activeElement;
    if (items.indexOf(cur) < 0) { items[0].focus(); return; }
    var r = cur.getBoundingClientRect();
    var cx = r.left + r.width / 2;
    var cy = r.top + r.height / 2;
    var best = null;
    var bestScore = Infinity;
    items.forEach(function (el) {
      if (el === cur) { return; }
      var q = el.getBoundingClientRect();
      var x = q.left + q.width / 2 - cx;
      var y = q.top + q.height / 2 - cy;
      var along = dx ? x * dx : y * dy;
      if (along <= 1) { return; }
      var across = dx ? Math.abs(y) : Math.abs(x);
      var score = along + across * 2;
      if (score < bestScore) { bestScore = score; best = el; }
    });
    if (best) { best.focus(); }
  }

  document.addEventListener('keydown', function (ev) {
    if (!shown || leaving) { return; }
    switch (ev.keyCode) {
      case 37: move(-1, 0); break;
      case 39: move(1, 0); break;
      case 38: move(0, -1); break;
      case 40: move(0, 1); break;
      case 13: activate(document.activeElement); break;
      case 461: break;  // Back on the home screen does nothing
      default: return;
    }
    ev.preventDefault();
  });

  document.addEventListener('webOSRelaunch', function () {
    if (shown && !leaving) { clearTimeout(retryTimer); attempt(); }
  }, true);

  attempt();
}());
