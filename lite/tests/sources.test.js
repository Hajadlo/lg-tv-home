// Tests for lite/js/sources.js, the on-TV data layer (run: node --test lite/tests/*.test.js).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../js/sources.js');

/* A fake `http`: routes is a list of [test(req) -> bool, answer | (req) -> answer]. */
function fakeHttp(routes) {
  const calls = [];
  const http = (req) => {
    calls.push(req);
    for (const [match, answer] of routes) {
      if (match(req)) {
        const a = typeof answer === 'function' ? answer(req) : answer;
        return a instanceof Error ? Promise.reject(a) : Promise.resolve({ headers: {}, ...a });
      }
    }
    return Promise.reject(new Error('unexpected request ' + req.url));
  };
  http.calls = calls;
  return http;
}
const has = (s) => (req) => req.url.includes(s);
const body = (obj) => ({ status: 200, body: JSON.stringify(obj) });

test('weather: Open-Meteo answer -> the contract, with the rain note', async () => {
  const hours = Array.from({ length: 24 }, (_, i) => `2026-10-20T${String(i).padStart(2, '0')}:00`);
  const codes = hours.map((_, i) => (i >= 17 && i < 21 ? 61 : 3));
  const forecast = {
    current: { time: '2026-10-20T14:45', temperature_2m: 11.4, weather_code: 3 },
    daily: { temperature_2m_max: [14.2], temperature_2m_min: [6.6] },
    hourly: { time: hours, weather_code: codes, precipitation: codes.map((c) => (c === 61 ? 0.5 : 0)), precipitation_probability: codes.map(() => 10) },
  };
  const http = fakeHttp([[has('api.open-meteo.com/v1/forecast'), body(forecast)]]);
  const w = await S.fetchWeather(http, { latitude: 49.2, longitude: 16.6 });
  assert.deepEqual(w, { tempC: 11, code: 'cloudy', label: 'Overcast', highC: 14, lowC: 7, note: 'Rain from 17:00' });
  assert.match(http.calls[0].url, /latitude=49\.2&longitude=16\.6&timezone=auto/);
  // raining now, stops at 21:00
  forecast.current.time = '2026-10-20T18:10';
  assert.equal(S.summarizeWeather(forecast).note, 'Rain until 21:00');
  assert.equal(S.mapCode(1234)[0], 'cloudy');
});

test('geocoding and the IP fallback chain', async () => {
  const http = fakeHttp([
    [has('geocoding-api.open-meteo.com'), body({ results: [{ name: 'Brno', country: 'Czechia', admin1: 'South Moravian', latitude: 49.19, longitude: 16.61 }] })],
    [has('get.geojs.io'), new Error('down')],
    [has('ipwho.is'), body({ success: true, city: 'Brno', country: 'Czechia', latitude: 49.19, longitude: 16.6 })],
  ]);
  const places = await S.geocode(http, 'Brno');
  assert.equal(places[0].name, 'Brno');
  assert.match(http.calls[0].url, /name=Brno&count=5/);
  const ip = await S.ipLocation(http);
  assert.equal(ip.name, 'Brno');
  assert.equal(ip.latitude, 49.19);
  const none = await S.ipLocation(fakeHttp([[() => true, new Error('offline')]]));
  assert.equal(none, null);
});

test('channel list parsing: handles, ids, URLs, junk', () => {
  assert.deepEqual(S.parseChannels('@veritasium, https://www.youtube.com/@mkbhd/videos  UCabcdefghijklmnopqrstuv;\n@veritasium  bare_name ??'),
    ['@veritasium', '@mkbhd', 'UCabcdefghijklmnopqrstuv', '@bare_name']);
  assert.deepEqual(S.parseChannels(''), []);
});

test('resolveChannel: ids pass through, handles via the channel page with the consent cookie', async () => {
  const id = 'UC' + 'x'.repeat(22);
  const http = fakeHttp([
    [has('/@known'), { status: 200, match: ['"externalId":"' + id + '"', id] }],
    [has('/@gone'), { status: 404 }],
  ]);
  assert.equal(await S.resolveChannel(http, id), id);
  assert.equal(await S.resolveChannel(http, '@known'), id);
  assert.equal(await S.resolveChannel(http, '@gone'), null);
  assert.equal(http.calls[0].headers.Cookie, 'SOCS=CAI');
  assert.ok(Array.isArray(http.calls[0].match));
});

const CH = 'UC' + 'a'.repeat(22);
function feed(entries) {
  return `<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Example &amp; Co</title>
 ${entries.map((e) => `<entry>
  <id>yt:video:${e.id}</id>
  <yt:videoId>${e.id}</yt:videoId>
  <yt:channelId>${CH}</yt:channelId>
  <title>${e.title}</title>
  <link rel="alternate" href="https://www.youtube.com/${e.short ? 'shorts/' : 'watch?v='}${e.id}"/>
  <author><name>Example &amp; Co</name></author>
  <published>${e.published}</published>
  <media:group><media:community><media:statistics views="${e.views || 0}"/></media:community></media:group>
 </entry>`).join('\n')}
</feed>`;
}

