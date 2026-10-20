"""Jellyfin: credentials, the three home rows, and the contract's Item mapping."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("tvhome.jellyfin")

# When Jellyfin runs on this machine, use 127.0.0.1 in [jellyfin] url: some
# container port forwards (rootless Podman) answer 503 on the machine's own LAN
# address while loopback and other LAN hosts work.
KEY_NAME = "tv-home"
ROW_LIMIT = 12
FETCH_LIMIT = 24  # fetch extra so rows stay full after dedupe
CLIENT_AUTH = 'MediaBrowser Client="TV Home", Device="tv-home-server", DeviceId="tv-home-server", Version="1.0"'
TICKS_PER_MINUTE = 600_000_000
KINDS = {"Episode": "episode", "Movie": "movie", "Series": "series", "Season": "series", "Video": "movie"}
LATEST_VIEWS = {"movies", "tvshows", "homevideos", None}


class JellyfinError(Exception):
    pass


class NotFound(JellyfinError):
    pass


def _request(method: str, url: str, headers: dict, body: Any = None, timeout: float = 8.0) -> bytes:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(url) from exc
        raise JellyfinError(f"HTTP {exc.code} for {method} {urllib.parse.urlsplit(url).path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JellyfinError(f"{method} {urllib.parse.urlsplit(url).path}: {exc}") from exc


def load_credentials(path: Path) -> dict | None:
    try:
        creds = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    return creds if creds.get("apiKey") and creds.get("userId") else None


def save_credentials(path: Path, creds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(creds, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def bootstrap(path: Path, url: str, user: str, password: str = "") -> dict:
    """Sign in once, reuse or create the `tv-home` API key, store it (mode 600).

    The sign-in session is logged out afterwards so no extra device lingers.
    """
    auth = json.loads(_request("POST", f"{url}/Users/AuthenticateByName",
                               {"Authorization": CLIENT_AUTH}, {"Username": user, "Pw": password}))
    session = {"Authorization": f'{CLIENT_AUTH}, Token="{auth["AccessToken"]}"'}
    try:
        def find_key() -> str | None:
            keys = json.loads(_request("GET", f"{url}/Auth/Keys", session)).get("Items", [])
            named = [k for k in keys if k.get("AppName") == KEY_NAME]
            named.sort(key=lambda k: k.get("DateCreated") or "")
            return named[-1]["AccessToken"] if named else None

        key = find_key()
        if key is None:
            _request("POST", f"{url}/Auth/Keys?app={KEY_NAME}", session)
            key = find_key()
        if key is None:
            raise JellyfinError("API key was created but is not listed")
    finally:
        try:
            _request("POST", f"{url}/Sessions/Logout", session)
        except JellyfinError:
            pass
    creds = {"url": url, "userId": auth["User"]["Id"], "userName": user, "apiKey": key}
    save_credentials(path, creds)
    log.info("stored Jellyfin API key %r for user %s in %s", KEY_NAME, user, path)
    return creds


class Jellyfin:
    def __init__(self, creds: dict, request: Callable = _request):
        self.url = creds["url"]
        self.user_id = creds["userId"]
        self._headers = {"Authorization": f'MediaBrowser Token="{creds["apiKey"]}"'}
        self._request = request

    def get_json(self, path: str, **params) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return json.loads(self._request("GET", f"{self.url}{path}?{query}", self._headers))

    def post(self, path: str, body: Any = None, **params) -> None:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        self._request("POST", f"{self.url}{path}?{query}", self._headers, body)

    def image(self, item_id: str, image_type: str, width: int | None) -> tuple[bytes, str]:
        params = {"quality": 90}
        if width:
            params["maxWidth"] = width
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}/Items/{item_id}/Images/{image_type}?{query}",
                                     headers={**self._headers, "Accept": "image/webp,image/*;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise NotFound(item_id) from exc
            raise JellyfinError(f"image HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JellyfinError(f"image fetch: {exc}") from exc

    def rows(self) -> dict:
        common = {"userId": self.user_id, "limit": FETCH_LIMIT, "enableImageTypes": "Primary,Backdrop,Thumb,Logo",
                  "imageTypeLimit": 1, "fields": "ProductionYear,DateCreated"}
        resume = self.get_json("/UserItems/Resume", mediaTypes="Video", **common)["Items"]
        next_up = self.get_json("/Shows/NextUp", enableResumable="false", enableRewatching="false",
                                **common)["Items"]
        # Latest without parentId groups across libraries badly (a handful of
        # items); ask each library and merge newest first.
        views = self.get_json("/UserViews", userId=self.user_id)["Items"]
        latest: list[dict] = []
        for view in views:
            if view.get("CollectionType") in LATEST_VIEWS:
                latest += self.get_json("/Items/Latest", parentId=view["Id"], groupItems="true", **common)
        latest.sort(key=lambda raw: raw.get("DateCreated") or "", reverse=True)
        return build_rows(resume, next_up, latest)


def _img(item_id: str | None, tag: Any, kind: str) -> str | None:
    if not item_id or not tag:
        return None
    return f"/api/img/{item_id}/{kind}"


def map_item(raw: dict) -> dict | None:
    """Jellyfin BaseItemDto -> contract Item; None for types the home page skips."""
    kind = KINDS.get(raw.get("Type", ""))
    if kind is None:
        return None
    item_id = raw["Id"]
    tags = raw.get("ImageTags") or {}
    user = raw.get("UserData") or {}
    episode = kind == "episode"
    season = raw.get("Type") == "Season"

    if season:
        title = raw.get("SeriesName") or raw.get("Name")
        # Season names can repeat the show: "The Good Place (2016) The Selection".
        name = raw.get("Name") or ""
        if title and name.startswith(title):
            name = re.sub(r"^\s*\(\d{4}\)", "", name[len(title):]).strip(" -·:")
        subtitle = name or None
    elif episode:
        title = raw.get("SeriesName") or raw.get("Name")
        parts = []
        if raw.get("ParentIndexNumber") is not None:
            parts.append(f"S{raw['ParentIndexNumber']}")
        if raw.get("IndexNumber") is not None:
            parts.append(f"E{raw['IndexNumber']}")
        if raw.get("Name"):
            parts.append(raw["Name"])
        subtitle = " · ".join(parts) or None
    else:
        title = raw.get("Name")
        unplayed = user.get("UnplayedItemCount")
        if kind == "series" and unplayed:
            subtitle = f"{unplayed} unwatched episode" + ("s" if unplayed != 1 else "")
        else:
            subtitle = None

    position = user.get("PlaybackPositionTicks") or 0
    runtime = raw.get("RunTimeTicks") or 0
    progress = minutes_left = None
    if position > 0 and runtime > 0:
        progress = round(min(position / runtime, 1.0), 3)
        minutes_left = max(math.ceil((runtime - position) / TICKS_PER_MINUTE), 0)

    # Own images first; an episode falls back to its series (Parent*/Series* fields).
    backdrop = _img(item_id, raw.get("BackdropImageTags"), "backdrop") or \
        _img(raw.get("ParentBackdropItemId"), raw.get("ParentBackdropImageTags"), "backdrop")
    logo = _img(item_id, tags.get("Logo"), "logo") or \
        _img(raw.get("ParentLogoItemId"), raw.get("ParentLogoImageTag"), "logo")
    if episode or season:
        own_still = _img(item_id, tags.get("Primary"), "primary") if episode else None
        thumb = own_still or _img(raw.get("ParentThumbItemId"), raw.get("ParentThumbImageTag"), "thumb") or backdrop
        poster = (_img(item_id, tags.get("Primary"), "poster") if season else None) or \
            _img(raw.get("SeriesId"), raw.get("SeriesPrimaryImageTag"), "poster")
    else:
        thumb = _img(item_id, tags.get("Thumb"), "thumb") or backdrop
        poster = _img(item_id, tags.get("Primary"), "poster")

    return {
        "id": item_id, "kind": kind, "title": title, "subtitle": subtitle,
        "year": raw.get("ProductionYear"), "progress": progress, "minutesLeft": minutes_left,
        "backdrop": backdrop, "thumb": thumb, "poster": poster, "logo": logo,
    }


def _series_of(raw: dict) -> str | None:
    if raw.get("Type") == "Series":
        return raw.get("Id")
    return raw.get("SeriesId")


def build_rows(resume: list[dict], next_up: list[dict], latest: list[dict], limit: int = ROW_LIMIT) -> dict:
    """Map and dedupe: each title appears once, in the first row that has it.

    Resume wins over next up wins over latest. A show already in resume or next
    up is dropped from the later rows whether the later card is the series or
    one of its episodes.
    """
    seen_ids: set[str] = set()
    seen_series: set[str] = set()

    def take(raws: list[dict]) -> list[dict]:
        out = []
        for raw in raws:
            if len(out) >= limit:
                break
            series = _series_of(raw)
            if raw.get("Id") in seen_ids or (series and series in seen_series):
                continue
            item = map_item(raw)
            if item is None:
                continue
            out.append(item)
            seen_ids.add(raw["Id"])
            if series:
                seen_series.add(series)
        return out

    return {"resume": take(resume), "nextUp": take(next_up), "latest": take(latest)}
