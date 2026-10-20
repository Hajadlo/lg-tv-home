"""YouTube section: subscribed channels and their newest videos (public RSS), minus watched ones.

Channels come every 6 h (Subscriptions) from the browser's YouTube login
(yt_watched, InnerTube FEchannels), else from the Data API's
`subscriptions.list`; videos come from each channel's RSS feed every 30 min
(YouTubeSource). Videos watched to 90 % or more (yt_watched) are left out and
the rest carry `progress`. Shorts are dropped: the feed links a Short as
youtube.com/shorts/<id>, and anything else is checked once with a HEAD on
/shorts/<id> (200 = Short, 303 to /watch = normal video).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import google_oauth
import images
import net
import notify as notify_mod
import yt_watched

log = logging.getLogger("tvhome.youtube")

CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
SHORTS_URL = "https://www.youtube.com/shorts/{}"
THUMB_URLS = ("https://i.ytimg.com/vi/{}/maxresdefault.jpg", "https://i.ytimg.com/vi/{}/hqdefault.jpg")
# Without a consent cookie EU requests are redirected to consent.youtube.com.
CONSENT = {"Cookie": "SOCS=CAI"}
WATCH_URL = "https://www.youtube.com/watch?v={}"
CATEGORY_RE = re.compile(rb'"category":"([^"]{1,60})"')
MAX_CATEGORY_CHECKS = 25  # per refresh; answers are cached forever
# Live state from the same watch page: "live" (on air now), "upcoming" (scheduled
# stream or premiere), "stream" (a finished stream) or "video".
LIVE_NOW_RE = re.compile(rb'"isLiveNow":(true|false)')
LIVE_CONTENT_RE = re.compile(rb'"isLiveContent":(true|false)')
UPCOMING_RE = re.compile(rb'"isUpcoming":true')
FINAL_KINDS = ("video", "stream")  # cached forever; "live" and "upcoming" change
KIND_RECHECK = 10 * 60.0
MAX_KIND_CHECKS = 40  # watch pages per refresh; unchecked videos show as normal ones
HIDDEN_KINDS = ("stream", "upcoming")  # finished streams are left out; scheduled ones until they go live
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
VIDEO_LIMIT = 30
WORKERS = 4
MAX_SHORTS_CHECKS = 80  # per refresh; the rest wait for the next one
SUBS_EVERY = 6 * 3600.0
SUBS_RETRY = 15 * 60.0
AUTH_COMMAND = str(Path(__file__).resolve().parent / "youtube-auth")
AUTH_MESSAGES = {
    "missing": "TV Home is not signed in to YouTube, so new subscriptions are not picked up. "
               f"On the TV Home server run:\n{AUTH_COMMAND}\nthen open the URL it prints and enter its code.",
    "expired": "Google rejected TV Home's YouTube login (expired or revoked); the YouTube row keeps the "
               f"old channel list. On the TV Home server run:\n{AUTH_COMMAND}\nthen open the URL it prints "
               "and enter its code.",
}


def parse_feed(xml: bytes) -> list[dict]:
    """Entries of a channel feed; `short` is True when the feed links /shorts/, else None."""
    root = ET.fromstring(xml)
    channel = root.findtext("a:title", default="", namespaces=NS)
    out = []
    for entry in root.findall("a:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not VIDEO_ID.match(vid):
            continue
        link = entry.find("a:link[@rel='alternate']", NS)
        href = link.get("href", "") if link is not None else ""
        stats = entry.find("media:group/media:community/media:statistics", NS)
        try:
            views = int(stats.get("views")) if stats is not None else None
        except (TypeError, ValueError):
            views = None
        out.append({
            "id": vid,
            "title": entry.findtext("a:title", default="", namespaces=NS),
            "channel": entry.findtext("a:author/a:name", default=channel, namespaces=NS) or channel,
            "channelId": entry.findtext("yt:channelId", default="", namespaces=NS),
            "published": entry.findtext("a:published", default="", namespaces=NS),
            "views": views,
            "short": True if "/shorts/" in href else None,
        })
    return out


def check_short(video_id: str, fetch: net.Fetch = net.request) -> bool | None:
    """True for a Short, False for a normal video, None when YouTube's answer is unclear."""
    resp = fetch(SHORTS_URL.format(video_id), method="HEAD", headers=CONSENT, follow=False, timeout=10)
    if resp.status == 200:
        return True
    if resp.status in (301, 302, 303, 307, 308) and "/watch" in resp.headers.get("location", ""):
        return False
    return None


