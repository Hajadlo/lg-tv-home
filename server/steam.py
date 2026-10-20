"""Steam section: recently played games, read from the gaming PC's Steam files over ssh.

Only while the PC is already on (never wakes it), and only when `[steam]
ssh_host` is set in config.toml. The last result is kept in
~/.cache/tv-home/steam.json, so the section survives restarts and shows while
the PC sleeps. Reads (all read-only, `ssh <ssh_host> cat`), under `steam_root`:

- config/loginusers.vdf: the active account (MostRecent, else newest timestamp;
  `steam_user_id` in config.toml overrides it)
- steamapps/libraryfolders.vdf: library paths
- userdata/<accountid>/config/localconfig.vdf: LastPlayed and Playtime (minutes) per app
- <library>/steamapps/appmanifest_*.acf: names and LastPlayed of installed apps
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import net

log = logging.getLogger("tvhome.steam")

STEAM_ROOT = "~/.local/share/Steam"
STEAMID64_BASE = 76561197960265728
SEP = "@@TVHOME-SEP@@"
GAME_LIMIT = 12
REFRESH_S = 10 * 60.0
APPDETAILS = "https://store.steampowered.com/api/appdetails?appids={}&filters=basic"
NAME_RETRY_S = 30 * 86400  # an appid the store does not know is asked again after this
# Steam tools that show up in localconfig/manifests with a LastPlayed.
TOOL_IDS = {7, 760, 228980, 1070560, 1391110, 1628350, 4183110, 1493710, 2180100, 1826330, 1887720,
            2348590, 2805730, 3658110, 961940, 1113280, 1054830, 1245040, 1420170, 1580130, 858280,
            930400, 996510, 1161040, 4099820}
TOOL_NAME = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common Redistributables|"
                       r"Steamworks SDK|Steam Audio|SteamVR|Ubisoft Connect|EA App|"
                       r"Proton EasyAntiCheat|Proton BattlEye)", re.I)
GAME_TYPES = {"game", "demo", "mod", "application", None}
ASSET_BASES = ("https://shared.steamstatic.com/store_item_assets/steam/apps/{}/",
               "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{}/",
               "https://cdn.cloudflare.steamstatic.com/steam/apps/{}/")
# library_600x900.jpg is 300x450 today; the _2x file is the 600x900 cover.
ASSET_FILES = {"cover": ("library_600x900_2x.jpg", "library_600x900.jpg"),
               "hero": ("library_hero.jpg",),
               "logo": ("logo.png",),
               "header": ("header.jpg",)}


# -- KeyValues (VDF) -----------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(//[^\n]*)|("(?:[^"\\]|\\.)*")|(\{)|(\})|([^\s{}"]+))', re.S)


def _unquote(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s[1:-1])


def parse_vdf(text: str) -> dict:
    """Text KeyValues to nested dicts (later duplicate keys win)."""
    root: dict = {}
    stack = [root]
    key = None
    pos = 0
    text = text.lstrip("﻿")
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        comment, quoted, opened, closed, bare = m.groups()
        if comment is not None:
            continue
        if opened:
            child: dict = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif closed:
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            value = _unquote(quoted) if quoted is not None else bare
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
    return root


def get_ci(d: dict, *path: str):
    """Case-insensitive nested lookup; None when any step is missing."""
    cur = d
    for part in path:
        if not isinstance(cur, dict):
            return None
        low = part.lower()
        cur = next((v for k, v in cur.items() if k.lower() == low), None)
    return cur


def active_account(loginusers: dict) -> int | None:
    users = get_ci(loginusers, "users") or {}
    best = None
    for steamid, info in users.items():
        if not isinstance(info, dict) or not steamid.isdigit():
            continue
        recent = str(get_ci(info, "MostRecent") or "0") == "1"
        stamp = int(get_ci(info, "Timestamp") or 0)
        rank = (recent, stamp)
        if best is None or rank > best[0]:
            best = (rank, int(steamid))
    return None if best is None else best[1] - STEAMID64_BASE


def library_paths(libraryfolders: dict) -> list[str]:
    folders = get_ci(libraryfolders, "libraryfolders") or {}
    return [v["path"] for v in folders.values() if isinstance(v, dict) and v.get("path")]


def is_tool(appid: int, name: str | None, kind: str | None = None) -> bool:
    return appid in TOOL_IDS or bool(name and TOOL_NAME.match(name)) or kind not in GAME_TYPES


def played_apps(localconfig: dict, manifests: list[dict]) -> dict[int, dict]:
    """appid -> {"lastPlayed": epoch, "playtimeMin": int | None, "name": str | None}."""
    apps: dict[int, dict] = {}
    for key, info in (get_ci(localconfig, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
                      or {}).items():
        if not key.isdigit() or not isinstance(info, dict):
            continue
        last = int(get_ci(info, "LastPlayed") or 0)
        minutes = get_ci(info, "Playtime")
        apps[int(key)] = {"lastPlayed": last, "playtimeMin": int(minutes) if minutes else None, "name": None}
    for manifest in manifests:
        state = get_ci(manifest, "AppState") or {}
        appid = str(state.get("appid", ""))
        if not appid.isdigit():
            continue
        entry = apps.setdefault(int(appid), {"lastPlayed": 0, "playtimeMin": None, "name": None})
        entry["name"] = state.get("name") or entry["name"]
        entry["lastPlayed"] = max(entry["lastPlayed"], int(state.get("LastPlayed") or 0))
    return apps


def iso_local(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def to_game(appid: int, name: str, last: int, minutes: int | None) -> dict:
    base = f"/api/steam/{appid}"
    return {"appid": appid, "name": name, "lastPlayed": iso_local(last),
            "playtimeH": None if minutes is None else round(minutes / 60, 1),
            "cover": f"{base}/cover", "hero": f"{base}/hero", "logo": f"{base}/logo", "header": f"{base}/header"}


def asset_candidates(appid: int, kind: str, extra: str | None = None) -> list[str]:
    urls = [b.format(appid) + f for b in ASSET_BASES for f in ASSET_FILES[kind]]
    if extra:
        urls.append(extra)
    return urls


# -- names from the store ------------------------------------------------------

class StoreNames:
    """appid -> {"name", "type", "header"} from the store's appdetails, cached on disk forever."""

    def __init__(self, path: Path, fetch: net.Fetch = net.request, clock: Callable[[], float] = time.time):
        self.path = path
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self.cache: dict[str, dict] = net.read_json(path, {}) or {}

    def get(self, appid: int, lookup: bool = True) -> dict | None:
        key = str(appid)
        with self._lock:
            hit = self.cache.get(key)
        if hit and (hit.get("name") or self._clock() - hit.get("at", 0) < NAME_RETRY_S):
            return hit
        if not lookup:
            return hit
        try:
            resp = self._fetch(APPDETAILS.format(appid), timeout=10)
            body = resp.json() if resp.status == 200 else None
        except (OSError, ValueError) as exc:
            log.info("appdetails %s: %s", appid, exc)
            return hit
        if body is None:
            return hit
        info = (body or {}).get(key) or {}
        data = (info.get("data") or {}) if info.get("success") else {}
        entry = {"name": data.get("name"), "type": data.get("type"), "header": data.get("header_image"),
                 "at": round(self._clock())}
        with self._lock:
            self.cache[key] = entry
            net.write_json(self.path, self.cache)
        return entry


