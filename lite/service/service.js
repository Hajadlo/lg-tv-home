/*
 * TV Home lite: the small Node service packed inside the app
 * (luna://com.tvhome.lite.service/<method>). The page does all the logic;
 * this only does what a web page cannot:
 *
 *   http     any HTTP(S) request with free headers (cookies), no CORS, and
 *            redirects not followed unless asked; can return only a regex
 *            match instead of a large body
 *   wol      Wake-on-LAN magic packet (UDP broadcast)
 *   tcp      is host:port open (is the gaming PC's Sunshine up)
 *   devmode  read the Dev Mode session token on the TV, ask LG how long is
 *            left and, with renew:true, reset it to the maximum
 *   apps     which apps are installed (directory listing; the app manager
 *            refuses dev apps a list)
 *   jfPlay   Jellyfin remote control: wait for the TV's Jellyfin app, then
 *            play an item (runs here so it survives the page going to the
 *            background)
 *
 * ES5 and old Node APIs only: older webOS TVs ship old Node.js.
 */
'use strict';
var Service = require('webos-service');
var http = require('http');
var https = require('https');
var dgram = require('dgram');
var net = require('net');
var fs = require('fs');
var urlMod = require('url');
var lib = require('./lib.js');

var SERVICE_ID = 'com.tvhome.lite.service';
var service = new Service(SERVICE_ID);
var MAX_BYTES = 4 * 1024 * 1024;
var TOKEN_FILE = '/var/luna/preferences/devmode_enabled';
var APP_DIRS = ['/media/developer/apps/usr/palm/applications', '/media/cryptofs/apps/usr/palm/applications',
                '/usr/palm/applications'];

function fail(message, text, extra) {
  var out = { returnValue: false, errorText: String(text) };
  if (extra) { Object.keys(extra).forEach(function (k) { out[k] = extra[k]; }); }
  message.respond(out);
}

