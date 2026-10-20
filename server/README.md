# TV Home server

Serves the home page (`../ui/`) and its JSON API, by default on
`0.0.0.0:8797`. Python 3.11+ standard library, plus Pillow if it is installed
(it scales images and trims logos; without it images are served as they are
and `logoDark` stays `null`). Settings come from
`~/.config/tv-home/config.toml` (see `../config.example.toml`).

| File | Role |
|---|---|
| `tvhome_server.py` | HTTP server, routes, static files, wiring (entry point) |
| `tvconfig.py` | Loads config.toml, decides which sources are on; `tvconfig.py check` |
| `refresher.py` | Background refresh that serves the last good value |
| `jellyfin.py` | API key bootstrap, the three rows, dedupe |
| `jf_remote.py` | Plays a Jellyfin item on the TV through Jellyfin's remote control |
| `images.py` | Jellyfin image proxy and public-image cache on disk; Pillow shaping; `logoDark` |
| `weather.py` | Open-Meteo forecast, WMO mapping, rain note |
| `pc.py` | Gaming PC reachability and the Play job (Wake-on-LAN, Sunshine) |
| `youtube.py` | Channel list, RSS feeds, Shorts and watched filter |
| `yt_cookies.py` | Copies the YouTube cookies from a Firefox-family browser profile |
| `innertube.py` | Read-only InnerTube calls with those cookies, answer parsing |
| `yt_watched.py` | Watched state, Continue watching and channel list as the configured channel |
| `google_oauth.py`, `youtube-auth` | Optional OAuth fallback for the channel list |
| `steam.py` | Recently played games read from the PC over ssh, Steam art URLs |
| `net.py`, `notify.py` | HTTP helper and mode-600 JSON files; ntfy alerts |

Run by hand: `python3 tvhome_server.py [--port N] [--host H] [--debug] [--yt-channels UC...,UC...]`.
One log line per request; the line for `GET /` includes the browser's
User-Agent, which tells you the TV's Chromium version.

## Sources and what turns them on

| Source | On when | Tab |
|---|---|---|
| Jellyfin | `[jellyfin] url` and `user` | Jellyfin |
| YouTube | `[youtube] channel` + `browser_profile`, or `subscriptions_csv`, or an OAuth client file | YouTube |
| Steam | `[steam] pc_ip`, `pc_mac` and `moonlight_host_uuid` | Steam |
| Weather | `[weather] latitude` and `longitude` | (top left line) |

A source that is off is never fetched, its key in `/api/home` is `null`, and
`features` tells the UI not to build its tab.

## API

All answers carry `Access-Control-Allow-Origin: *`.

| Endpoint | Answer |
|---|---|
| `GET /` and other paths | Files from `../ui/` (`no-cache` plus `Last-Modified`; images and fonts cached a week) |
| `GET /tv/bridge.js` | `../tv/app/bridge.js` with the settings line filled from config (404 without `[server] url`) |
| `GET /api/home` | The home payload below. Never waits on a source: each refreshes on its own thread and keeps its last good value |
| `GET /api/pc` | `{"state", "sunshine", "ready", "phase", "error"}` (404 when Steam is off) |
| `POST /api/pc/play` | Starts the Play job (or joins it) and returns `{"state", "sunshine"}` at once |
| `POST /api/jellyfin/open/<itemId>` | Waits for the TV's Jellyfin app to connect, then tells it to play (or show) the item |
| `GET /api/img/<itemId>/<kind>?w=` | Jellyfin image (`backdrop`, `thumb`, `poster`, `logo`, `primary`); `w` rounds up to 160 px steps |
| `GET /api/yt/<videoId>/thumb?w=` | YouTube thumbnail (maxres, else hq) |
| `GET /api/steam/<appid>/<kind>?w=` | Steam art: `cover` 600x900, `hero` 1920x620, `logo`, `header` |
| `GET /api/health` | Per-source state, errors and ages; never cookie values |

`/api/home`:

```json
{
  "now": "2026-10-20T19:42:00+02:00",
  "features": {"jellyfin": true, "youtube": true, "steam": true, "weather": true},
  "weather": {"tempC": 11, "code": "rain", "label": "Light rain", "highC": 14, "lowC": 7, "note": "Rain until 21:00"},
  "jellyfin": {"resume": [Item], "nextUp": [Item], "latest": [Item]},
  "pc": {"state": "off|waking|on", "sunshine": "listening|down|unknown"},
  "system": {"hddFreeGb": 412, "hddTotalGb": 1863},
  "youtube": {"updated": "…", "channelCount": 42, "auth": "ok|missing|expired",
              "watched": "ok|unavailable", "videos": [Video], "continue": [Video]},
  "steam": {"updated": "…", "recent": [Game]}
}
```

- `Item`: `id`, `kind` (`episode|movie|series`), `title`, `subtitle`
  (`S2 · E5 · Name` for episodes), `year`, `progress` (0-1), `minutesLeft`,
  `backdrop`, `thumb`, `poster`, `logo` (relative image URLs), `logoDark`
  (the logo is too dark for the page; the UI sets the title in type).
