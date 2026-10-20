# TV Home

A custom home screen for LG webOS TVs. It replaces LG's launcher with a calm,
remote-friendly page that shows what you actually watch and play, and opens
the right app in one press.

## What it does

- **Apps as tabs.** Across the top sit the "doors": Jellyfin, YouTube and
  Steam. Moving onto a door shows that app's content below it; Enter opens
  the app. Up from any row brings you back to the same tab. Only the apps you
  set up get a tab.
- **Takes over the Home button.** When the TV turns on, TV Home is the first
  thing on screen. Optionally the remote's Home button opens it too; "LG apps"
  still reaches LG's own launcher.
- **Jellyfin.** Continue watching, Next up and Recently added from your
  Jellyfin server. Picking an episode opens the Jellyfin app and starts that
  episode through Jellyfin's remote control.
- **YouTube without ads.** New videos from your subscriptions, played in
  YouTube AdFree (an ad-free YouTube app for webOS). Videos you have already
  watched (on any device) are left out, half-watched ones show a progress bar,
  and a Continue watching row picks up where you stopped.
- **Steam via Moonlight.** One press wakes your gaming PC, makes sure Sunshine
  is running, and opens Moonlight streaming Steam Big Picture. Recently played
  games are listed with their art.
- Also: clock, weather, shortcuts to Picture, Sound, Inputs and all settings,
  a slow slideshow when idle, and a small offline screen if the server is down.

## What you need

- **An always-on Linux machine on the same network as the TV** (a home
  server, NAS or Raspberry Pi-class box with Python 3.11+). It runs the TV Home
  server and a few small helpers.
- **An LG TV with webOS and Developer Mode** (free LG developer account).
  Developer Mode lets you install your own apps; it has to be renewed, which
  TV Home does automatically.
- **The apps you want on the TV:** Jellyfin, YouTube AdFree, Moonlight TV, as
  you like. YouTube AdFree and Moonlight TV are installed in Developer Mode
  too.
- For YouTube's watched state: a Firefox-family browser (Firefox, Zen,
  LibreWolf) somewhere on your network where you are signed in to YouTube.
- For Steam: a gaming PC with Sunshine and Wake-on-LAN.

## Setting it up

Setup takes a coding agent (Claude Code, Codex, ...) on the always-on machine
and you with the TV remote. Clone this repo there and **give `SETUP.md` to
your coding agent.** It checks your TV first (older webOS versions differ),
asks before anything that needs the remote, and never prints or commits your
keys.

## Layout

| Path | What |
|---|---|
| `server/` | The TV Home server (Python, no dependencies; Pillow optional) |
| `ui/` | The home page (static HTML/CSS/JS, served by the server) |
| `tv/` | The webOS app, installer, auto-launch watcher, Dev Mode renewal |
| `systemd/` | User service templates and `install-units` |
| `config.example.toml` | Every setting, commented |
| `tests/`, `check.sh` | Tests; run `./check.sh` |

Try the page in a desktop browser without a TV or any sources:
`python3 server/tvhome_server.py --port 8811`, then open
`http://localhost:8811/?mock=1` (arrow keys and Enter work).

## License

MIT (see `LICENSE`). The fonts in `ui/fonts/` (Fraunces, Hanken Grotesk) are
under the SIL Open Font License; their license files sit next to them.