def parse_watch(body: bytes) -> dict:
    """Category ("Music", "Gaming", ...) and kind (see LIVE_NOW_RE) of a watch page; None where unclear."""
    m = CATEGORY_RE.search(body)
    category = m.group(1).decode("utf-8", "replace").replace("\\u0026", "&") if m else None
    content = LIVE_CONTENT_RE.search(body)
    now = LIVE_NOW_RE.search(body)
    if UPCOMING_RE.search(body):
        kind = "upcoming"
    elif now and now.group(1) == b"true":
        kind = "live"
    elif content:
        kind = "stream" if content.group(1) == b"true" else "video"
    else:
        kind = None
    return {"category": category, "kind": kind}


def watch_info(video_id: str, fetch: net.Fetch = net.request) -> dict | None:
    """parse_watch of the public watch page; None when YouTube does not answer 200."""
    resp = fetch(WATCH_URL.format(video_id), headers={**CONSENT, "Accept-Language": "en"}, timeout=15)
    return parse_watch(resp.body) if resp.status == 200 else None


def video_category(video_id: str, fetch: net.Fetch = net.request) -> str | None:
    """YouTube's category ("Music", "Gaming", ...) from the public watch page; None when unclear."""
    info = watch_info(video_id, fetch)
    return info["category"] if info else None


def thumb_ok(data: bytes) -> bool:
    """Reject YouTube's 120x90 'no thumbnail' placeholder."""
    size = images.jpeg_size(data)
    return size[0] > 120 if size else len(data) > 4096


def thumb_candidates(video_id: str) -> list[str]:
    return [u.format(video_id) for u in THUMB_URLS]


def to_video(entry: dict, progress: float | None = None) -> dict:
    return {"id": entry["id"], "title": entry["title"], "channel": entry["channel"],
            "channelId": entry["channelId"], "published": entry["published"], "views": entry["views"],
            "thumb": f"/api/yt/{entry['id']}/thumb", "live": bool(entry.get("live")),
            "progress": None if progress is None else round(min(1.0, max(0.0, progress)), 3)}


def continue_videos(unfinished: list[dict], entries: list[dict]) -> list[dict]:
    """History's unfinished videos as contract Videos, enriched from the RSS entries when known."""
    by_id = {e["id"]: e for e in entries}
    out = []
    for v in unfinished:
        e = by_id.get(v["id"])
        base = e if e else {"id": v["id"], "title": v.get("title") or "", "channel": v.get("channel") or "",
                            "channelId": None, "published": None, "views": None}
        out.append(to_video(base, v["progress"]))
    return out


class StaticChannels:
    """A fixed channel list (testing, or a Takeout CSV without any login)."""

    def __init__(self, ids: list[str], titles: dict[str, str] | None = None):
        self.channels = [{"id": c, "title": (titles or {}).get(c, "")} for c in ids if CHANNEL_ID.match(c)]

    def get(self) -> tuple[list[dict], str]:
        return self.channels, "ok"

    def wants_refresh(self) -> bool:
        return False


def read_takeout_csv(path: Path) -> list[dict]:
    """Channels of a Google Takeout `subscriptions.csv` (Channel Id, Channel Url, Channel Title)."""
    import csv
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if row and CHANNEL_ID.match(row[0].strip()):
                out.append({"id": row[0].strip(), "title": row[2].strip() if len(row) > 2 else ""})
    return out


def csv_channels(path: Path) -> StaticChannels:
    channels = read_takeout_csv(path)
    return StaticChannels([c["id"] for c in channels], {c["id"]: c["title"] for c in channels})


