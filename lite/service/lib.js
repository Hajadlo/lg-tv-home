/*
 * Pure helpers of the TV Home lite service: Wake-on-LAN packets, LG's Dev
 * Mode session answers, and Jellyfin's remote-control "play this" flow.
 *
 * Old-Node compatible on purpose (older webOS TVs ship old Node.js): ES5
 * syntax only, no Promise-only APIs, no Buffer.from/alloc without a fallback.
 * Loaded by service.js on the TV and by lite/tests in Node.
 */
'use strict';

var MAC_RE = /^([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})[:-]?([0-9a-f]{2})$/i;

function makeBuffer(size) {
  if (typeof Buffer.alloc === 'function') { return Buffer.alloc(size); }
  var b = new Buffer(size); // eslint-disable-line no-buffer-constructor
  b.fill(0);
  return b;
}

/* "aa:bb:cc:dd:ee:ff" (or with dashes, or none) -> 102-byte magic packet. */
function magicPacket(mac) {
  var m = MAC_RE.exec(String(mac || '').trim());
  if (!m) { throw new Error('bad MAC address: ' + mac); }
  var raw = [];
  for (var i = 1; i <= 6; i++) { raw.push(parseInt(m[i], 16)); }
  var buf = makeBuffer(6 + 16 * 6);
  for (var j = 0; j < 6; j++) { buf[j] = 0xff; }
  for (var k = 0; k < 16; k++) {
    for (var n = 0; n < 6; n++) { buf[6 + k * 6 + n] = raw[n]; }
  }
  return buf;
}

/* Where to send it: the configured broadcast addresses plus the global one. */
function wolTargets(broadcasts) {
  var out = [];
  (broadcasts || []).concat(['255.255.255.255']).forEach(function (a) {
    a = String(a || '').trim();
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(a) && out.indexOf(a) < 0) { out.push(a); }
  });
  return out;
}

/* The token line in /var/luna/preferences/devmode_enabled. */
function parseToken(text) {
  var lines = String(text || '').split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (/^[0-9A-Za-z]{16,}$/.test(line)) { return line; }
  }
  return null;
}

/* CheckDevModeSession/ResetDevModeSession JSON -> {ok, hours, error}.
   Success: {"result":"success","errorCode":"200","errorMsg":"999:58:40"}
   (the remaining time); failure e.g. {"result":"fail","errorCode":"ERR_005"}. */
function parseSession(body) {
  var data;
  try { data = JSON.parse(body); } catch (e) {
    return { ok: false, hours: null, error: 'unreadable answer from LG' };
  }
  var ok = data && data.result === 'success';
  var hours = null;
  var m = /^(\d+):(\d{2}):(\d{2})$/.exec(String(data && data.errorMsg || ''));
  if (ok && m) {
    hours = Math.round((+m[1] + +m[2] / 60 + +m[3] / 3600) * 100) / 100;
  }
  return { ok: !!ok, hours: hours, error: ok ? null : String((data && (data.errorCode || data.errorMsg)) || 'failed') };
}

var DEVMODE_API = 'https://developer.lge.com/secure/';
function devmodeUrl(method, token) {
  return DEVMODE_API + method + '.dev?sessionToken=' + encodeURIComponent(token);
}

/* ---------- Jellyfin: play an item on the TV through remote control ----------
   Port of server/jf_remote.py. The Jellyfin webOS app ignores launch params;
   its web client registers a remote-controllable session. We wait for that
   session, then send PlayNow (from the saved position) for playable items or
   DisplayContent for series and seasons. */
var PLAYABLE = { Episode: 1, Movie: 1, Video: 1, MusicVideo: 1 };
var TV_HINTS = ['lg', 'webos', 'smart tv'];

function pickSession(sessions) {
  var found = (sessions || []).filter(function (s) {
    var name = String(s.DeviceName || '').toLowerCase();
    return s.SupportsRemoteControl && s.Client === 'Jellyfin Web' &&
      TV_HINTS.some(function (h) { return name.indexOf(h) >= 0; });
  });
  found.sort(function (a, b) {
    var x = a.LastActivityDate || '', y = b.LastActivityDate || '';
    return x < y ? 1 : x > y ? -1 : 0;
  });
  return found[0] || null;
}

