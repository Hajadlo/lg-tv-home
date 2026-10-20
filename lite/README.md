# TV Home, TV only

The TV-only edition of TV Home: one app you install on an LG TV, with no
server or other computer involved. It shows the same home page as the server
version (clock, weather, Jellyfin, YouTube and Steam tabs, TV settings
shortcuts) and is set up on the TV with the remote.

How it compares with the server version: [../docs/editions.md](../docs/editions.md).

## Install (plug and play)

You need an LG TV with webOS and a free LG account. Allow about 15 minutes.

1. **Developer Mode.** On a computer, create an account at
   <https://webostv.developer.lge.com> (Sign in > Create account). On the TV,
   install **Developer Mode** from the LG Content Store, open it, sign in with
   that account and switch **Dev Mode Status** on. The TV restarts. Open
   Developer Mode again and switch **Key Server** on; leave the app open, it
   shows the TV's IP address and a passphrase for the next step.
2. **Install TV Home.** Download `com.tvhome.lite_<version>_all.ipk` from the
   [latest release](https://github.com/Hajadlo/lg-tv-home/releases/tag/lite-v0.1.0).
   The easiest tool is **webOS Dev Manager**
   (<https://github.com/webosbrew/dev-manager-desktop/releases>, Windows,
   macOS, Linux): add the TV (it asks for the IP and passphrase from step 1),
   then open **Apps**, press **Install** and pick the `.ipk`. With LG's command
   line tools instead:
   `ares-setup-device` once, then `ares-install --device <tv> com.tvhome.lite_0.1.0_all.ipk`.
3. **Open "TV Home"** from the TV's app list. The first time, the Set up
   screen opens by itself.
4. **Set up.** Fill in what you use and press **Save** (or the red button).
   Everything is optional; with nothing filled in you still get the clock,
   weather for your area, TV settings shortcuts and a tab per app found on the
   TV. Set up stays at the top right of the home page.

On the remote: Up/Down move between rows, Left/Right along a row, OK starts
typing into a box (the TV's keyboard appears) or presses a button, Back closes
Set up without saving.

## What to fill in

| Part | What it needs | Where to find it |
|---|---|---|
| Weather | Your town (optional) | Left empty, the town is guessed from the internet connection |
| YouTube | Channel @names, separated by commas | The channel's page in the YouTube app or on youtube.com (`@name`) |
| Jellyfin | Server address, user, password | As in the Jellyfin app (e.g. `http://192.0.2.10:8096`). The password is used once; only a sign-in token is kept |
| Steam games | Steam Web API key and SteamID (17 digits) | Key: <https://steamcommunity.com/dev/apikey>. SteamID: your profile URL, or a site such as steamid.io. Steam profile > Privacy: **Game details** must be Public |
| Waking the PC | The PC's MAC address; broadcast and PC address optional | Wake-on-LAN on in the PC's BIOS and network adapter. With the PC's IP address filled in, TV Home waits until the PC answers before opening Moonlight |
| Moonlight straight to Big Picture | Moonlight host id and app id (optional) | Without them Moonlight opens its host list |
| Apps | App ids, if yours differ | Defaults: `org.jellyfin.webos`, `youtube.leanback.v4`, `com.limelight.webos` |

Videos open in the TV's YouTube app; with **YouTube AdFree**
(<https://github.com/webosbrew/youtube-webos>, installed the same way as TV
Home, replaces LG's YouTube app) they play without ads. Turn on its "Bypass
initial account selection on startup" (green button inside YouTube AdFree)
so a video starts in one press. Steam games are streamed with **Moonlight TV**
(<https://github.com/mariotaku/moonlight-tv>) from a PC running Sunshine.

## What works and what doesn't

Checked on a 2025 LG OLED (webOS 25, Chromium 132, Node 20) unless marked
*(not yet checked on a TV)*; those parts are covered by tests and a desktop
browser run.

| | |
|---|---|
| Install as its own app (`com.tvhome.lite`, "TV Home"), service inside it | Yes |
| Home page: clock, weather, tabs, rows, TV settings shortcuts | Yes in a desktop browser *(not yet checked on a TV screen)* |
| Set up on the TV, first-run prompt | Opens on first start (checked); typing with the TV keyboard *(not yet checked)* |
| Weather: town search, or a guess from the connection | Yes (Open-Meteo; guess via geojs.io, then ipwho.is) |
| YouTube: new videos from a channel list, Shorts left out | Yes (public channel feeds; @names resolved from the channel page) |
| YouTube: follow your subscriptions, hide watched videos, Continue watching | No (needs your YouTube login; the server version does it) |
| Jellyfin: sign in, Continue watching, Next up, Recently added | Yes (sign-in checked on the TV) |
| Jellyfin: start the picked episode | Through Jellyfin's remote control, run by the app's service *(not yet checked on a TV)* |
| Steam: recently played games | Through the Steam Web API *(checked with recorded answers only)* |
| Steam: wake the PC, open Moonlight (optionally straight into Big Picture) | Wake-on-LAN from the TV *(not yet checked on a TV)*; no Sunshine restart |
| Tabs only for apps on the TV | No: webOS refuses an app the app list, so every app with an id gets a tab; clear an id in Set up to hide its tab |
| Home button and power-on open TV Home | No: open it from the app list |
| Developer Mode | Extended automatically each time TV Home opens (checked) |
| Older TVs (webOS 4-6) | Built for them (ES5, CSS fallbacks), *not yet checked on one*; see below |

Older TVs: the code is plain ES5 and the CSS is rewritten for Chromium 53
(webOS 4.x) with fallbacks for CSS grid, flexbox `gap`, `inset` and
`rgb(... / a)` colours. Still newer than webOS 4: the fonts are variable fonts
(older engines show one weight, so headings look less refined) and
`font-display`; nothing that breaks the layout.

## Developer Mode

LG ends Developer Mode after 1000 hours (about 41 days) unless it is extended;
at the next restart after that, the TV removes TV Home and every other app
installed this way. TV Home extends it itself each time it opens: its service reads the
TV's Developer Mode session token and asks LG to reset the session, the same
call LG's Developer Mode app makes for "Extend". Opening TV Home at least once
a month is enough. Set up shows the days left, and the home page shows a
reminder when fewer than 7 remain. If a TV refuses this, Set up says so: then
open LG's Developer Mode app and press **Extend** about once a month (Set up
has a shortcut to it).

## Updating

Install the newer `.ipk` the same way (Dev Manager or `ares-install`); it
replaces the app and keeps your settings. Releases:
<https://github.com/Hajadlo/lg-tv-home/releases>.

## For developers

```sh
lite/build.py               # assemble lite/build/{app,service} and package lite/dist/*.ipk (needs ares-package)
lite/build.py --no-package  # only assemble
node --test lite/tests/*.test.js   # data layer and service tests (also run by ./check.sh)
```

| Path | What |
|---|---|
| `build.py` | Copies `../ui` (page, CSS rewritten for older TVs), `../tv/app/bridge.js` and the pieces below into one app, then `ares-package` |
| `js/sources.js` | Data layer: Open-Meteo, YouTube feeds, Jellyfin, Steam Web API, into the server's `/api/home` shape |
| `js/lite.js` | `TVH.api` for `ui/js/app.js`: settings, refresh timing, features, Wake-on-LAN, Dev Mode |
| `js/setup.js`, `css/setup.css` | The Set up screen |
| `js/compat.js`, `css/compat.css` | Fallbacks for older engines (CSS grid, flexbox gap) |
| `service/` | The JS service inside the app (`luna://com.tvhome.lite.service`): HTTP without CORS, Wake-on-LAN, TCP check, Dev Mode renewal, app list, Jellyfin remote play. ES5 for old Node |
| `app/appinfo.json` | App id `com.tvhome.lite`, title "TV Home" |