/* One request; cb(err, {status, headers, body, url}). */
function request(opts, cb) {
  var u = urlMod.parse(opts.url);
  var mod = u.protocol === 'https:' ? https : u.protocol === 'http:' ? http : null;
  if (!mod) { cb(new Error('unsupported URL: ' + opts.url)); return; }
  var headers = opts.headers || {};
  var body = null;
  if (opts.body !== undefined && opts.body !== null) {
    body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    if (!headers['Content-Type'] && !headers['content-type']) { headers['Content-Type'] = 'application/json'; }
  }
  if (!headers['User-Agent']) {
    headers['User-Agent'] = 'Mozilla/5.0 (Web0S; Linux) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0 Safari/537.36 TVHomeLite/0.1';
  }
  var done = false;
  function end(err, res) { if (!done) { done = true; cb(err, res); } }
  var req = mod.request({
    method: opts.method || 'GET', hostname: u.hostname, port: u.port, path: u.path, headers: headers
  }, function (res) {
    var chunks = [], size = 0, cap = opts.maxBytes || MAX_BYTES;
    res.on('data', function (c) {
      size += c.length;
      if (size <= cap) { chunks.push(c); } else { req.abort(); end(new Error('answer too large')); }
    });
    res.on('end', function () {
      end(null, { status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8'), url: opts.url });
    });
    res.on('error', function (e) { end(e); });
  });
  req.setTimeout(opts.timeout || 15000, function () { req.abort(); end(new Error('timed out')); });
  req.on('error', function (e) { end(e); });
  if (body !== null) { req.write(body); }
  req.end();
}

/* Follow up to `follow` redirects (relative locations resolved). */
function requestFollow(opts, follow, cb) {
  request(opts, function (err, res) {
    if (err || follow <= 0 || [301, 302, 303, 307, 308].indexOf(res.status) < 0 || !res.headers.location) {
      cb(err, res);
      return;
    }
    var next = {};
    Object.keys(opts).forEach(function (k) { next[k] = opts[k]; });
    next.url = urlMod.resolve(opts.url, res.headers.location);
    if (res.status === 303) { next.method = 'GET'; next.body = null; }
    requestFollow(next, follow - 1, cb);
  });
}

service.register('http', function (message) {
  var p = message.payload || {};
  if (!p.url) { fail(message, 'url missing'); return; }
  requestFollow(p, p.follow || 0, function (err, res) {
    if (err) { fail(message, err.message); return; }
    var out = { returnValue: true, status: res.status, url: res.url, headers: {
      location: res.headers.location || null, etag: res.headers.etag || null,
      'last-modified': res.headers['last-modified'] || null, 'content-type': res.headers['content-type'] || null } };
    if (p.match) {
      // One pattern or a list: the first that matches wins.
      out.match = null;
      [].concat(p.match).some(function (pattern, i) {
        var m = new RegExp(pattern).exec(res.body);
        if (m) { out.match = Array.prototype.slice.call(m); out.matchIndex = i; }
        return !!m;
      });
    } else if (p.method !== 'HEAD') {
      out.body = res.body;
    }
    message.respond(out);
  });
});

service.register('wol', function (message) {
  var p = message.payload || {};
  var packet;
  try { packet = lib.magicPacket(p.mac); } catch (e) { fail(message, e.message); return; }
  var targets = lib.wolTargets(p.broadcast ? [].concat(p.broadcast) : []);
  var ports = [9, 7];
  var sock = dgram.createSocket('udp4');
  var pending = 0, sent = [], errors = [];
  sock.on('error', function (e) { errors.push(e.message); });
  sock.bind(function () {
    sock.setBroadcast(true);
    targets.forEach(function (addr) {
      ports.forEach(function (port) {
        pending += 1;
        sock.send(packet, 0, packet.length, port, addr, function (err) {
          if (err) { errors.push(addr + ':' + port + ' ' + err.message); } else { sent.push(addr + ':' + port); }
          pending -= 1;
          if (pending === 0) {
            sock.close();
            message.respond({ returnValue: sent.length > 0, sent: sent, errors: errors,
                              errorText: sent.length ? undefined : errors.join('; ') });
          }
        });
      });
    });
  });
});

service.register('tcp', function (message) {
  var p = message.payload || {};
  if (!p.host || !p.port) { fail(message, 'host and port needed'); return; }
  var done = false;
  var sock = net.connect({ host: p.host, port: +p.port });
  function end(open) {
    if (done) { return; }
    done = true;
    sock.destroy();
    message.respond({ returnValue: true, open: open });
  }
  sock.setTimeout(p.timeout || 1500, function () { end(false); });
  sock.on('connect', function () { end(true); });
  sock.on('error', function () { end(false); });
});

service.register('devmode', function (message) {
  var p = message.payload || {};
  fs.readFile(TOKEN_FILE, 'utf8', function (err, text) {
    if (err) { fail(message, 'cannot read the Dev Mode token: ' + err.code, { stage: 'token' }); return; }
    var token = lib.parseToken(text);
    if (!token) { fail(message, 'no Dev Mode token in ' + TOKEN_FILE, { stage: 'token' }); return; }
    function call(method, cb) {
      request({ url: lib.devmodeUrl(method, token), timeout: 20000 }, function (e, res) {
        cb(e ? { ok: false, hours: null, error: e.message } : lib.parseSession(res.body));
      });
    }
    call('CheckDevModeSession', function (before) {
      if (!before.ok || !p.renew) {
        message.respond({ returnValue: before.ok, hours: before.hours, renewed: false,
                          errorText: before.ok ? undefined : 'LG: ' + before.error, stage: 'check' });
        return;
      }
      call('ResetDevModeSession', function (reset) {
        call('CheckDevModeSession', function (after) {
          message.respond({ returnValue: true, renewed: reset.ok && after.ok, hoursBefore: before.hours,
                            hours: after.ok ? after.hours : before.hours,
                            renewError: reset.ok ? undefined : 'LG: ' + reset.error });
        });
      });
    });
  });
});

service.register('apps', function (message) {
  var found = {}, readable = [];
  APP_DIRS.forEach(function (dir) {
    try {
      fs.readdirSync(dir).forEach(function (id) { found[id] = true; });
      readable.push(dir);
    } catch (e) { /* not visible from the jail */ }
  });
  message.respond({ returnValue: true, apps: Object.keys(found).sort(), dirs: readable });
});

service.register('jfPlay', function (message) {
  var p = message.payload || {};
  if (!p.url || !p.token || !p.userId || !p.itemId) { fail(message, 'url, token, userId and itemId needed'); return; }
  lib.jfPlay(p, {
    request: function (method, url, headers, body, cb) {
      request({ method: method, url: url, headers: headers, body: body, timeout: 8000 }, function (err, res) {
        cb(err, res && res.status, res && res.body);
      });
    }
  }, function (result) {
    result.returnValue = result.state !== 'failed';
    if (result.error) { result.errorText = result.error; }
    message.respond(result);
  });
});

service.register('ping', function (message) {
  var seen = {};
  APP_DIRS.concat([__dirname, '/media/developer/apps/usr/palm']).forEach(function (dir) {
    try { seen[dir] = fs.readdirSync(dir).length; } catch (e) { seen[dir] = e.code; }
  });
  message.respond({ returnValue: true, node: process.version, pid: process.pid, uid: process.getuid && process.getuid(), dirs: seen });
});
