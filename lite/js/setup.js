/* TV Home lite: the Set up screen, driven by the remote.

   Up/Down move between rows, Left/Right between the items of a row (inside a
   text box they move the cursor until it reaches an end), Enter types into a
   box (webOS shows its own keyboard) or presses a button, the red button
   saves, Back closes without saving. Settings are kept in localStorage
   (lite/js/lite.js); a Jellyfin password is used once to sign in and only the
   token is kept. ES5 only. */
(function () {
  "use strict";
  var S = TVLite.sources;
  var KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13, BACK: 461, ESC: 27, RED: 403 };
  var root = null, scroller = null, rows = [], rowIdx = 0, colIdx = 0, open = false, firstRun = false, busy = false;
  var draft = null;

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") el.className = attrs[k];
      else if (k === "text") el.textContent = attrs[k];
      else if (k.indexOf("data-") === 0 || k === "placeholder" || k === "type" || k === "spellcheck" || k === "autocomplete") el.setAttribute(k, attrs[k]);
      else el[k] = attrs[k];
    });
    (kids || []).forEach(function (c) { if (c) el.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return el;
  }
  function copy(o) { return JSON.parse(JSON.stringify(o)); }

  /* ---------- building the form ---------- */
  var fields = {};   // name -> input
  var notes = {};    // name -> status element
  function field(name, label, value, opts) {
    opts = opts || {};
    var input = h("input", { class: "su-input" + (opts.wide ? " wide" : ""), type: opts.password ? "password" : "text",
                             value: value || "", placeholder: opts.placeholder || "", spellcheck: "false", autocomplete: "off" });
    fields[name] = input;
    notes[name] = h("span", { class: "su-note", text: opts.note || "" });
    return h("div", { class: "su-row", "data-row": "" }, [h("label", { class: "su-label", text: label }), input, notes[name]]);
  }
  function buttons(list, statusName) {
    var kids = list.map(function (b) {
      return h("button", { class: "su-btn" + (b.primary ? " primary" : ""), text: b.text, onclick: function () { run(b.fn, statusName); } });
    });
    if (statusName) { notes[statusName] = h("span", { class: "su-status" }); kids.push(notes[statusName]); }
    return h("div", { class: "su-row su-actions", "data-row": "" }, [h("span", { class: "su-label" })].concat(kids));
  }
  function section(title, help, kids) {
    return h("section", { class: "su-sec" }, [h("h2", { text: title }), help ? h("p", { class: "su-help", text: help }) : null].concat(kids));
  }
  function say(name, text, bad) {
    var el = notes[name];
    if (!el) return;
    el.textContent = text || "";
    el.className = el.className.replace(/ (bad|good)/g, "") + (text ? (bad ? " bad" : " good") : "");
  }

  function build() {
    var c = draft;
    fields = {}; notes = {};
    var body = h("div", { class: "su-body" }, [
      section("Weather", "Type your town. Left empty, TV Home guesses it from the internet connection.", [
        field("city", "Town", c.weather.city, { placeholder: "e.g. Brno" }),
        buttons([{ text: "Find town", fn: findCity }], "city-status")
      ]),
      section("YouTube", "Channels whose new videos you want here: their @names from YouTube, separated by commas. Videos open in the YouTube app.", [
        field("channels", "Channels", c.youtube.channels, { wide: true, placeholder: "@channel, @another" }),
        buttons([{ text: "Check channels", fn: checkChannels }], "channels-status")
      ]),
      section("Jellyfin", "Your Jellyfin server's address, as in the Jellyfin app. The password is used once to sign in and not stored.", [
        field("jfUrl", "Server", c.jellyfin.url, { placeholder: "http://192.0.2.10:8096" }),
        field("jfUser", "User", c.jellyfin.user),
        field("jfPass", "Password", "", { password: true, placeholder: "empty if none" }),
        buttons([{ text: "Sign in", fn: jfSignIn }, { text: "Sign out", fn: jfSignOut }], "jf-status")
      ]),
      section("Steam and the gaming PC", "Recently played games need a free Steam Web API key (steamcommunity.com/dev/apikey) and your 17-digit SteamID, with game details public. Waking the PC needs its MAC address and Wake-on-LAN switched on in the PC.", [
        field("steamKey", "Web API key", c.steam.apiKey, { wide: true }),
        field("steamId", "SteamID", c.steam.steamId, { placeholder: "7656119..." }),
        field("mac", "PC MAC address", c.steam.mac, { placeholder: "aa:bb:cc:dd:ee:ff" }),
        field("broadcast", "Broadcast address", c.steam.broadcast, { placeholder: "optional, e.g. 192.0.2.255" }),
        field("pcIp", "PC address", c.steam.pcIp, { placeholder: "optional: wait for the PC before Moonlight" }),
        field("hostUuid", "Moonlight host id", c.steam.hostUuid, { placeholder: "optional" }),
        field("appId", "Moonlight app id", c.steam.appId, { placeholder: "optional, e.g. Steam Big Picture's" }),
        buttons([{ text: "Wake the PC now", fn: wakeNow }], "pc-status")
      ]),
      section("Apps", "The apps the tabs open. Change only if yours have other ids; empty hides the tab.", [
        field("appJellyfin", "Jellyfin", c.apps.jellyfin),
        field("appYoutube", "YouTube", c.apps.youtube),
        field("appMoonlight", "Moonlight", c.apps.moonlight)
      ]),
      section("Developer Mode", "LG switches Developer Mode off after 1000 hours (about 41 days) unless it is extended, and then removes TV Home. TV Home extends it each time it opens, when this TV allows it.", [
        h("div", { class: "su-row su-info", "data-row": "" }, [h("span", { class: "su-label", text: "Status" }),
          notes.devmode = h("span", { class: "su-status", text: "Checking…" })]),
        buttons([
          { text: "Extend now", fn: renewNow },
          { text: c.devmode.autoRenew !== false ? "Extend on open: on" : "Extend on open: off", fn: toggleRenew },
          { text: "Open LG's Developer Mode app", fn: openDevApp }
        ], "dev-status")
      ]),
      h("div", { class: "su-row su-actions su-final", "data-row": "" }, [
        h("span", { class: "su-label" }),
        h("button", { class: "su-btn primary", text: "Save", onclick: function () { run(save, "save-status"); } }),
        h("button", { class: "su-btn", text: "Cancel", onclick: function () { close(false); } }),
        notes["save-status"] = h("span", { class: "su-status" })
      ])
    ]);
    scroller = h("div", { class: "su-scroll" }, [body]);
    root = h("div", { id: "setup", class: "su" }, [
      h("header", { class: "su-head" }, [
        h("h1", { text: "Set up" }),
        h("p", { class: "su-intro", text: firstRun
          ? "Welcome to TV Home. Everything here is optional: fill in what you use and press Save. Set up stays at the top right of the home screen."
          : "Fill in what you use and press Save." })
      ]),
      scroller,
      h("footer", { class: "su-foot", text: "Enter types or presses   ·   Red button saves   ·   Back closes without saving" })
    ]);
    document.body.appendChild(root);
    rows = Array.prototype.slice.call(root.querySelectorAll("[data-row]")).filter(function (r) { return items(r).length; });
    showApps();
    showDevmode();
    if (c.jellyfin.creds) say("jf-status", "Signed in as " + c.jellyfin.creds.userName);
    if (c.weather.place) say("city-status", placeName(c.weather.place));
  }
  function items(row) {
    return Array.prototype.slice.call(row.querySelectorAll("input, button"));
  }

  /* ---------- focus ---------- */
  function focusAt(r, c) {
    rowIdx = Math.max(0, Math.min(rows.length - 1, r));
    var its = items(rows[rowIdx]);
    colIdx = Math.max(0, Math.min(its.length - 1, c));
    var el = its[colIdx];
    Array.prototype.forEach.call(root.querySelectorAll(".focus"), function (e) { e.classList.remove("focus"); });
    el.classList.add("focus");
    rows[rowIdx].classList.add("focus");
    if (document.activeElement && document.activeElement !== el && document.activeElement.blur) document.activeElement.blur();
    if (el.tagName === "BUTTON") el.focus();
    // keep the row in the middle third of the view
    var top = rows[rowIdx].offsetTop, hView = scroller.clientHeight;
    var want = Math.max(0, top - hView / 2 + 60);
    scroller.scrollTop = want;
  }
  function current() { return items(rows[rowIdx])[colIdx]; }
  function editing() { var el = current(); return el && el.tagName === "INPUT" && document.activeElement === el; }

  function onKey(e) {
    if (!open) return;
    var k = e.keyCode, el = current();
    var stop = function () { e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation(); };
    if (k === KEY.RED) { stop(); run(save, "save-status"); return; }
    if (k === KEY.BACK || k === KEY.ESC) {
      stop();
      if (editing()) { el.blur(); return; }
      close(false);
      return;
    }
    if (k === KEY.UP || k === KEY.DOWN) {
      stop();
      if (editing()) el.blur();
      focusAt(rowIdx + (k === KEY.UP ? -1 : 1), colIdx);
      return;
    }
    if (k === KEY.LEFT || k === KEY.RIGHT) {
      if (editing()) {
        var pos = null;
        try { pos = el.selectionStart; } catch (err) { pos = null; }
        var atEdge = pos === null || (k === KEY.LEFT ? pos === 0 : pos >= el.value.length);
        if (!atEdge) { e.stopPropagation(); return; }   // move the text cursor
        el.blur();
      }
      stop();
      var n = items(rows[rowIdx]).length;
      var c = colIdx + (k === KEY.LEFT ? -1 : 1);
      if (c >= 0 && c < n) focusAt(rowIdx, c);
      return;
    }
    if (k === KEY.ENTER) {
      stop();
      if (!el) return;
      if (el.tagName === "INPUT") {
        if (editing()) { el.blur(); focusAt(rowIdx + 1, 0); }
        else { el.focus(); try { el.setSelectionRange(el.value.length, el.value.length); } catch (err) { /* password/number */ } }
      } else {
        el.click();
      }
      return;
    }
    // Any other key (digits, letters from a keyboard) goes to a focused text box.
    if (el && el.tagName === "INPUT" && !editing()) el.focus();
    e.stopPropagation();
  }

  /* ---------- actions ---------- */
  function run(fn, statusName) {
    if (busy) return;
    busy = true;
    say(statusName, "Working…");
    Promise.resolve().then(fn).then(function (msg) {
      busy = false;
      if (typeof msg === "string") say(statusName, msg);
      else if (msg === undefined) say(statusName, "");
    }, function (err) {
      busy = false;
      say(statusName, err && err.message || String(err), true);
    });
  }
  function val(name) { return String(fields[name].value || "").trim(); }
  function placeName(p) { return p ? p.name + (p.country ? ", " + p.country : "") : ""; }

  function findCity() {
    var name = val("city");
    if (!name) {
      draft.weather.place = null;
      return S.ipLocation(TVLite.http).then(function (p) {
        return p ? "Guessed from the connection: " + placeName(p) : "Couldn't guess the town; type it";
      });
    }
    return S.geocode(TVLite.http, name).then(function (list) {
      if (!list.length) throw new Error("No town called \"" + name + "\" found");
      draft.weather.place = list[0];
      draft.weather.city = name;
      return "Found " + placeName(list[0]);
    });
  }
  function checkChannels() {
    var tokens = S.parseChannels(val("channels"));
    if (!tokens.length) return "No channels entered";
    return S.pool(tokens, 3, function (t) { return S.resolveChannel(TVLite.http, t); }).then(function (ids) {
      var missing = tokens.filter(function (t, i) { return typeof ids[i] !== "string"; });
      if (missing.length) throw new Error("Not found: " + missing.join(", "));
      return "All " + tokens.length + " found";
    });
  }
  function jfSignIn() {
    var url = val("jfUrl"), user = val("jfUser");
    if (!url || !user) throw new Error("Server and user first");
    return S.jfSignIn(TVLite.http, url, user, fields.jfPass.value, draft.deviceId).then(function (creds) {
      fields.jfPass.value = "";
      fields.jfUrl.value = creds.url;
      draft.jellyfin = { url: creds.url, user: user, creds: creds };
      return "Signed in as " + creds.userName;
    }, function (e) {
      throw new Error(/HTTP|network|timed out|ENOTFOUND|ECONNREFUSED|EHOSTUNREACH/i.test(e.message)
        ? "Couldn't reach " + url + " (" + e.message + ")" : e.message);
    });
  }
  function jfSignOut() {
    var creds = draft.jellyfin.creds;
    draft.jellyfin.creds = null;
    if (!creds) return "Not signed in";
    return S.jfSignOut(TVLite.http, creds, draft.deviceId).then(function () { return "Signed out"; }, function () { return "Signed out"; });
  }
  function wakeNow() {
    var mac = val("mac");
    if (!mac) throw new Error("Enter the PC's MAC address first");
    var bc = val("broadcast").split(/[\s,]+/).filter(Boolean);
    return TVLite.call("wol", { mac: mac, broadcast: bc }, 8000).then(function (r) {
      return "Sent to " + (r.sent || []).join(", ");
    });
  }
  function renewNow() {
    say("devmode", "Checking…");
    return TVLite.checkDevmode(true).then(function (d) {
      showDevmode();
      if (d.error && !d.renewed) throw new Error(d.error);
      return d.renewed ? "Extended" : "";
    });
  }
  function toggleRenew(btn) {
    draft.devmode.autoRenew = draft.devmode.autoRenew === false;
    var b = current();
    if (b && b.tagName === "BUTTON") b.textContent = draft.devmode.autoRenew ? "Extend on open: on" : "Extend on open: off";
    return "Saved with Save";
  }
  function openDevApp() {
    return window.tvHome.launch ? window.tvHome.luna("luna://com.webos.applicationManager/launch", { id: "com.palmdts.devmode" })
      .then(function () { return ""; }) : "";
  }
  function showDevmode() {
    TVLite.devmode().then(function (d) {
      if (!notes.devmode) return;
      if (typeof d.hours === "number") {
        var days = Math.floor(d.hours / 24);
        notes.devmode.textContent = days + " days left" + (d.renewed ? ", extended when TV Home opened" : "") +
          (d.error ? " (" + d.error + ")" : "");
        notes.devmode.className = "su-status " + (days < 7 ? "bad" : "good");
      } else {
        notes.devmode.textContent = "Unknown on this TV (" + (d.error || "no answer") + "). Open LG's Developer Mode app and press Extend about once a month.";
        notes.devmode.className = "su-status bad";
      }
    });
  }
  function showApps() {
    TVLite.installed().then(function (list) {
      [["appJellyfin", "jellyfin"], ["appYoutube", "youtube"], ["appMoonlight", "moonlight"]].forEach(function (p) {
        if (!list) { say(p[0], ""); return; }
        var id = val(p[0]);
        say(p[0], list.indexOf(id) >= 0 ? "Installed" : "Not found on this TV", list.indexOf(id) < 0);
      });
    });
  }

  function collect() {
    var c = draft;
    c.weather.city = val("city");
    c.youtube.channels = S.parseChannels(val("channels")).join(", ");
    c.jellyfin.url = val("jfUrl");
    c.jellyfin.user = val("jfUser");
    c.steam = { apiKey: val("steamKey"), steamId: val("steamId"), mac: val("mac"), broadcast: val("broadcast"),
                pcIp: val("pcIp"), hostUuid: val("hostUuid"), appId: val("appId") };
    c.apps = { jellyfin: val("appJellyfin"), youtube: val("appYoutube"), moonlight: val("appMoonlight") };
    return c;
  }
  function save() {
    var before = TVLite.config();
    var c = collect();
    var steps = [];
    if (c.steam.mac) { try { validMac(c.steam.mac); } catch (e) { return Promise.reject(e); } }
    if (c.steam.steamId && !/^\d{17}$/.test(c.steam.steamId)) return Promise.reject(new Error("The SteamID is the 17-digit number (7656119...)"));
    if (c.weather.city && (!c.weather.place || c.weather.city !== before.weather.city)) steps.push(findCity);
    if (!c.weather.city) c.weather.place = null;
    var cr = c.jellyfin.creds;
    var jfChanged = c.jellyfin.url && c.jellyfin.user &&
      (!cr || S.jfBase(c.jellyfin.url) !== cr.url || c.jellyfin.user !== before.jellyfin.user || fields.jfPass.value);
    if (jfChanged) steps.push(jfSignIn);
    if (!c.jellyfin.url) c.jellyfin.creds = null;
    var p = Promise.resolve();
    steps.forEach(function (s) { p = p.then(s); });
    return p.then(function () {
      TVLite.saveConfig(c);
      close(true);
      return "Saved";
    });
  }
  function validMac(mac) {
    if (!/^([0-9a-f]{2}[:-]?){5}[0-9a-f]{2}$/i.test(mac)) throw new Error("The MAC address looks like aa:bb:cc:dd:ee:ff");
  }

  /* ---------- open / close ---------- */
  function show(isFirst) {
    if (open) return;
    firstRun = !!isFirst;
    draft = copy(TVLite.config());
    build();
    open = true;
    document.body.classList.add("setup-open");
    focusAt(0, 0);
  }
  function close(saved) {
    if (!open) return;
    if (!saved && firstRun) TVLite.saveConfig(draft ? collectSafe() : TVLite.config());
    open = false;
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    root.parentNode.removeChild(root);
    root = null;
    document.body.classList.remove("setup-open");
    if (saved || firstRun) window.location.reload();
  }
  function collectSafe() {
    // First run closed with Back: keep what was typed except a half-done Jellyfin sign-in.
    var c = collect();
    if (!c.jellyfin.creds) { c.jellyfin.url = ""; c.jellyfin.user = ""; }
    if (c.weather.city && !c.weather.place) c.weather.city = "";
    return c;
  }

  window.addEventListener("keydown", onKey, true);
  TVH.actions = TVH.actions || {};
  TVH.actions.setup = function () { show(false); };
  TVLite.setup = { show: show, close: close, isOpen: function () { return open; } };
  if (!TVLite.hasConfig()) setTimeout(function () { show(true); }, 600);
})();
