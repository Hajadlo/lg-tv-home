"""Read-only YouTube InnerTube (WEB client) calls with the browser's cookies.

Authorization follows yt-dlp (extractor/youtube/_base.py, 2026-07): for each of
SAPISID (falling back to __Secure-3PAPISID), __Secure-1PAPISID and
__Secure-3PAPISID one `<SCHEME> <ts>_<sha1("[<user session id> ]<ts> <sid> <origin>")>[_u]`,
space-joined. Only `browse` and `account/accounts_list` are called here:
nothing that plays, records watch time or writes history.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable

import net

ORIGIN = "https://www.youtube.com"
API = ORIGIN + "/youtubei/v1/{}?prettyPrint=false"
CLIENT_NAME = "WEB"
CLIENT_ID = "1"
CLIENT_VERSION = "2.20260708.00.00"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SID_SCHEMES = (("SAPISIDHASH", ("SAPISID", "__Secure-3PAPISID")),
               ("SAPISID1PHASH", ("__Secure-1PAPISID",)),
               ("SAPISID3PHASH", ("__Secure-3PAPISID",)))


def local_timezone() -> str:
    """This machine's IANA zone (from /etc/localtime), so history's day headers match; else UTC."""
    try:
        target = os.path.realpath("/etc/localtime")
        return target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else "UTC"
    except OSError:
        return "UTC"


class SessionError(Exception):
    """YouTube did not accept the cookies as a signed-in session (signed out, rotated, expired)."""


def youtube_cookies(cookies: list[dict]) -> dict[str, str]:
    """name -> value for the cookies a www.youtube.com request carries (.youtube.com only)."""
    return {c["name"]: c["value"] for c in cookies if c.get("host") == ".youtube.com"}


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def make_sid_authorization(scheme: str, sid: str, origin: str, ts: int, user_session_id: str | None = None) -> str:
    parts = [user_session_id] if user_session_id else []
    parts += [str(ts), sid, origin]
    digest = hashlib.sha1(" ".join(parts).encode()).hexdigest()
    return f"{scheme} {ts}_{digest}" + ("_u" if user_session_id else "")


def authorization(jar: dict[str, str], origin: str = ORIGIN, ts: int | None = None,
                  user_session_id: str | None = None) -> str | None:
    ts = round(time.time()) if ts is None else ts
    out = []
    for scheme, names in SID_SCHEMES:
        sid = next((jar[n] for n in names if jar.get(n)), None)
        if sid:
            out.append(make_sid_authorization(scheme, sid, origin, ts, user_session_id))
    return " ".join(out) or None


class Client:
    """POSTs to /youtubei/v1/ with the cookie session; `page_id` selects a brand channel."""

    def __init__(self, cookies: list[dict], fetch: net.Fetch = net.request, clock: Callable[[], float] = time.time):
        self.jar = youtube_cookies(cookies)
        self._fetch = fetch
        self._clock = clock
        self.auth_user = 0  # Google's session index (several accounts signed in to the browser)
        self.page_id: str | None = None
        self.user_session_id: str | None = None

    def headers(self) -> dict:
        auth = authorization(self.jar, ts=round(self._clock()), user_session_id=self.user_session_id)
        if not auth or "LOGIN_INFO" not in self.jar:
            raise SessionError("no YouTube login cookies (LOGIN_INFO, SAPISID)")
        h = {"Content-Type": "application/json", "Cookie": cookie_header(self.jar), "Authorization": auth,
             "Origin": ORIGIN, "X-Origin": ORIGIN, "X-Youtube-Client-Name": CLIENT_ID,
             "X-Youtube-Client-Version": CLIENT_VERSION, "X-Goog-AuthUser": str(self.auth_user),
             "X-Youtube-Bootstrap-Logged-In": "true", "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
        if self.page_id:
            h["X-Goog-PageId"] = self.page_id
        return h

    def call(self, endpoint: str, body: dict, timeout: float = 20) -> dict:
        context = {"client": {"clientName": CLIENT_NAME, "clientVersion": CLIENT_VERSION, "hl": "en", "gl": "US",
                              "timeZone": local_timezone(), "utcOffsetMinutes": 0}}
        if self.page_id:
            context["user"] = {"onBehalfOfUser": self.page_id}
        payload = json.dumps({"context": context, **body}).encode()
        resp = self._fetch(API.format(endpoint), method="POST", headers=self.headers(), data=payload,
                           timeout=timeout)
        if resp.status in (401, 403):
            raise SessionError(f"{endpoint}: HTTP {resp.status}")
        if resp.status != 200:
            raise OSError(f"{endpoint}: HTTP {resp.status}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OSError(f"{endpoint}: not JSON") from exc
        if not isinstance(data, dict):
            raise OSError(f"{endpoint}: unexpected answer")
        return data

    def browse(self, browse_id: str | None = None, continuation: str | None = None) -> dict:
        body = {"continuation": continuation} if continuation else {"browseId": browse_id}
        return self.call("browse", body)

    def accounts_list(self) -> dict:
        """The channel switcher: every channel of the Google account at `auth_user`."""
        return self.call("account/accounts_list", {"requestType": "ACCOUNTS_LIST_REQUEST_TYPE_CHANNEL_SWITCHER",
                                                   "callCircumstance": "SWITCHING_USERS_FULL"})


# -- parsing (shapes checked against live answers on 2026-10-20) -----------------

NOT_CONTENT = frozenset({"header", "menu", "chipBarViewModel", "topbar", "secondaryContents"})


def _walk(obj, visit: Callable[[dict], bool], skip: frozenset = frozenset()) -> None:
    """Depth first over dicts; `visit` returns True to stop descending into that dict.

    Values under keys in `skip` are not visited.
    """
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if visit(o):
                continue
            stack.extend(reversed([v for k, v in o.items() if k not in skip]))
        elif isinstance(o, list):
            stack.extend(reversed(o))


def _text(t) -> str:
    if isinstance(t, str):
        return t
    if not isinstance(t, dict):
        return ""
    if "simpleText" in t:
        return t["simpleText"] or ""
    if "content" in t:
        return t["content"] or ""
    return "".join(r.get("text", "") for r in t.get("runs", []) if isinstance(r, dict))


def _percent(value) -> float | None:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, pct / 100.0))


def datasync_id(data: dict) -> str | None:
    ctx = (data.get("responseContext") or {}).get("mainAppWebResponseContext") or {}
    return ctx.get("datasyncId")


def logged_out(data: dict) -> bool:
    ctx = (data.get("responseContext") or {}).get("mainAppWebResponseContext") or {}
    return bool(ctx.get("loggedOut"))


def _lockup_video(lv: dict) -> dict | None:
    if lv.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO" or not lv.get("contentId"):
        return None
    progress = None

    def find(o: dict) -> bool:
        nonlocal progress
        bar = o.get("thumbnailOverlayProgressBarViewModel")
        if isinstance(bar, dict) and progress is None:
            progress = _percent(bar.get("startPercent", 0))
            return True
        return False

    _walk((lv.get("contentImage") or {}), find)
    md = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = _text(md.get("title"))
    channel = ""
    rows = ((md.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []
    if rows and (rows[0].get("metadataParts") or []):
        channel = _text((rows[0]["metadataParts"][0] or {}).get("text"))  # the byline row starts with the channel
    return {"id": lv["contentId"], "title": title, "channel": channel, "progress": progress}


def _renderer_video(vr: dict) -> dict | None:
    """Older videoRenderer/gridVideoRenderer items with a resume overlay."""
    if not vr.get("videoId"):
        return None
    progress = None
    for ov in vr.get("thumbnailOverlays") or []:
        resume = ov.get("thumbnailOverlayResumePlaybackRenderer") if isinstance(ov, dict) else None
        if isinstance(resume, dict):
            progress = _percent(resume.get("percentDurationWatched"))
    channel = _text(vr.get("shortBylineText") or vr.get("ownerText") or vr.get("longBylineText"))
    return {"id": vr["videoId"], "title": _text(vr.get("title")), "channel": channel, "progress": progress}


def parse_videos(data: dict) -> list[dict]:
    """Videos of a browse answer (feed, history, continuation) as {"id", "title", "channel", "progress"}.

    `progress` is 0.0-1.0 from the thumbnail's red bar, None when it has none.
    Shorts (shortsLockupViewModel, reel shelves) are skipped.
    """
    out: list[dict] = []

    def visit(o: dict) -> bool:
        if "lockupViewModel" in o and isinstance(o["lockupViewModel"], dict):
            v = _lockup_video(o["lockupViewModel"])
            if v:
                out.append(v)
            return True
        for key in ("videoRenderer", "gridVideoRenderer"):
            if isinstance(o.get(key), dict):
                v = _renderer_video(o[key])
                if v:
                    out.append(v)
                return True
        return "shortsLockupViewModel" in o or "reelShelfRenderer" in o

    _walk(data.get("contents") or data.get("onResponseReceivedActions") or {}, visit, NOT_CONTENT)
    return out


def parse_history(data: dict) -> list[dict]:
    """History videos in order, each with `recent`: watched within the last week.

    FEhistory groups videos under day headers: "Today", "Yesterday" and
    weekday names for the last week, then dates ("Oct 13") for anything older.
    A header with a digit in it is therefore older than a week.
    """
    out: list[dict] = []

    def visit(o: dict) -> bool:
        sec = o.get("itemSectionRenderer")
        if not isinstance(sec, dict):
            return False
        hdr = (sec.get("header") or {}).get("itemSectionHeaderRenderer") or {}
        day = _text(hdr.get("title"))
        recent = bool(day) and not any(ch.isdigit() for ch in day)
        for v in parse_videos({"contents": sec.get("contents") or []}):
            out.append({**v, "day": day, "recent": recent})
        return True

    _walk(data.get("contents") or {}, visit, NOT_CONTENT)
    return out


def parse_channels(data: dict) -> list[dict]:
    """Subscribed channels of FEchannels (and its continuations) as {"id", "title"}."""
    out: list[dict] = []

    def visit(o: dict) -> bool:
        cr = o.get("channelRenderer")
        if isinstance(cr, dict):
            if cr.get("channelId"):
                out.append({"id": cr["channelId"], "title": _text(cr.get("title"))})
            return True
        return False

    _walk(data.get("contents") or data.get("onResponseReceivedActions") or {}, visit, NOT_CONTENT)
    return out


def continuation(data: dict) -> str | None:
    """The token of the list's own 'load more' item (not of chips or menus)."""
    token = None

    def visit(o: dict) -> bool:
        nonlocal token
        item = o.get("continuationItemRenderer")
        if isinstance(item, dict) and token is None:
            cmd = (item.get("continuationEndpoint") or {}).get("continuationCommand") or {}
            token = cmd.get("token")
            return True
        return False

    _walk(data.get("contents") or data.get("onResponseReceivedActions") or {}, visit, NOT_CONTENT)
    return token


