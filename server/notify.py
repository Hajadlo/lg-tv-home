"""One-shot phone notifications through ntfy, when [ntfy] url is set in config.toml.

The URL is the full topic URL, e.g. https://ntfy.sh/some-long-random-topic.
The NTFY_URL environment variable overrides it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("tvhome.notify")

_configured: str | None = None


def configure(url: str | None) -> None:
    global _configured
    _configured = url or None


def ntfy_url() -> str | None:
    return os.environ.get("NTFY_URL") or _configured


def send(title: str, message: str, url: str | None = None) -> bool:
    url = url or ntfy_url()
    if not url:
        log.warning("no ntfy url configured; would have sent: %s | %s", title, message)
        return False
    root, topic = url.rsplit("/", 1)
    body = json.dumps({"topic": urllib.parse.unquote(topic), "title": title, "message": message,
                       "tags": ["tv"], "priority": 4}).encode()
    req = urllib.request.Request(root + "/", data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except OSError as exc:
        log.warning("ntfy failed: %s", exc)
        return False
    log.info("ntfy sent: %s", title)
    return True
