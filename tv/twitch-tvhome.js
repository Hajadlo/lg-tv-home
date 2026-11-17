/* tv-home: begin */
/* TV Home's additions to Twitch AdFree, put in front of its userScript by
   tools/tv-home/tv/twitch-adfree-patch. Runs in lg.tv.twitch.tv pages. */
(function () {
  'use strict';
  if (window.__tvHomeTwitch || location.hostname !== 'lg.tv.twitch.tv') { return; }
  window.__tvHomeTwitch = true;

  // Twitch AdFree's own chat overlay starts off; its settings (green button)
  // can turn it on again. Set before its config is read, further down.
  try {
    var cfg = JSON.parse(localStorage.getItem('taf-configuration') || '{}') || {};
    if (cfg.enableChatOverlay === undefined) {
      cfg.enableChatOverlay = false;
      localStorage.setItem('taf-configuration', JSON.stringify(cfg));
    }
  } catch (err) { /* unreadable config: leave it to Twitch AdFree */ }

  // Twitch's chat column beside the player: hidden and the player full width,
  // on the player page only. The yellow button shows it again (and hides it).
  var css = 'main:has(> section video)';
  var style = document.createElement('style');
  style.textContent =
    'html:not(.tvh-chat) ' + css + ' > aside { display: none !important; }\n' +
    'html:not(.tvh-chat) ' + css + ' > section, html:not(.tvh-chat) ' + css + ' > section > section,\n' +
    'html:not(.tvh-chat) ' + css + ' > section video { width: 100% !important; max-width: none !important; }';
  (document.head || document.documentElement).appendChild(style);
  window.addEventListener('keydown', function (e) {
    if (e.keyCode === 405 || e.keyCode === 170) { document.documentElement.classList.toggle('tvh-chat'); }
  }, true);

  // A channel from TV Home while Twitch runs: webOS only fires webOSRelaunch
  // (Twitch AdFree reads its launch params once, at start).
  document.addEventListener('webOSRelaunch', function (e) {
    var ch = e.detail && e.detail.channel;
    if (typeof ch !== 'string' || !/^[0-9A-Za-z_]{1,25}$/.test(ch)) { return; }
    if (location.pathname.toLowerCase() !== '/' + ch.toLowerCase()) { location.href = 'https://lg.tv.twitch.tv/' + ch; }
  }, true);

  // Always the best quality. The player starts in auto mode, which sometimes
  // sat at 360p with 1080p60 on offer. Once the player had only 160p and 360p
  // (on a cold start; a reload got all five), so a list that stays at 360p or
  // less reloads the page, at most twice per channel and tab.
  function player() {
    var v = document.querySelector('video');
    var key = v && Object.keys(v).filter(function (k) { return k.indexOf('__reactFiber') === 0; })[0];
    for (var f = key && v[key], i = 0; f && i < 60; f = f.return, i++) {
      var p = f.memoizedProps && f.memoizedProps.playerController;
      if (p && typeof p.getQualities === 'function' && typeof p.setQuality === 'function') { return p; }
    }
    return null;
  }
  function better(a, b) {
    return b.height > a.height || (b.height === a.height && (b.framerate || 0) > (a.framerate || 0));
  }
  var lowSince = 0;
  setInterval(function () {
    var p = player();
    var qs = p ? (p.getQualities() || []).filter(function (q) { return q && q.height; }) : [];
    if (!qs.length) { lowSince = 0; return; }
    var best = qs.reduce(function (a, b) { return better(a, b) ? b : a; });
    var cur = p.getQuality();
    if (!cur || better(cur, best)) { p.setQuality(best); }
    if (best.height > 360) { lowSince = 0; return; }
    if (!lowSince) { lowSince = Date.now(); return; }
    if (Date.now() - lowSince < 15000) { return; }
    var mark = 'tvh-reloads:' + location.pathname;
    var n = +(sessionStorage.getItem(mark) || 0);
    if (n < 2) { sessionStorage.setItem(mark, String(n + 1)); location.reload(); }
  }, 3000);
}());
/* tv-home: end */
