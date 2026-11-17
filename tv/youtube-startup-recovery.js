/* TV Home: recover a video target missed while YouTube AdFree is preloaded.
   Run once per page load, after the app has had time to handle the target. */
(function () {
  'use strict';
  setTimeout(function () {
    try {
      var p = window.launchParams;
      if (typeof p === 'string') p = JSON.parse(p);
      if (!p || !p.firstLaunchSincePreload || typeof p.target !== 'string') return;
      var match = /^v=([A-Za-z0-9_-]{11})$/.exec(p.target);
      if (!match) return;
      var id = match[1], href = window.location.href;
      if (new RegExp('[?&]v=' + id + '(?:[&#]|$)').test(href)) return;
      // A failed redirect must not cause an endless reload.
      var key = 'tvhome-youtube-startup-recovery';
      var last = JSON.parse(window.sessionStorage.getItem(key) || 'null');
      if (last && last.id === id && Date.now() - last.at < 30000) return;
      window.sessionStorage.setItem(key, JSON.stringify({ id: id, at: Date.now() }));
      window.location.href = 'https://www.youtube.com/tv?env_forceFullAnimation=1&env_enableWebSpeech=1&env_enableVoice=1&v=' + id + '#/';
    } catch (err) { /* Leave YouTube usable if its launch state is unavailable. */ }
  }, 5000);
})();
/* tv-home youtube recovery: end */
