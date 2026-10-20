"""Background refresh with stale serving.

A Refresher calls a slow fetch function on its own thread and keeps the last
good value. Readers never wait on the source: get() returns at once, giving the
last good value (or None before the first success) however slow or broken the
source is.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("tvhome.refresher")


class Refresher:
    def __init__(self, name: str, fetch: Callable[[], Any], interval: float,
                 retry: float | None = None, clock: Callable[[], float] = time.time):
        self.name = name
        self._fetch = fetch
        self.interval = interval
        self.retry = retry if retry is not None else min(interval, 30.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.value: Any = None
        self.last_success: float | None = None
        self.last_attempt: float | None = None
        self.last_error: str | None = None
        self.busy = False

    def get(self) -> Any:
        with self._lock:
            return self.value

    def refresh_now(self) -> bool:
        """Run one fetch on the calling thread; keep the old value on failure."""
        with self._lock:
            self.busy = True
            self.last_attempt = self._clock()
        try:
            value = self._fetch()
        except Exception as exc:  # any source failure keeps the last good value
            with self._lock:
                self.busy = False
                self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("%s refresh failed: %s", self.name, self.last_error)
            return False
        with self._lock:
            self.busy = False
            self.value = value
            self.last_success = self._clock()
            self.last_error = None
        return True

    def poke(self, min_age: float) -> None:
        """Ask the thread for an early refresh if the value is older than min_age."""
        with self._lock:
            age = None if self.last_attempt is None else self._clock() - self.last_attempt
            if self.busy or (age is not None and age < min_age):
                return
        self._wake.set()

    def start(self) -> "Refresher":
        self._thread = threading.Thread(target=self._run, name=f"refresh-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            ok = self.refresh_now()
            self._wake.wait(self.interval if ok else self.retry)
            self._wake.clear()

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            return {
                "ok": self.last_error is None and self.last_success is not None,
                "loaded": self.value is not None,
                "ageS": None if self.last_success is None else round(now - self.last_success),
                "lastError": self.last_error,
            }
