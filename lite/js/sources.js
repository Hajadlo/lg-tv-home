/* TV Home lite: the data sources, run on the TV itself.

   Each function turns public web answers into the pieces of the server's
   /api/home payload (see server/README.md), so ui/js/app.js renders them
   unchanged. Network access goes through an injected `http(req)` returning a
   Promise of {status, headers, body?, match?, url}; on the TV that is the
   app's own service (lite/service), in tests a fake.

   ES5 only (older TVs run Chromium 53): no arrow functions, no let/const,
   no template strings. Works in the page and in Node (module.exports). */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else { root.TVLite = root.TVLite || {}; root.TVLite.sources = factory(); }
}(this, function () {
  "use strict";

  /* ---------- small helpers ---------- */
  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== null && v !== undefined && v !== "") parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }
  function json(res, what) {
    if (!res || res.status < 200 || res.status >= 300) {
      throw new Error((what || "request") + ": HTTP " + (res ? res.status : "?"));
    }
    try { return typeof res.body === "string" ? JSON.parse(res.body) : res.body; } catch (e) {
      throw new Error((what || "request") + ": bad JSON");
    }
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  /* Run fn over items, at most `limit` at a time; resolves to results in order
     (a failed item gives {error}). */
  function pool(items, limit, fn) {
    return new Promise(function (resolve) {
      var out = new Array(items.length), next = 0, running = 0, done = 0;
      if (!items.length) { resolve(out); return; }
      function start() {
        while (running < limit && next < items.length) {
          (function (i) {
            running += 1;
            Promise.resolve().then(function () { return fn(items[i], i); }).then(function (v) { out[i] = v; },
              function (e) { out[i] = { error: e && e.message || String(e) }; }).then(function () {
              running -= 1; done += 1;
              if (done === items.length) resolve(out); else start();
            });
          })(next++);
        }
      }
      start();
    });
  }

  /* ---------- weather (Open-Meteo, no key) ---------- */
  var WMO = {
    0: ["clear", "Clear"], 1: ["partly", "Mainly clear"], 2: ["partly", "Partly cloudy"], 3: ["cloudy", "Overcast"],
    45: ["fog", "Fog"], 48: ["fog", "Freezing fog"],
    51: ["drizzle", "Light drizzle"], 53: ["drizzle", "Drizzle"], 55: ["drizzle", "Heavy drizzle"],
    56: ["drizzle", "Freezing drizzle"], 57: ["drizzle", "Freezing drizzle"],
    61: ["rain", "Light rain"], 63: ["rain", "Rain"], 65: ["rain", "Heavy rain"],
    66: ["rain", "Freezing rain"], 67: ["rain", "Freezing rain"],
    71: ["snow", "Light snow"], 73: ["snow", "Snow"], 75: ["snow", "Heavy snow"], 77: ["snow", "Snow grains"],
    80: ["rain", "Light showers"], 81: ["rain", "Showers"], 82: ["rain", "Heavy showers"],
    85: ["snow", "Snow showers"], 86: ["snow", "Heavy snow showers"],
    95: ["storm", "Thunderstorm"], 96: ["storm", "Thunderstorm with hail"], 99: ["storm", "Thunderstorm with hail"]
  };
  var WET = { drizzle: 1, rain: 1, snow: 1, storm: 1 };
  var NOUN = { drizzle: "Rain", rain: "Rain", snow: "Snow", storm: "Storms" };
  var NOTE_HOURS = 12;
  function mapCode(c) { return WMO[c] || ["cloudy", "Cloudy"]; }
  function hourIsWet(code, mm, prob) {
    return !!WET[mapCode(code)[0]] && ((mm || 0) >= 0.2 || (prob || 0) >= 50);
  }
  /* Port of server/weather.py rain_note: "Rain from 17:00", "Rain until 21:00", "Rain all day" or null.
     `hour` is the local hour "YYYY-MM-DDTHH:00" (Open-Meteo's times are local with timezone=auto). */
  function rainNote(hourly, hour) {
    var times = hourly.time || [], codes = hourly.weather_code || [];
    var mm = hourly.precipitation || [], prob = hourly.precipitation_probability || [];
    var start = times.indexOf(hour);
    if (start < 0) return null;
    var wet = [];
    for (var i = start; i < Math.min(start + NOTE_HOURS, times.length); i++) wet.push(hourIsWet(codes[i], mm[i], prob[i]));
    if (!wet.length) return null;
    function hhmm(j) { return times[j].slice(11, 16); }
    var k;
    if (wet[0]) {
      var noun = NOUN[mapCode(codes[start])[0]];
      for (k = 0; k < wet.length; k++) if (!wet[k]) return noun + " until " + hhmm(start + k);
      return wet.length >= NOTE_HOURS ? noun + " all day" : null;
    }
    for (k = 0; k < wet.length; k++) if (wet[k]) return NOUN[mapCode(codes[start + k])[0]] + " from " + hhmm(start + k);
    return null;
  }
  function summarizeWeather(f) {
    var cur = f.current, daily = f.daily || {};
    var m = mapCode(cur.weather_code);
    function first(key) {
      var v = daily[key] || [];
      return v.length && v[0] !== null && v[0] !== undefined ? Math.round(v[0]) : null;
    }
    return {
      tempC: Math.round(cur.temperature_2m), code: m[0], label: m[1],
      highC: first("temperature_2m_max"), lowC: first("temperature_2m_min"),
      note: rainNote(f.hourly || {}, String(cur.time || "").slice(0, 13) + ":00")
    };
  }
  function forecastUrl(lat, lon) {
    return "https://api.open-meteo.com/v1/forecast" + qs({
      latitude: lat, longitude: lon, timezone: "auto", forecast_days: 2,
      current: "temperature_2m,weather_code",
      hourly: "weather_code,precipitation,precipitation_probability",
      daily: "temperature_2m_max,temperature_2m_min"
    });
  }
  function fetchWeather(http, loc) {
    return http({ url: forecastUrl(loc.latitude, loc.longitude) }).then(function (res) {
      return summarizeWeather(json(res, "weather"));
    });
  }
  /* City name -> places, best first: [{name, country, admin1, latitude, longitude}]. */
  function geocode(http, name) {
    var url = "https://geocoding-api.open-meteo.com/v1/search" + qs({ name: name, count: 5, language: "en", format: "json" });
    return http({ url: url }).then(function (res) {
      return (json(res, "city search").results || []).map(function (r) {
        return { name: r.name, country: r.country || "", admin1: r.admin1 || "", latitude: r.latitude, longitude: r.longitude };
      });
    });
  }
  /* A rough location from the internet connection, keyless HTTPS services, first that answers. */
  var IP_SERVICES = [
    { url: "https://get.geojs.io/v1/ip/geo.json", pick: function (d) {
      return { name: d.city || d.region || "", country: d.country || "", latitude: +d.latitude, longitude: +d.longitude };
    } },
    { url: "https://ipwho.is/", pick: function (d) {
      return d.success === false ? null : { name: d.city || "", country: d.country || "", latitude: +d.latitude, longitude: +d.longitude };
    } }
  ];
  function ipLocation(http) {
    var i = 0;
    function attempt() {
      if (i >= IP_SERVICES.length) return Promise.resolve(null);
      var svc = IP_SERVICES[i++];
      return http({ url: svc.url, timeout: 8000 }).then(function (res) {
        var loc = svc.pick(json(res, "location"));
        if (!loc || isNaN(loc.latitude) || isNaN(loc.longitude)) throw new Error("no location");
        return loc;
      }).catch(attempt);
    }
    return attempt();
  }

  /* ---------- YouTube: public channel feeds ---------- */
  var CONSENT = { "Cookie": "SOCS=CAI" };   // without it EU requests land on consent.youtube.com
  var CHANNEL_ID = /^UC[0-9A-Za-z_-]{22}$/;
  var VIDEO_ID = /^[0-9A-Za-z_-]{11}$/;
  var VIDEO_LIMIT = 30, MAX_SHORTS_CHECKS = 40, PER_CHANNEL = 15;
  /* Live state from the watch page (first pattern found wins): "upcoming" (scheduled
     stream or premiere), "live" (on air), "stream" (finished stream) or "video".
     Finished and scheduled streams are left out; live ones get a LIVE badge. */
  var KIND_PATTERNS = ["\"isUpcoming\":true", "\"isLiveNow\":true", "\"isLiveContent\":true", "\"isLiveContent\":false"];
  var KIND_OF = { "\"isUpcoming\":true": "upcoming", "\"isLiveNow\":true": "live",
                  "\"isLiveContent\":true": "stream", "\"isLiveContent\":false": "video" };
  var FINAL_KINDS = { video: 1, stream: 1 }, HIDDEN_KINDS = { stream: 1, upcoming: 1 };
  var MAX_KIND_CHECKS = 40, KIND_RECHECK = 10 * 60 * 1000;

  /* "@a, @b UCxxxx https://www.youtube.com/@c" -> ["@a", "@b", "UCxxxx", "@c"] (unique). */
  function parseChannels(text) {
    var out = [];
    String(text || "").split(/[\s,;]+/).forEach(function (t) {
      if (!t) return;
      var m = /youtube\.com\/(?:channel\/(UC[0-9A-Za-z_-]{22})|(@[^/?#]+))/i.exec(t);
      if (m) t = m[1] || m[2];
      if (CHANNEL_ID.test(t)) { /* keep */ }
      else if (t.charAt(0) === "@" && t.length > 1) t = "@" + t.slice(1);
      else if (/^[0-9A-Za-z._-]{3,}$/.test(t)) t = "@" + t;
      else return;
      if (out.indexOf(t) < 0) out.push(t);
    });
    return out;
  }
  var CHANNEL_PATTERNS = [
    "\"externalId\":\"(UC[0-9A-Za-z_-]{22})\"",
    "<link rel=\"canonical\" href=\"https://www\\.youtube\\.com/channel/(UC[0-9A-Za-z_-]{22})\"",
    "\"channelId\":\"(UC[0-9A-Za-z_-]{22})\""
  ];
  /* "@handle" or "UC..." -> channel id (null when YouTube does not know it). */
  function resolveChannel(http, token) {
    if (CHANNEL_ID.test(token)) return Promise.resolve(token);
    var url = "https://www.youtube.com/" + encodeURIComponent(token).replace(/^%40/, "@");
    return http({ url: url, headers: CONSENT, match: CHANNEL_PATTERNS, follow: 3, timeout: 15000 }).then(function (res) {
      if (res.status === 404) return null;
      if (res.status !== 200) throw new Error("channel page: HTTP " + res.status);
      return res.match ? res.match[1] : null;
    });
  }

  var ENTITIES = { amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'" };
  function unxml(s) {
    return String(s || "").replace(/&(#x?[0-9a-f]+|amp|lt|gt|quot|apos);/gi, function (all, e) {
      if (e.charAt(0) === "#") {
        var code = e.charAt(1).toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return isNaN(code) ? all : String.fromCharCode(code);
      }
      return ENTITIES[e.toLowerCase()] || all;
    });
  }
  function tag(block, name) {
    var m = new RegExp("<" + name + "(?:\\s[^>]*)?>([\\s\\S]*?)</" + name + ">").exec(block);
    return m ? unxml(m[1].replace(/^<!\[CDATA\[|\]\]>$/g, "")).trim() : "";
  }
  /* A channel's Atom feed -> {title, entries: [{id, title, channel, channelId, published, views, short}]}. */
  function parseFeed(xml) {
    xml = String(xml || "");
    var head = xml.split("<entry")[0];
    var channel = tag(head, "title");
    var entries = [];
    var re = /<entry>([\s\S]*?)<\/entry>/g, m;
    while ((m = re.exec(xml))) {
      var b = m[1];
      var vid = tag(b, "yt:videoId");
      if (!VIDEO_ID.test(vid)) continue;
      var link = /<link rel="alternate" href="([^"]*)"/.exec(b);
      var views = /<media:statistics views="(\d+)"/.exec(b);
      var author = /<author>([\s\S]*?)<\/author>/.exec(b);
      entries.push({
        id: vid, title: tag(b, "title"),
        channel: (author && tag(author[1], "name")) || channel,
        channelId: tag(b, "yt:channelId"), published: tag(b, "published"),
        views: views ? +views[1] : null,
        short: link && link[1].indexOf("/shorts/") >= 0 ? true : null
      });
    }
    return { title: channel, entries: entries };
  }
  function feedUrl(cid) { return "https://www.youtube.com/feeds/videos.xml?channel_id=" + cid; }
  /* True for a Short, false for a normal video, null when unclear (HEAD /shorts/<id>, not followed). */
  function checkShort(http, vid) {
    return http({ url: "https://www.youtube.com/shorts/" + vid, method: "HEAD", headers: CONSENT, timeout: 10000 })
      .then(function (res) {
        if (res.status === 200) return true;
        var loc = (res.headers && res.headers.location) || "";
        if (res.status >= 300 && res.status < 400 && loc.indexOf("/watch") >= 0) return false;
        return null;
      }, function () { return null; });
  }
  /* The kind of a video (see KIND_PATTERNS), null when the watch page is unclear. */
  function checkKind(http, vid) {
    return http({ url: "https://www.youtube.com/watch?v=" + vid, headers: CONSENT, match: KIND_PATTERNS, timeout: 15000 })
      .then(function (res) {
        return res.status === 200 && res.match ? KIND_OF[res.match[0]] || null : null;
      }, function () { return null; });
  }
  function toVideo(e) {
    return { id: e.id, title: e.title, channel: e.channel, channelId: e.channelId, published: e.published,
             views: e.views, progress: null, live: !!e.live,
             thumb: "https://i.ytimg.com/vi/" + e.id + "/maxresdefault.jpg" };
  }
  /* channelIds -> the payload's youtube object. cache: {feeds: {cid: {etag, modified, entries, title}},
     shorts: {vid: bool}, kinds: {vid: "video" | "stream"}, passing: {vid: ["live" | "upcoming", checked ms]}}
     is updated in place (the caller stores it). */
  function fetchYouTube(http, channelIds, cache, opts) {
    opts = opts || {};
    cache.feeds = cache.feeds || {};
    cache.shorts = cache.shorts || {};
    cache.kinds = cache.kinds || {};
    cache.passing = cache.passing || {};
    var now = opts.now ? opts.now() : Date.now();
    var limit = opts.limit || VIDEO_LIMIT;
    var ids = channelIds.filter(function (c) { return CHANNEL_ID.test(c); });
    return pool(ids, 4, function (cid) {
      var old = cache.feeds[cid] || {};
      var headers = {};
      if (old.etag) headers["If-None-Match"] = old.etag;
      if (old.modified) headers["If-Modified-Since"] = old.modified;
      return http({ url: feedUrl(cid), headers: headers, timeout: 15000 }).then(function (res) {
        if (res.status === 304 && old.entries) return old.entries;
        if (res.status !== 200) throw new Error("feed HTTP " + res.status);
        var feed = parseFeed(res.body);
        cache.feeds[cid] = { etag: res.headers && res.headers.etag, modified: res.headers && res.headers["last-modified"],
                             entries: feed.entries.slice(0, PER_CHANNEL), title: feed.title };
        return cache.feeds[cid].entries;
      }).catch(function (err) {
        if (old.entries) return old.entries;
        throw err;
      });
    }).then(function (results) {
      var entries = [], failed = 0;
      results.forEach(function (r) { if (r && r.error) failed += 1; else entries = entries.concat(r || []); });
      Object.keys(cache.feeds).forEach(function (cid) { if (ids.indexOf(cid) < 0) delete cache.feeds[cid]; });
      if (ids.length && failed === ids.length) throw new Error("all " + failed + " channel feeds failed");
      var seen = {}, candidates = [];
      entries.sort(function (a, b) { return a.published < b.published ? 1 : a.published > b.published ? -1 : 0; });
      entries.forEach(function (e) {
        if (seen[e.id]) return;
        seen[e.id] = 1;
        if (e.short) { cache.shorts[e.id] = true; return; }
        candidates.push(e);
      });
      // Check unknown candidates for Shorts in batches until the list is full.
      var picked = [], checks = 0, kindChecks = 0, i = 0;
      function kindsOf(list) {
        var out = {}, todo = [];
        list.forEach(function (e) {
          var passing = cache.passing[e.id];
          if (cache.kinds[e.id]) out[e.id] = cache.kinds[e.id];
          else {
            out[e.id] = passing ? passing[0] : null;  // until checked
            if (!passing || now - passing[1] >= KIND_RECHECK) todo.push(e);
          }
        });
        todo = todo.slice(0, Math.max(0, MAX_KIND_CHECKS - kindChecks));
        kindChecks += todo.length;
        return pool(todo, 4, function (e) { return checkKind(http, e.id); }).then(function (answers) {
          todo.forEach(function (e, k) {
            var kind = answers[k];
            if (!kind || (answers[k] && answers[k].error)) return;
            out[e.id] = kind;
            if (FINAL_KINDS[kind]) { cache.kinds[e.id] = kind; delete cache.passing[e.id]; }
            else cache.passing[e.id] = [kind, now];
          });
          return out;
        });
      }
      function step() {
        if (i >= candidates.length || picked.length >= limit) return Promise.resolve();
        var batch = candidates.slice(i, i + (limit - picked.length));
        i += batch.length;
        var unknown = batch.filter(function (e) { return !(e.id in cache.shorts); }).slice(0, Math.max(0, MAX_SHORTS_CHECKS - checks));
        checks += unknown.length;
        return pool(unknown, 4, function (e) { return checkShort(http, e.id); }).then(function (answers) {
          unknown.forEach(function (e, k) { if (answers[k] === true || answers[k] === false) cache.shorts[e.id] = answers[k]; });
          var normal = batch.filter(function (e) { return !cache.shorts[e.id]; });
          return kindsOf(normal).then(function (kinds) {
            normal.forEach(function (e) {
              if (HIDDEN_KINDS[kinds[e.id]]) return;
              e.live = kinds[e.id] === "live";
              picked.push(e);
            });
            return step();
          });
        });
      }
      return step().then(function () {
        Object.keys(cache.shorts).forEach(function (vid) { if (!seen[vid]) delete cache.shorts[vid]; });
        Object.keys(cache.kinds).forEach(function (vid) { if (!seen[vid]) delete cache.kinds[vid]; });
        Object.keys(cache.passing).forEach(function (vid) { if (!seen[vid]) delete cache.passing[vid]; });
        return {
          updated: new Date().toISOString(), channelCount: ids.length, auth: "ok", watched: "unavailable",
          videos: picked.slice(0, limit).map(toVideo), "continue": [], failedChannels: failed
        };
      });
    });
  }

  /* ---------- Jellyfin ---------- */
  var JF_KINDS = { Episode: "episode", Movie: "movie", Series: "series", Season: "series", Video: "movie" };
  var JF_LATEST_VIEWS = { movies: 1, tvshows: 1, homevideos: 1 };
  var JF_ROW_LIMIT = 12, JF_FETCH_LIMIT = 24, TICKS_PER_MINUTE = 600000000;
  var JF_IMAGE_TYPES = { backdrop: "Backdrop", thumb: "Thumb", poster: "Primary", logo: "Logo", primary: "Primary" };

  function jfClientAuth(deviceId, token) {
    return "MediaBrowser Client=\"TV Home\", Device=\"LG TV\", DeviceId=\"" + (deviceId || "tvhome-lite") +
      "\", Version=\"0.1.1\"" + (token ? ", Token=\"" + token + "\"" : "");
  }
  function jfBase(url) {
    url = String(url || "").trim().replace(/\/+$/, "");
    if (url && !/^https?:\/\//i.test(url)) url = "http://" + url;
    return url;
  }
  /* Sign in once; the password is not kept, only the access token. */
  function jfSignIn(http, url, user, password, deviceId) {
    var base = jfBase(url);
    return http({ url: base + "/Users/AuthenticateByName", method: "POST",
                  headers: { "Authorization": jfClientAuth(deviceId), "Content-Type": "application/json" },
                  body: JSON.stringify({ Username: user, Pw: password || "" }), timeout: 10000 })
      .then(function (res) {
        if (res.status === 401) throw new Error("Jellyfin did not accept that user name and password");
        var d = json(res, "Jellyfin sign-in");
        return { url: base, userId: d.User.Id, userName: d.User.Name || user, token: d.AccessToken };
      });
  }
  function jfSignOut(http, creds, deviceId) {
    return http({ url: creds.url + "/Sessions/Logout", method: "POST",
                  headers: { "Authorization": jfClientAuth(deviceId, creds.token) }, timeout: 8000 });
  }
  function jfImage(creds, id, tagValue, kind) {
    if (!id || !tagValue) return null;
    return creds.url + "/Items/" + id + "/Images/" + JF_IMAGE_TYPES[kind] + "?tag=" + encodeURIComponent(tagValue) +
      "&quality=90&maxWidth={w}&api_key=" + encodeURIComponent(creds.token);
  }
  /* Jellyfin BaseItemDto -> contract Item (port of server/jellyfin.py map_item). */
  function jfMapItem(creds, raw) {
    var kind = JF_KINDS[raw.Type || ""];
    if (!kind) return null;
    var id = raw.Id, tags = raw.ImageTags || {}, user = raw.UserData || {};
    var episode = kind === "episode", season = raw.Type === "Season";
    var title, subtitle;
    if (season) {
      title = raw.SeriesName || raw.Name;
      var name = raw.Name || "";
      if (title && name.indexOf(title) === 0) name = name.slice(title.length).replace(/^\s*\(\d{4}\)/, "").replace(/^[\s\-·:]+|[\s\-·:]+$/g, "");
      subtitle = name || null;
    } else if (episode) {
      title = raw.SeriesName || raw.Name;
      var parts = [];
      if (raw.ParentIndexNumber !== null && raw.ParentIndexNumber !== undefined) parts.push("S" + raw.ParentIndexNumber);
      if (raw.IndexNumber !== null && raw.IndexNumber !== undefined) parts.push("E" + raw.IndexNumber);
      if (raw.Name) parts.push(raw.Name);
      subtitle = parts.join(" · ") || null;
    } else {
      title = raw.Name;
      var unplayed = user.UnplayedItemCount;
      subtitle = kind === "series" && unplayed ? unplayed + " unwatched episode" + (unplayed !== 1 ? "s" : "") : null;
    }
    var position = user.PlaybackPositionTicks || 0, runtime = raw.RunTimeTicks || 0;
    var progress = null, minutesLeft = null;
    if (position > 0 && runtime > 0) {
      progress = Math.round(Math.min(position / runtime, 1) * 1000) / 1000;
      minutesLeft = Math.max(Math.ceil((runtime - position) / TICKS_PER_MINUTE), 0);
    }
    function first(arr) { return arr && arr.length ? arr[0] : null; }
    var img = function (iid, t, k) { return jfImage(creds, iid, t, k); };
    var backdrop = img(id, first(raw.BackdropImageTags), "backdrop") ||
      img(raw.ParentBackdropItemId, first(raw.ParentBackdropImageTags), "backdrop");
    var logo = img(id, tags.Logo, "logo") || img(raw.ParentLogoItemId, raw.ParentLogoImageTag, "logo");
    var thumb, poster;
    if (episode || season) {
      var still = episode ? img(id, tags.Primary, "primary") : null;
      thumb = still || img(raw.ParentThumbItemId, raw.ParentThumbImageTag, "thumb") || backdrop;
      poster = (season ? img(id, tags.Primary, "poster") : null) || img(raw.SeriesId, raw.SeriesPrimaryImageTag, "poster");
    } else {
      thumb = img(id, tags.Thumb, "thumb") || backdrop;
      poster = img(id, tags.Primary, "poster");
    }
    return { id: id, kind: kind, title: title, subtitle: subtitle, year: raw.ProductionYear || null,
             progress: progress, minutesLeft: minutesLeft, backdrop: backdrop, thumb: thumb, poster: poster,
             logo: logo, logoDark: false };
  }
  function seriesOf(raw) { return raw.Type === "Series" ? raw.Id : raw.SeriesId; }
  /* Each title once, in the first row that has it (resume > next up > latest). */
  function jfBuildRows(creds, resume, nextUp, latest, limit) {
    limit = limit || JF_ROW_LIMIT;
    var ids = {}, series = {};
    function take(raws) {
      var out = [];
      for (var i = 0; i < raws.length && out.length < limit; i++) {
        var raw = raws[i], s = seriesOf(raw);
        if (ids[raw.Id] || (s && series[s])) continue;
        var item = jfMapItem(creds, raw);
        if (!item) continue;
        out.push(item);
        ids[raw.Id] = 1;
        if (s) series[s] = 1;
      }
      return out;
    }
    return { resume: take(resume), nextUp: take(nextUp), latest: take(latest) };
  }
  function jfRows(http, creds, deviceId) {
    var headers = { "Authorization": jfClientAuth(deviceId, creds.token) };
    function get(paths, params) {
      // Newer servers (10.9+) moved some user routes; try each path in turn on 404.
      paths = [].concat(paths);
      function attempt(i) {
        return http({ url: creds.url + paths[i] + qs(params), headers: headers, timeout: 10000 }).then(function (res) {
          if (res.status === 404 && i + 1 < paths.length) return attempt(i + 1);
          if (res.status === 401) { var e = new Error("Jellyfin sign-in expired"); e.code = "auth"; throw e; }
          return json(res, "Jellyfin " + paths[i]);
        });
      }
      return attempt(0);
    }
    var common = { userId: creds.userId, limit: JF_FETCH_LIMIT, enableImageTypes: "Primary,Backdrop,Thumb,Logo",
                   imageTypeLimit: 1, fields: "ProductionYear,DateCreated" };
    function extend(extra) {
      var o = {};
      Object.keys(common).forEach(function (k) { o[k] = common[k]; });
      Object.keys(extra).forEach(function (k) { o[k] = extra[k]; });
      return o;
    }
    var uid = creds.userId;
    return Promise.all([
      get(["/UserItems/Resume", "/Users/" + uid + "/Items/Resume"], extend({ mediaTypes: "Video" })),
      get("/Shows/NextUp", extend({ enableResumable: "false", enableRewatching: "false" })),
      get(["/UserViews", "/Users/" + uid + "/Views"], { userId: uid })
    ]).then(function (r) {
      var views = (r[2].Items || []).filter(function (v) { return !v.CollectionType || JF_LATEST_VIEWS[v.CollectionType]; });
      return Promise.all(views.map(function (v) {
        return get(["/Items/Latest", "/Users/" + uid + "/Items/Latest"], extend({ parentId: v.Id, groupItems: "true" }))
          .catch(function () { return []; });
      })).then(function (lists) {
        var latest = [];
        lists.forEach(function (l) { latest = latest.concat(l || []); });
        latest.sort(function (a, b) {
          var x = a.DateCreated || "", y = b.DateCreated || "";
          return x < y ? 1 : x > y ? -1 : 0;
        });
        return jfBuildRows(creds, r[0].Items || [], r[1].Items || [], latest);
      });
    });
  }

  /* ---------- Steam (Web API, public game details) ---------- */
  var STEAM_TOOLS = [7, 760, 228980, 1070560, 1391110, 1628350, 4183110, 1493710, 2180100, 1826330, 1887720,
                     2348590, 2805730, 3658110, 961940, 1113280, 1054830, 1245040, 1420170, 1580130, 858280,
                     930400, 996510, 1161040, 4099820];
  var STEAM_TOOL_NAME = /^(Proton\b|Steam Linux Runtime|Steamworks Common Redistributables|Steamworks SDK|Steam Audio|SteamVR)/i;
  var STEAM_CDN = "https://shared.steamstatic.com/store_item_assets/steam/apps/";
  var STEAM_LIMIT = 12;
  function steamArt(appid) {
    var b = STEAM_CDN + appid + "/";
    return { cover: b + "library_600x900_2x.jpg", hero: b + "library_hero.jpg", logo: b + "logo.png", header: b + "header.jpg" };
  }
  function steamGame(g, lastPlayedSec) {
    var art = steamArt(g.appid);
    return { appid: g.appid, name: g.name || ("App " + g.appid),
             lastPlayed: lastPlayedSec ? new Date(lastPlayedSec * 1000).toISOString() : null,
             playtimeH: typeof g.playtime_forever === "number" ? Math.round(g.playtime_forever / 6) / 10 : null,
             cover: art.cover, hero: art.hero, logo: art.logo, header: art.header, logoDark: false };
  }
  function isTool(g) { return STEAM_TOOLS.indexOf(g.appid) >= 0 || STEAM_TOOL_NAME.test(g.name || ""); }
  /* Owned games sorted by last played (it carries the date); recently played
     (last two weeks) as the fallback when the owned list is empty. */
  function fetchSteam(http, key, steamId) {
    var api = "https://api.steampowered.com/";
    var base = { key: key, steamid: steamId, format: "json" };
    function call(path, extra) {
      var p = {};
      Object.keys(base).forEach(function (k) { p[k] = base[k]; });
      Object.keys(extra).forEach(function (k) { p[k] = extra[k]; });
      return http({ url: api + path + qs(p), timeout: 15000 }).then(function (res) {
        if (res.status === 401 || res.status === 403) throw new Error("Steam refused the Web API key");
        return json(res, "Steam").response || {};
      });
    }
    return call("IPlayerService/GetOwnedGames/v1/", { include_appinfo: 1, include_played_free_games: 1 }).then(function (owned) {
      var games = (owned.games || []).filter(function (g) { return g.rtime_last_played > 0 && !isTool(g); });
      games.sort(function (a, b) { return b.rtime_last_played - a.rtime_last_played; });
      if (games.length) return games.slice(0, STEAM_LIMIT).map(function (g) { return steamGame(g, g.rtime_last_played); });
      return call("IPlayerService/GetRecentlyPlayedGames/v1/", { count: STEAM_LIMIT }).then(function (recent) {
        return (recent.games || []).filter(function (g) { return !isTool(g); }).map(function (g) { return steamGame(g, null); });
      });
    }).then(function (games) {
      return { updated: new Date().toISOString(), recent: games };
    });
  }

  return {
    qs: qs, pool: pool, pad: pad,
    mapCode: mapCode, rainNote: rainNote, summarizeWeather: summarizeWeather, forecastUrl: forecastUrl,
    fetchWeather: fetchWeather, geocode: geocode, ipLocation: ipLocation,
    parseChannels: parseChannels, resolveChannel: resolveChannel, parseFeed: parseFeed, checkShort: checkShort,
    fetchYouTube: fetchYouTube, CONSENT: CONSENT,
    jfBase: jfBase, jfSignIn: jfSignIn, jfSignOut: jfSignOut, jfMapItem: jfMapItem, jfBuildRows: jfBuildRows,
    jfRows: jfRows, jfClientAuth: jfClientAuth,
    fetchSteam: fetchSteam, steamArt: steamArt
  };
}));
