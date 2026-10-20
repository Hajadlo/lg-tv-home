/*
 * window.tvHome: the TV side of TV Home (launch apps, settings, LG's launcher).
 *
 * Where it runs (verified on a 2025 LG OLED, webOS 25 / SDK 11.2):
 * - the packaged shell (index.html loads it);
 * - webOSUserScripts/userScript.js: webOS injects it into the top-level page
 *   the app shows, the hosted UI from the TV Home server included, but only after
 *   the page's load event. A mock installed by then is replaced, and
 *   'tvhome-ready' fires on window;
 * - the hosted UI may also load it itself (synchronously, before its mock
 *   check) if the backend serves it; then the userScript sees it and stops.
 *
 * In a plain browser (no PalmServiceBridge) it defines nothing, so the UI's
 * mock loads. Luna calls go through PalmServiceBridge directly (what
 * webOSTV.js does). ES5 plus Promise on purpose.
 */
(function () {
  'use strict';
  if (window.tvHome && window.tvHome.isTV) { return; }
  var inShellFrame = window.top !== window && window.name === 'tvhome-shell';
  if (typeof window.PalmServiceBridge === 'undefined' && !inShellFrame) { return; }

  // tv/install rewrites the SETTINGS line from config.toml (tv/render.py);
  // keep it on one line. apps lists only the sources that are configured.
  var SETTINGS = {"uiUrl": "http://127.0.0.1:8797/", "apps": {"jellyfin": "org.jellyfin.webos", "moonlight": "com.limelight.webos", "youtube": "youtube.leanback.v4"}, "moonlight": {"hostUuid": "", "appId": 0}};
  var CONFIG = {
    uiUrl: SETTINGS.uiUrl,
    apps: SETTINGS.apps,
    // moonlight-tv reads {host_uuid, host_app_id} from argv, i.e. only on a
    // cold start: it selects the host and starts streaming that app
    // (src/app/platform/webos/app_webos.c). A running Moonlight just comes to
    // the front; a dev app may not close it (closeByAppId is denied), so the
    // server closes it over ares first (tv/close-app).
    // hostUuid: Sunshine's /serverinfo uniqueid; appId: the Sunshine app to
    // stream, e.g. "Steam Big Picture" (see SETUP.md for how to find both).
    moonlight: SETTINGS.moonlight,
    lgHomeId: 'com.webos.app.home',
    // Settings deep links, verified on webOS 25 (see tv/README.md).
    settings: {
      all: { id: 'com.palm.app.settings', params: {} },
      picture: { id: 'com.palm.app.settings', params: { target: 'picture' } },
      sound: { id: 'com.palm.app.settings', params: { target: 'sound' } },
      // webOS 25 keeps Network under System (it is the first entry there);
      // target "network" is ignored and lands on Picture.
      network: { id: 'com.palm.app.settings', params: { target: 'system' } },
      input: { id: 'com.webos.app.quickinputpicker', params: {} }
    }
  };

  var LAUNCH_URIS = [
    'luna://com.webos.applicationManager/launch',
    'luna://com.webos.service.applicationmanager/launch'
  ];
  var live = [];  // keep bridges referenced until their callback fires
  var VIDEO_ID = /^[0-9A-Za-z_-]{11}$/;

  function makeError(code, message, detail) {
    var e = new Error(message);
    e.code = code;
    e.detail = detail;
    return e;
  }

  function luna(uri, params, timeoutMs) {
    return new Promise(function (resolve, reject) {
      if (typeof window.PalmServiceBridge === 'undefined') {
        reject(makeError('no-bridge', 'PalmServiceBridge is not available here'));
        return;
      }
      var bridge = new window.PalmServiceBridge();
      var settled = false;
      live.push(bridge);
      function finish() {
        settled = true;
        var i = live.indexOf(bridge);
        if (i >= 0) { live.splice(i, 1); }
        try { bridge.cancel(); } catch (ignore) { /* already done */ }
      }
      var timer = setTimeout(function () {
        if (settled) { return; }
        finish();
        reject(makeError('timeout', uri + ' timed out'));
      }, timeoutMs || 8000);
      bridge.onservicecallback = function (msg) {
        if (settled) { return; }
        clearTimeout(timer);
        finish();
        var res;
        try { res = JSON.parse(msg); } catch (err) {
          reject(makeError('bad-response', 'unparsable luna response', msg));
          return;
        }
        if (res.returnValue === false || res.errorCode !== undefined && res.returnValue !== true) {
          reject(makeError('luna-' + (res.errorCode === undefined ? 'error' : res.errorCode),
            res.errorText || res.errorMessage || 'luna call failed', res));
          return;
        }
        resolve(res);
      };
      bridge.call(uri, JSON.stringify(params || {}));
    });
  }

  // Try the public applicationManager alias first, then the newer name.
  function launchId(id, params) {
    var body = { id: id };
    if (params && Object.keys(params).length) { body.params = params; }
    var lastErr;
    var i = 0;
    function next() {
      if (i >= LAUNCH_URIS.length) { return Promise.reject(lastErr); }
      var uri = LAUNCH_URIS[i++];
      return luna(uri, body).then(function (res) {
        return { ok: true, id: id, params: body.params || null, via: uri, result: res };
      }, function (err) {
        lastErr = err;
        if (err.code === 'no-bridge') { throw err; }
        return next();
      });
    }
    return next();
  }

  function launch(app, opts) {
    opts = opts || {};
    var id = CONFIG.apps[app];
    if (!id) { return Promise.reject(makeError('unknown-app', 'unknown app: ' + app)); }
    var params = {};
    var note = null;
    if (app === 'moonlight') {
      var m = CONFIG.moonlight;
      var hostUuid = opts.hostUuid || m.hostUuid;
      var appId = opts.appId === undefined ? m.appId : opts.appId;
      if (hostUuid && opts.stream !== false) {
        params.host_uuid = hostUuid;
        if (appId) { params.host_app_id = appId; }
      }
    } else if (app === 'youtube' && opts.videoId) {
      // youtube-webos (YouTube AdFree 0.5.x) turns launch params into the
      // youtube.com/tv URL in handleLaunch(): `target` (its own key) or LG's
      // `contentTarget`, "v=<id>" opens the watch page. It reads them on a
      // cold start (window.launchParams) and on webOSRelaunch while running.
      // `target` because its source notes that contentTarget launches do not
      // respect handlesRelaunch.
      if (VIDEO_ID.test(opts.videoId)) {
        params.target = 'v=' + opts.videoId;
      } else {
        note = 'not a YouTube video id; opened YouTube';
      }
    } else if (app === 'jellyfin' && opts.jellyfinItemId) {
      // jellyfin-webos (1.2.x) ignores launch params: it always reopens its
      // saved server. Open Jellyfin and report that the deep link was skipped.
      note = 'jellyfin-webos has no deep links; opened Jellyfin';
    }
    return launchId(id, params).then(function (r) {
      r.app = app;
      r.deepLinked = app === 'moonlight' ? !!params.host_app_id : !!params.target;
      if (note) { r.note = note; }
      return r;
    });
  }

  function openSettings(target) {
    var entry = CONFIG.settings[target] || CONFIG.settings.all;
    return launchId(entry.id, entry.params).catch(function (err) {
      if (entry === CONFIG.settings.all) { throw err; }
      return launchId(CONFIG.settings.all.id, CONFIG.settings.all.params);
    });
  }

  // LG's own launcher, same call as moonlight-tv's "open ribbon". With the
  // autolaunch watcher's takeover on, leaving Home for LG's launcher opens a
  // bypass window so the takeover does not bounce the user straight back.
  function lgHome() {
    return launchId(CONFIG.lgHomeId, { launchType: 'homeKey' });
  }

  function launchParams() {
    try {
      var raw = window.PalmSystem && window.PalmSystem.launchParams;
      return raw ? JSON.parse(raw) : {};
    } catch (err) { return {}; }
  }

  // Inside the shell's iframe (mode "iframe") without a bridge of our own:
  // forward calls to the shell over postMessage.
  if (typeof window.PalmServiceBridge === 'undefined') {
    var seq = 0;
    var waiting = {};
    window.addEventListener('message', function (ev) {
      var msg = ev.data;
      if (!msg || msg.tvHome !== 'result' || !waiting[msg.id]) { return; }
      var w = waiting[msg.id];
      delete waiting[msg.id];
      if (msg.ok) { w.resolve(msg.value); } else {
        w.reject(makeError(msg.value && msg.value.code, msg.value && msg.value.message, msg.value && msg.value.detail));
      }
    });
    var proxy = function (fn) {
      return function () {
        var args = Array.prototype.slice.call(arguments);
        return new Promise(function (resolve, reject) {
          var id = ++seq;
          waiting[id] = { resolve: resolve, reject: reject };
          window.top.postMessage({ tvHome: 'call', id: id, fn: fn, args: args }, '*');
        });
      };
    };
    window.tvHome = {
      isTV: true, version: '0.2.0', config: CONFIG, proxied: true,
      launch: proxy('launch'), openSettings: proxy('openSettings'), lgHome: proxy('lgHome'),
      launchParams: function () { return {}; }, luna: proxy('luna')
    };
    announce();
    return;
  }

  // appinfo has handlesRelaunch: a relaunch (autolaunch watcher, the app tile)
  // only fires this event; without activate() Home stays behind the current
  // app, e.g. LG's launcher.
  document.addEventListener('webOSRelaunch', function () {
    if (window.PalmSystem && window.PalmSystem.activate) { window.PalmSystem.activate(); }
  }, true);

  window.tvHome = {
    isTV: true,
    version: '0.2.0',
    config: CONFIG,
    launch: launch,
    openSettings: openSettings,
    lgHome: lgHome,
    launchParams: launchParams,
    luna: luna
  };
  announce();

  // The userScript may run after the page's own scripts (and after a browser
  // mock was installed): tell the page the real bridge is here.
  function announce() {
    var fire = function () {
      var ev;
      try { ev = new CustomEvent('tvhome-ready'); } catch (err) {
        ev = document.createEvent('Event');
        ev.initEvent('tvhome-ready', false, false);
      }
      window.dispatchEvent(ev);
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fire);
    } else { fire(); }
  }
}());
