#!/usr/bin/env python3
"""Fill the webOS app from config.toml: bridge.js settings and appinfo.json.

  render.py <out_dir> [--ui-url URL]

Copies tv/app/ to <out_dir>, rewrites the one-line `var SETTINGS = {...};` in
bridge.js (server URL, app ids of the configured sources, Moonlight host and
app) and sets id/title in appinfo.json. Used by tv/install; nothing on the
TV is touched here.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

import tvconfig  # noqa: E402

SETTINGS_LINE = re.compile(r"^  var SETTINGS = .*;$", re.M)
APP_ID = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")


def settings(cfg: dict, ui_url: str | None = None) -> dict:
    f = tvconfig.features(cfg)
    apps = cfg["tv"]["apps"]
    st = cfg["steam"]
    url = ui_url or cfg["server"]["url"]
    if not url:
        raise SystemExit("render: set [server] url in config.toml (how the TV reaches the server)")
    return {
        "uiUrl": url if url.endswith("/") else url + "/",
        "apps": {name: apps[app] for name, app, on in (("jellyfin", "jellyfin", f["jellyfin"]),
                                                       ("moonlight", "moonlight", f["steam"]),
                                                       ("youtube", "youtube", f["youtube"])) if on and apps.get(app)},
        "moonlight": {"hostUuid": st["moonlight_host_uuid"], "appId": int(st["moonlight_app_id"] or 0)},
    }


def render_bridge(text: str, values: dict) -> str:
    line = "  var SETTINGS = " + json.dumps(values, ensure_ascii=True) + ";"
    new, n = SETTINGS_LINE.subn(lambda _: line, text)
    if n != 1:
        raise SystemExit("render: the SETTINGS line is missing from bridge.js")
    return new


def render_appinfo(text: str, cfg: dict) -> str:
    info = json.loads(text)
    app_id = cfg["tv"]["app_id"]
    if not APP_ID.match(app_id):
        raise SystemExit(f"render: [tv] app_id {app_id!r} must look like com.example.tvhome (lowercase)")
    info["id"] = app_id
    info["title"] = cfg["tv"]["title"] or "Home"
    return json.dumps(info, indent=2) + "\n"


def render(out: Path, cfg: dict, ui_url: str | None = None) -> None:
    shutil.copytree(HERE / "app", out, dirs_exist_ok=True)
    bridge = render_bridge((HERE / "app" / "bridge.js").read_text(), settings(cfg, ui_url))
    (out / "bridge.js").write_text(bridge)
    # webOS injects this file into every page the app shows, the hosted UI
    # included; that is how the hosted page gets window.tvHome.
    (out / "webOSUserScripts").mkdir(exist_ok=True)
    (out / "webOSUserScripts" / "userScript.js").write_text(bridge)
    (out / "appinfo.json").write_text(render_appinfo((HERE / "app" / "appinfo.json").read_text(), cfg))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path)
    p.add_argument("--ui-url")
    args = p.parse_args()
    render(args.out, tvconfig.load(), args.ui_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
