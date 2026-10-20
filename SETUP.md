# Setting up TV Home (instructions for a coding agent)

You are setting up TV Home for a person who has an LG webOS TV and an
always-on Linux machine on the same network. You run on that machine, in a
clone of this repo. Read `README.md` first, then work through the steps in
order. Each step ends with a check; do not move on until it passes.

## Ground rules

- **Secrets.** Never print, log, paste or commit the SSAP key, the Dev Mode
  key/passphrase, cookies, OAuth tokens or the Jellyfin API key. They live in
  mode-600 files under `~/.config/tv-home/` and `~/.ssh/`. When you need to
  know whether one exists, test for the file, do not `cat` it.
- **The config holds no secrets** but does hold the person's addresses: it
  lives at `~/.config/tv-home/config.toml`, never in the repo.
- **Ask before anything on the TV that needs the remote** (accepting a
  pairing prompt, turning on Developer Mode, changing an app's settings,
  restarting the TV). Say exactly what they will see and what to press, then
  wait for them.
- **Don't wake or restart other machines** (the gaming PC) without asking.
- This TV may be older than the one TV Home was built on (a 2025 LG OLED on
  webOS 25). Step 1 finds out what differs; adapt rather than guess.
- After any change to this repo, run `./check.sh`.

## 0. The machine

```sh
hostname; python3 --version; command -v uv node npm ssh
```

Need: Python 3.11+ (`tomllib`), `uv` (for the SSAP helpers; install from
https://docs.astral.sh/uv/ if missing), Node.js 18+ and npm (for LG's `ares`
CLI). Pillow for Python is optional (nicer images): `python3 -c 'import PIL'`.

