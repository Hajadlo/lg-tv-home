/* TV Home lite: the page side that replaces the server.

   Defines TVH.api (the same interface ui/js/api.js offers for the server) on
   top of lite/js/sources.js, so ui/js/app.js renders the result unchanged.
   Settings live in localStorage (lite/js/setup.js edits them); network calls
   and the rest go to the app's own service (lite/service/service.js) through
   luna. ES5 only. */
window.TVH = window.TVH || {};
window.TVLite = window.TVLite || {};
(function () {
  "use strict";
  var S = TVLite.sources;
  var SERVICE = "luna://com.tvhome.lite.service/";
  var CONFIG_KEY = "tvhome.lite.config", CACHE_KEY = "tvhome.lite.cache";
  var DEFAULT_APPS = { jellyfin: "org.jellyfin.webos", youtube: "youtube.leanback.v4", moonlight: "com.limelight.webos" };
  var TTL = { weather: 15 * 60000, youtube: 30 * 60000, jellyfin: 20000, steam: 10 * 60000, location: 24 * 3600000 };
  var SOURCE_TIMEOUT = 20000;
  var SUNSHINE_PORT = 47989;
  var WARN_DAYS = 7;

  /* ---------- settings and cache (localStorage) ---------- */
  function readJSON(key, fallback) {
    try { var v = JSON.parse(localStorage.getItem(key)); return v && typeof v === "object" ? v : fallback; } catch (e) { return fallback; }
  }
  function writeJSON(key, v) {
    try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { console.warn("lite: cannot save " + key); }
  }
  function defaults() {
    return {
      weather: { city: "", place: null },
      youtube: { channels: "" },
      jellyfin: { url: "", user: "", creds: null },
      steam: { apiKey: "", steamId: "", mac: "", broadcast: "", pcIp: "", hostUuid: "", appId: "" },
      apps: { jellyfin: DEFAULT_APPS.jellyfin, youtube: DEFAULT_APPS.youtube, moonlight: DEFAULT_APPS.moonlight },
      devmode: { autoRenew: true },
      deviceId: "tvhome-lite-" + Math.random().toString(36).slice(2, 10)
    };
  }
  function loadConfig() {
    var d = defaults(), c = readJSON(CONFIG_KEY, null);
    if (!c) return d;
    Object.keys(d).forEach(function (k) {
      if (c[k] === undefined) c[k] = d[k];
      else if (d[k] && typeof d[k] === "object") {
        Object.keys(d[k]).forEach(function (j) { if (c[k][j] === undefined) c[k][j] = d[k][j]; });
      }
    });
    return c;
  }
  var config = loadConfig();
  var cache = readJSON(CACHE_KEY, {});
  function saveCache() { writeJSON(CACHE_KEY, cache); }

  /* ---------- the service ---------- */
  function onTV() { return typeof window.PalmServiceBridge !== "undefined" && window.tvHome && window.tvHome.luna; }
  function call(method, params, timeoutMs) {
    if (!onTV()) return Promise.reject(new Error("not on a TV: " + method));
    return window.tvHome.luna(SERVICE + method, params || {}, timeoutMs || 25000);
  }
  /* HTTP through the service (free headers, no CORS). In a desktop browser,
     plain XHR so the page can be tried there (only CORS-friendly APIs work). */
  function xhr(req) {
    return new Promise(function (resolve, reject) {
      var x = new XMLHttpRequest();
      x.open(req.method || "GET", req.url);
      x.timeout = req.timeout || 15000;
      Object.keys(req.headers || {}).forEach(function (h) { if (h !== "Cookie") x.setRequestHeader(h, req.headers[h]); });
      x.onload = function () {
        var body = x.responseText, match = null;
        if (req.match) [].concat(req.match).some(function (p) { match = new RegExp(p).exec(body); return !!match; });
        resolve({ status: x.status, url: x.responseURL || req.url, body: body, match: match ? Array.prototype.slice.call(match) : null,
                  headers: {} });
      };
      x.onerror = x.ontimeout = function () { reject(new Error("network error for " + req.url)); };
      x.send(req.body || null);
    });
  }
  function http(req) {
    if (!onTV()) return xhr(req);
    return call("http", req, (req.timeout || 15000) + 5000);
  }
  TVLite.http = http;   // tests on the TV may swap this for a fake

  function withTimeout(p, ms, what) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error(what + " timed out")); }, ms);
      p.then(function (v) { clearTimeout(t); resolve(v); }, function (e) { clearTimeout(t); reject(e); });
    });
  }

  /* ---------- the TV bridge gets the configured app ids ---------- */
  function applyBridge() {
    var tv = window.tvHome;
    if (!tv || !tv.config) return;
    tv.config.apps = {
      jellyfin: config.apps.jellyfin || DEFAULT_APPS.jellyfin,
      youtube: config.apps.youtube || DEFAULT_APPS.youtube,
      moonlight: config.apps.moonlight || DEFAULT_APPS.moonlight
    };
    tv.config.moonlight = { hostUuid: config.steam.hostUuid || "", appId: +config.steam.appId || 0 };
  }
  applyBridge();
  window.addEventListener("tvhome-ready", applyBridge);

  /* ---------- installed apps (best effort) ---------- */
  var installed = null;   // null: unknown
  function detectApps() {
    return call("apps", {}, 8000).then(function (r) {
      installed = r.dirs && r.dirs.length ? r.apps : null;
      return installed;
    }, function () { installed = null; return null; });
  }
  var appsReady = detectApps();
  function isInstalled(id) { return installed ? installed.indexOf(id) >= 0 : null; }

  /* ---------- which parts are on ---------- */
  function channelList() { return S.parseChannels(config.youtube.channels); }
  function configured() {
    var st = config.steam;
    return {
      jellyfin: !!(config.jellyfin.creds && config.jellyfin.creds.token),
      youtube: channelList().length > 0,
      steamGames: !!(st.apiKey && /^\d{17}$/.test(String(st.steamId).trim())),
      pc: !!st.mac
    };
  }
  function features() {
    // A tab shows when its source is set up or its app is on the TV. The TV
    // may refuse to list apps (webOS 25 does); then every app with an id
    // counts as there. An empty app id in Set up hides the tab.
    var c = configured();
    var has = function (app) { return !!config.apps[app] && isInstalled(config.apps[app]) !== false; };
    return {
      jellyfin: !!config.apps.jellyfin && (c.jellyfin || has("jellyfin")),
      youtube: !!config.apps.youtube && (c.youtube || has("youtube")),
      steam: !!config.apps.moonlight && (c.steamGames || c.pc || has("moonlight")),
      weather: true
    };
  }

  /* ---------- per-source refresh, keeping the last good value ----------
     The last good value of each source is kept in localStorage too, so the
     page shows at once when the app opens and refreshes behind it. A source
     slower than FIRST_WAIT shows its last value (or "Loading") and the page
     is told to load again when it arrives (event tvhome-refresh). */
  var FIRST_WAIT = 3500;
  var state = {};   // name -> {at, value, error, running}
  var refreshTimer = null;
  function pageRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () {
      var ev = document.createEvent("Event");
      ev.initEvent("tvhome-refresh", false, false);
      window.dispatchEvent(ev);
    }, 300);
  }
  function stateOf(name) {
    if (state[name]) return state[name];
    var last = (cache.last || {})[name];
    return (state[name] = { at: last ? last.at : 0, value: last ? last.value : undefined, error: null, running: null });
  }
  function remember(name, s) {
    cache.last = cache.last || {};
    cache.last[name] = { at: s.at, value: s.value };
    saveCache();
  }
  function refresh(name, ttl, fetcher) {
    var s = stateOf(name);
    if (s.value !== undefined && Date.now() - s.at < ttl) return Promise.resolve(s.value);
    if (!s.running) s.running = withTimeout(fetcher(), SOURCE_TIMEOUT, name).then(function (v) {
      s.at = Date.now(); s.value = v; s.error = null; s.running = null;
      remember(name, s);
      if (s.late) { s.late = false; pageRefresh(); }
      return v;
    }, function (e) {
      s.error = e && e.message || String(e); s.running = null;
      console.warn("lite: " + name + ": " + s.error);
      if (s.late) { s.late = false; pageRefresh(); }
      if (s.value === undefined) throw e;
      return s.value;
    });
    return soon(s);
  }
  /* The source's answer, or after FIRST_WAIT its last value (or LOADING). */
  var LOADING = {};
  function soon(s) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { s.late = true; resolve(s.value === undefined ? LOADING : s.value); }, FIRST_WAIT);
      s.running.then(function (v) { clearTimeout(t); resolve(v); }, function (e) { clearTimeout(t); reject(e); });
    });
  }
  function loading(v, empty) { return v === LOADING ? empty : v; }
  function forget(name) { delete state[name]; if (cache.last) delete cache.last[name]; }

  function place() {
    var p = config.weather.place;
    if (p && typeof p.latitude === "number") return Promise.resolve(p);
    var ip = cache.ipPlace;
    if (ip && Date.now() - ip.at < TTL.location) return Promise.resolve(ip.place);
    return S.ipLocation(http).then(function (place) {
      if (place) { cache.ipPlace = { at: Date.now(), place: place }; saveCache(); }
      return place;
    });
  }
  function weather() {
    return refresh("weather", TTL.weather, function () {
      return place().then(function (loc) { return loc ? S.fetchWeather(http, loc) : null; });
    }).then(function (v) { return loading(v, null); }).catch(function () { return null; });
  }

  /* YouTube: resolve @handles once (kept in the cache), then the feeds. */
  function channelIds() {
    var tokens = channelList(), map = cache.handles || (cache.handles = {});
    return S.pool(tokens, 3, function (t) {
      if (map[t]) return map[t];
      return S.resolveChannel(http, t).then(function (id) { if (id) { map[t] = id; } return id; });
    }).then(function (ids) {
      Object.keys(map).forEach(function (t) { if (tokens.indexOf(t) < 0) delete map[t]; });
      saveCache();
      var out = [], missing = [];
      ids.forEach(function (id, i) {
        if (typeof id === "string") { if (out.indexOf(id) < 0) out.push(id); } else missing.push(tokens[i]);
      });
      return { ids: out, missing: missing };
    });
  }
  function notice(head, text, door) { return { head: head, text: text, door: door || "" }; }
  function youtube(on) {
    if (!on.youtube) return Promise.resolve({ updated: null, channelCount: 0, auth: "ok", videos: [], "continue": [],
      notice: notice("No channels yet", "Add YouTube channels in Set up (top right) and their new videos show up here. YouTube itself opens as usual.", "Not set up") });
    return refresh("youtube", TTL.youtube, function () {
      return channelIds().then(function (r) {
        var yt = cache.yt || (cache.yt = {});
        return S.fetchYouTube(http, r.ids, yt).then(function (out) {
          saveCache();
          out.missingChannels = r.missing;
          return out;
        });
      });
    }).then(function (v) {
      return loading(v, { videos: [], auth: "ok", notice: notice("Loading", "Fetching the newest videos from your channels\u2026", "Loading") });
    }).catch(function (e) {
      return { videos: [], auth: "ok", notice: notice("YouTube isn't answering", "Couldn't load the channel feeds (" + e.message + "). YouTube itself opens as usual.", "Not reachable") };
    });
  }

  function jellyfin(on) {
    var creds = config.jellyfin.creds;
    if (!on.jellyfin) return Promise.resolve({ resume: [], nextUp: [], latest: [],
      notice: notice("Jellyfin isn't set up here yet", "Sign in under Set up (top right) and what you watch shows up here. Enter opens Jellyfin as usual.", "Not set up") });
    return refresh("jellyfin", TTL.jellyfin, function () { return S.jfRows(http, creds, config.deviceId); })
      .then(function (v) { return loading(v, { resume: [], nextUp: [], latest: [], notice: notice("Loading", "Asking Jellyfin\u2026", "") }); })
      .catch(function (e) {
        if (e && e.code === "auth") return { resume: [], nextUp: [], latest: [],
          notice: notice("Jellyfin signed TV Home out", "Sign in again under Set up. Jellyfin itself opens as usual.", "Sign in again") };
        return { resume: [], nextUp: [], latest: [],
          notice: notice("Jellyfin isn't answering", "Couldn't reach " + creds.url + ". The Jellyfin app may still open on its own.", "Not reachable right now") };
      });
  }

  function steam(on) {
    var st = config.steam;
    if (!on.steamGames) return Promise.resolve({ recent: [],
      notice: notice("No games listed", "Add a Steam Web API key and your SteamID under Set up to see what you played recently. Enter " +
        (st.mac ? "wakes the PC and opens Moonlight." : "opens Moonlight."), "") });
    return refresh("steam", TTL.steam, function () { return S.fetchSteam(http, st.apiKey.trim(), String(st.steamId).trim()); })
      .then(function (v) { return loading(v, { recent: [], notice: notice("Loading", "Asking Steam\u2026", "") }); })
      .catch(function (e) {
        return { recent: [], notice: notice("Steam isn't answering", "Couldn't load your games (" + e.message + "). Enter still opens Moonlight.", "") };
      });
  }

  /* ---------- the gaming PC ---------- */
  function pcState() {
    var ip = String(config.steam.pcIp || "").trim();
    if (!ip) return Promise.resolve({ state: "unknown", sunshine: "unknown", ready: false, phase: "idle", error: null });
    return call("tcp", { host: ip, port: SUNSHINE_PORT, timeout: 1200 }, 6000).then(function (r) {
      return { state: r.open ? "on" : (waking ? "waking" : "off"), sunshine: r.open ? "listening" : "unknown",
               ready: !!r.open, phase: r.open ? "idle" : (waking ? "waking" : "off"), error: null };
    }, function () {
      return { state: "unknown", sunshine: "unknown", ready: false, phase: "idle", error: null };
    });
  }
  var waking = false;
  function wake() {
    var st = config.steam;
    if (!st.mac) return Promise.resolve(false);
    var bc = String(st.broadcast || "").split(/[\s,]+/).filter(Boolean);
    return call("wol", { mac: st.mac, broadcast: bc }, 8000).then(function () { return true; });
  }
  function play() {
    var hasIp = !!String(config.steam.pcIp || "").trim();
    return pcState().then(function (s) {
      if (s.ready) return s;
      return wake().then(function (sent) {
        // Without the PC's address nothing tells when it is up: Moonlight
        // opens right away and waits for the host itself.
        if (!hasIp) return { state: sent ? "waking" : "unknown", sunshine: "unknown", ready: true, phase: "idle", error: null };
        waking = true;
        setTimeout(function () { waking = false; }, 180000);
        resend();
        return { state: "waking", sunshine: "unknown", ready: false, phase: "waking", error: null };
      });
    }).catch(function (e) {
      var err = new Error(e.message);
      err.userMessage = "Couldn't wake the PC (" + e.message + ")";
      throw err;
    });
  }
  /* Resend the magic packet every 30 s while waiting, like the server. */
  var resendTimer = null;
  function resend() {
    clearTimeout(resendTimer);
    resendTimer = setTimeout(function () {
      if (!waking) return;
      pcState().then(function (s) { if (!s.ready && waking) { wake().catch(function () {}); resend(); } });
    }, 30000);
  }

  /* ---------- Developer Mode ---------- */
  var devmode = { checked: false, hours: null, renewed: false, error: null, at: 0 };
  function checkDevmode(renew) {
    return call("devmode", { renew: !!renew }, 70000).then(function (r) {
      devmode = { checked: true, hours: typeof r.hours === "number" ? r.hours : null, renewed: !!r.renewed,
                  error: r.renewError || null, at: Date.now() };
      cache.devmode = devmode; saveCache();
      return devmode;
    }, function (e) {
      devmode = { checked: true, hours: null, renewed: false, error: e.message, at: Date.now() };
      return devmode;
    });
  }
  var devmodeReady = onTV() ? checkDevmode(config.devmode.autoRenew !== false) : Promise.resolve(devmode);
  function devmodeNotice() {
    if (typeof devmode.hours !== "number") return "";
    var days = Math.floor(devmode.hours / 24);
    if (days >= WARN_DAYS) return "";
    return days <= 0 ? "Developer Mode ends today: extend it in Set up" :
      "Developer Mode ends in " + days + " day" + (days === 1 ? "" : "s") + ": extend it in Set up";
  }

  /* ---------- TVH.api ---------- */
  function home() {
    return appsReady.then(function () {
      var on = configured(), f = features();
      return Promise.all([
        weather(),
        f.jellyfin ? jellyfin(on) : null,
        f.youtube ? youtube(on) : null,
        f.steam ? steam(on) : null,
        f.steam ? pcState() : null
      ]).then(function (r) {
        return {
          now: new Date().toISOString(), features: f, weather: r[0], jellyfin: r[1], youtube: r[2], steam: r[3], pc: r[4],
          system: null, notice: devmodeNotice(), lite: true
        };
      });
    });
  }
  function img(url, w) {
    if (!url) return null;
    if (url.indexOf("{w}") >= 0) return url.replace("{w}", String(w));
    if (url.indexOf("i.ytimg.com/") >= 0 && w <= 480) return url.replace("maxresdefault", "mqdefault");
    return url;
  }
  function jfOpen(id) {
    var c = config.jellyfin.creds;
    if (!c) return Promise.resolve({ itemId: id, state: "failed" });
    // Runs in the service: the page goes to the background once Jellyfin opens.
    call("jfPlay", { url: c.url, token: c.token, userId: c.userId, itemId: id }, 45000).then(function (r) {
      console.info("lite: jellyfin " + id + " " + r.state);
    }, function (e) { console.warn("lite: jellyfin open " + id + ": " + e.message); });
    return Promise.resolve({ itemId: id, state: "waiting" });
  }

  TVH.params = TVH.params || new URLSearchParams(window.location.search);
  TVH.api = {
    mock: false, base: "", lite: true,
    home: function () { return home().then(null, function (e) { console.warn("lite home: " + e.message); throw e; }); },
    pc: pcState, play: play, jfOpen: jfOpen, img: img
  };

  TVLite.config = function () { return config; };
  TVLite.saveConfig = function (c) {
    config = c; writeJSON(CONFIG_KEY, c); applyBridge();
    state = {}; cache.last = {}; saveCache();   // settings changed: nothing old is shown
  };
  TVLite.hasConfig = function () { return !!readJSON(CONFIG_KEY, null); };
  TVLite.call = call;
  TVLite.onTV = onTV;
  TVLite.features = features;
  TVLite.installed = function () { return appsReady.then(function () { return installed; }); };
  TVLite.devmode = function () { return devmodeReady.then(function () { return devmode; }); };
  TVLite.checkDevmode = function (renew) { devmodeReady = checkDevmode(renew); return devmodeReady; };
  TVLite.channelIds = channelIds;
  TVLite.forget = forget;
  TVLite.DEFAULT_APPS = DEFAULT_APPS;
  TVLite.defaults = defaults;
})();