test('parseFeed reads entries, entities and Shorts links', () => {
  const f = S.parseFeed(feed([{ id: 'aaaaaaaaaaa', title: 'One &quot;quoted&quot; &#39;x&#39;', published: '2026-10-19T10:00:00+00:00', views: 42 },
                              { id: 'bbbbbbbbbbb', title: 'Short', published: '2026-10-19T11:00:00+00:00', short: true }]));
  assert.equal(f.title, 'Example & Co');
  assert.equal(f.entries.length, 2);
  assert.deepEqual(f.entries[0], { id: 'aaaaaaaaaaa', title: 'One "quoted" \'x\'', channel: 'Example & Co', channelId: CH,
    published: '2026-10-19T10:00:00+00:00', views: 42, short: null });
  assert.equal(f.entries[1].short, true);
});

test('fetchYouTube: newest first, Shorts dropped (feed link and HEAD check), cache reused', async () => {
  const xml = feed([
    { id: 'aaaaaaaaaaa', title: 'Old', published: '2026-10-10T10:00:00+00:00' },
    { id: 'bbbbbbbbbbb', title: 'New', published: '2026-10-19T10:00:00+00:00' },
    { id: 'ccccccccccc', title: 'Linked short', published: '2026-10-18T10:00:00+00:00', short: true },
    { id: 'ddddddddddd', title: 'Hidden short', published: '2026-10-17T10:00:00+00:00' },
  ]);
  const http = fakeHttp([
    [has('feeds/videos.xml'), (req) => (req.headers['If-None-Match'] ? { status: 304 } : { status: 200, body: xml, headers: { etag: 'e1' } })],
    [has('/shorts/ddddddddddd'), { status: 200 }],
    [has('/shorts/'), { status: 303, headers: { location: 'https://www.youtube.com/watch?v=x' } }],
  ]);
  const cache = {};
  const yt = await S.fetchYouTube(http, [CH, 'not-an-id'], cache);
  assert.deepEqual(yt.videos.map((v) => v.id), ['bbbbbbbbbbb', 'aaaaaaaaaaa']);
  assert.equal(yt.channelCount, 1);
  assert.equal(yt.auth, 'ok');
  assert.match(yt.videos[0].thumb, /i\.ytimg\.com\/vi\/bbbbbbbbbbb\/maxresdefault\.jpg$/);
  assert.equal(cache.shorts.ddddddddddd, true);
  assert.equal(cache.shorts.bbbbbbbbbbb, false);
  const heads = http.calls.filter((c) => c.method === 'HEAD').length;
  assert.equal(heads, 3);
  // second round: 304 from the feed, no new Shorts checks
  const again = await S.fetchYouTube(http, [CH], cache);
  assert.deepEqual(again.videos.map((v) => v.id), ['bbbbbbbbbbb', 'aaaaaaaaaaa']);
  assert.equal(http.calls.filter((c) => c.method === 'HEAD').length, 3);
  // every feed failing is an error; one failing with a cached copy is not
  await assert.rejects(S.fetchYouTube(fakeHttp([[() => true, { status: 500 }]]), [CH], {}), /all 1 channel feeds failed/);
});

const creds = { url: 'http://jf.invalid:8096', userId: 'u1', token: 'tok' };
test('Jellyfin sign-in keeps only the token', async () => {
  const http = fakeHttp([[has('/Users/AuthenticateByName'), body({ AccessToken: 'tok', User: { Id: 'u1', Name: 'Ann' } })]]);
  const c = await S.jfSignIn(http, 'jf.invalid:8096/', 'Ann', '', 'dev1');
  assert.deepEqual(c, { url: 'http://jf.invalid:8096', userId: 'u1', userName: 'Ann', token: 'tok' });
  assert.match(http.calls[0].headers.Authorization, /DeviceId="dev1"/);
  assert.deepEqual(JSON.parse(http.calls[0].body), { Username: 'Ann', Pw: '' });
  await assert.rejects(S.jfSignIn(fakeHttp([[() => true, { status: 401 }]]), 'http://x', 'a', 'b'), /did not accept/);
});