class Subscriptions:
    """The subscribed channels, cached on disk; serves the last list when auth breaks.

    State in `cache_path`: {"updated", "channels", "auth", "source", "notified"}.
    With `cookie_channels` (the browser-login route, yt_watched) that route is
    tried first; OAuth is only the fallback, and its failures then send no ntfy
    (the cookie route alerts on its own). Without it, one ntfy goes out when
    auth turns "missing" or "expired"; it is re-armed by success.
    """

    def __init__(self, auth: google_oauth.GoogleAuth, cache_path: Path,
                 fetch_subs: Callable = google_oauth.fetch_subscriptions,
                 notify: Callable[[str, str], bool] = notify_mod.send,
                 every: float = SUBS_EVERY, retry: float = SUBS_RETRY, clock: Callable[[], float] = time.time,
                 cookie_channels: Callable[[], list[dict]] | None = None):
        self.auth = auth
        self.cache_path = cache_path
        self._fetch_subs = fetch_subs
        self._notify = notify
        self._cookie_channels = cookie_channels
        self.cookie_error: str | None = None
        self.every = every
        self.retry = retry
        self._clock = clock
        self._lock = threading.Lock()
        self.state = net.read_json(cache_path, {}) or {}
        self._last_attempt: float | None = None
        self._last_ok = False
        self._seen_mtime: float | None = None
        self.last_error: str | None = None

    def wants_refresh(self) -> bool:
        """The token file changed (youtube-auth just ran)."""
        return self._last_attempt is not None and self.auth.token_mtime() != self._seen_mtime

    def get(self) -> tuple[list[dict] | None, str]:
        with self._lock:
            now = self._clock()
            wait = self.every if self._last_ok else self.retry
            if self._last_attempt is None or now - self._last_attempt >= wait or self.wants_refresh():
                self._refresh(now)
            return self.state.get("channels"), self.state.get("auth") or "missing"

    def _refresh(self, now: float) -> None:
        self._last_attempt = now
        self._seen_mtime = self.auth.token_mtime()
        if self._cookie_channels is not None:
            try:
                channels = self._cookie_channels()
            except Exception as exc:  # fall back to OAuth below
                self.cookie_error = f"{type(exc).__name__}: {exc}"
                log.warning("subscriptions via the browser login failed (%s); trying OAuth", self.cookie_error)
            else:
                self.cookie_error = None
                return self._store(channels, "cookies")
        try:
            channels = self._fetch_subs(self.auth)
        except google_oauth.AuthMissing as exc:
            return self._auth_failed("missing", exc)
        except google_oauth.AuthExpired as exc:
            return self._auth_failed("expired", exc)
        except Exception as exc:  # transient: keep the list and the auth state
            self._last_ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("subscriptions refresh failed: %s", self.last_error)
            self.state.setdefault("auth", "ok" if self.state.get("channels") is not None else "missing")
            return
        self._store(channels, "oauth")

    def _store(self, channels: list[dict], source: str) -> None:
        self._last_ok = True
        self.last_error = None
        self.state = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "channels": channels, "auth": "ok", "source": source, "notified": None}
        net.write_json(self.cache_path, self.state)
        log.info("subscriptions: %d channels (%s)", len(channels), source)

    def _auth_failed(self, kind: str, exc: Exception) -> None:
        self._last_ok = False
        self.last_error = f"auth {kind}: {exc}"
        log.warning("YouTube %s", self.last_error)
        self.state["auth"] = kind
        if self._cookie_channels is None and self.state.get("notified") != kind:
            self._notify(f"TV Home: YouTube login {kind}", AUTH_MESSAGES[kind])
            self.state["notified"] = kind
        net.write_json(self.cache_path, self.state)


