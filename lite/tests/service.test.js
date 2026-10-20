// Tests for lite/service/lib.js (run: node --test lite/tests).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const lib = require('../service/lib.js');

test('magic packet: 6 x 0xff then the MAC 16 times', () => {
  const p = lib.magicPacket('02:00:5e:10:20:30');
  assert.equal(p.length, 102);
  assert.deepEqual([...p.subarray(0, 6)], [255, 255, 255, 255, 255, 255]);
  for (let i = 0; i < 16; i++) assert.deepEqual([...p.subarray(6 + i * 6, 12 + i * 6)], [2, 0, 0x5e, 0x10, 0x20, 0x30]);
  assert.deepEqual(lib.magicPacket('02-00-5E-10-20-30'), p);
  assert.deepEqual(lib.magicPacket('02005e102030'), p);
  assert.throws(() => lib.magicPacket('02:00:5e:10:20'), /bad MAC/);
});

test('WoL targets: configured broadcasts first, global one last, no junk', () => {
  assert.deepEqual(lib.wolTargets(['192.0.2.255', ' 192.0.2.255', 'nope', '']), ['192.0.2.255', '255.255.255.255']);
  assert.deepEqual(lib.wolTargets(), ['255.255.255.255']);
});

test('Dev Mode token and session answers', () => {
  assert.equal(lib.parseToken('\n0123456789ABCDEFabcdef0123\n'), '0123456789ABCDEFabcdef0123');
  assert.equal(lib.parseToken('short\n'), null);
  assert.deepEqual(lib.parseSession('{"result":"success","errorCode":"200","errorMsg":"999:30:00"}'),
    { ok: true, hours: 999.5, error: null });
  const bad = lib.parseSession('{"result":"fail","errorCode":"ERR_005","errorMsg":"Check user session"}');
  assert.equal(bad.ok, false);
  assert.equal(bad.error, 'ERR_005');
  assert.equal(lib.parseSession('<html>').ok, false);
  assert.match(lib.devmodeUrl('CheckDevModeSession', 'abc'), /^https:\/\/developer\.lge\.com\/secure\/CheckDevModeSession\.dev\?sessionToken=abc$/);
});

test('pickSession: the newest remote-controllable Jellyfin Web session on an LG', () => {
  const s = lib.pickSession([
    { Id: 'phone', Client: 'Jellyfin Web', DeviceName: 'Firefox', SupportsRemoteControl: true, LastActivityDate: '2026-10-20T10:00' },
    { Id: 'old', Client: 'Jellyfin Web', DeviceName: 'LG Smart TV', SupportsRemoteControl: true, LastActivityDate: '2026-10-20T09:00' },
    { Id: 'tv', Client: 'Jellyfin Web', DeviceName: 'webOS TV', SupportsRemoteControl: true, LastActivityDate: '2026-10-20T11:00' },
    { Id: 'us', Client: 'TV Home', DeviceName: 'LG TV', SupportsRemoteControl: false, LastActivityDate: '2026-10-20T12:00' },
  ]);
  assert.equal(s.Id, 'tv');
  assert.equal(lib.pickSession([]), null);
});

// A fake clock and server for jfPlay.
function harness(item, sessionsSeq) {
  let t = 0, i = 0;
  const posts = [];
  const timers = [];
  const deps = {
    now: () => t,
    setTimeout: (fn, ms) => timers.push([t + ms, fn]),
    request(method, url, headers, body, cb) {
      assert.match(headers.Authorization, /Token="tok"/);
      if (url.includes('/Items/')) return cb(null, 200, JSON.stringify(item));
      if (url.includes('/Sessions?')) {
        const list = sessionsSeq[Math.min(i++, sessionsSeq.length - 1)];
        return cb(null, 200, JSON.stringify(list));
      }
      posts.push({ url, body });
      cb(null, 204, '');
    },
  };
  function runAll() {
    while (timers.length) {
      timers.sort((a, b) => a[0] - b[0]);
      const [at, fn] = timers.shift();
      t = at;
      fn();
    }
  }
  return { deps, posts, runAll };
}
const TV = (playing) => [{ Id: 's1', Client: 'Jellyfin Web', DeviceName: 'LG webOS', SupportsRemoteControl: true,
  NowPlayingItem: playing ? { Id: playing } : null }];

test('jfPlay waits for the TV session, sends PlayNow from the saved position, confirms', () => {
  const h = harness({ Id: 'ep1', Type: 'Episode', UserData: { PlaybackPositionTicks: 1200 } }, [[], [], TV(null), TV(null), TV('ep1')]);
  let result;
  lib.jfPlay({ url: 'http://jf.invalid/', token: 'tok', userId: 'u1', itemId: 'ep1', pollMs: 1000 }, h.deps, (r) => { result = r; });
  h.runAll();
  assert.deepEqual(result, { state: 'playing' });
  assert.equal(h.posts.length, 1);
  assert.match(h.posts[0].url, /^http:\/\/jf\.invalid\/Sessions\/s1\/Playing\?playCommand=PlayNow&itemIds=ep1&startPositionTicks=1200$/);
});

test('jfPlay shows a series page (DisplayContent twice) and gives up after the timeout', () => {
  const h = harness({ Id: 'ser', Type: 'Series', Name: 'Show' }, [TV(null)]);
  let result;
  lib.jfPlay({ url: 'http://jf.invalid', token: 'tok', userId: 'u1', itemId: 'ser' }, h.deps, (r) => { result = r; });
  h.runAll();
  assert.deepEqual(result, { state: 'shown' });
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[0].body.Name, 'DisplayContent');
  assert.equal(h.posts[0].body.Arguments.ItemId, 'ser');

  const none = harness({ Id: 'm', Type: 'Movie' }, [[]]);
  lib.jfPlay({ url: 'http://jf.invalid', token: 'tok', userId: 'u1', itemId: 'm', timeoutMs: 5000 }, none.deps, (r) => { result = r; });
  none.runAll();
  assert.equal(result.state, 'failed');
  assert.equal(none.posts.length, 0);
});