# -- reading the desktop ---------------------------------------------------------

def ssh_reader(host: str, timeout: float = 20) -> Callable[[str], str]:
    def run(command: str) -> str:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", host, command],
                                capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            raise OSError(f"ssh {host} exited {result.returncode}: {result.stderr.strip()[:200]}")
        return result.stdout
    return run


def _cd_root(root: str) -> str:
    return '"$HOME"/' + shlex.quote(root[2:]) if root.startswith("~/") else shlex.quote(root)


def read_desktop(run: Callable[[str], str], root: str = STEAM_ROOT, user_id: int = 0) -> tuple[dict, list[dict]]:
    """(localconfig, manifests) of the active account; two read-only ssh calls."""
    cd = _cd_root(root)
    first = run(f"cd {cd} && cat config/loginusers.vdf && echo {SEP} && cat steamapps/libraryfolders.vdf")
    login_text, _, libs_text = first.partition(SEP)
    account = user_id or active_account(parse_vdf(login_text))
    if account is None:
        raise OSError("no Steam account in loginusers.vdf")
    libs = library_paths(parse_vdf(libs_text)) or ["."]
    globs = " ".join(shlex.quote(p) + "/steamapps/appmanifest_*.acf" for p in libs)
    second = run(f"cd {cd} && cat userdata/{int(account)}/config/localconfig.vdf && "
                 f"for f in {globs}; do [ -r \"$f\" ] && echo {SEP} && cat \"$f\"; done; true")
    parts = second.split(SEP)
    return parse_vdf(parts[0]), [parse_vdf(p) for p in parts[1:]]


class SteamSource:
    """The contract's `steam` object. Reads the desktop only while it is on."""

    def __init__(self, desktop_on: Callable[[], bool], state_path: Path, names: StoreNames,
                 read: Callable[[], tuple[dict, list[dict]]] | None = None, limit: int = GAME_LIMIT,
                 every: float = REFRESH_S, clock: Callable[[], float] = time.time):
        self.desktop_on = desktop_on
        self.state_path = state_path
        self.names = names
        self._read = read
        self.limit = limit
        self.every = every
        self._clock = clock
        self.last: dict | None = net.read_json(state_path)
        self.last_read: float | None = None  # this process's last successful desktop read

    def due(self) -> bool:
        """The desktop is on and its data is older than `every` (lets /api/home poke us)."""
        return self._read is not None and self.desktop_on() and (self.last_read is None or self._clock() - self.last_read >= self.every)

    def __call__(self) -> dict | None:
        if self._read is None or not self.desktop_on():
            return self.last
        localconfig, manifests = self._read()
        games = self.build(played_apps(localconfig, manifests))
        self.last = {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "recent": games}
        self.last_read = self._clock()
        net.write_json(self.state_path, self.last)
        return self.last

    def build(self, apps: dict[int, dict]) -> list[dict]:
        games = []
        for appid, info in sorted(apps.items(), key=lambda kv: kv[1]["lastPlayed"], reverse=True):
            if len(games) >= self.limit:
                break
            if info["lastPlayed"] <= 0 or is_tool(appid, info["name"]):
                continue
            store = self.names.get(appid, lookup=not info["name"]) or {}
            name = info["name"] or store.get("name")
            if not name or is_tool(appid, name, store.get("type")):
                continue
            games.append(to_game(appid, name, info["lastPlayed"], info["playtimeMin"]))
        return games