- `Video`: `id`, `title`, `channel`, `channelId`, `published`, `views`,
  `progress` (0-1 or `null`), `thumb`.
- `Game`: `appid`, `name`, `lastPlayed`, `playtimeH`, `cover`, `hero`,
  `logo`, `header`, `logoDark`.

## How each source works

**Jellyfin.** Resume, Next up and Recently added (per library, merged newest
first), every 60 s and early when a request finds data older than 15 s. A
title appears once across the rows. On first start the server signs in as
`[jellyfin] user` with a blank password, reuses or creates an API key named
`tv-home`, stores it in `~/.config/tv-home/jellyfin.json` (600) and logs that
session out. To use a password, run the bootstrap once by hand:

```sh
cd server && python3 -c 'import getpass, jellyfin, tvconfig; c = tvconfig.load()["jellyfin"]; jellyfin.bootstrap(tvconfig.CONFIG_DIR / "jellyfin.json", c["url"], c["user"], getpass.getpass())'
```

Rotate the key: revoke `tv-home` in Jellyfin (Dashboard > API Keys), delete
`jellyfin.json`, restart the service.

The webOS Jellyfin app ignores launch parameters, so picking an item on the
home page opens Jellyfin and the server then plays the item through
Jellyfin's remote control (`jf_remote.py`) once the TV's Jellyfin session
appears (up to 30 s).

**YouTube.** Channels every 6 h; videos from each channel's public RSS feed
(15 newest) every 30 min, newest first, 30 shown. Shorts are dropped (one
`HEAD /shorts/<id>` per new video, cached). With the browser login
(`[youtube] channel` + `browser_profile`):

- About hourly the server copies `cookies.sqlite` (+ `-wal`) of that profile
  (locally, or with one read-only `ssh <ssh_host>` call while the host is
  reachable), keeps only the `.youtube.com` cookies in
  `~/.config/tv-home/yt-cookies.json` (600) and deletes the copy.
- It then calls YouTube's own web API (InnerTube) read-only, as the channel
  named in the account switcher: `FEchannels` (subscriptions),
  `FEsubscriptions` and `FEhistory` (the red progress bars). Nothing that
  plays, writes history or reports watch time is called.
- Videos watched 90 % or more are left out; partly watched ones carry
  `progress`. "Continue watching" lists up to five videos from the last week
  of history that are 11-90 % watched, not music.
- When the login stops working (signed out, cleared cookies, wrong channel
  name) the list falls back to the unfiltered RSS list and one ntfy goes out
  (when `[ntfy] url` is set). Opening youtube.com in that browser, signed in,
  fixes it within the hour; no restart.

Without the browser login, the channel list comes from `subscriptions_csv`
(Google Takeout) or from Google OAuth: put an OAuth client of type "TVs and
Limited Input devices" (scope `youtube.readonly`, Google's JSON download) at
`~/.config/tv-home/google-oauth-client.json` (600), run `./youtube-auth` and
follow its prompt. A Google Cloud project left in "Testing" expires the token
after 7 days; set it to "In production".

**Steam.** A monitor thread probes the PC (Sunshine port, and 22) every 15 s.
Play: if the PC is off, send Wake-on-LAN (resent every 30 s, up to 3 min);
wait for Sunshine (20 s, 45 s after a fresh boot); if it is still closed and
`ssh_host` + `sunshine_unit` are set, run
`ssh <ssh_host> systemctl --user restart <sunshine_unit>` once and wait 45 s
more. When ready, the server closes a suspended Moonlight on the TV over
ares (`../tv/close-app`), because Moonlight only honours "stream this host
and app" on a cold start, and the UI launches it. With `ssh_host` set, the
recently played games (12, tools such as Proton dropped) are read from the
PC's Steam files every 10 min while it is on, and kept in
`~/.cache/tv-home/steam.json` for when it sleeps. Art comes from Steam's CDN.

**Weather.** Open-Meteo every 15 min for `[weather]` latitude/longitude; the
note says when rain starts or stops in the next 12 h.

## Files

| Path | Contents |
|---|---|
| `~/.config/tv-home/config.toml` | Settings (no secrets) |
| `~/.config/tv-home/jellyfin.json` (600) | Jellyfin url, user id, API key |
| `~/.config/tv-home/yt-cookies.json` (600) | `.youtube.com` cookies; secret |
| `~/.config/tv-home/google-oauth-*.json` (600) | Optional OAuth client and token |
| `~/.config/tv-home/ssap-client-key` (600) | TV network-control key (`../tv/ssap_pair.py`) |
| `~/.config/tv-home/devmode-token` (600) | Cached Dev Mode session token |
| `~/.cache/tv-home/` | Last results, image caches (pruned to 400 MB and 300 MB) |

`TVHOME_CONFIG` overrides the config file, `TVHOME_CONFIG_DIR` and
`TVHOME_CACHE_DIR` the two directories.
