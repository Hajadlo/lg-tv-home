"""YouTube cookies from a Firefox-family browser (Firefox, Zen, LibreWolf, ...).

The browser where you are signed in to YouTube is the login TV Home uses.
`[youtube] browser_profile` in config.toml names its profile directory (the
one holding cookies.sqlite), or the browser's root directory (with
installs.ini; the default profile is picked). With `[youtube] ssh_host` the
path is on that machine and is read with one read-only ssh call, only while
it is reachable (nothing wakes it).

About hourly, the profile's `cookies.sqlite` and its `-wal` are streamed as a
tar into a private temp dir here; only the `.youtube.com` cookies are kept, in
~/.config/tv-home/yt-cookies.json (600). The temp copy is deleted at once.
Cookie values are never logged.
"""

from __future__ import annotations

import io
import logging
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import net

log = logging.getLogger("tvhome.ytcookies")

HOSTS = (".youtube.com",)  # only these go to youtube.com; keep the Google session out
DB_FILES = ("cookies.sqlite", "cookies.sqlite-wal")
SYNC_EVERY = 3600.0
SYNC_RETRY = 10 * 60.0
# Read-only: cd, sed, ls, tar to stdout. Profile names contain spaces and parentheses.
# $ROOT is a profile dir (has cookies.sqlite) or a browser root: then installs.ini's
# Default=, else the newest */cookies.sqlite.
REMOTE = (
    "cd \"$ROOT\" || exit 3; p=.; "
    "[ -f cookies.sqlite ] || p=$(sed -n 's/^Default=//p' installs.ini 2>/dev/null | head -n 1); "
    "[ -n \"$p\" ] && [ -f \"$p/cookies.sqlite\" ] || "
    "p=$(ls -t -- */cookies.sqlite 2>/dev/null | head -n 1 | sed 's,/cookies.sqlite$,,'); "
    "[ -n \"$p\" ] && [ -f \"$p/cookies.sqlite\" ] || exit 4; "
    "cd -- \"$p\" || exit 3; printf 'profile:%s\\n' \"$(basename \"$PWD\")\" >&2; "
    "if [ -f cookies.sqlite-wal ]; then tar -cf - cookies.sqlite cookies.sqlite-wal; "
    "else tar -cf - cookies.sqlite; fi"
)


def remote_script(profile: str) -> str:
    """REMOTE with ROOT set; `~/` is expanded by the shell that runs it."""
    profile = profile.removesuffix("/cookies.sqlite")
    if profile.startswith("~/"):
        root = '"$HOME"/' + shlex.quote(profile[2:])
    else:
        root = shlex.quote(profile)
    return f"ROOT={root}; {REMOTE}"


class CookieError(Exception):
    """The browser's cookie store could not be read or holds no YouTube login."""


def tar_reader(profile: str, ssh_host: str = "", timeout: float = 60) -> Callable[[], tuple[bytes, str]]:
    """A callable returning (tar bytes, profile dir name), run locally or over ssh."""
    script = remote_script(profile)

    def read() -> tuple[bytes, str]:
        if ssh_host:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", ssh_host, script]
        else:
            cmd = ["sh", "-c", script]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        err = result.stderr.decode(errors="replace").strip()
        if result.returncode != 0:
            where = f"ssh {ssh_host}" if ssh_host else "cookie copy"
            raise OSError(f"{where} exited {result.returncode}: {err[-200:]}")
        name = next((line[8:] for line in err.splitlines() if line.startswith("profile:")), "")
        return result.stdout, name

    return read


