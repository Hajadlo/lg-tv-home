# Three ways to run TV Home

TV Home can be built three ways, depending on what you have: a TV alone, a TV
plus an always-on machine, or a rooted TV. **Only the server version exists
today**; the other two are designs, written down so it is clear what each
could and could not do. Items marked *(unverified)* are expected to work but
have not been tried on a TV yet.

| | TV only (Developer Mode) | TV + server (this repo) | Rooted TV |
|---|---|---|---|
| **Status** | Design, not built | **Built, in daily use** | Design, not built |
| Needs | LG TV in Developer Mode | LG TV in Developer Mode + an always-on machine on the LAN | LG TV whose firmware can be rooted |
| Home page, tabs, clock, weather, settings shortcuts | Yes | Yes | Yes |
| Shows on power-on | Only if the TV reopens the last app *(unverified, depends on model)* | Yes | Yes |
| Home button opens TV Home | No | Yes | Yes *(unverified)* |
| YouTube: new uploads from subscriptions | Yes, from a channel list or a Google sign-in that expires weekly | Yes, follows your subscriptions automatically | Same as TV only, or as server if one is added |
| YouTube: hide watched, Continue watching | No | Yes | No without a server |
| Jellyfin rows | Yes | Yes | Yes |
| Jellyfin: play an episode directly | Yes *(unverified)* | Yes | Yes *(unverified)* |
| Steam: recently played | Yes, through the Steam Web API (public profile) *(unverified)* | Yes, read from the PC | Yes |
| Steam: wake the PC and open Big Picture | Wake-on-LAN from the TV *(unverified)* | Yes, and restarts Sunshine if needed | Yes *(unverified)* |
| Developer Mode renewal | **By hand** every ~41 days, or LG deletes the app | Automatic | Not needed (root replaces Developer Mode) |
| Effort | Medium: package the page into the app, move the fetching into a small on-TV service | Done | High: port to the TV, set up start-at-boot; rooting risks voiding the warranty |

## TV only (Developer Mode, no server)

Everything lives in the TV app: the page itself is packaged into the app, and
a small background service inside the same app (webOS "JS service", Node.js)
fetches the data the page shows. Nothing else has to be running.

What it can do:

- The full home page: tabs, rows, clock, weather (Open-Meteo, no key),
  settings shortcuts, and launching Jellyfin, YouTube AdFree and Moonlight.
- **YouTube:** new uploads from each subscribed channel's public feed. The
  list of channels comes from a file you upload once (or a Google Takeout
  export), or from signing in to Google on the TV with a code. Google expires
  that sign-in every 7 days for personal projects, so a fixed list is the
  practical choice.
- **Jellyfin:** Continue watching, Next up and Recently added straight from
  the Jellyfin server, and starting an episode directly through Jellyfin's
  remote control.
- **Steam:** recently played games through the Steam Web API (needs a free API
  key and a public game list), and Wake-on-LAN to the PC before Moonlight
  opens.

What it cannot do:

- **Take over the Home button.** That needs something watching the TV in the
  background; an unrooted TV stops app services shortly after they finish.
  TV Home is opened like any app (or the TV may reopen it at power-on if it
  was the last app, depending on the model and the "Home auto launch"
  setting).
- **Hide watched videos or show Continue watching.** That needs your YouTube
  login, which the server copies from a desktop browser; there is no
  reasonable way to do that on the TV.
- **Renew Developer Mode.** It lasts at most 1000 hours (about 41 days). Open
  LG's Developer Mode app and press "Extend" before then, or LG removes all
  Developer Mode apps (TV Home, YouTube AdFree, Moonlight). The app can show a
  reminder when the deadline is near.

## TV + server (this repo)

A small Python server on an always-on machine on the same network (home
server, NAS, Raspberry Pi) serves the page and does all the fetching; the TV
app is a thin shell that loads it. This is the version in this repository,
set up by following [SETUP.md](../SETUP.md).

Everything in the table works, including the parts the TV can't do alone:

- The server watches the TV over the network and brings TV Home back whenever
  LG's launcher appears, so it is what you see at power-on and on the Home
  button.
- It copies your YouTube login from a Firefox-family browser on your network,
  so watched videos disappear and Continue watching follows you across
  devices.
- It renews Developer Mode every day, so apps are never removed.
- For Steam it wakes the PC, checks that Sunshine is listening (restarting it
  once over SSH if not) and only then opens Moonlight.

If the server is down, the TV app shows a simple fallback screen with the app
tiles.

## Rooted TV

Many LG TVs, mostly with older firmware, can be rooted with the community
tools behind the [webOS Homebrew Channel](https://www.webosbrew.org/)
(RootMyTV, DejaVuln and others; which one works depends on the exact model
and firmware version). Root gives the TV what the other two versions need a
server for:

- **No Developer Mode expiry.** Apps installed through the Homebrew Channel
  stay installed.
- **Start at boot.** A script started at boot can watch the foreground app and
  bring TV Home back, so power-on and the Home button work without a server.
- **Run the fetching on the TV**, unrestricted. The server could move onto the
  TV, but it would have to be ported first: the TV ships Node.js, not Python.

Still out of reach without a server: hiding watched videos and Continue
watching, for the same login reason as above.

Rooting is done at your own risk: it can void the warranty, and LG firmware
updates can close the hole (block updates once rooted). Check your model and
firmware on webosbrew.org before planning on it.

## Which one to pick

- You already have an always-on machine: **TV + server.**
- You don't, and your TV can be rooted: **rooted TV** gets close to the full
  experience (everything except watched / Continue watching).
- You don't, and it can't be rooted: **TV only**, accepting no Home-button
  takeover and a monthly "Extend" press.

Find the model and firmware under Settings → General → About This TV (or
Support → TV Information).
