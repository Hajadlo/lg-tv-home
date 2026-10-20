/* TV Home. Renders /api/home, drives focus, Play on PC, idle and launches.

   The doors are tabs, one per source configured on the server
   (/api/home `features`; a source that is off has no door and no room). Each door's room (hero and grids) lives in its
   own stage, built when its data arrives and then kept: switching rooms only
   toggles classes and starts compositor animations, so a key press costs a
   few milliseconds and nothing has to be rastered mid-slide. A room is rebuilt
   only when its data changed, and only while the page is quiet (no key for a
   moment, no switch running). Layout is measured after a build, never in a
   key handler. */
(function () {
  "use strict";
  var api = TVH.api, nav = TVH.nav, params = TVH.params;

  var ROW_TARGET = 380;        /* a focused grid row sits here; the row below stays in view */
  var MAX_TILES = 12, MAX_VIDEOS = 30;
  /* Image widths asked of the backend: decoded size close to the displayed size. */
  var W = { tile: 320, cover: 300, logo: 480, backdrop: 1600, ytBackdrop: 1280, wide: 1920 };
  var IDLE_AFTER = +(params.get("idleAfter") || 180000);
  var PC_TIMEOUT = +(params.get("pcTimeout") || 90000);
  var REFRESH_MS = 5 * 60 * 1000;
  var RETRY_MS = 30 * 1000;
  var QUIET_MS = 900;          /* rebuilds wait until no key came for this long */
  var FADE_MS = 220, SLIDE_MS = 250, SLIDE_PX = 22, EASE = "cubic-bezier(0.2, 0.7, 0.2, 1)";
  var DOORS = ["jellyfin", "youtube", "pc"];   // narrowed by applyFeatures
  var FEATURE = { jellyfin: "jellyfin", youtube: "youtube", pc: "steam" };

  var $ = function (id) { return document.getElementById(id); };
  var body = document.body;
  var scroller = $("scroll");
  var stages = {}, washes = {};
  DOORS.forEach(function (n) {
    stages[n] = document.querySelector('.stage[data-room="' + n + '"]');
    washes[n] = document.querySelector('.wash[data-room="' + n + '"]');
  });
  var selected = "jellyfin";   // which door's room is showing
  var data = null;             // last /api/home payload
  var down = false, ready = false;
  var lastKeyAt = 0, busyUntil = 0;

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "class") el.className = attrs[k];
      else if (k === "text") setText(el, attrs[k]);
      else if (k.indexOf("data-") === 0) el.setAttribute(k, attrs[k]);
      else el[k] = attrs[k];
    }
    if (children) for (var i = 0; i < children.length; i++) if (children[i]) el.appendChild(children[i]);
    return el;
  }
  function lazyImg(src, cls) {
    var img = h("img", { class: cls || "", alt: "", loading: "lazy", decoding: "async" });
    img.onload = function () { img.classList.add("loaded"); };
    img.src = src;
    return img;
  }

  /* ---------- time and words ---------- */
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function plural(n, one) { return n + " " + one + (n === 1 ? "" : "s"); }
  function ago(iso) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return "";
    var s = (Date.now() - t) / 1000;
    if (s < 90) return "just now";
    var m = Math.floor(s / 60);
    if (m < 60) return m + " min ago";
    var hr = Math.floor(m / 60);
    if (hr < 24) return plural(hr, "hour") + " ago";
    var d = Math.floor(hr / 24);
    if (d < 2) return "yesterday";
    if (d < 7) return d + " days ago";
    if (d < 31) return plural(Math.floor(d / 7), "week") + " ago";
    if (d < 365) return plural(Math.max(1, Math.round(d / 30.4)), "month") + " ago";
    return plural(Math.floor(d / 365), "year") + " ago";
  }
  /* The short form for tiles: "3 h ago", "2 d ago", "1 w ago". */
  function since(iso) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return "";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + " min ago";
    var hr = Math.floor(s / 3600);
    if (hr < 24) return hr + " h ago";
    var d = Math.floor(hr / 24);
    if (d < 7) return d + " d ago";
    if (d < 30) return Math.floor(d / 7) + " w ago";
    if (d < 365) return Math.max(1, Math.round(d / 30.4)) + " mo ago";
    return Math.floor(d / 365) + " y ago";
  }
  function played(hours) {
    if (typeof hours !== "number" || hours <= 0) return "";
    if (hours < 1) return Math.round(hours * 60) + " min played";
    return plural(Math.round(hours), "hour") + " played";
  }
  /* Day bucket for a timestamp: 0 today, 1 yesterday, 2 this week, 3 earlier. */
  function dayBucket(iso) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return 3;
    var now = new Date(), d = new Date(t);
    var start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    var dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    var days = Math.round((start - dayStart) / 86400000);
    if (days <= 0) return 0;
    if (days === 1) return 1;
    if (days < 7) return 2;
    return 3;
  }
  /* Ages ("3 h ago") are written by setText from {age, ts, prefix} and kept
     current by refreshAges, since rooms are no longer rebuilt on every visit. */
  var AGE = { ago: ago, since: since };
  function ageText(el) {
    var t = AGE[el.dataset.age](el.dataset.ts);
    return t ? (el.dataset.prefix || "") + t : "";
  }
  function setText(el, v) {
    if (v && typeof v === "object") {
      el.dataset.age = v.age;
      el.dataset.ts = v.ts || "";
      if (v.prefix) el.dataset.prefix = v.prefix;
      v = ageText(el);
    }
    el.textContent = v || "";
  }
  function refreshAges(root) {
    var els = root.querySelectorAll("[data-age]");
    for (var i = 0; i < els.length; i++) {
      var t = ageText(els[i]);
      if (els[i].textContent !== t) els[i].textContent = t;
    }
  }
  function setIfChanged(el, text) { if (el.textContent !== text) el.textContent = text; }

  /* ---------- clock ---------- */
  var dateFmt = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });
  function tickClock() {
    var now = new Date();
    setIfChanged($("clock"), pad(now.getHours()) + ":" + pad(now.getMinutes()));
    setIfChanged($("date"), dateFmt.format(now));
    setTimeout(tickClock, 60000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 50);
  }

  /* ---------- top block ---------- */
  function lower(s) { return s ? s.charAt(0).toLowerCase() + s.slice(1) : ""; }
  function renderTop(d) {
    var w = d && d.weather, weather = "", note = "";
    if (w && typeof w.tempC === "number") {
      weather = Math.round(w.tempC) + "°" + (w.label ? ", " + lower(w.label) : "");
      note = w.note || "";
      if (!note && typeof w.highC === "number" && typeof w.lowC === "number") {
        note = "High " + Math.round(w.highC) + "°, low " + Math.round(w.lowC) + "°";
      }
    }
    setIfChanged($("weather"), weather);
    setIfChanged($("weather-note"), note);
    var sys = d && d.system;
    var status = "";
    if (sys && typeof sys.hddFreeGb === "number" && sys.hddTotalGb) {
      var frac = sys.hddFreeGb / sys.hddTotalGb;
      if (frac < 0.1 || sys.hddFreeGb < 100) status = "Disk almost full, " + Math.round(sys.hddFreeGb) + " GB free";
    }
    setIfChanged($("status"), status);
  }

  /* ---------- backdrop: one picture layer per room, two spare ----------
     Every layer stays rastered, so showing a room's picture only moves
     opacity. A new picture is fetched and decoded off screen first. When the
     open room's picture changes, it crossfades in on a spare layer, which then
     becomes that room's; the idle slideshow runs on the spares. */
  var bdLayers = Array.prototype.slice.call(document.querySelectorAll("#backdrop .bd"));
  var roomBd = {}, bdKey = {};
  bdLayers.forEach(function (l) { if (l.dataset.room) roomBd[l.dataset.room] = l; });
  function spareLayer(not) {
    var free = bdLayers.filter(function (l) { return !l.dataset.room && l !== not; });
    return free.filter(function (l) { return !l.classList.contains("on"); })[0] || free[0];
  }
  function putPicture(layer, url, wide) {
    layer.src = url;
    layer.classList.toggle("wide", !!wide);
    layer.hasPicture = true;
  }
  /* Fetch and decode the first candidate URL that loads; cb(url) or cb(null). */
  function loadPicture(urls, cb) {
    var i = 0;
    (function attempt() {
      if (i >= urls.length) return cb(null);
      var url = urls[i++], img = new Image();
      img.onload = function () {
        if (img.decode) img.decode().then(function () { cb(url); }, function () { cb(url); });
        else cb(url);
      };
      img.onerror = attempt;
      img.src = url;
    })();
  }
  function roomOnView(name) { return name === selected && !idle.active; }
  function setRoomBackdrop(name, urls, wide) {
    urls = (urls || []).filter(Boolean);
    var key = urls.join(" ") + (wide ? " wide" : "");
    if (bdKey[name] === key) return;
    bdKey[name] = key;
    loadPicture(urls, function (url) {
      if (bdKey[name] !== key) return;
      var old = roomBd[name];
      if (!url) {
        old.classList.remove("on");
        old.hasPicture = false;
        return;
      }
      if (roomOnView(name) && old.classList.contains("on")) {
        var next = spareLayer();
        next.dataset.room = name;
        old.removeAttribute("data-room");
        roomBd[name] = next;
        putPicture(next, url, wide);
        next.classList.add("on");
        old.classList.remove("on");
      } else {
        putPicture(old, url, wide);
        if (ready && !roomOnView(name)) warm(name);
        if (roomOnView(name)) old.classList.add("on");
      }
    });
  }
  function showRoomBackdrop(name, on) {
    var l = roomBd[name];
    l.classList.toggle("on", !!on && !!l.hasPicture);
  }

  /* ---------- hero pieces shared by the rooms ---------- */
  /* A logo drawn dark for a bright banner (some Steam logos are black on
     transparent) gives way to the title set in type, as does one that fails
     to load. The backend works out `logoDark` and trims the padding. */
  function heroLogo(url, title, dark) {
    if (dark) return title;
    var logo = lazyImg(url, "hero-logo");
    logo.onerror = function () { if (logo.parentNode) logo.parentNode.replaceChild(title, logo); };
    return logo;
  }
  function heroButton(opts) {
    // opts: launch, logo (url or null), logoDark, title, long, sub, verb, meta, progress
    var btn = h("button", { class: "hero-btn", "data-item": "", "data-launch": opts.launch });
    var title = h("h1", { class: "hero-title" + (opts.long ? " long" : ""), text: opts.title || "" });
    btn.appendChild(opts.logo ? heroLogo(opts.logo, title, opts.logoDark) : title);
    if (opts.sub) btn.appendChild(h("p", { class: "hero-sub", text: opts.sub }));
    btn.appendChild(h("p", { class: "hero-meta" }, [
      h("span", { class: "verb", text: opts.verb + (opts.meta ? ", " : "") }),
      h("span", { class: "meta", text: opts.meta || "" }),
      h("span", { class: "hero-pc" })
    ]));
    if (typeof opts.progress === "number" && opts.progress > 0) {
      var bar = h("span", { class: "hero-progress" }, [h("i")]);
      bar.firstChild.style.transform = "scaleX(" + Math.min(1, opts.progress) + ")";
      btn.appendChild(bar);
    }
    return btn;
  }
  function heroEmpty(el, head, text) {
    el.appendChild(h("p", { class: "hero-empty" }, [
      head ? h("b", { text: head }) : null,
      document.createTextNode(text || "")
    ]));
  }
  function progressBar(p) {
    if (typeof p !== "number" || !(p > 0)) return null;
    var bar = h("span", { class: "tile-progress" }, [h("i")]);
    bar.firstChild.style.transform = "scaleX(" + Math.min(1, p) + ")";
    return bar;
  }
  /* sub: the line under the title; when: a short age shown at its right end. */
  function tileButton(launch, frame, title, sub, when) {
    return h("button", { class: "tile", "data-item": "", "data-launch": launch, title: title || "" }, [
      frame,
      h("span", { class: "tile-ring" }),
      h("span", { class: "tile-text" }, [
        h("span", { class: "tile-title", text: title || "" }),
        h("span", { class: "tile-sub" }, [
          h("span", { class: "who", text: sub || "" }),
          when ? h("span", { class: "when", text: when }) : null
        ])
      ])
    ]);
  }
  /* A group of tiles that wraps into full rows: 5 frames or 8 covers across. */
  function addGrid(rows, heading, tiles, cls) {
    if (!tiles.length) return;
    var cols = cls === "portrait" ? 8 : 5;
    rows.appendChild(h("section", { class: "row" + (cls ? " " + cls : ""), "data-section": "row", "data-cols": cols }, [
      h("h2", { text: heading }), h("div", { class: "grid" }, tiles)
    ]));
  }
  var DOWN_HEAD = "The home server isn't answering";

  /* ---------- Jellyfin room ---------- */
  function pickHero(jf) {
    if (!jf) return null;
    if (jf.resume && jf.resume.length) return { item: jf.resume[0], from: "resume" };
    if (jf.nextUp && jf.nextUp.length) return { item: jf.nextUp[0], from: "nextUp" };
    if (jf.latest && jf.latest.length) return { item: jf.latest[0], from: "latest" };
    return null;
  }
  function jfMeta(it, from) {
    // The TV app ignores deep links, so the verb says what really happens.
    if (from === "resume") return it.minutesLeft ? it.minutesLeft + " min left" : "Continue watching";
    if (from === "nextUp") return "Up next";
    return it.kind === "series" ? "New episodes" : "New in your library";
  }
  function jfSub(it) {
    return it.kind === "episode" ? (it.subtitle || "") : (it.subtitle || (it.year ? String(it.year) : ""));
  }
  function jfTile(it) {
    var img = it.thumb || it.backdrop || it.poster;
    var frame = h("span", { class: "tile-img" }, [img ? lazyImg(api.img(img, W.tile)) : null, progressBar(it.progress)]);
    return tileButton("jf:" + it.id, frame, it.title, jfSub(it));
  }
  function renderJellyfin(hero, rows, d) {
    if (down) {
      heroEmpty(hero, DOWN_HEAD, "Jellyfin still opens on its own.");
      return { backdrops: [] };
    }
    var jf = d && d.jellyfin;
    if (!jf) {
      heroEmpty(hero, "Jellyfin isn't answering", "The home server can't reach it right now. It may still open on its own.");
      return { backdrops: [] };
    }
    var pick = pickHero(jf);
    if (!pick) {
      heroEmpty(hero, "Nothing waiting", "Nothing to continue and nothing new in your library.");
      return { backdrops: [] };
    }
    var it = pick.item;
    hero.appendChild(heroButton({
      launch: "jf:" + it.id,
      logo: it.logo ? api.img(it.logo, W.logo) : null, logoDark: it.logoDark,
      title: it.title, sub: jfSub(it),
      verb: it.kind === "series" ? "Open" : (it.progress > 0 ? "Resume" : "Play"),
      meta: jfMeta(it, pick.from), progress: it.progress
    }));
    [["Continue watching", jf.resume, "resume"], ["Next up", jf.nextUp, "nextUp"], ["Recently added", jf.latest, "latest"]]
      .forEach(function (def) {
        var items = (def[1] || []).filter(function (x) {
          return x && x.id && !(pick.from === def[2] && x.id === it.id);
        }).slice(0, MAX_TILES);
        addGrid(rows, def[0], items.map(jfTile));
      });
    var seen = {}, pool = [];
    ["resume", "nextUp", "latest"].forEach(function (k) {
      (jf[k] || []).forEach(function (x) {
        var u = api.img(x.backdrop, W.backdrop);
        if (u && !seen[u]) { seen[u] = 1; pool.push({ url: u }); }
      });
    });
    return { backdrop: [api.img(it.backdrop, W.backdrop)], backdrops: pool };
  }

  /* ---------- YouTube room ---------- */
  function ytThumb(v, w) {
    var img = lazyImg(api.img(v.thumb, w));
    img.onerror = function () {
      // the mock points at maxresdefault, which older videos lack
      if (img.src.indexOf("maxresdefault") >= 0) img.src = img.src.replace("maxresdefault", "hqdefault");
    };
    return img;
  }
  function ytTile(v) {
    var frame = h("span", { class: "tile-img" }, [v.thumb ? ytThumb(v, W.tile) : null, progressBar(v.progress)]);
    return tileButton("yt:" + v.id, frame, v.title, v.channel, v.published ? { age: "since", ts: v.published } : null);
  }
  function renderYouTube(hero, rows, d) {
    if (down) {
      heroEmpty(hero, DOWN_HEAD, "YouTube still opens on its own.");
      return { backdrops: [] };
    }
    var yt = d && d.youtube;
    var vids = yt ? (yt.videos || []).filter(function (v) { return v && v.id; }).slice(0, MAX_VIDEOS) : [];
    if (!vids.length) {
      // null, or an account that was never connected, or a connected one with nothing to show
      if (yt && yt.auth === "ok") heroEmpty(hero, "Nothing new", "Your channels haven't posted lately. YouTube itself opens as usual.");
      else heroEmpty(hero, "No YouTube account connected",
        "Connect one on the home server and new videos from your subscriptions show up here. YouTube itself opens as usual.");
      return { backdrops: [] };
    }
    var top = vids[0];
    hero.appendChild(heroButton({
      launch: "yt:" + top.id, title: top.title, long: (top.title || "").length > 24,
      sub: top.channel, verb: top.progress > 0 ? "Continue" : "Play",
      meta: ago(top.published) ? { age: "ago", ts: top.published } : "",
      progress: top.progress
    }));
    // One row of the newest, then what was left unfinished, then the rest.
    var rest = vids.slice(1);
    var unfinished = (yt.continue || []).filter(function (v) { return v && v.id; });
    addGrid(rows, "Latest", rest.slice(0, 5).map(ytTile), "video");
    addGrid(rows, "Continue watching", unfinished.map(ytTile), "video");
    addGrid(rows, "Earlier", rest.slice(5).map(ytTile), "video");
    var u = api.img(top.thumb, W.ytBackdrop);
    return { backdrop: [u, u && u.indexOf("maxresdefault") >= 0 ? u.replace("maxresdefault", "hqdefault") : null], backdrops: [] };
  }

  /* ---------- Steam room ---------- */
  function gameTile(g) {
    var frame = h("span", { class: "tile-img" });
    var name = h("span", { class: "tile-name", text: g.name || "" });
    if (g.cover) {
      var img = lazyImg(api.img(g.cover, W.cover));
      img.onerror = function () { if (img.parentNode) img.parentNode.replaceChild(name, img); };
      frame.appendChild(img);
    } else {
      frame.appendChild(name);
    }
    return tileButton("game", frame, g.name, { age: "ago", ts: g.lastPlayed });
  }
  function renderSteam(hero, rows, d) {
    if (down) {
      heroEmpty(hero, DOWN_HEAD, "Steam needs it to wake the desktop. Try again in a moment.");
      return { backdrops: [] };
    }
    var st = d && d.steam;
    var games = st ? (st.recent || []).filter(function (g) { return g && g.appid; }).slice(0, MAX_TILES) : [];
    if (!games.length) {
      heroEmpty(hero, "No games seen yet",
        "Recently played games are read from the desktop while it's on. Open Steam and they'll be here next time.");
      return { backdrops: [] };
    }
    var top = games[0];
    hero.appendChild(heroButton({
      launch: "game", logo: top.logo ? api.img(top.logo, W.logo) : null, logoDark: top.logoDark, title: top.name,
      sub: ago(top.lastPlayed) ? { age: "ago", ts: top.lastPlayed, prefix: "Last played " } : "",
      verb: "Open Steam", meta: played(top.playtimeH)
    }));
    addGrid(rows, "Recently played", games.slice(1).map(gameTile), "portrait");
    var pool = games.map(function (g) {
      var u = api.img(g.hero, W.wide);
      return u ? { url: u, wide: true } : null;
    }).filter(Boolean);
    return { backdrop: [api.img(top.hero, W.wide), api.img(top.header, W.wide)], wide: true, backdrops: pool };
  }

  var ROOMS = { jellyfin: renderJellyfin, youtube: renderYouTube, pc: renderSteam };
  function roomData(name, d) {
    return d && (name === "jellyfin" ? d.jellyfin : name === "youtube" ? d.youtube : d.steam);
  }

  /* ---------- building rooms (only when their data changed, only when quiet) ---------- */
  var roomKey = {}, pending = {}, roomPools = {}, backdropPool = [];
  var doorsDirty = false, flushTimer = null;
  function quiet() { var t = Date.now(); return t - lastKeyAt > QUIET_MS && t > busyUntil; }

  function buildRoom(name) {
    pending[name] = false;
    var st = stages[name], hero = st.querySelector(".hero"), rows = st.querySelector(".rows");
    var sec = nav.section(), mine = !!sec && sec.stage === st;
    var kind = mine ? sec.kind : null, idx = mine ? sec.index : 0;
    var rowIdx = kind === "row" ? nav.sections().indexOf(sec) : -1;
    hero.textContent = "";
    rows.textContent = "";
    var out = ROOMS[name](hero, rows, data);
    roomPools[name] = out.backdrops || [];
    if (name === selected) backdropPool = roomPools[name];  // idle shows this room's pictures under this room's wash
    setRoomBackdrop(name, out.backdrop, out.wide);
    if (ready && name !== selected) warm(name);
    if (name === "pc" && pc.busy) pcMirror(pcNote.textContent, pcNote.classList.contains("alert"));
    nav.rebuild();
    if (!mine) return;
    if (kind === "row") {
      var secs = nav.sections();
      var s = secs[Math.min(rowIdx, secs.length - 1)];
      if (!s || s.kind !== "row") nav.home();
      else nav.focusEl(s.items[Math.min(idx, s.items.length - 1)]);
    } else if (!nav.focusSection(kind, idx)) {
      nav.home();
    }
  }

  /* Let Chrome paint, decode and raster a room that is not on view (see .warm
     in home.css). Done when quiet; a switch to it meanwhile simply shows it. */
  var WARM_MS = 2500;
  function warm(name) {
    var st = stages[name], bd = roomBd[name];
    clearTimeout(st.warmTimer);
    st.classList.add("warm");
    bd.classList.add("warm");
    st.warmTimer = setTimeout(function () {
      st.classList.remove("warm");
      roomBd[name].classList.remove("warm");
      bd.classList.remove("warm");
    }, WARM_MS);
  }

  /* Door positions for the travelling line, measured here so a switch never reads layout. */
  var line = $("doorline"), doorPos = {};
  function doorEl(name) { return document.querySelector('.door[data-app="' + name + '"]'); }
  function layoutDoors() {
    DOORS.forEach(function (n) {
      var word = doorEl(n).querySelector(".door-word");
      doorPos[n] = { x: word.offsetLeft, w: word.offsetWidth, top: word.offsetTop + word.offsetHeight - 3 };
    });
    line.style.top = doorPos[selected].top + "px";
    placeLine();
  }
  function placeLine() {
    var p = doorPos[selected];
    if (p) line.style.transform = "translateX(" + p.x + "px) scaleX(" + (p.w / 100) + ")";
  }

  /* Pending work, done when quiet: one room per task keeps each task short. */
  function flush() {
    clearTimeout(flushTimer);
    flushTimer = null;
    if (ready && !quiet()) { flushTimer = setTimeout(flush, 300); return; }
    var todo = DOORS.filter(function (n) { return pending[n]; });
    if (!ready) todo.forEach(buildRoom);          // first paint: everything at once
    else if (todo.length) buildRoom(todo[0]);
    if (todo.length > 1 && ready) { flushTimer = setTimeout(flush, 50); return; }
    DOORS.forEach(function (n) { refreshAges(stages[n]); });
    if (doorsDirty) {
      doorsDirty = false;
      layoutDoors();
      nav.rebuild();
    }
  }

  /* ---------- switching rooms: class toggles and compositor animations ---------- */
  function slide(el, from, to, dir) {
    if (el.anims) el.anims.forEach(function (a) { a.cancel(); });
    var x0 = from === 0 ? SLIDE_PX * dir : 0, x1 = to === 0 ? -SLIDE_PX * dir : 0;
    el.anims = [
      el.animate([{ opacity: from }, { opacity: to }], { duration: FADE_MS, easing: "ease" }),
      el.animate([{ transform: "translateX(" + x0 + "px)" }, { transform: "translateX(" + x1 + "px)" }],
                 { duration: SLIDE_MS, easing: EASE })
    ];
  }
  function switchTo(name) {
    if (name === selected || !ROOMS[name]) return;
    var dir = DOORS.indexOf(name) > DOORS.indexOf(selected) ? 1 : -1;
    var from = stages[selected], to = stages[name];
    if (!idle.active) { showRoomBackdrop(selected, false); showRoomBackdrop(name, true); }
    selected = name;
    busyUntil = Date.now() + SLIDE_MS + 100;
    nav.setHome(DOORS.indexOf(name));
    nav.setScope(to);
    slide(from, 1, 0, dir);
    slide(to, 0, 1, dir);
    from.classList.remove("on", "first");
    to.classList.add("on");
    backdropPool = roomPools[name] || [];
    updateDoors();
  }

  /* ---------- doors: notes, selected state, the travelling line ---------- */
  function updateDoors() {
    DOORS.forEach(function (n) {
      doorEl(n).classList.toggle("selected", n === selected);
      body.classList.toggle("room-" + n, n === selected);
      washes[n].classList.toggle("on", n === selected);
    });
    placeLine();
    if (ready) line.classList.add("on");
    var busy = selected === "pc" && (pcDoor.classList.contains("waking") || pcDoor.classList.contains("ready"));
    line.classList.toggle("hidden", busy);
  }
  var jfNote = $("jf-note"), ytNoteEl = $("yt-note");   // kept: a removed door's note is detached, not gone
  function renderDoors(d) {
    var before = jfNote.textContent + "|" + ytNoteEl.textContent + "|" + pcNote.textContent;
    setIfChanged(jfNote, (!down && d && d.jellyfin === null) ? "Not reachable right now" : "");
    var yt = d && d.youtube, ytNote = "";
    if (!down && d) {
      var vids = (yt && yt.videos) || [];
      if (!vids.length) ytNote = yt && yt.auth === "ok" ? "" : "No account connected";
      else if (yt.auth === "expired") ytNote = "Sign-in expired";
      else {
        var today = vids.filter(function (v) { return dayBucket(v.published) === 0; }).length;
        if (today) ytNote = today + " new today";
      }
    }
    setIfChanged(ytNoteEl, ytNote);
    if (!pc.busy) setPcNote(d && d.pc ? d.pc : null);
    // A note wider than its word moves the doors after it: measure again (when quiet).
    if (before !== jfNote.textContent + "|" + ytNoteEl.textContent + "|" + pcNote.textContent) doorsDirty = true;
  }

  /* ---------- which doors exist: the server's `features` ---------- */
  var featuresKey = null;
  function hasDoor(name) { return DOORS.indexOf(name) >= 0; }
  function applyFeatures(f) {
    if (!f) return;
    var keep = ["jellyfin", "youtube", "pc"].filter(function (n) { return f[FEATURE[n]] !== false; });
    if (!keep.length) return;   // nothing configured: keep every door, each says what is missing
    var key = keep.join(",");
    if (key === featuresKey) return;
    featuresKey = key;
    DOORS.forEach(function (n) {
      if (keep.indexOf(n) >= 0) return;
      [doorEl(n), stages[n], washes[n], roomBd[n]].forEach(function (el) {
        if (el && el.parentNode) el.parentNode.removeChild(el);
      });
      bdLayers = bdLayers.filter(function (l) { return l !== roomBd[n]; });
      body.classList.remove("room-" + n);
    });
    DOORS = keep;
    if (!hasDoor(selected)) {
      var was = stages[selected];
      selected = DOORS[0];
      stages[selected].classList.add("on");
      if (!ready && was.classList.contains("first")) stages[selected].classList.add("first");
      backdropPool = roomPools[selected] || [];
    }
    doorsDirty = true;
    if (ready) {
      layoutDoors();
      nav.setScope(stages[selected]);
      nav.rebuild();
      nav.home();
      showRoomBackdrop(selected, true);
      updateDoors();
    }
  }

  /* ---------- data ---------- */
  function apply(d) {
    applyFeatures(d && d.features);
    data = d;
    renderTop(d);
    renderDoors(d);
    DOORS.forEach(function (n) {
      var k = down ? "down" : JSON.stringify(roomData(n, d) || null);
      if (k !== roomKey[n]) { roomKey[n] = k; pending[n] = true; }
    });
    flush();
  }
  var retryTimer = null;
  function load() {
    return api.home().then(function (d) {
      down = false;
      apply(d);
    }).catch(function (e) {
      console.warn("home: " + e.message);
      if (!down || !ready) {
        down = true;
        apply(null);
        setPcNote(null);
      }
      clearTimeout(retryTimer);
      retryTimer = setTimeout(load, RETRY_MS);
    }).then(function () {
      if (ready) return;
      layoutDoors();
      nav.setScope(stages[selected]);
      nav.rebuild();
      if (!nav.section()) nav.home();
      ready = true;
      body.classList.add("ready");
      showRoomBackdrop(selected, true);
      updateDoors();
      setTimeout(function () { DOORS.forEach(function (n) { stages[n].classList.remove("first"); }); }, 2500);
      setTimeout(function () { DOORS.forEach(function (n) { if (n !== selected) warm(n); }); }, 700);
    });
  }

  /* ---------- scrolling on focus: the focused grid row rides at ROW_TARGET ---------- */
  /* Entering or leaving the grids restyles much of the page (body.browsing):
     that goes one frame after the focus and scroll, so neither frame runs long. */
  var scrollY = 0, browsingNow = false, browsingSeq = 0;
  function setBrowsing(on) {
    if (on === browsingNow) return;
    browsingNow = on;
    var seq = ++browsingSeq;
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { if (seq === browsingSeq) body.classList.toggle("browsing", on); });
    });
  }
  nav.onFocus(function (sec, el, i) {
    var browsing = sec.kind === "row";
    setBrowsing(browsing);
    var y = browsing ? ROW_TARGET - sec.y[i] : 0;
    if (y !== scrollY) {
      scrollY = y;
      scroller.style.transform = y ? "translateY(" + y + "px)" : "none";
    }
    if (sec.kind === "doors") switchTo(el.dataset.app);
  });

  /* ---------- launching ---------- */
  var toastTimer = null;
  function toast(text, ms) {
    var t = $("toast");
    t.textContent = text;
    t.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("on"); }, ms || 2500);
  }
  function bridge() { return window.tvHome; }
  function go(app, opts, label) {
    if (!bridge()) { toast("No TV bridge"); return; }
    toast("Opening " + label);
    Promise.resolve(bridge().launch(app, opts || {})).catch(function () { toast("Couldn't open " + label, 4000); });
  }
  function openSettings(target) {
    if (!bridge()) return;
    var labels = { picture: "Picture settings", sound: "Sound settings", input: "Inputs", all: "Settings" };
    toast("Opening " + (labels[target] || "Settings"));
    Promise.resolve(bridge().openSettings(target)).catch(function () { toast("Couldn't open settings", 4000); });
  }
  function lgHome() {
    if (!bridge()) return;
    toast("Opening LG apps");
    Promise.resolve(bridge().lgHome()).catch(function () { toast("Couldn't open LG apps", 4000); });
  }
  function activate(el) {
    if (!el) return;
    if (el.dataset.app === "jellyfin") return go("jellyfin", {}, "Jellyfin");
    if (el.dataset.app === "youtube") return go("youtube", {}, "YouTube");
    if (el.dataset.app === "pc") return playOnPc();
    if (el.dataset.settings) return openSettings(el.dataset.settings);
    if (el.hasAttribute("data-lg")) return lgHome();
    var launch = el.dataset.launch || "";
    if (launch.indexOf("jf:") === 0) {
      // The Jellyfin app ignores deep links; the server plays the item through
      // Jellyfin's remote control once the app has connected.
      api.jfOpen(launch.slice(3)).catch(function () {});
      return go("jellyfin", { jellyfinItemId: launch.slice(3) }, "Jellyfin");
    }
    if (launch.indexOf("yt:") === 0) return go("youtube", { videoId: launch.slice(3) }, "YouTube");
    if (launch === "game") {
      // Same flow as the Steam door; bring the hero back so its state is in view.
      if (nav.section() && nav.section().kind === "row") nav.focusSection("hero", 0);
      return playOnPc();
    }
  }

  /* ---------- Play on PC ---------- */
  var pc = { busy: false, timer: null, deadline: 0 };
  var pcDoor = $("door-pc"), pcNote = $("pc-note"), pcBarFill = pcDoor.querySelector(".door-bar i");
  function pcMirror(text, alert) {
    // While the desktop wakes, the Steam hero says so too.
    var m = stages.pc.querySelector(".hero-pc");
    if (!m) return;
    m.textContent = text || "";
    m.classList.toggle("alert", !!alert);
    m.parentNode.classList.toggle("busy", !!text);
  }
  function setPcNote(snapshot, cls) {
    var text = "";
    if (snapshot) {
      if (snapshot.state === "waking") text = "Waking the desktop";
      else if (snapshot.state === "on") text = snapshot.sunshine === "listening" ? "Desktop is on" : "Desktop is on, Sunshine isn't running";
      else if (snapshot.state === "off") text = "Desktop is asleep";
    } else if (down) {
      text = "Needs the home server";
    }
    setIfChanged(pcNote, text);
    pcNote.className = "door-note" + (cls ? " " + cls : "");
  }
  /* The bar's fill is a compositor animation (no forced style flush). */
  var pcFill = null;
  function fillBar(to, ms, easing) {
    var from = 0;
    if (pcFill) {
      var t = pcFill.effect.getComputedTiming();
      from = pcFill.fromTo[0] + (pcFill.fromTo[1] - pcFill.fromTo[0]) * (t.progress === null ? 1 : t.progress);
      pcFill.cancel();
    }
    pcFill = ms ? pcBarFill.animate([{ transform: "scaleX(" + from + ")" }, { transform: "scaleX(" + to + ")" }],
                                    { duration: ms, easing: easing || "linear", fill: "forwards" }) : null;
    if (pcFill) pcFill.fromTo = [from, to];
  }
  function pcPhase(phase, text) {
    pcDoor.classList.remove("waking", "ready");
    if (phase) pcDoor.classList.add(phase);
    if (phase === "waking") { if (pcFill) { pcFill.cancel(); pcFill = null; } fillBar(1, PC_TIMEOUT); }
    if (phase === "ready") fillBar(1, 400, EASE);  // from where the slow fill is, quickly to the end
    if (!phase && pcFill) { pcFill.cancel(); pcFill = null; }
    pcNote.textContent = text;
    pcNote.className = "door-note" + (phase === "failed" ? " alert" : "");
    pcMirror(phase ? text : "", phase === "failed");
    updateDoors();
  }
  function pcFail(text) {
    pc.busy = false;
    clearTimeout(pc.timer);
    pcPhase(null, "");
    pcNote.textContent = text + ". Press to try again.";
    pcNote.className = "door-note alert";
    pcMirror(text + ". Press to try again.", true);
  }
  function pcStatus(text) {
    pcNote.textContent = text;
    pcMirror(text, false);
  }
  function playOnPc() {
    if (pc.busy) return;
    pc.busy = true;
    pcPhase("waking", "Waking the desktop, this can take a minute");
    api.play().then(function (r) {
      if (r.ready || (r.state === "on" && r.sunshine === "listening")) return pcReady();
      pc.deadline = Date.now() + PC_TIMEOUT;
      pc.timer = setTimeout(pollPc, 1500);
    }).catch(function () { pcFail("The home server isn't answering"); });
  }
  function pollPc() {
    api.pc().then(function (r) {
      if (r.ready) return pcReady();
      if (r.error) return pcFail(String(r.error).replace(/\.$/, ""));
      if (Date.now() > pc.deadline) return pcFail("The desktop didn't answer");
      if (r.state === "on" && r.sunshine !== "listening") pcStatus("Desktop is on, starting Sunshine");
      pc.timer = setTimeout(pollPc, 2000);
    }).catch(function () {
      if (Date.now() > pc.deadline) return pcFail("The home server isn't answering");
      pc.timer = setTimeout(pollPc, 3000);
    });
  }
  function pcReady() {
    clearTimeout(pc.timer);
    pcPhase("ready", "Ready, opening Steam");
    setTimeout(function () {
      go("moonlight", {}, "Steam");
      pc.busy = false;
      setTimeout(function () { pcPhase(null, ""); setPcNote({ state: "on", sunshine: "listening" }); }, 3000);
    }, 700);
  }

  /* ---------- idle: the room goes dark, the window shows a slow slideshow ---------- */
  var idle = { active: false, timer: null, cycle: null, drift: null, i: 0, layer: null };
  function idleTouch() {
    clearTimeout(idle.timer);
    idle.timer = setTimeout(idleEnter, IDLE_AFTER);
  }
  function idleEnter() {
    if (idle.active || pc.busy) { idleTouch(); return; }
    idle.active = true;
    nav.home();
    body.classList.add("idle");
    idleDrift();
    idleCycle();
  }
  function idleDrift() {
    var top = $("top");
    top.style.transform = "translate(" + Math.round(Math.random() * 60 - 30) + "px, " + Math.round(Math.random() * 40 - 20) + "px)";
    idle.drift = setTimeout(idleDrift, 40000);
  }
  function idleCycle() {
    if (backdropPool.length) {
      idle.i = (idle.i + 1) % backdropPool.length;
      var b = backdropPool[idle.i];
      loadPicture([b.url], function (url) {
        if (!idle.active || !url) return;
        var next = spareLayer(idle.layer);
        if (next.drift) next.drift.cancel();
        putPicture(next, url, b.wide);
        next.drift = next.animate([{ transform: "scale(1)" }, { transform: "scale(1.08) translate(-1.5%, 1%)" }],
                                  { duration: 48000, easing: "linear", fill: "forwards" });
        next.classList.add("on");
        if (idle.layer) idle.layer.classList.remove("on");
        else showRoomBackdrop(selected, false);
        idle.layer = next;
      });
    }
    idle.cycle = setTimeout(idleCycle, 45000);
  }
  function idleWake() {
    if (!idle.active) return false;
    idle.active = false;
    clearTimeout(idle.cycle); clearTimeout(idle.drift);
    body.classList.remove("idle");
    $("top").style.transform = "";
    var l = idle.layer;
    idle.layer = null;
    if (l) {
      l.classList.remove("on");
      setTimeout(function () { if (!l.classList.contains("on") && l.drift) { l.drift.cancel(); l.drift = null; } }, 400);
    }
    showRoomBackdrop(selected, true);
    nav.home();
    load();   // rebuilds only what changed, when quiet
    idleTouch();
    return true;
  }

  /* pixel shift against burn-in: a few px every two minutes */
  var shift = 0;
  setInterval(function () {
    shift = (shift + 1) % 9;
    var dx = (shift % 3) - 1, dy = Math.floor(shift / 3) - 1;
    $("ui").style.transform = "translate(" + dx * 2 + "px, " + dy * 2 + "px)";
  }, 120000);

  /* ---------- input ---------- */
  var KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13, BACK: 461, ESC: 27, BACKSPACE: 8,
              RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406 };
  document.addEventListener("keydown", function (e) {
    var k = e.keyCode;
    lastKeyAt = Date.now();
    if (idleWake()) { e.preventDefault(); return; }
    idleTouch();
    switch (k) {
      case KEY.LEFT: nav.move("left"); break;
      case KEY.UP: nav.move("up"); break;
      case KEY.RIGHT: nav.move("right"); break;
      case KEY.DOWN: nav.move("down"); break;
      case KEY.ENTER: activate(nav.current()); break;
      case KEY.BACK: case KEY.ESC: case KEY.BACKSPACE: nav.home(); break;
      case KEY.RED: if (hasDoor("jellyfin")) go("jellyfin", {}, "Jellyfin"); break;
      case KEY.GREEN: if (hasDoor("youtube")) go("youtube", {}, "YouTube"); break;
      case KEY.YELLOW: if (hasDoor("pc")) playOnPc(); break;
      case KEY.BLUE: openSettings("all"); break;
      default: return;
    }
    e.preventDefault();
  });
  var lastMouse = 0;
  document.addEventListener("mousemove", function () {
    var t = Date.now();
    if (t - lastMouse < 500) return;
    lastMouse = t;
    if (!idleWake()) idleTouch();
  });
  /* The item under the pointer. Hidden rooms' stages are transparent but still
     hit, so look through them (pointer only; keys never come here). */
  function pointed(e) {
    var els = document.elementsFromPoint ? document.elementsFromPoint(e.clientX, e.clientY) : [e.target];
    for (var i = 0; i < els.length; i++) {
      var st = els[i].closest && els[i].closest(".stage");
      if (st && !st.classList.contains("on")) continue;
      return els[i].closest ? els[i].closest("[data-item]") : null;
    }
    return null;
  }
  document.addEventListener("mouseover", function (e) {
    if (idle.active) return;
    var el = pointed(e);
    if (el && el !== nav.current()) nav.focusEl(el);
  });
  document.addEventListener("click", function (e) {
    if (idleWake()) return;
    idleTouch();
    var el = pointed(e);
    if (el) { nav.focusEl(el); activate(el); }
  });

  /* ---------- start ---------- */
  tickClock();
  idleTouch();
  nav.setScope(stages[selected]);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { doorsDirty = true; flush(); });
  load();
  setInterval(function () { if (!idle.active && !pc.busy) load(); }, REFRESH_MS);
  if (params.get("idle") === "1") setTimeout(idleEnter, 1500);
})();
