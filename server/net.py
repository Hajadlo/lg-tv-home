"""Small HTTP helpers shared by the YouTube, Google and Steam sources."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "tv-home/1.0"


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_plain = urllib.request.build_opener()
_no_redirect = urllib.request.build_opener(_NoRedirect)


def request(url: str, method: str = "GET", headers: dict | None = None, data: bytes | dict | None = None,
            timeout: float = 15.0, follow: bool = True) -> Response:
    """One HTTP call. HTTP errors come back as a Response; network errors raise OSError.

    `data` as a dict is sent form-encoded. With follow=False a redirect is
    returned as is (status 3xx, `location` header).
    """
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = _plain if follow else _no_redirect
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)


Fetch = Callable[..., Response]


def write_private(path: Path, text: str) -> None:
    """Atomic write with mode 600 (secrets and state alike)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path: Path, obj: Any) -> None:
    write_private(path, json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