function query(params) {
  var parts = [];
  Object.keys(params).forEach(function (k) {
    if (params[k] !== null && params[k] !== undefined && params[k] !== '') {
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
    }
  });
  return parts.length ? '?' + parts.join('&') : '';
}

/*
 * opts: {url, token, userId, itemId, timeoutMs, pollMs, verifyMs}
 * deps: {request(method, url, headers, body, cb(err, status, text)), setTimeout, now}
 * done(result): {state: "playing"|"shown"|"failed", error?}
 */
function jfPlay(opts, deps, done) {
  var base = String(opts.url || '').replace(/\/+$/, '');
  var headers = { 'Authorization': 'MediaBrowser Token="' + opts.token + '"' };
  var timeoutMs = opts.timeoutMs || 30000, pollMs = opts.pollMs || 1000, verifyMs = opts.verifyMs || 6000;
  var later = deps.setTimeout || setTimeout;
  var now = deps.now || Date.now;
  var deadline = now() + timeoutMs;
  var item = null, playable = false, ticks = 0, sentDisplay = 0, finished = false;

  function finish(res) { if (!finished) { finished = true; done(res); } }
  function getJSON(path, params, cb) {
    deps.request('GET', base + path + query(params), headers, null, function (err, status, text) {
      if (err) { cb(err); return; }
      if (status < 200 || status >= 300) { cb(new Error('HTTP ' + status + ' for ' + path)); return; }
      var data;
      try { data = JSON.parse(text); } catch (e) { cb(new Error('bad JSON from ' + path)); return; }
      cb(null, data);
    });
  }
  function post(path, params, body, cb) {
    deps.request('POST', base + path + query(params), headers, body, function (err, status) {
      if (!err && (status < 200 || status >= 300)) { err = new Error('HTTP ' + status + ' for ' + path); }
      cb(err || null);
    });
  }
  function sessions(cb) {
    getJSON('/Sessions', { controllableByUserId: opts.userId, activeWithinSeconds: 900 }, function (err, list) {
      cb(err, err ? null : pickSession(list));
    });
  }
  function nowPlaying(s) { return s && s.NowPlayingItem && s.NowPlayingItem.Id; }

  function loop() {
    if (finished) { return; }
    if (now() >= deadline) { finish({ state: 'failed', error: "the TV's Jellyfin app did not take the command" }); return; }
    sessions(function (err, s) {
      if (err || !s) { later(loop, pollMs); return; }
      if (playable && nowPlaying(s) === opts.itemId) { finish({ state: 'playing' }); return; }
      if (playable) {
        post('/Sessions/' + s.Id + '/Playing', { playCommand: 'PlayNow', itemIds: opts.itemId,
          startPositionTicks: ticks || null }, null, function () { verify(0); });
        return;
      }
      post('/Sessions/' + s.Id + '/Command', {}, { Name: 'DisplayContent', Arguments: {
        ItemId: opts.itemId, ItemType: item.Type || '', ItemName: item.Name || '' } }, function () {
        sentDisplay += 1;
        if (sentDisplay >= 2) { finish({ state: 'shown' }); return; }
        later(loop, 3000);
      });
    });
  }
  function verify(waited) {
    if (finished) { return; }
    if (waited >= verifyMs) { loop(); return; }
    later(function () {
      sessions(function (err, s) {
        if (!err && nowPlaying(s) === opts.itemId) { finish({ state: 'playing' }); return; }
        verify(waited + pollMs);
      });
    }, pollMs);
  }

  getJSON('/Users/' + opts.userId + '/Items/' + opts.itemId, {}, function (err, it) {
    if (err) { finish({ state: 'failed', error: err.message }); return; }
    item = it;
    playable = !!PLAYABLE[it.Type];
    ticks = +((it.UserData || {}).PlaybackPositionTicks || 0);
    loop();
  });
}

module.exports = {
  magicPacket: magicPacket,
  wolTargets: wolTargets,
  parseToken: parseToken,
  parseSession: parseSession,
  devmodeUrl: devmodeUrl,
  pickSession: pickSession,
  jfPlay: jfPlay,
  query: query
};