def parse_accounts(data: dict) -> list[dict]:
    """Channel switcher entries: {"name", "handle", "selected", "pageId", "datasyncId"}."""
    out: list[dict] = []

    def visit(o: dict) -> bool:
        item = o.get("accountItemRenderer") or o.get("accountItem")
        if not isinstance(item, dict):
            return False
        entry = {"name": _text(item.get("accountName")), "handle": _text(item.get("channelHandle")) or None,
                 "selected": bool(item.get("isSelected")), "pageId": None, "datasyncId": None}
        tokens = ((item.get("serviceEndpoint") or {}).get("selectActiveIdentityEndpoint") or {}).get(
            "supportedTokens") or []
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            if "pageIdToken" in tok:
                entry["pageId"] = (tok["pageIdToken"] or {}).get("pageId")
            if "datasyncIdToken" in tok:
                entry["datasyncId"] = (tok["datasyncIdToken"] or {}).get("datasyncIdToken")
        out.append(entry)
        return True

    _walk(data.get("actions") or data, visit)
    return out


def user_session_id(datasync: str | None) -> str | None:
    """'<page id>||<user>' for a brand channel, '<user>||' for the account's own (as yt-dlp parses it)."""
    if not datasync:
        return None
    first, _, second = datasync.partition("||")
    return second or first or None