test('Jellyfin items map like the server, images carry the token and a width slot', () => {
  const ep = S.jfMapItem(creds, { Id: 'e1', Type: 'Episode', SeriesName: 'Show', Name: 'Pilot', ParentIndexNumber: 1, IndexNumber: 2,
    SeriesId: 's1', SeriesPrimaryImageTag: 'sp', ImageTags: { Primary: 'p1' }, ParentBackdropItemId: 's1', ParentBackdropImageTags: ['b1'],
    UserData: { PlaybackPositionTicks: 600000000 * 10 }, RunTimeTicks: 600000000 * 40, ProductionYear: 2024 });
  assert.equal(ep.title, 'Show');
  assert.equal(ep.subtitle, 'S1 · E2 · Pilot');
  assert.equal(ep.progress, 0.25);
  assert.equal(ep.minutesLeft, 30);
  assert.equal(ep.thumb, 'http://jf.invalid:8096/Items/e1/Images/Primary?tag=p1&quality=90&maxWidth={w}&api_key=tok');
  assert.match(ep.backdrop, /Items\/s1\/Images\/Backdrop\?tag=b1/);
  assert.match(ep.poster, /Items\/s1\/Images\/Primary\?tag=sp/);
  const season = S.jfMapItem(creds, { Id: 'x', Type: 'Season', SeriesName: 'The Good Place', Name: 'The Good Place (2016) The Selection' });
  assert.equal(season.subtitle, 'The Selection');
  assert.equal(S.jfMapItem(creds, { Id: 'a', Type: 'Audio' }), null);
});

test('Jellyfin rows: one card per title, falls back to the old user routes', async () => {
  const ep = (id, series) => ({ Id: id, Type: 'Episode', SeriesId: series, SeriesName: series, Name: id });
  const http = fakeHttp([
    [has('/UserItems/Resume'), { status: 404 }],
    [has('/Users/u1/Items/Resume'), body({ Items: [ep('e1', 'A')] })],
    [has('/Shows/NextUp'), body({ Items: [ep('e2', 'A'), ep('e3', 'B')] })],
    [has('/UserViews'), body({ Items: [{ Id: 'v1', CollectionType: 'tvshows' }, { Id: 'v2', CollectionType: 'music' }] })],
    [has('/Items/Latest'), body([{ Id: 'B', Type: 'Series', Name: 'B', DateCreated: '2026-01-01' }, { Id: 'C', Type: 'Series', Name: 'C', DateCreated: '2026-02-01' }])],
  ]);
  const rows = await S.jfRows(http, creds, 'dev1');
  assert.deepEqual(rows.resume.map((i) => i.id), ['e1']);
  assert.deepEqual(rows.nextUp.map((i) => i.id), ['e3']);
  assert.deepEqual(rows.latest.map((i) => i.id), ['C']);
  assert.equal(http.calls.filter((c) => c.url.includes('/Items/Latest')).length, 1);  // music library skipped
  const expired = fakeHttp([[() => true, { status: 401 }]]);
  await assert.rejects(S.jfRows(expired, creds), (e) => e.code === 'auth');
});

test('Steam: owned games by last played, tools dropped, CDN art; recently played as fallback', async () => {
  const http = fakeHttp([
    [has('GetOwnedGames'), body({ response: { games: [
      { appid: 10, name: 'Old', rtime_last_played: 1700000000, playtime_forever: 90 },
      { appid: 1493710, name: 'Proton Experimental', rtime_last_played: 1800000000 },
      { appid: 20, name: 'New', rtime_last_played: 1790000000, playtime_forever: 600 },
      { appid: 30, name: 'Never', rtime_last_played: 0 },
    ] } })],
  ]);
  const st = await S.fetchSteam(http, 'KEY', '76561190000000000');
  assert.deepEqual(st.recent.map((g) => g.appid), [20, 10]);
  assert.equal(st.recent[0].playtimeH, 10);
  assert.equal(st.recent[0].lastPlayed, new Date(1790000000 * 1000).toISOString());
  assert.equal(st.recent[0].cover, 'https://shared.steamstatic.com/store_item_assets/steam/apps/20/library_600x900_2x.jpg');
  assert.match(http.calls[0].url, /key=KEY&steamid=76561190000000000/);

  const fallback = fakeHttp([
    [has('GetOwnedGames'), body({ response: {} })],
    [has('GetRecentlyPlayedGames'), body({ response: { games: [{ appid: 40, name: 'Recent', playtime_forever: 30 }] } })],
  ]);
  assert.deepEqual((await S.fetchSteam(fallback, 'K', '1')).recent.map((g) => g.name), ['Recent']);
  await assert.rejects(S.fetchSteam(fakeHttp([[() => true, { status: 403 }]]), 'bad', '1'), /refused the Web API key/);
});

test('pool keeps order and limits concurrency', async () => {
  let running = 0, peak = 0;
  const out = await S.pool([1, 2, 3, 4, 5], 2, (n) => {
    running++; peak = Math.max(peak, running);
    return new Promise((r) => setTimeout(() => { running--; r(n === 3 ? Promise.reject(new Error('x')) : n * 10); }, 5));
  });
  assert.equal(peak, 2);
  assert.deepEqual(out, [10, 20, { error: 'x' }, 40, 50]);
});
