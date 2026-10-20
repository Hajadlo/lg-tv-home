/* API access. Real backend at window.TVHOME_API_BASE (default same origin);
   with ?mock=1 the page reads mock/home.json and simulates the PC flow.

   Query parameters (mock and development only):
     mock=1          use mock/home.json instead of the API
     api=<origin>    override the API base (e.g. http://127.0.0.1:8797)
     img=<origin>    rewrite the Jellyfin origin in mock image URLs
     pc=off|on|waking|fail   initial PC state in the mock (fail: never ready)
     pcDelay=<ms>    how long the mock desktop takes to wake (default 7000)
     down=1          every mock call fails, like a dead backend
     nojf=1          mock Jellyfin is unreachable (jellyfin: null)
     noyt=1          mock YouTube source is down (youtube: null)
     ytauth=missing|expired   mock YouTube account never connected / sign-in expired
     nosteam=1       mock Steam has no games yet (steam: null)
     off=a,b         sources not configured on the server (features), e.g. off=jellyfin,steam
     noweather=1     mock weather is unavailable (weather: null)
     disk=<gb>       mock free disk space
     idleAfter=<ms>  idle timeout (default 180000)
     pcTimeout=<ms>  how long to wait for the desktop (default 90000) */
window.TVH = window.TVH || {};
(function () {
  "use strict";
  var params = new URLSearchParams(location.search);
  var BASE = params.get("api") || window.TVHOME_API_BASE || "";
  var MOCK = params.get("mock") === "1";
  var MOCK_ORIGIN = "http://jellyfin.invalid:8096";

  function getJSON(url, opts) {
    var o = Object.assign({ cache: "no-store" }, opts || {});
    return fetch(url, o).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " for " + url);
      return r.json();
    });
  }

  function makeMock() {
    var down = params.get("down") === "1";
    var failMode = params.get("pc") === "fail";
    var pcState = failMode ? "off" : (params.get("pc") || "off");
    var wakeMs = +(params.get("pcDelay") || 7000);
    var readyAt = pcState === "waking" ? Date.now() + wakeMs : 0;
    var imgBase = params.get("img");

    function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function gate(ms) {
      return delay(ms).then(function () { if (down) throw new Error("mock: backend down"); });
    }
    function snapshot() {
      if (pcState === "waking" && Date.now() >= readyAt) pcState = "on";
      var on = pcState === "on";
      return {
        state: pcState,
        sunshine: on ? "listening" : (pcState === "waking" ? "unknown" : "down"),
        ready: on,
        phase: on ? "idle" : pcState,
        error: null
      };
    }
    function fixImages(d) {
      if (!imgBase) return d;
      return JSON.parse(JSON.stringify(d).split(MOCK_ORIGIN).join(imgBase));
    }
    return {
      home: function () {
        return gate(150).then(function () { return getJSON("mock/home.json"); }).then(function (d) {
          d = fixImages(d);
          d.now = new Date().toISOString();
          d.pc = snapshot();
          if (params.get("nojf") === "1") d.jellyfin = null;
          if (params.get("noyt") === "1") d.youtube = null;
          if (params.get("ytauth") === "missing") d.youtube = { updated: null, channelCount: 0, auth: "missing", videos: [] };
          if (params.get("ytauth") === "expired" && d.youtube) d.youtube.auth = "expired";
          if (params.get("nosteam") === "1") d.steam = null;
          if (params.get("noweather") === "1") d.weather = null;
          if (params.get("disk")) d.system.hddFreeGb = +params.get("disk");
          (params.get("off") || "").split(",").forEach(function (k) {
            if (k && d.features && k in d.features) { d.features[k] = false; d[k] = null; if (k === "steam") d.pc = null; }
          });
          return d;
        });
      },
      pc: function () { return gate(120).then(snapshot); },
      play: function () {
        return gate(250).then(function () {
          if (pcState !== "on") {
            pcState = "waking";
            readyAt = failMode ? Infinity : Date.now() + wakeMs;
          }
          return snapshot();
        });
      }
    };
  }

  var mock = MOCK ? makeMock() : null;

  TVH.params = params;
  TVH.api = {
    mock: MOCK,
    base: BASE,
    home: function () { return mock ? mock.home() : getJSON(BASE + "/api/home"); },
    pc: function () { return mock ? mock.pc() : getJSON(BASE + "/api/pc"); },
    play: function () { return mock ? mock.play() : getJSON(BASE + "/api/pc/play", { method: "POST" }); },
    /* Ask the server to play (or show) a Jellyfin item once the TV's Jellyfin app connects. */
    jfOpen: function (id) {
      return mock ? Promise.resolve({ itemId: id, state: "waiting" })
                  : getJSON(BASE + "/api/jellyfin/open/" + encodeURIComponent(id), { method: "POST" });
    },
    /* Image URL with a width hint. Backend URLs are relative and accept ?w=;
       absolute URLs (mock, pointing at Jellyfin) carry their own size. */
    img: function (url, w) {
      if (!url) return null;
      if (url.charAt(0) === "/") return BASE + url + (url.indexOf("?") < 0 ? "?" : "&") + "w=" + w;
      return url;
    }
  };
})();
