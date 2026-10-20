"""Open a Jellyfin item on the TV through Jellyfin's own remote control.

The Jellyfin TV app ignores launch params, but its web client registers a
remote-controllable session with the server. Home launches the app and calls
`POST /api/jellyfin/open/<id>`; this waits for the TV's session, then sends
PlayNow (from the saved position) for playable items, or DisplayContent (the
item's page) for series and seasons. PlayNow is resent until the session
reports the item as playing, because a freshly started app may not route the
first command yet.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("tvhome.jf_remote")

PLAYABLE = {"Episode", "Movie", "Video", "MusicVideo"}
TV_DEVICE_HINTS = ("lg", "webos", "smart tv")


def pick_session(sessions: list[dict]) -> dict | None:
    """The TV's remote-controllable Jellyfin web session, most recently active first."""
    found = [s for s in sessions
             if s.get("SupportsRemoteControl")
             and s.get("Client") == "Jellyfin Web"
             and any(h in (s.get("DeviceName") or "").lower() for h in TV_DEVICE_HINTS)]
    found.sort(key=lambda s: s.get("LastActivityDate") or "", reverse=True)
    return found[0] if found else None


class Opener:
    """At most one open job at a time; a newer request replaces the older one."""

    def __init__(self, get_client: Callable, timeout: float = 30.0, poll: float = 1.0,
                 verify: float = 6.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._get_client = get_client
        self.timeout, self.poll, self.verify = timeout, poll, verify
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._generation = 0
        self.last: dict = {}

    def open(self, item_id: str) -> dict:
        with self._lock:
            self._generation += 1
            gen = self._generation
            self.last = {"itemId": item_id, "state": "waiting"}
        threading.Thread(target=self._run, args=(item_id, gen), daemon=True, name="jf-open").start()
        return dict(self.last)

    def _current(self, gen: int) -> bool:
        return gen == self._generation

    def _set(self, gen: int, **kw) -> None:
        with self._lock:
            if self._current(gen):
                self.last.update(kw)

    def _run(self, item_id: str, gen: int) -> None:
        try:
            self._job(item_id, gen)
        except Exception as exc:
            log.warning("open %s failed: %s", item_id, exc)
            self._set(gen, state="failed", error=f"{type(exc).__name__}: {exc}")

    def _job(self, item_id: str, gen: int) -> None:
        jf = self._get_client()
        item = jf.get_json(f"/Users/{jf.user_id}/Items/{item_id}")
        playable = item.get("Type") in PLAYABLE
        ticks = int(((item.get("UserData") or {}).get("PlaybackPositionTicks")) or 0)
        deadline = self._clock() + self.timeout
        sent_display = 0
        while self._clock() < deadline and self._current(gen):
            sessions = jf.get_json("/Sessions", controllableByUserId=jf.user_id, activeWithinSeconds=900)
            session = pick_session(sessions)
            if session is None:
                self._sleep(self.poll)
                continue
            sid = session["Id"]
            now_playing = (session.get("NowPlayingItem") or {}).get("Id")
            if playable and now_playing == item_id:
                self._set(gen, state="playing")
                log.info("Jellyfin on the TV is playing %s", item_id)
                return
            if playable:
                jf.post(f"/Sessions/{sid}/Playing", playCommand="PlayNow", itemIds=item_id,
                        startPositionTicks=ticks or None)
                self._set(gen, state="sent")
                waited = 0.0
                while waited < self.verify and self._current(gen):
                    self._sleep(self.poll)
                    waited += self.poll
                    s = pick_session(jf.get_json("/Sessions", controllableByUserId=jf.user_id,
                                                 activeWithinSeconds=900))
                    if s and (s.get("NowPlayingItem") or {}).get("Id") == item_id:
                        self._set(gen, state="playing")
                        log.info("Jellyfin on the TV is playing %s", item_id)
                        return
                continue
            # Series or season: show its page. The app cannot confirm it, so send
            # once when the session appears and once more after it has settled.
            jf.post(f"/Sessions/{sid}/Command", body={"Name": "DisplayContent", "Arguments": {
                "ItemId": item_id, "ItemType": item.get("Type") or "", "ItemName": item.get("Name") or ""}})
            sent_display += 1
            self._set(gen, state="shown")
            if sent_display >= 2:
                return
            self._sleep(3.0)
        if self._current(gen) and self.last.get("state") not in ("playing", "shown"):
            self._set(gen, state="failed", error="the TV's Jellyfin app did not take the command")
