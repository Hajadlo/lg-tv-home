"""Google OAuth for the YouTube Data API: device flow, token refresh, subscriptions.

The OAuth client is a "TVs and Limited Input devices" client; its downloaded
JSON lives at ~/.config/tv-home/google-oauth-client.json. `youtube-auth` runs
the device flow once and stores the refresh token in google-oauth-token.json
(mode 600). The server then mints access tokens itself.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import net

log = logging.getLogger("tvhome.google")

SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SUBSCRIPTIONS_URL = "https://www.googleapis.com/youtube/v3/subscriptions"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_PAGES = 40  # 2000 subscriptions


class AuthMissing(Exception):
    """No client JSON or no stored refresh token."""


class AuthExpired(Exception):
    """Google rejected the refresh token (revoked, expired, or the client was deleted)."""


class GoogleError(Exception):
    """A transient or unexpected API failure."""


def load_client(path: Path) -> dict | None:
    """client_id/client_secret/token_uri from a downloaded client JSON (any of its shapes)."""
    raw = net.read_json(path)
    if not isinstance(raw, dict):
        return None
    for key in ("installed", "web"):
        if isinstance(raw.get(key), dict):
            raw = raw[key]
            break
    if not raw.get("client_id"):
        return None
    return {"client_id": raw["client_id"], "client_secret": raw.get("client_secret", ""),
            "token_uri": raw.get("token_uri") or TOKEN_URL}


def _error_code(resp: net.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"http_{resp.status}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):  # API errors: {"error": {"code", "message", "errors": [...]}}
        reasons = [e.get("reason") for e in err.get("errors", []) if isinstance(e, dict)]
        return reasons[0] if reasons and reasons[0] else str(err.get("status") or resp.status)
    return str(err or f"http_{resp.status}")


# -- device flow (youtube-auth) ------------------------------------------------

def start_device_flow(client: dict, fetch: net.Fetch = net.request) -> dict:
    resp = fetch(DEVICE_CODE_URL, method="POST", data={"client_id": client["client_id"], "scope": SCOPE})
    if resp.status != 200:
        raise GoogleError(f"device code request failed: {_error_code(resp)} {resp.body[:300]!r}")
    return resp.json()


def poll_device_flow(client: dict, device: dict, fetch: net.Fetch = net.request,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> dict:
    """Poll until the user approves; return Google's token response."""
    interval = float(device.get("interval") or 5)
    deadline = clock() + float(device.get("expires_in") or 1800)
    while clock() < deadline:
        sleep(interval)
        resp = fetch(client["token_uri"], method="POST", data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "device_code": device["device_code"], "grant_type": DEVICE_GRANT})
        if resp.status == 200:
            return resp.json()
        code = _error_code(resp)
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval += 5
            continue
        raise GoogleError(f"device flow ended: {code}")
    raise GoogleError("device flow ended: the code expired before approval")


def save_token(path: Path, token: dict) -> None:
    if not token.get("refresh_token"):
        raise GoogleError("Google returned no refresh token")
    # The same four keys the parent's first sign-in wrote; the server reads only refresh_token.
    net.write_json(path, {"refresh_token": token["refresh_token"], "scope": token.get("scope", SCOPE),
                          "token_type": token.get("token_type", "Bearer"),
                          "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


# -- server side ---------------------------------------------------------------

class GoogleAuth:
    """Access tokens from the stored refresh token, refreshed when near expiry."""

    def __init__(self, client_path: Path, token_path: Path, fetch: net.Fetch = net.request,
                 clock: Callable[[], float] = time.time):
        self.client_path = client_path
        self.token_path = token_path
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._access: str | None = None
        self._expires = 0.0
        self._token_mtime: float | None = None

    def token_mtime(self) -> float | None:
        try:
            return self.token_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def invalidate(self) -> None:
        with self._lock:
            self._access = None

    def access_token(self) -> str:
        with self._lock:
            mtime = self.token_mtime()
            if self._access and self._clock() < self._expires - 60 and mtime == self._token_mtime:
                return self._access
            client = load_client(self.client_path)
            token = net.read_json(self.token_path) or {}
            if not client:
                raise AuthMissing(f"no OAuth client at {self.client_path}")
            if not token.get("refresh_token"):
                raise AuthMissing(f"no refresh token at {self.token_path}; run youtube-auth")
            try:
                resp = self._fetch(client["token_uri"], method="POST", data={
                    "client_id": client["client_id"], "client_secret": client["client_secret"],
                    "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
            except OSError as exc:
                raise GoogleError(f"token refresh: {exc}") from exc
            if resp.status != 200:
                code = _error_code(resp)
                if code in ("invalid_grant", "invalid_client", "unauthorized_client", "deleted_client"):
                    raise AuthExpired(code)
                raise GoogleError(f"token refresh: {code}")
            body = resp.json()
            if body.get("refresh_token") and body["refresh_token"] != token["refresh_token"]:
                save_token(self.token_path, {**token, **body})
                mtime = self.token_mtime()
            self._access = body["access_token"]
            self._expires = self._clock() + float(body.get("expires_in") or 3600)
            self._token_mtime = mtime
            return self._access


def fetch_subscriptions(auth: GoogleAuth, fetch: net.Fetch = net.request) -> list[dict]:
    """Every subscribed channel as {"id", "title"}, in the API's order."""
    channels: list[dict] = []
    page = None
    retried = False
    for _ in range(MAX_PAGES):
        params = {"part": "snippet", "mine": "true", "maxResults": "50"}
        if page:
            params["pageToken"] = page
        url = SUBSCRIPTIONS_URL + "?" + urllib.parse.urlencode(params)
        try:
            resp = fetch(url, headers={"Authorization": f"Bearer {auth.access_token()}"})
        except OSError as exc:
            raise GoogleError(f"subscriptions: {exc}") from exc
        if resp.status == 401 and not retried:
            auth.invalidate()
            retried = True
            continue
        if resp.status != 200:
            raise GoogleError(f"subscriptions: HTTP {resp.status} {_error_code(resp)}")
        body = resp.json()
        for item in body.get("items", []):
            snippet = item.get("snippet") or {}
            cid = (snippet.get("resourceId") or {}).get("channelId")
            if cid:
                channels.append({"id": cid, "title": snippet.get("title") or ""})
        page = body.get("nextPageToken")
        if not page:
            return channels
    return channels
