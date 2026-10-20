#!/usr/bin/env python3
"""TV Home configuration: one TOML file, ~/.config/tv-home/config.toml.

The path can be overridden with the TVHOME_CONFIG environment variable.
`config.example.toml` at the top of the repo documents every key. Secrets
(SSAP key, cookies, OAuth tokens, Jellyfin API key) never live here; they stay
in their own mode-600 files under ~/.config/tv-home/.

Every source is optional: a section that is missing (or lacks its required
keys) turns that source off, and the UI hides its tab.

Command line, for the shell scripts:

  tvconfig.py get tv.ip            # prints the value, exit 1 when unset
  tvconfig.py check                # prints which sources are on
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("TVHOME_CONFIG_DIR", Path.home() / ".config/tv-home"))
DEFAULT_PATH = CONFIG_DIR / "config.toml"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8797, "url": "", "disk": ""},
    "tv": {
        "ip": "", "ares_device": "tv", "app_id": "com.example.tvhome", "title": "Home", "takeover": True,
        "apps": {"jellyfin": "org.jellyfin.webos", "youtube": "youtube.leanback.v4",
                 "moonlight": "com.limelight.webos"},
    },
    "weather": {"latitude": None, "longitude": None, "name": ""},
    "jellyfin": {"url": "", "user": ""},
    "youtube": {"channel": "", "browser_profile": "", "ssh_host": "", "subscriptions_csv": ""},
    "steam": {"pc_ip": "", "pc_mac": "", "broadcast": [], "sunshine_port": 47989,
              "moonlight_host_uuid": "", "moonlight_app_id": 0, "ssh_host": "", "sunshine_unit": "",
              "steam_root": "~/.local/share/Steam", "steam_user_id": 0},
    "ntfy": {"url": ""},
}


class ConfigError(Exception):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def path() -> Path:
    return Path(os.environ.get("TVHOME_CONFIG") or DEFAULT_PATH).expanduser()


def load(p: Path | None = None) -> dict:
    """The config with defaults filled in; a missing file is an empty config (everything off)."""
    p = p or path()
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError:
        raw = {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    return _merge(DEFAULTS, raw)


def from_dict(raw: dict) -> dict:
    return _merge(DEFAULTS, raw)


def features(cfg: dict) -> dict[str, bool]:
    """Which sources are configured. The UI shows a tab only for these."""
    w, s = cfg["weather"], cfg["steam"]
    return {
        "jellyfin": bool(cfg["jellyfin"]["url"] and cfg["jellyfin"]["user"]),
        "youtube": youtube_cookies(cfg) or bool(cfg["youtube"]["subscriptions_csv"])
        or (CONFIG_DIR / "google-oauth-client.json").is_file(),
        "steam": bool(s["pc_ip"] and s["pc_mac"] and s["moonlight_host_uuid"]),
        "weather": w["latitude"] is not None and w["longitude"] is not None,
    }


def youtube_cookies(cfg: dict) -> bool:
    """The browser-login route (watched state, Continue watching, channel list) is set up."""
    return bool(cfg["youtube"]["channel"] and cfg["youtube"]["browser_profile"])


def get(cfg: dict, dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "get":
        try:
            value = get(load(), argv[1])
        except KeyError:
            print(f"tvconfig: no key {argv[1]}", file=sys.stderr)
            return 2
        if value in ("", None, [], 0) and not isinstance(value, bool):
            return 1
        print(json.dumps(value) if isinstance(value, (dict, list, bool)) else value)
        return 0
    if argv[:1] == ["check"]:
        cfg = load()
        print(f"config: {path()}")
        for name, on in features(cfg).items():
            print(f"  {name}: {'on' if on else 'off'}")
        for key in ("server.url", "tv.ip", "tv.app_id"):
            try:
                print(f"  {key}: {get(cfg, key) or '(unset)'}")
            except KeyError:
                pass
        return 0
    print("usage: tvconfig.py get <section.key> | check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
