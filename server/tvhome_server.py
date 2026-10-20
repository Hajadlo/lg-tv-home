#!/usr/bin/env python3
"""TV Home backend: serves ../ui/ and the JSON API (see README.md, "API").

Run: python3 tvhome_server.py [--port 8797] [--ui DIR]
Settings come from ~/.config/tv-home/config.toml (TVHOME_CONFIG overrides the path).
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import google_oauth
import images
import jellyfin
import jf_remote
import notify
import pc as pc_mod
import steam
import tvconfig
import weather
import youtube
import yt_cookies
import yt_watched
from refresher import Refresher

log = logging.getLogger("tvhome")

HERE = Path(__file__).resolve().parent
DEFAULT_UI = HERE.parent / "ui"
CONFIG_DIR = tvconfig.CONFIG_DIR
CACHE_DIR = Path(os.environ.get("TVHOME_CACHE_DIR", Path.home() / ".cache/tv-home"))
JELLYFIN_EVERY = 60.0
JELLYFIN_POKE_AGE = 15.0  # an /api/home older than this asks for a background refresh
WEATHER_EVERY = 15 * 60.0
YOUTUBE_EVERY = 30 * 60.0
STEAM_EVERY = steam.REFRESH_S
LONG_CACHE = "public, max-age=604800"
IMG_CACHE = "public, max-age=86400"
LOGO_PROBE_W = 480         # the width the UI asks for hero logos; the analysis reads the same cached file
LOGO_WAIT_S = 1.5          # /api/home waits at most this long for an unknown hero logo
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".avif", ".woff", ".woff2", ".ttf"}
IMG_PATH = re.compile(r"^/api/img/([^/]+)/([a-z]+)$")
YT_THUMB_PATH = re.compile(r"^/api/yt/([0-9A-Za-z_-]{11})/thumb$")
STEAM_IMG_PATH = re.compile(r"^/api/steam/(\d{1,10})/([a-z]+)$")
PLACEHOLDER = b"""<!doctype html><meta charset=utf-8><title>TV Home</title>
<body style="background:#111;color:#ddd;font:24px sans-serif;padding:3em">
TV Home backend is running; the UI (ui/) is not there yet.
See <a style="color:#9cf" href="/api/home">/api/home</a>."""


ALL_FEATURES = {"jellyfin": True, "youtube": True, "steam": True, "weather": True}


def hdd_space(path: Path | None = None) -> dict:
    if path is None:
        return {"hddFreeGb": None, "hddTotalGb": None}
    try:
        if not os.path.ismount(path):
            return {"hddFreeGb": None, "hddTotalGb": None}
        st = os.statvfs(path)
    except OSError:
        return {"hddFreeGb": None, "hddTotalGb": None}
    gb = 1024 ** 3  # GiB, as the desktop's file manager shows it
    return {"hddFreeGb": round(st.f_bavail * st.f_frsize / gb), "hddTotalGb": round(st.f_blocks * st.f_frsize / gb)}


class JellyfinSource:
    """Lazily bootstraps credentials, then fetches the three rows."""

    def __init__(self, creds_path: Path, url: str, user: str):
        self.creds_path = creds_path
        self.url = url.rstrip("/")
        self.user = user
        self.client: jellyfin.Jellyfin | None = None
        self._lock = threading.Lock()

    def get_client(self) -> jellyfin.Jellyfin:
        with self._lock:
            if self.client is None:
                creds = jellyfin.load_credentials(self.creds_path)
                if not creds or creds.get("userName") != self.user:
                    creds = jellyfin.bootstrap(self.creds_path, self.url, self.user)
                self.client = jellyfin.Jellyfin({**creds, "url": self.url})
            return self.client

    def rows(self) -> dict:
        return self.get_client().rows()

    def image(self, item_id: str, image_type: str, width: int | None):
        return self.get_client().image(item_id, image_type, width)


class App:
    def __init__(self, ui_dir: Path, jellyfin_rows, weather_fetch, pc: pc_mod.PC, image_cache: images.ImageCache,
                 hdd=hdd_space, jellyfin_every=JELLYFIN_EVERY, weather_every=WEATHER_EVERY,
                 youtube_source: youtube.YouTubeSource | None = None, steam_source: steam.SteamSource | None = None,
                 web_images: images.UrlImageCache | None = None, logo_cache: Path | None = None,
                 jellyfin_client=None, features: dict[str, bool] | None = None, bridge_js: bytes | None = None):
        self.ui_dir = ui_dir.resolve()
        self.bridge_js = bridge_js  # tv/app/bridge.js filled from config.toml, served at /tv/bridge.js
        self.features = dict(features or ALL_FEATURES)
        self.started = time.time()
        self.jellyfin = Refresher("jellyfin", jellyfin_rows, jellyfin_every, retry=20)
        self.weather = Refresher("weather", weather_fetch, weather_every, retry=120)
        self.youtube_source = youtube_source
        self.steam_source = steam_source
        self.youtube = Refresher("youtube", youtube_source or (lambda: None), YOUTUBE_EVERY, retry=300)
        self.steam = Refresher("steam", steam_source or (lambda: None), STEAM_EVERY, retry=120)
        if steam_source is not None:
            self.steam.value = steam_source.last  # the persisted result, shown before the first read
        self.pc = pc
        self.jf_open = jf_remote.Opener(jellyfin_client or (lambda: None))
        self.images = image_cache
        self.web_images = web_images
        self.shaper = images.Shaper()
        self.logos = images.LogoInfo(logo_cache, self._logo_source) if logo_cache is not None else None
        self.hdd = hdd

    def start(self) -> "App":
        if self.features["jellyfin"]:
            self.jellyfin.start()
        if self.features["weather"]:
            self.weather.start()
        if self.features["steam"]:
            self._pc_stop = self.pc.start_monitor()
        if self.youtube_source is not None:
            self.youtube.start()
        if self.steam_source is not None:
            self.steam.start()
        return self

    def home(self) -> dict:
        f = self.features
        if f["jellyfin"]:
            self.jellyfin.poke(JELLYFIN_POKE_AGE)
        if f["youtube"] and self.youtube_source is not None and self.youtube_source.wants_refresh():
            self.youtube.poke(5)  # youtube-auth just stored a token
        if f["steam"] and self.steam_source is not None and self.steam_source.due():
            self.steam.poke(60)  # the PC came on
        return {
            "now": datetime.now().astimezone().isoformat(timespec="seconds"),
            # Which tabs the UI builds; a source that is off is also null below.
            "features": dict(f),
            "weather": self.weather.get() if f["weather"] else None,
            "jellyfin": self._with_logos(self.jellyfin.get(), ("resume", "nextUp", "latest")) if f["jellyfin"] else None,
            "pc": self.pc.home_status() if f["steam"] else None,
            "system": self.hdd(),
            "youtube": self.youtube.get() if f["youtube"] else None,
            "steam": self._with_logos(self.steam.get(), ("recent",)) if f["steam"] else None,
        }

    def _with_logos(self, block: dict | None, keys: tuple[str, ...]) -> dict | None:
        """Copies of the items with `logoDark` added (the cached values are shared between threads).

        The first item of each list can become the hero, so an unknown logo
        there is worked out now (bounded by LOGO_WAIT_S); the rest in the background.
        """
        if not block:
            return block
        deadline = time.monotonic() + LOGO_WAIT_S
        out = dict(block)
        for key in keys:
            items = []
            for i, item in enumerate(block.get(key) or []):
                dark = None
                if self.logos is not None and item.get("logo"):
                    wait = max(0.0, deadline - time.monotonic()) if i == 0 else 0.0
                    dark = self.logos.dark(item["logo"], wait)
                items.append({**item, "logoDark": dark})
            if key in block:
                out[key] = items
        return out

    def image(self, path: str, w: str | None) -> tuple[bytes, str] | None:
        """(bytes, content type) for an image route, None when `path` is not one.

        Jellyfin scales its own images (`w` rounds up to a 160 px step); public
        images are scaled here (20 px steps). Logos lose their transparent
        padding. Raises NotFound, or another error when the source is down.
        """
        m = IMG_PATH.match(path)
        if m:
            item_id, kind = m.groups()
            width = images.bucket_width(w)
            src = self.images.get(item_id, kind, width)
            return self.shaper.get(f"jf_{item_id}_{kind}_{width}", src, None, trim=kind == "logo")
        m = YT_THUMB_PATH.match(path)
        if m:
            vid = m.group(1)
            src = self._web(f"yt_{vid}_thumb", youtube.thumb_candidates(vid), youtube.thumb_ok)
            return self.shaper.get(f"yt_{vid}_thumb", src, images.resize_width(w))
        m = STEAM_IMG_PATH.match(path)
        if m and m.group(2) in steam.ASSET_FILES:
            appid, kind = int(m.group(1)), m.group(2)
            extra = None
            if kind == "header" and self.steam_source is not None:
                extra = (self.steam_source.names.get(appid, lookup=False) or {}).get("header")
            src = self._web(f"steam_{appid}_{kind}", steam.asset_candidates(appid, kind, extra))
            return self.shaper.get(f"steam_{appid}_{kind}", src, images.resize_width(w), trim=kind == "logo")
        return None

    def _web(self, key: str, candidates, accept=None) -> tuple[bytes, str]:
        if self.web_images is None:
            raise jellyfin.NotFound(key)
        return self.web_images.get(key, candidates, accept)

    def _logo_source(self, url: str) -> bytes:
        """The untrimmed logo behind a logo URL from /api/home, for LogoInfo."""
        m = IMG_PATH.match(url)
        if m:
            return self.images.get(m.group(1), m.group(2), images.bucket_width(str(LOGO_PROBE_W)))[0]
        m = STEAM_IMG_PATH.match(url)
        if m and m.group(2) == "logo":
            return self._web(f"steam_{m.group(1)}_logo", steam.asset_candidates(int(m.group(1)), "logo"))[0]
        raise jellyfin.NotFound(url)

    def health(self) -> dict:
        jf, wx = self.jellyfin.status(), self.weather.status()
        yt = self.youtube.status()
        subs = getattr(self.youtube_source, "channels", None)
        if isinstance(subs, youtube.Subscriptions):
            yt["auth"] = subs.state.get("auth")
            yt["subscriptionsSource"] = subs.state.get("source")
            yt["subscriptionsError"] = subs.last_error
            yt["subscriptionsCookieError"] = subs.cookie_error
        watched = getattr(self.youtube_source, "watched", None)
        if watched is not None:
            yt.update(watched.status())
        return {
            "ok": (jf["ok"] or not self.features["jellyfin"]) and (wx["ok"] or not self.features["weather"]),
            "features": dict(self.features),
            "uptimeS": round(time.time() - self.started),
            "jellyfin": jf,
            "weather": wx,
            "youtube": yt,
            "steam": {**self.steam.status(), "lastDesktopRead": None if self.steam_source is None
                      or self.steam_source.last_read is None
                      else round(time.time() - self.steam_source.last_read)},
            "pc": {**self.pc.status(), "probeAgeS": None if self.pc.last_probe_at is None
                   else round(time.monotonic() - self.pc.last_probe_at)},
            "ui": (self.ui_dir / "index.html").is_file(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "tv-home/1.0"
    protocol_version = "HTTP/1.1"
    app: App  # set on the subclass made by make_server

    def log_message(self, fmt, *args):  # replaced by one line per request in _log
        pass

    def _log(self, status: int) -> None:
        path = self.path.split("?", 1)[0]
        ms = (time.monotonic() - self._t0) * 1000
        quiet = path.startswith(("/api/img/", "/api/yt/", "/api/steam/")) or path == "/api/health"
        ua = f" ua={self.headers.get('User-Agent', '')!r}" if path == "/" else ""  # shows the TV's browser engine
        log.log(logging.DEBUG if quiet else logging.INFO, "%s %s %d %.0fms %s%s",
                self.command, path, status, ms, self.client_address[0], ua)

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-cache", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status)

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self._send(status, body, "application/json; charset=utf-8", "no-store")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def do_OPTIONS(self):
        self._t0 = time.monotonic()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._log(204)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._t0 = time.monotonic()
        url = urlsplit(self.path)
        path = url.path
        try:
            if path == "/api/home":
                return self._json(self.app.home())
            if path == "/api/pc":
                if not self.app.features["steam"]:
                    return self._error(404, "steam is not configured")
                return self._json(self.app.pc.status())
            if path == "/api/health":
                return self._json(self.app.health())
            if path.startswith(("/api/img/", "/api/yt/", "/api/steam/")):
                served = self._image(path, parse_qs(url.query).get("w", [None])[0])
                if served:
                    return
            if path.startswith("/api/"):
                return self._error(404, "no such endpoint")
            if path == "/tv/bridge.js":
                if self.app.bridge_js is None:
                    return self._error(404, "no bridge: set [server] url in config.toml")
                return self._send(200, self.app.bridge_js, "application/javascript; charset=utf-8")
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("GET %s failed", path)
            self._error(500, "internal error")

    def do_POST(self):
        self._t0 = time.monotonic()
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))
        if path.startswith("/api/jellyfin/open/") and self.app.features["jellyfin"]:
            item_id = path.rsplit("/", 1)[-1]
            if not item_id.isalnum() or len(item_id) > 64:
                return self._error(400, "bad item id")
            return self._json(self.app.jf_open.open(item_id))
        if path == "/api/pc/play" and self.app.features["steam"]:
            status = self.app.pc.play()
            return self._json({"state": status["state"], "sunshine": status["sunshine"]})
        self._error(404, "no such endpoint")

    def _image(self, path: str, w: str | None) -> bool:
        """Answer an image route; False when `path` is not one."""
        try:
            got = self.app.image(path, w)
        except jellyfin.NotFound:
            self._error(404, "no such image")
            return True
        except Exception as exc:
            log.warning("image %s: %s", path, exc)
            self._error(502, "image source unavailable")
            return True
        if got is None:
            return False
        self._send(200, got[0], got[1], IMG_CACHE)
        return True

    def _static(self, path: str):
        rel = unquote(path).lstrip("/") or "index.html"
        target = (self.app.ui_dir / rel).resolve()
        if target != self.app.ui_dir and self.app.ui_dir not in target.parents:
            return self._error(404, "not found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if rel == "index.html":
                return self._send(200, PLACEHOLDER, "text/html; charset=utf-8")
            return self._error(404, "not found")
        st = target.stat()
        mtime = int(st.st_mtime)
        last_modified = email.utils.formatdate(mtime, usegmt=True)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        cache = LONG_CACHE if target.suffix.lower() in IMAGE_EXT else "no-cache"
        since = self.headers.get("If-Modified-Since")
        if since:
            try:
                if int(email.utils.parsedate_to_datetime(since).timestamp()) >= mtime:
                    self.send_response(304)
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Cache-Control", cache)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return self._log(304)
            except (TypeError, ValueError):
                pass
        self._send(200, target.read_bytes(), ctype, cache, {"Last-Modified": last_modified})


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def desktop_on(pc: pc_mod.PC) -> bool:
    """From the PC monitor's last probe; probes once itself before the monitor's first."""
    last = pc.last
    return last.on if last is not None else pc_mod.tcp_open(pc.host, pc_mod.SSH_PORT)


def build_app(cfg: dict, ui_dir: Path, yt_channels: list[str] | None = None, yt_cookie_sync: bool = True) -> App:
    features = tvconfig.features(cfg)
    notify.configure(cfg["ntfy"]["url"])
    jf_cfg, yt_cfg, st_cfg, wx_cfg = cfg["jellyfin"], cfg["youtube"], cfg["steam"], cfg["weather"]

    source = JellyfinSource(CONFIG_DIR / "jellyfin.json", jf_cfg["url"], jf_cfg["user"])
    location = {"latitude": wx_cfg["latitude"], "longitude": wx_cfg["longitude"]}

    pc = pc_mod.PC(
        host=st_cfg["pc_ip"], sunshine_port=int(st_cfg["sunshine_port"]),
        wake=lambda: pc_mod.send_wol(st_cfg["pc_mac"], st_cfg["broadcast"]),
        restart=lambda: pc_mod.restart_sunshine(st_cfg["ssh_host"], st_cfg["sunshine_unit"]),
        prepare_tv=lambda: pc_mod.close_moonlight(cfg["tv"]["apps"]["moonlight"]),
    )

    youtube_source = None
    if features["youtube"]:
        watched = None
        if tvconfig.youtube_cookies(cfg):
            ssh_host = yt_cfg["ssh_host"]
            if not yt_cookie_sync:
                reachable = lambda: False  # noqa: E731
            elif ssh_host and features["steam"] and ssh_host == st_cfg["ssh_host"]:
                reachable = lambda: desktop_on(pc)  # noqa: E731  the gaming PC: only while it is on
            else:
                reachable = lambda: True  # noqa: E731
            cookie_store = yt_cookies.CookieStore(CONFIG_DIR / "yt-cookies.json", reachable,
                                                  yt_cookies.tar_reader(yt_cfg["browser_profile"], ssh_host))
            watched = yt_watched.CookieYouTube(cookie_store, CACHE_DIR / "yt-watched.json",
                                               channel_name=yt_cfg["channel"].lstrip("@"))
        if yt_channels:
            channels = youtube.StaticChannels(yt_channels)
        elif watched is None and yt_cfg["subscriptions_csv"]:
            channels = youtube.csv_channels(Path(yt_cfg["subscriptions_csv"]).expanduser())
        else:
            auth = google_oauth.GoogleAuth(CONFIG_DIR / "google-oauth-client.json",
                                           CONFIG_DIR / "google-oauth-token.json")
            channels = youtube.Subscriptions(auth, CACHE_DIR / "yt-subscriptions.json",
                                             cookie_channels=watched.channels if watched else None)
        youtube_source = youtube.YouTubeSource(channels, CACHE_DIR, watched=watched)

    steam_source = None
    if features["steam"] and st_cfg["ssh_host"]:
        read = steam.ssh_reader(st_cfg["ssh_host"])
        steam_source = steam.SteamSource(
            lambda: desktop_on(pc), CACHE_DIR / "steam.json", steam.StoreNames(CACHE_DIR / "steam-names.json"),
            read=lambda: steam.read_desktop(read, st_cfg["steam_root"], int(st_cfg["steam_user_id"] or 0)))

    disk = cfg["server"]["disk"]
    bridge_js = None
    try:
        sys.path.insert(0, str(HERE.parent / "tv"))
        import render
        bridge_js = render.render_bridge((HERE.parent / "tv/app/bridge.js").read_text(),
                                         render.settings(cfg)).encode()
    except (SystemExit, OSError) as exc:
        log.warning("not serving /tv/bridge.js: %s", exc)
    return App(
        ui_dir=ui_dir,
        jellyfin_rows=source.rows,
        jellyfin_client=source.get_client if features["jellyfin"] else None,
        weather_fetch=weather.make_fetch(lambda: location),
        pc=pc,
        image_cache=images.ImageCache(CACHE_DIR / "img", source.image if features["jellyfin"] else None),
        youtube_source=youtube_source,
        steam_source=steam_source,
        web_images=images.UrlImageCache(CACHE_DIR / "web"),
        logo_cache=CACHE_DIR / "logo-info.json",
        hdd=lambda: hdd_space(Path(disk).expanduser() if disk else None),
        features=features,
        bridge_js=bridge_js,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--ui", type=Path, default=DEFAULT_UI)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--yt-channels", help="comma-separated channel ids instead of the account's "
                        "subscriptions (testing)")
    parser.add_argument("--no-cookie-sync", action="store_true",
                        help="use yt-cookies.json as it is; never copy it from the browser (testing)")
    args = parser.parse_args()
    cfg = tvconfig.load()
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    # journald stamps lines itself; a plain log file needs the time.
    stamp = "" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s "
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format=stamp + "%(levelname)s %(name)s: %(message)s")
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("image/webp", ".webp")
    app = build_app(cfg, args.ui, args.yt_channels.split(",") if args.yt_channels else None,
                    yt_cookie_sync=not args.no_cookie_sync).start()
    server = make_server(app, host, port)
    log.info("config %s; sources on: %s", tvconfig.path(),
             ", ".join(k for k, v in app.features.items() if v) or "none")
    log.info("listening on %s:%d, ui %s", host, port, app.ui_dir)
    server.serve_forever()


if __name__ == "__main__":
    main()