Find the machine's LAN address (`ip -4 addr`) and the TV's (router, or the
TV's Settings > General > Network > Wi-Fi/Wired > Advanced). Both should
have fixed leases; ask the person to set them in the router if they are not.

Check: `./check.sh` passes; `ping -c1 <tv-ip>` answers.

## 1. Know the TV

Ask the person for the TV model and the webOS version (Settings > General >
System > TV information / About this TV). webOS versions and their browser
engines:

| webOS | Years | Chromium |
|---|---|---|
| 3.x | 2016-2017 | 38 |
| 4.x | 2018-2019 | 53 |
| 5 | 2020 | 68 |
| 6 | 2021 | 79 |
| 22 | 2022 | 87 |
| 23 | 2023 | 94 |
| 24 | 2024 | 108 |
| 25 | 2025 | 120 |

What TV Home needs from the engine:

- `ui/js/` uses `fetch`, `Promise`, `URLSearchParams`, `Object.assign`,
  `Element.closest`, `Element.animate` and `Intl.DateTimeFormat` (all in
  Chromium 53+). The webOS app itself (`tv/app/`) is plain ES5 + Promise.
- `ui/css/home.css` uses `inset:` and flexbox `gap` (Chromium 84-87) and
  `rgb(r g b / a)` colours (Chromium 65). On webOS 6 or older the layout
  breaks without changes: replace `inset: X` with `top/right/bottom/left`,
  `gap` with margins, and `rgb(var(--x) / a)` with precomputed `rgba()`. On
  webOS 4 also check `ui/js/` for anything newer than Chromium 53.
- The exact engine shows up in the server log: once the app is installed
  (step 7), the `GET /` line carries the TV browser's User-Agent.

Two webOS features TV Home relies on, to verify on this TV in step 7:

1. **Luna calls from the hosted page.** The page is served by the TV Home
   server but shown inside the TV Home app, and launches apps through
   `PalmServiceBridge` (`tv/app/bridge.js`). Verified on webOS 25. If it is
   missing or denied on this TV, the fallback is the iframe mode the shell
   already has: launch with `-p '{"mode":"iframe"}'`, where the packaged shell
   runs the Luna calls for the framed page (`tvHome.proxied` is true); make
   it the default in `tv/app/shell.js` if that works.
2. **`webOSUserScripts` injection.** Not needed any more for the bridge
   (`ui/index.html` loads `/tv/bridge.js` from the server), but harmless.

## 2. Developer Mode and the ares CLI

1. Install LG's CLI: `npm i -g @webos-tools/cli`. Check: `ares --version`.
2. Ask the person to: create a free LG developer account
   (https://webostv.developer.lge.com), install **Developer Mode** from the
   TV's app store, open it, sign in, switch **Dev Mode Status** on (the TV
   restarts), open the app again and switch **Key Server** on. Ask them to
   read you the IP and passphrase shown in the app.
3. Register the TV (name it `tv`, or put another name in `[tv] ares_device`):
   `ares-setup-device -a tv -i "host=<tv-ip>" -i "port=9922" -i "username=prisoner"`
4. Fetch the key: `ares-novacom --device tv --getkey` (enter the passphrase
   when it asks; do not echo it anywhere else).

Check: `ares-device --device tv --system-info` prints the TV's model and
firmware (note `sdkVersion`), and `ares-install --device tv --list` answers.

Note: plain `ssh` to the TV may fail with modern OpenSSH (the TV only offers
`ssh-rsa`); ares works regardless, and TV Home only uses ares.

## 3. The apps on the TV

Ask which of Jellyfin, YouTube and Steam they want. Install what is missing:

- **Jellyfin**: from LG's app store, or the webOS build from
  https://github.com/jellyfin/jellyfin-webos/releases (`ares-install --device tv <ipk>`).
- **YouTube AdFree**: https://github.com/webosbrew/youtube-webos/releases
  (`ares-install --device tv <ipk>`).
- **Moonlight TV**: https://github.com/mariotaku/moonlight-tv/releases
  (or the webOS Homebrew Channel); pair it with the PC's Sunshine once, on the
  TV, with the person.

Then list the installed ids; they can differ from the defaults:

```sh
ares-install --device tv --list        # dev-installed apps
ares-launch --device tv --list         # everything launchable, if supported
```

Check: you have the ids for each wanted app (defaults: `org.jellyfin.webos`,
`youtube.leanback.v4`, `com.limelight.webos`).

## 4. config.toml

```sh
mkdir -p ~/.config/tv-home && chmod 700 ~/.config/tv-home
cp config.example.toml ~/.config/tv-home/config.toml
```

Fill in `[server] url` (this machine's LAN address and port), `[tv] ip`,
`ares_device`, the app ids under `[tv.apps]`, and `[weather]` (ask for the
town; look up its coordinates). Remove the sections of sources they do not
want; the optional sections are set up in step 9. Pick an `[tv] app_id`
such as `com.<theirname>.tvhome` (lowercase).

Check: `python3 server/tvconfig.py check` lists the expected sources as on.

## 5. The server as a service

```sh
systemd/install-units --print     # review
systemd/install-units             # writes ~/.config/systemd/user/tv-home*.{service,timer}
loginctl enable-linger "$USER"    # keep user services running without a login (may need sudo)
systemctl --user enable --now tv-home.service
```

Open the port if a firewall runs (`firewall-cmd`/`ufw`): TCP 8797 (or your
`[server] port`) from the LAN.

Check: `curl -s http://<lan-ip>:8797/api/health` shows `"features"` as
configured; `journalctl --user -u tv-home -n 30` shows no tracebacks;
`curl -s http://<lan-ip>:8797/tv/bridge.js | grep SETTINGS` shows your URL
and app ids.

## 6. SSAP pairing (network control)

The watcher that brings TV Home up on power-on talks to the TV over SSAP.
Tell the person a prompt will appear on the TV and they should press
**Accept** with the remote, then run:

```sh
uv run -q --no-project --with aiowebostv python -I tv/ssap_pair.py
```

Check: it prints "paired"; `test -s ~/.config/tv-home/ssap-client-key && echo ok`.

## 7. Install the TV app

```sh
tv/install
```

TV Home should open on the TV. Ask the person what they see. Then check:

- `journalctl --user -u tv-home -n 50 | grep 'GET / '` shows the TV's
  User-Agent; note its `Chrome/<version>` and compare with step 1.
- Ask them to move to a door and press Enter: the app opens (Luna works from
  the hosted page). If nothing opens, follow the iframe fallback in step 1.
- If the page looks broken (overlapping text, no layout), fix the CSS as in
  step 1, then reload (no reinstall needed for `ui/` changes; relaunch the
  app with `ares-launch --device tv <app_id>`).

## 8. Auto-launch, Home button, Dev Mode renewal

```sh
systemctl --user enable --now tv-home-autolaunch.service tv-home-devmode-renew.timer
tv/devmode-renew            # renews now; prints before_hours/after_hours (about 1000)
```

`[tv] takeover = true` makes the Home button open TV Home (restart
`tv-home-autolaunch` after changing it).

Check: `tv/devmode-renew --check` shows hours near 1000;
`journalctl --user -u tv-home-autolaunch -n 20` shows `"event": "start"`.
Ask the person to switch the TV off and on once: TV Home should come up; and
to press Home: TV Home again (with takeover).

Why the renewal matters: Developer Mode expires after at most 1000 h, and the
next real reboot then deletes TV Home, YouTube AdFree and Moonlight. If
renewal ever reports `ERR_005`, the session already expired: Dev Mode has to
be switched on again in the Developer Mode app and the apps reinstalled.

## 9. Optional parts

### Jellyfin

`[jellyfin] url` as this machine reaches Jellyfin, `user` the Jellyfin user.
A user without a password needs nothing else. With a password, run the
one-time bootstrap from `server/README.md` (it asks for the password and
stores only an API key). Check: `/api/health` shows `jellyfin.ok: true`;
the Jellyfin tab lists their shows.

### YouTube

Best: the browser login. Ask which machine and browser they use for YouTube
(Firefox-family only: Firefox, Zen, LibreWolf, Floorp), and which channel
name shows in YouTube's account switcher. Set `[youtube] channel` and
`browser_profile` (profile dir with `cookies.sqlite`, or the browser's root
with `installs.ini`). If the browser is on another machine, set `ssh_host`
and make sure key-based `ssh <host> true` works from here without a prompt.
Never open or print the cookie database; the server copies only the YouTube
cookies into a 600 file.

Fallbacks without a login: `subscriptions_csv` (Google Takeout, "YouTube and
YouTube Music" > subscriptions), or OAuth via `server/youtube-auth` (see
`server/README.md`).

Check (after `systemctl --user restart tv-home`, give it a minute):
`/api/health` shows `youtube.watched: "ok"` and a channel count. Suggest the
person turns on "Bypass initial account selection on startup" in YouTube
AdFree (green button) so a video starts in one press.

### Steam via Moonlight/Sunshine

Needs Sunshine on the gaming PC, Moonlight TV paired with it, and
Wake-on-LAN enabled in the PC's BIOS/OS. Fill `[steam]`:

- `pc_ip`, `pc_mac` (from the PC: `ip link`), `broadcast` (your LAN's, e.g.
  `192.0.2.255`; ask for theirs).
- `moonlight_host_uuid`: `curl -s http://<pc-ip>:47989/serverinfo` while the
  PC is on; the `<uniqueid>` value.
- `moonlight_app_id`: the Sunshine app Moonlight should start (e.g. "Steam
  Big Picture"). Sunshine does not show these ids in its web UI; they are in
  the answer of `/applist`, which needs a paired client. If you cannot read
  it reliably, leave `0`: Moonlight then opens the PC's app list and the
  person picks Steam with one more press. Verify whichever you choose on the
  TV with the person.
- Optional `ssh_host` (key login to the PC) for the Sunshine restart
  (`sunshine_unit`, the systemd user unit running Sunshine) and the recently
  played list (`steam_root`, `steam_user_id`).

After changing `[steam]`, run `tv/install` again (the Moonlight values are
baked into the app) and restart `tv-home`. Check: `curl -s
http://<lan-ip>:8797/api/pc` shows the PC state. Ask before testing Play: it
wakes the PC and starts a stream.

### Phone alerts

`[ntfy] url` = a private ntfy topic URL; the person installs the ntfy app
and subscribes. Alerts: Dev Mode renewal failed or low, YouTube login lost.

## Done when

- TV Home comes up on power-on (and on Home, with takeover).
- Each tab opens its app; Settings shortcuts work.
- `systemctl --user list-timers | grep devmode` shows the daily renewal.
- `./check.sh` passes, and `git status` shows no config or secret files.
