"""Twitch for the page: the followed channels live now, and the ones live lately.

The login is the configured Firefox-family browser's Twitch session: its `.twitch.tv`
cookies are copied like YouTube's (yt_cookies.CookieStore, twitch-cookies.json)
and the `auth-token` cookie authorises Twitch's own GraphQL API under the web
client's id, as twitch.tv itself does. Helix would need a registered app and
the `user:read:follows` scope, which the web token lacks. Twitch answers
`currentUser.follows` and `followedLiveUsers` only to a query that carries an
operationName ("service error" without one), and a second page of follows
(`after:` a cursor) fails Twitch's integrity check, so the follows come as the
newest 100 and the oldest 100 (`order: DESC` / `ASC`), one request.

Preview images are Twitch's public CDN URLs, loaded by the TV directly; their
`?t=` changes every PREVIEW_STEP_S so the TV fetches a fresh frame.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

import net
import yt_cookies

log = logging.getLogger("tvhome.twitch")

GQL_URL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # twitch.tv's own; the cookie token is issued to it
MAX_LIVE = 30
MAX_RECENT = 12
PREVIEW_STEP_S = 300
PREVIEW = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{w}x{h}.jpg?t={t}"

LIVE_QUERY = """query TvHomeFollowingLive {
  currentUser {
    followedLiveUsers(first: 100) { edges { node {
      login displayName profileImageURL(width: 150)
      stream { title viewersCount createdAt game { displayName } }
    } } }
  }
}"""
FOLLOW_FIELDS = """edges { node {
  login displayName profileImageURL(width: 300)
  lastBroadcast { startedAt game { displayName } }
} }"""
FOLLOWS_QUERY = f"""query TvHomeFollows {{
  currentUser {{
    newest: follows(first: 100, order: DESC) {{ {FOLLOW_FIELDS} }}
    oldest: follows(first: 100, order: ASC) {{ {FOLLOW_FIELDS} }}
  }}
}}"""


class AuthError(Exception):
    """Twitch rejected the copied token (signed out on the desktop, or rotated)."""


def token(cookies: list[dict]) -> str | None:
    return next((c["value"] for c in cookies if c.get("host") == ".twitch.tv" and c.get("name") == "auth-token"
                 and c.get("value")), None)


def gql(tok: str, name: str, query: str, variables: dict | None = None, fetch: net.Fetch = net.request) -> dict:
    """`data` of one named query; AuthError on 401, OSError on any other failure."""
    body = json.dumps({"operationName": name, "query": query, "variables": variables or {}}).encode()
    resp = fetch(GQL_URL, "POST", {"Client-Id": WEB_CLIENT_ID, "Authorization": "OAuth " + tok,
                                   "Content-Type": "application/json"}, body)
    if resp.status == 401:
        raise AuthError("Twitch answered 401")
    if resp.status != 200:
        raise OSError(f"Twitch GQL {name}: HTTP {resp.status}")
    out = resp.json()
    if out.get("errors") or not isinstance(out.get("data"), dict):
        raise OSError(f"Twitch GQL {name}: {str(out.get('errors'))[:200]}")
    if out["data"].get("currentUser") is None:
        raise AuthError("Twitch knows no current user for the token")
    return out["data"]["currentUser"]


def _edges(block: dict | None) -> list[dict]:
    return [e["node"] for e in ((block or {}).get("edges") or []) if e and e.get("node")]


def live_streams(user: dict, now: float) -> list[dict]:
    """Followed channels on air, most viewers first (Twitch's order)."""
    t = int(now // PREVIEW_STEP_S)
    out = []
    for n in _edges(user.get("followedLiveUsers")):
        s, login = n.get("stream"), n.get("login")
        if not s or not login:
            continue
        out.append({
            "login": login, "name": n.get("displayName") or login, "title": (s.get("title") or "").strip(),
            "game": (s.get("game") or {}).get("displayName"), "viewers": s.get("viewersCount"),
            "started": s.get("createdAt"), "avatar": n.get("profileImageURL"),
            "thumb": PREVIEW.format(login=login, w=640, h=360, t=t),
            "backdrop": PREVIEW.format(login=login, w=1280, h=720, t=t),
        })
    return out[:MAX_LIVE]


def recent_channels(follows: list[dict], live: set[str]) -> list[dict]:
    """Followed channels off air, the most recently live first."""
    out, seen = [], set(live)
    for n in follows:
        last = n.get("lastBroadcast") or {}
        if not n.get("login") or n["login"] in seen or not last.get("startedAt"):
            continue
        seen.add(n["login"])
        out.append({"login": n["login"], "name": n.get("displayName") or n["login"], "avatar": n.get("profileImageURL"),
                    "lastLive": last["startedAt"], "game": (last.get("game") or {}).get("displayName")})
    out.sort(key=lambda c: c["lastLive"], reverse=True)
    return out[:MAX_RECENT]


class TwitchSource:
    """The `twitch` block of /api/home, for a Refresher.

    {"updated", "auth": "ok" | "missing" | "expired", "live": [...], "recent": [...]}.
    A rejected token asks the store for a fresh copy once before saying "expired".
    """

    def __init__(self, store: yt_cookies.CookieStore, fetch: net.Fetch = net.request,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self._fetch = fetch
        self._clock = clock

    def __call__(self) -> dict:
        self.store.sync_if_due()
        try:
            return self._read()
        except AuthError:
            if not self.store.resync():
                return self._block("expired")
            try:
                return self._read()
            except AuthError:
                return self._block("expired")

    def _block(self, auth: str, live: list | None = None, recent: list | None = None) -> dict:
        return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "auth": auth,
                "live": live or [], "recent": recent or []}

    def _read(self) -> dict:
        tok = token(self.store.cookies())
        if not tok:
            return self._block("missing")
        live = live_streams(gql(tok, "TvHomeFollowingLive", LIVE_QUERY, fetch=self._fetch), self._clock())
        user = gql(tok, "TvHomeFollows", FOLLOWS_QUERY, fetch=self._fetch)
        follows = _edges(user.get("newest")) + _edges(user.get("oldest"))
        return self._block("ok", live, recent_channels(follows, {s["login"] for s in live}))