def unpack(tar_bytes: bytes, dest: Path) -> None:
    """Write only the two cookie files (regular files, plain names) into dest."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for member in tar.getmembers():
            if member.name not in DB_FILES or not member.isfile():
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            path = dest / member.name
            with open(path, "xb") as out:
                shutil.copyfileobj(src, out)
            path.chmod(0o600)
    if not (dest / "cookies.sqlite").is_file():
        raise CookieError("cookies.sqlite missing from the copy")


def _expiry_seconds(raw: int | None) -> int | None:
    """Firefox stores seconds; Firefox 132+ milliseconds. Normalise to seconds."""
    if not raw:
        return None
    return int(raw // 1000) if raw > 10 ** 11 else int(raw)


def read_db(db: Path, hosts: tuple[str, ...] = HOSTS, now: float | None = None) -> list[dict]:
    """Unexpired cookies of `hosts` from a Firefox cookies.sqlite (its -wal beside it is applied).

    Only the default container without a partition key (originAttributes "").
    """
    now = time.time() if now is None else now
    con = sqlite3.connect(f"file:{db}?mode=rw", uri=True)  # rw so SQLite can replay the WAL
    try:
        marks = ",".join("?" * len(hosts))
        rows = con.execute(
            f"SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies "
            f"WHERE host IN ({marks}) AND originAttributes = '' ORDER BY host, name, lastAccessed",
            hosts).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CookieError(f"cannot read cookies.sqlite: {exc}") from exc
    finally:
        con.close()
    out: dict[tuple, dict] = {}
    for host, name, value, path, expiry, secure, http_only in rows:
        exp = _expiry_seconds(expiry)
        if exp is not None and exp <= now:
            continue
        out[(host, name, path)] = {"host": host, "name": name, "value": value, "path": path or "/",
                                   "expiry": exp, "secure": bool(secure), "httpOnly": bool(http_only)}
    return list(out.values())


def has_login(cookies: list[dict]) -> bool:
    """A signed-in YouTube session: LOGIN_INFO plus one of the SAPISID cookies on .youtube.com."""
    names = {c["name"] for c in cookies if c["host"] == ".youtube.com"}
    return "LOGIN_INFO" in names and bool(names & {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"})


def extract(tar_bytes: bytes, now: float | None = None) -> list[dict]:
    """The wanted cookies from the browser's tar; the temp copy is removed before returning."""
    tmp = Path(tempfile.mkdtemp(prefix="tvhome-cookies-"))  # 0700
    try:
        unpack(tar_bytes, tmp)
        return read_db(tmp / "cookies.sqlite", now=now)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class CookieStore:
    """yt-cookies.json: {"synced", "profile", "cookies": [...]}, refreshed from the browser.

    `sync_if_due()` copies at most every `every` seconds (SYNC_RETRY after a
    failure) and only while `desktop_on()` says the browser's machine is up.
    """

    def __init__(self, path: Path, desktop_on: Callable[[], bool],
                 fetch_tar: Callable[[], tuple[bytes, str]] | None = None,
                 every: float = SYNC_EVERY, retry: float = SYNC_RETRY, clock: Callable[[], float] = time.time):
        self.path = path
        self._desktop_on = desktop_on
        self._fetch_tar = fetch_tar or (lambda: (_ for _ in ()).throw(CookieError("no browser_profile configured")))
        self.every = every
        self.retry = retry
        self._clock = clock
        self._lock = threading.Lock()
        self._last_attempt: float | None = None
        self._last_ok = False
        self.last_error: str | None = None
        self.last_sync: float | None = None

    def load(self) -> dict | None:
        data = net.read_json(self.path)
        return data if isinstance(data, dict) and isinstance(data.get("cookies"), list) else None

    def cookies(self) -> list[dict]:
        data = self.load()
        return data["cookies"] if data else []

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None

    def sync_if_due(self) -> bool:
        """True when a fresh copy was stored now."""
        with self._lock:
            now = self._clock()
            wait = self.every if self._last_ok else self.retry
            if self._last_attempt is not None and now - self._last_attempt < wait:
                return False
            if not self._desktop_on():
                return False
            self._last_attempt = now
            return self._sync()

    def sync_now(self) -> bool:
        with self._lock:
            self._last_attempt = self._clock()
            return self._sync()

    def _sync(self) -> bool:
        try:
            tar_bytes, profile = self._fetch_tar()
            cookies = extract(tar_bytes)
        except (OSError, CookieError, tarfile.TarError, subprocess.SubprocessError) as exc:
            self._last_ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("cookie sync failed: %s", self.last_error)
            return False
        self._last_ok = True
        self.last_error = None
        self.last_sync = self._clock()
        net.write_json(self.path, {"synced": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "profile": profile, "login": has_login(cookies), "cookies": cookies})
        log.info("cookie sync: %d cookies from browser profile %r, signed in: %s",
                 len(cookies), profile, has_login(cookies))
        return True

    def status(self) -> dict:
        data = self.load() or {}
        return {"synced": data.get("synced"), "login": data.get("login"), "count": len(data.get("cookies") or []),
                "lastError": self.last_error}
