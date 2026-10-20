# TV Home: the webOS side

A thin packaged app on the TV that shows the page served by the TV Home
server, plus the scripts that keep it in front and keep Developer Mode alive.
Setup steps are in `../SETUP.md`; this file explains how it works.

| Path | What |
|---|---|
| `app/` | The webOS app: `appinfo.json`, icons, `index.html` + `shell.js` + `shell.css` (probe, offline fallback), `bridge.js` (`window.tvHome`) |
| `render.py` | Fills `bridge.js` and `appinfo.json` from config.toml (used by `install`) |
| `install` | Render, package with ares, install, launch |
| `autolaunch.py` | SSAP watcher: open TV Home on power-on; with `[tv] takeover`, on the Home button too |
| `ssap_pair.py` | One-time SSAP pairing (accept the prompt on the TV) |
| `close-app` | Closes an app over ares (Moonlight before a stream) |
| `devmode-renew` | Renews the Dev Mode session (daily timer) |

## How the app works

On launch the shell (`index.html`, from the app package) probes `[server] url`
for 2.5 s. If it answers, the shell navigates to it (`location.replace`), so
the page you see is served by the server and design changes need no
reinstall. If not, it shows a small packaged fallback (clock, app tiles,
settings) and retries every 10 s.

A page loaded this way keeps the app's webOS privileges: `PalmServiceBridge`
works in it and Luna calls run with the app's identity (checked on webOS 25).
That is how the page launches apps and opens settings. `window.tvHome` (from
`bridge.js`) reaches the page two ways:

1. `ui/index.html` loads `/tv/bridge.js` from the server at once. On the TV it
   defines `window.tvHome`; in a desktop browser it defines nothing and the UI
   loads its mock (`ui/js/bridge-mock.js`).
2. `install` also packs the same file as `webOSUserScripts/userScript.js`,
   which webOS injects into the app's top page after its `load` event, on
   versions that support user scripts. It sees the bridge already there and
   stops.

`appinfo.json` sets `handlesRelaunch` (launching TV Home while it runs fires
`webOSRelaunch`; `bridge.js` then calls `PalmSystem.activate()`, without
which it stays behind the current app) and `disableBackHistoryAPI` (Back
arrives as keydown 461).

## What the bridge does (checked on a 2025 LG OLED, webOS 25)

| Call | How |
|---|---|
| `tvHome.launch('jellyfin')` | `applicationManager/launch {id}`; the Jellyfin app reads no launch params, so items are played through Jellyfin's remote control by the server |
| `tvHome.launch('youtube', {videoId})` | `{id, params: {target: "v=<id>"}}`; YouTube AdFree (youtube-webos) opens that video, also when already running |
| `tvHome.launch('moonlight')` | `{id, params: {host_uuid, host_app_id}}`; moonlight-tv reads these only on a cold start, so the server closes a running Moonlight first (`close-app`) |
| `tvHome.openSettings(target)` | `com.palm.app.settings` with `target` `picture`, `sound`, `system` (Network lives under System), or `com.webos.app.quickinputpicker` for Inputs |
| `tvHome.lgHome()` | `com.webos.app.home` with `launchType: "homeKey"` |

Both `luna://com.webos.applicationManager/launch` and
`luna://com.webos.service.applicationmanager/launch` are tried. A dev app may
not list apps, read the foreground app or close other apps (`Denied method
call`); that is why `autolaunch.py` and `close-app` run on the server, over
SSAP and ares.

YouTube AdFree shows "Who's watching?" on every deep link unless its own
option "Bypass initial account selection on startup" is on (green button in
YouTube AdFree, then the option). Turn it on once if you want one-press video
starts.

## Auto-launch and the Home button (`autolaunch.py`)

Watches the TV's power state and foreground app over SSAP (aiowebostv, key
in `~/.config/tv-home/ssap-client-key`). A power-on is a change from an off
state (`Active Standby`, `Suspend`, `Power Off`) to `Active`; screen-off is
not. After a short settle it launches TV Home if the app in front is the one
webOS resumed or a passive one (LG home, Live TV, boot logo). It does nothing
if the user put another app in front meanwhile (Netflix button, cast, an
HDMI device over CEC).

With `[tv] takeover = true`, when LG's launcher comes to the front the
watcher relaunches TV Home (about half a second of LG's launcher shows
first). Leaving TV Home for LG's launcher on purpose ("LG apps" on the home
page, or the Home button while in TV Home) opens a 120 s bypass. If the
server is down, the Home button simply shows LG's launcher.

## Developer Mode (`devmode-renew`)

LG's Developer Mode session lasts up to 1000 h. When it runs out, the next
real reboot (unplugging, a firmware update; standby is not a reboot) turns
Developer Mode off and deletes every dev-installed app. `devmode-renew` reads
the session token from the TV over the Dev Mode SSH (`ares-novacom --run cat
/var/luna/preferences/devmode_enabled`, cached in
`~/.config/tv-home/devmode-token`), then calls LG's
`CheckDevModeSession.dev` and `ResetDevModeSession.dev` and prints one JSON
line with `before_hours` and `after_hours`. On failure, or under 72 h left,
it sends one ntfy and exits 1. `--check` only reports; `--via-app` uses LG's
visible method (`ares-launch com.palmdts.devmode -p extend=true`).

If the session has already expired, LG answers `ERR_005` and only a restart
of the TV plus switching Dev Mode on again in the Developer Mode app helps;
then reinstall the dev apps.

## Install and test

```sh
tv/install                    # render, package, install, launch
tv/install --no-launch
tv/install --ui-url http://<server-ip>:8799/     # point at a test server
ares-launch --device tv <app_id> -p '{"uiUrl":"…","mode":"navigate|iframe|fallback"}'
ares-install --device tv --remove <app_id>
```

Close the app first (`ares-launch --device tv -c <app_id>`) or launch params
arrive as a relaunch.
