"""Watched state and channel list from a browser's YouTube login (cookies + InnerTube).

`CookieYouTube` owns the cookie copy (yt_cookies.CookieStore) and a read-only
InnerTube session acting as the channel `channel_name` (`[youtube] channel` in
config.toml: a channel name or @handle, found in the channel switcher of each
signed-in Google account). It answers:

- `watch_state()`: per video id the red-bar progress from the subscriptions
  feed (FEsubscriptions, first page) and recent history (FEhistory, first page,
  about a week), at most every 30 min; `None` while unavailable.
- `channels()`: the subscribed channels (FEchannels), for youtube.Subscriptions.

On failure the last good watch state is used for up to `grace` seconds; after
that (or at once when the login itself is gone) the state is "unavailable" and
one ntfy goes out, re-armed by the next success.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import innertube
import net
import notify as notify_mod
import yt_cookies

log = logging.getLogger("tvhome.ytwatched")

WATCH_EVERY = 30 * 60.0  # never more often than this
IDENTITY_EVERY = 24 * 3600.0
GRACE = 2 * 3600.0  # a transient failure keeps the last watch state this long
WATCHED_AT = 0.9  # hide at 90 % watched or more
CONTINUE_FROM = 0.11  # past YouTube's 10 % minimum bar: anything less was a glance or a song
CONTINUE_LIMIT = 5  # one grid row
MAX_AUTH_USERS = 5
MAX_CHANNEL_PAGES = 20

FIX = ("Open youtube.com once in the browser TV Home copies its login from, signed in with the {name} "
       "channel; TV Home copies the login within an hour while that machine is on.")
MESSAGES = {
    "nocookies": "TV Home has no YouTube login from the browser yet, so watched videos are not hidden. " + FIX,
    "signedout": "TV Home's YouTube login from the browser stopped working (signed out or expired), so watched "
                 "videos show again on the TV. " + FIX,
    "nochannel": "TV Home cannot find the YouTube channel {name} in the browser's Google login, so watched videos "
                 "show again on the TV. " + FIX,
    "failing": "TV Home has not been able to read YouTube's watched state for over 2 h ({error}); watched "
               "videos show again on the TV. If it persists: " + FIX,
}


class NoChannel(innertube.SessionError):
    """Signed in, but no account offers the wanted channel."""


@dataclass
class Identity:
    auth_user: int
    page_id: str | None
    datasync: str
    found_at: float


@dataclass
class WatchState:
    progress: dict[str, float] = field(default_factory=dict)  # video id -> 0.0-1.0
    fetched_at: float = 0.0
    history: list[dict] = field(default_factory=list)  # parsed FEhistory, most recently watched first

    def unfinished(self, limit: int | None = None) -> list[dict]:
        """Videos watched in the last week and left between CONTINUE_FROM and the
        watched threshold, newest first. No limit by default: the caller drops
        music first, then cuts to CONTINUE_LIMIT."""
        out, seen = [], set()
        for v in self.history:
            if not v.get("recent", True):
                break  # history is newest first: everything after this is older than a week
            if v["id"] in seen or (v.get("channel") or "").endswith(" - Topic"):
                continue  # repeats, and YouTube Music's auto-generated "Artist - Topic" tracks
            seen.add(v["id"])
            p = self.progress.get(v["id"])
            if p is not None and CONTINUE_FROM <= p < WATCHED_AT:
                out.append({**v, "progress": p})
                if limit is not None and len(out) >= limit:
                    break
        return out

    def hidden(self, threshold: float = WATCHED_AT) -> set[str]:
        return {vid for vid, p in self.progress.items() if p >= threshold}


def merge_progress(*lists: list[dict]) -> dict[str, float]:
    """id -> progress over parsed video lists; the highest bar wins (history repeats rewatched videos)."""
    out: dict[str, float] = {}
    for videos in lists:
        for v in videos:
            p = v.get("progress")
            if p is not None and p > out.get(v["id"], -1.0):
                out[v["id"]] = p
    return out


class CookieYouTube:
    def __init__(self, store: yt_cookies.CookieStore, state_path: Path, channel_name: str,
                 fetch: net.Fetch = net.request, notify: Callable[[str, str], bool] = notify_mod.send,
                 clock: Callable[[], float] = time.time, every: float = WATCH_EVERY, grace: float = GRACE):
        self.store = store
        self.state_path = state_path
        self.channel_name = channel_name
        self._fetch = fetch
        self._notify = notify
        self._clock = clock
        self.every = every
        self.grace = grace
        self._lock = threading.Lock()
        saved = net.read_json(state_path, {}) or {}
        self.notified: str | None = saved.get("notified")
        self.identity: Identity | None = None
        self.watch: WatchState | None = None
        self._last_attempt: float | None = None
        self._failing_since: float | None = None
        self.status_word = "unavailable"
        self.last_error: str | None = None

    # -- session ---------------------------------------------------------------

    def _client(self) -> innertube.Client:
        self.store.sync_if_due()
        cookies = self.store.cookies()
        if not cookies:
            raise yt_cookies.CookieError("no cookies copied from the browser yet")
        if not yt_cookies.has_login(cookies):
            raise innertube.SessionError("the browser's cookies hold no YouTube login")
        client = innertube.Client(cookies, fetch=self._fetch, clock=self._clock)
        if self.identity is None or self._clock() - self.identity.found_at >= IDENTITY_EVERY:
            self.identity = self._find_identity(client)
        client.auth_user = self.identity.auth_user
        client.page_id = self.identity.page_id
        client.user_session_id = innertube.user_session_id(self.identity.datasync)
        return client

    def _find_identity(self, client: innertube.Client) -> Identity:
        """The (session index, page id) whose channel switcher lists `channel_name`."""
        seen_any = False
        for auth_user in range(MAX_AUTH_USERS):
            client.auth_user, client.page_id, client.user_session_id = auth_user, None, None
            data = client.accounts_list()
            if innertube.logged_out(data) or not innertube.datasync_id(data):
                break  # no Google account at this index
            seen_any = True
            for acc in innertube.parse_accounts(data):
                if self.channel_name in (acc["name"], (acc["handle"] or "").lstrip("@")) and acc["datasyncId"]:
                    log.info("YouTube channel %s: session index %d, %s", self.channel_name, auth_user,
                             "brand channel" if acc["pageId"] else "account's own channel")
                    return Identity(auth_user, acc["pageId"], acc["datasyncId"], self._clock())
        if not seen_any:
            raise innertube.SessionError("YouTube answers as signed out")
        raise NoChannel(f"no channel named {self.channel_name!r} in the signed-in accounts")

    def _browse(self, client: innertube.Client, browse_id: str | None = None,
                continuation: str | None = None) -> dict:
        data = client.browse(browse_id, continuation)
        if innertube.logged_out(data):
            self.identity = None
            raise innertube.SessionError(f"{browse_id or 'continuation'}: YouTube answers as signed out")
        got = innertube.datasync_id(data)
        if got is not None and self.identity and got != self.identity.datasync:
            self.identity = None
            raise innertube.SessionError(f"{browse_id or 'continuation'}: answered for another channel")
        return data

    # -- the two answers -------------------------------------------------------

    def channels(self) -> list[dict]:
        """Every subscribed channel as {"id", "title"}; raises on any failure (the caller falls back)."""
        with self._lock:
            client = self._client()
            data = self._browse(client, "FEchannels")
            out = innertube.parse_channels(data)
            for _ in range(MAX_CHANNEL_PAGES):
                token = innertube.continuation(data)
                if not token:
                    break
                data = self._browse(client, continuation=token)
                out.extend(innertube.parse_channels(data))
            if not out:
                raise OSError("FEchannels listed no channels")
            seen: set[str] = set()
            return [c for c in out if not (c["id"] in seen or seen.add(c["id"]))]

    def watch_state(self) -> WatchState | None:
        """The current watch state, refreshed at most every `every` s; None while unavailable."""
        with self._lock:
            now = self._clock()
            if self._last_attempt is None or now - self._last_attempt >= self.every:
                self._last_attempt = now
                self._refresh(now)
            return self.watch if self.status_word == "ok" else None

    def _refresh(self, now: float) -> None:
        try:
            client = self._client()
            subs = innertube.parse_videos(self._browse(client, "FEsubscriptions"))
            history = innertube.parse_history(self._browse(client, "FEhistory"))
        except Exception as exc:  # noqa: BLE001 - every failure ends up as a state, never a crash
            self._failed(now, exc)
            return
        self.watch = WatchState(merge_progress(subs, history), now, history)
        self.status_word = "ok"
        self.last_error = None
        self._failing_since = None
        log.info("watch state: %d feed and %d history videos, %d with progress, %d watched",
                 len(subs), len(history), len(self.watch.progress), len(self.watch.hidden()))
        if self.notified:
            self.notified = None
            self._save()

    def _failed(self, now: float, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        log.warning("watch state failed: %s", self.last_error)
        if self._failing_since is None:
            self._failing_since = now
        if isinstance(exc, yt_cookies.CookieError):
            kind = "nocookies"
        elif isinstance(exc, NoChannel):
            kind = "nochannel"
        elif isinstance(exc, innertube.SessionError):
            kind = "signedout"
        else:
            kind = "failing"
            if now - self._failing_since < self.grace:
                if self.watch is None:  # nothing to keep serving, but too early to alert
                    self.status_word = "unavailable"
                return  # transient: keep serving the last good state
        if kind != "failing":
            self.identity = None
        self.status_word = "unavailable"
        self.watch = None
        if not self.notified:
            text = MESSAGES[kind].format(name=self.channel_name, error=self.last_error)
            self._notify("TV Home: YouTube watched state unavailable", text)
            self.notified = kind
            self._save()

    def _save(self) -> None:
        try:
            net.write_json(self.state_path, {"notified": self.notified,
                                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        except OSError as exc:
            log.warning("cannot save %s: %s", self.state_path, exc)

    def status(self) -> dict:
        return {"watched": self.status_word, "lastError": self.last_error, "cookies": self.store.status(),
                "channel": None if self.identity is None else {
                    "authUser": self.identity.auth_user, "brand": self.identity.page_id is not None}}