class YouTubeSource:
    """Builds the contract's `youtube` object; called by a Refresher every 30 min."""

    def __init__(self, channels, cache_dir: Path, fetch: net.Fetch = net.request,
                 workers: int = WORKERS, limit: int = VIDEO_LIMIT, watched=None):
        self.channels = channels
        self.watched = watched  # yt_watched.CookieYouTube, or None (no watched filter)
        self._fetch = fetch
        self.workers = workers
        self.limit = limit
        self.shorts_path = cache_dir / "yt-shorts.json"
        self.shorts: dict[str, bool] = net.read_json(self.shorts_path, {}) or {}
        self.categories_path = cache_dir / "yt-categories.json"
        self.categories: dict[str, str] = net.read_json(self.categories_path, {}) or {}
        self.kinds_path = cache_dir / "yt-kinds.json"
        self.kinds: dict[str, str] = net.read_json(self.kinds_path, {}) or {}  # final kinds only
        self._passing: dict[str, tuple[str, float]] = {}  # id -> ("live" | "upcoming", checked at)
        self._clock = time.monotonic
        self._feeds: dict[str, dict] = {}  # channel id -> {"etag", "modified", "entries"}

    def wants_refresh(self) -> bool:
        return self.channels.wants_refresh()

    def _feed(self, cid: str) -> list[dict]:
        old = self._feeds.get(cid) or {}
        headers = {}
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("modified"):
            headers["If-Modified-Since"] = old["modified"]
        try:
            resp = self._fetch(FEED_URL.format(cid), headers=headers, timeout=15)
            if resp.status == 304 and old:
                return old["entries"]
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            entries = parse_feed(resp.body)
        except (OSError, ET.ParseError) as exc:
            if old:
                log.info("feed %s failed (%s); keeping %d entries", cid, exc, len(old["entries"]))
                return old["entries"]
            raise
        self._feeds[cid] = {"etag": resp.headers.get("etag"), "modified": resp.headers.get("last-modified"),
                            "entries": entries}
        return entries

    def __call__(self) -> dict | None:
        channels, auth = self.channels.get()
        if channels is None:
            return {"updated": None, "channelCount": 0, "auth": auth, "watched": "unavailable", "videos": [], "continue": []}
        ids = [c["id"] for c in channels if CHANNEL_ID.match(c.get("id", ""))]
        entries: list[dict] = []
        failed = 0
        with ThreadPoolExecutor(self.workers) as pool:
            for cid, result in zip(ids, pool.map(self._safe_feed, ids)):
                if result is None:
                    failed += 1
                else:
                    entries.extend(result)
        for gone in set(self._feeds) - set(ids):
            del self._feeds[gone]
        if ids and failed == len(ids):
            raise OSError(f"all {failed} channel feeds failed")
        if failed:
            log.warning("%d of %d channel feeds failed", failed, len(ids))
        state = self._watch_state()
        unfinished = continue_videos(self.drop_music(state.unfinished()), entries) if state else []
        # A video in "Continue watching" is not repeated among the new ones.
        hidden = (state.hidden() if state else set()) | {v["id"] for v in unfinished}
        videos = self.pick(entries, state.progress if state else {}, hidden)
        return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"),
                "channelCount": len(ids), "auth": auth, "watched": "ok" if state else "unavailable",
                "videos": videos, "continue": unfinished}

    def drop_music(self, videos: list[dict], limit: int = yt_watched.CONTINUE_LIMIT) -> list[dict]:
        """Keep non-music videos, up to `limit`; categories come from a cache, else the watch page."""
        out, checks, changed = [], 0, False
        for v in videos:
            cat = self.categories.get(v["id"])
            if cat is None and checks < MAX_CATEGORY_CHECKS:
                checks += 1
                try:
                    cat = video_category(v["id"], self._fetch)
                except OSError as exc:
                    log.warning("category of %s: %s", v["id"], exc)
                if cat:
                    self.categories[v["id"]] = cat
                    changed = True
            if cat is None or cat == "Music":
                continue  # unknown stays out until a later refresh has checked it
            out.append(v)
            if len(out) >= limit:
                break
        if changed:
            net.write_json(self.categories_path, self.categories)
        return out

    def _watch_state(self):
        if self.watched is None:
            return None
        try:
            return self.watched.watch_state()
        except Exception as exc:  # never lose the RSS list over the watched filter
            log.warning("watched state: %s", exc)
            return None

    def _safe_feed(self, cid: str) -> list[dict] | None:
        try:
            return self._feed(cid)
        except Exception as exc:
            log.info("feed %s failed: %s", cid, exc)
            return None

    def pick(self, entries: list[dict], progress: dict[str, float] | None = None,
             hidden: set[str] | frozenset = frozenset()) -> list[dict]:
        """Newest first, Shorts and `hidden` (watched) out, at most `limit`; HEAD-checks only what it needs."""
        progress = progress or {}
        seen: set[str] = set()
        candidates = []
        for e in sorted(entries, key=lambda e: e["published"], reverse=True):
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            if e["short"]:
                self.shorts[e["id"]] = True
                continue
            if e["id"] in hidden:
                continue
            candidates.append(e)
        picked: list[dict] = []
        checks = 0
        self._kind_checks = 0
        i = 0
        while i < len(candidates) and len(picked) < self.limit:
            batch = candidates[i:i + (self.limit - len(picked))]
            i += len(batch)
            unknown = [e["id"] for e in batch if e["id"] not in self.shorts][:max(0, MAX_SHORTS_CHECKS - checks)]
            checks += len(unknown)
            if unknown:
                with ThreadPoolExecutor(self.workers) as pool:
                    for vid, is_short in zip(unknown, pool.map(self._safe_check, unknown)):
                        if is_short is not None:
                            self.shorts[vid] = is_short
            normal = [e for e in batch if not self.shorts.get(e["id"])]
            kinds = self._kinds([e["id"] for e in normal])
            picked.extend({**e, "live": kinds.get(e["id"]) == "live"} for e in normal
                          if kinds.get(e["id"]) not in HIDDEN_KINDS)
        self.shorts = {k: v for k, v in self.shorts.items() if k in seen}
        self.kinds = {k: v for k, v in self.kinds.items() if k in seen}
        self._passing = {k: v for k, v in self._passing.items() if k in seen}
        try:
            net.write_json(self.kinds_path, self.kinds)
            net.write_json(self.categories_path, self.categories)
        except OSError as exc:
            log.warning("cannot save the live-state cache: %s", exc)
        try:
            net.write_json(self.shorts_path, self.shorts)
        except OSError as exc:
            log.warning("cannot save the Shorts cache: %s", exc)
        return [to_video(e, progress.get(e["id"])) for e in picked[:self.limit]]

    def _kinds(self, ids: list[str]) -> dict[str, str | None]:
        """id -> kind, from the caches or (up to MAX_KIND_CHECKS per refresh) the watch page."""
        now = self._clock()
        out: dict[str, str | None] = {}
        todo = []
        for vid in ids:
            passing = self._passing.get(vid)
            if vid in self.kinds:
                out[vid] = self.kinds[vid]
            elif passing and now - passing[1] < KIND_RECHECK:
                out[vid] = passing[0]
            else:
                out[vid] = passing[0] if passing else None  # until checked
                todo.append(vid)
        todo = todo[:max(0, MAX_KIND_CHECKS - self._kind_checks)]
        self._kind_checks += len(todo)
        if not todo:
            return out
        with ThreadPoolExecutor(self.workers) as pool:
            for vid, info in zip(todo, pool.map(self._safe_watch, todo)):
                if not info or not info["kind"]:
                    continue
                kind = info["kind"]
                out[vid] = kind
                if kind in FINAL_KINDS:
                    self.kinds[vid] = kind
                    self._passing.pop(vid, None)
                else:
                    self._passing[vid] = (kind, now)
                if info["category"] and vid not in self.categories:
                    self.categories[vid] = info["category"]
        return out

    def _safe_watch(self, vid: str) -> dict | None:
        try:
            return watch_info(vid, self._fetch)
        except OSError as exc:
            log.info("watch page %s failed: %s", vid, exc)
            return None

    def _safe_check(self, vid: str) -> bool | None:
        try:
            return check_short(vid, self._fetch)
        except OSError as exc:
            log.info("Shorts check %s failed: %s", vid, exc)
            return None
