/* Browser stand-in for the webOS bridge (tv/bridge.js). Loaded by index.html
   only when window.tvHome is absent. Every call resolves and logs what the TV
   would do; a small line at the bottom right shows it on screen. */
(function () {
  "use strict";
  var el = null, timer = null;
  function note(text) {
    console.info("[bridge-mock] " + text);
    if (!el) {
      el = document.createElement("div");
      el.style.cssText = "position:absolute;right:96px;bottom:56px;font:20px/1 'Hanken Grotesk',system-ui,sans-serif;" +
        "color:rgba(236,230,218,.45);opacity:0;transition:opacity .3s;pointer-events:none";
      document.body.appendChild(el);
    }
    el.textContent = "mock bridge: " + text;
    el.style.opacity = "1";
    clearTimeout(timer);
    timer = setTimeout(function () { el.style.opacity = "0"; }, 3000);
  }
  window.tvHome = {
    isTV: false,
    launch: function (app, opts) {
      var what = opts && opts.jellyfinItemId ? " item " + opts.jellyfinItemId
               : opts && opts.videoId ? " video " + opts.videoId : "";
      note("launch " + app + what);
      return Promise.resolve();
    },
    openSettings: function (target) {
      note("open settings " + (target || "all"));
      return Promise.resolve();
    },
    lgHome: function () {
      note("open LG home");
      return Promise.resolve();
    }
  };
})();
