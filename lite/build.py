#!/usr/bin/env python3
"""Build the TV-only edition of TV Home (app id com.tvhome.lite) into an IPK.

Assembles the shared page (../ui), the TV bridge (../tv/app/bridge.js) and
the lite pieces (data layer, Set up screen, service) into build/app and
build/service, rewrites the CSS for older TVs, then packages both with
ares-package into dist/.

  lite/build.py              # build + package
  lite/build.py --no-package # only assemble build/ (no ares needed)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

LITE = Path(__file__).resolve().parent
REPO = LITE.parent
UI = REPO / "ui"
BUILD = LITE / "build"
DIST = LITE / "dist"

BRIDGE_SETTINGS = re.compile(r"^(\s*var SETTINGS = ).*;$", re.M)
LITE_SETTINGS = {"uiUrl": "", "apps": {"jellyfin": "org.jellyfin.webos", "moonlight": "com.limelight.webos",
                                       "youtube": "youtube.leanback.v4"},
                 "moonlight": {"hostUuid": "", "appId": 0}}


def downlevel_css(css: str) -> str:
    """Rewrite what Chromium 53-83 cannot read in home.css.

    - `inset: X` -> top/right/bottom/left (Chromium 87)
    - space-separated colour triplets in custom properties (`--x: 1 2 3`) ->
      comma-separated, and `rgb(var(--x) / a)` -> `rgba(var(--x), a)`
      (the slash syntax is Chromium 65)
    """
    css = re.sub(r"(?<![-\w])inset:\s*([^;{}]+?)\s*;",
                 lambda m: "top: {0}; right: {0}; bottom: {0}; left: {0};".format(m.group(1))
                 if len(m.group(1).split()) == 1 else m.group(0), css)
    css = re.sub(r"(--[\w-]+:\s*)(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s*;", r"\1\2, \3, \4;", css)
    css = re.sub(r"rgb\(\s*(var\(--[\w-]+\))\s*/\s*([\d.]+%?)\s*\)", r"rgba(\1, \2)", css)
    return css


def patch_index(html: str) -> str:
    """ui/index.html -> the lite page: lite data layer instead of api.js, Set up, compat."""
    def sub(old: str, new: str) -> None:
        nonlocal html
        if old not in html:
            raise SystemExit(f"build: ui/index.html changed, cannot find {old!r}")
        html = html.replace(old, new, 1)

    sub("<title>Home</title>", "<title>TV Home</title>")
    sub('<link rel="stylesheet" href="css/home.css">',
        '<link rel="stylesheet" href="css/home.css">\n'
        '<link rel="stylesheet" href="lite/compat.css">\n'
        '<link rel="stylesheet" href="lite/setup.css">\n'
        '<script src="lite/compat.js"></script>')
    sub('<button class="setting setting-lg" data-item data-lg>LG apps</button>',
        '<button class="setting setting-lg" data-item data-lg>LG apps</button>\n'
        '      <button class="setting setting-setup" data-item data-action="setup">Set up</button>')
    sub('<script src="js/api.js"></script>',
        '<script src="lite/sources.js"></script>\n  <script src="lite/lite.js"></script>')
    sub('<script src="js/app.js"></script>',
        '<script src="js/app.js"></script>\n  <script src="lite/setup.js"></script>')
    return html


def patch_bridge(js: str) -> str:
    js, n = BRIDGE_SETTINGS.subn(lambda m: m.group(1) + json.dumps(LITE_SETTINGS) + ";", js)
    if n != 1:
        raise SystemExit("build: tv/app/bridge.js has no SETTINGS line")
    return js


def assemble() -> tuple[Path, Path]:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    app, svc = BUILD / "app", BUILD / "service"
    (app / "css").mkdir(parents=True)
    (app / "js").mkdir()
    (app / "tv").mkdir()
    shutil.copytree(UI / "fonts", app / "fonts")
    (app / "css" / "home.css").write_text(downlevel_css((UI / "css" / "home.css").read_text()))
    for name in ("app.js", "nav.js", "bridge-mock.js"):
        shutil.copy2(UI / "js" / name, app / "js" / name)
    (app / "index.html").write_text(patch_index((UI / "index.html").read_text()))
    (app / "tv" / "bridge.js").write_text(patch_bridge((REPO / "tv" / "app" / "bridge.js").read_text()))
    shutil.copytree(LITE / "js", app / "lite")
    for css in (LITE / "css").glob("*.css"):
        shutil.copy2(css, app / "lite" / css.name)
    shutil.copy2(LITE / "app" / "appinfo.json", app / "appinfo.json")
    for icon in ("icon.png", "largeIcon.png"):
        shutil.copy2(REPO / "tv" / "app" / icon, app / icon)
    svc.mkdir()
    for name in ("service.js", "lib.js", "package.json", "services.json"):
        shutil.copy2(LITE / "service" / name, svc / name)
    return app, svc


def package(app: Path, svc: Path) -> Path:
    if not shutil.which("ares-package"):
        raise SystemExit("build: ares-package not found (npm i -g @webos-tools/cli)")
    DIST.mkdir(exist_ok=True)
    info = json.loads((app / "appinfo.json").read_text())
    out = DIST / f"{info['id']}_{info['version']}_all.ipk"
    out.unlink(missing_ok=True)
    subprocess.run(["ares-package", "--no-minify", str(app), str(svc), "-o", str(DIST)], check=True)
    if not out.exists():
        raise SystemExit(f"build: expected {out}")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--no-package", action="store_true", help="only assemble build/")
    args = p.parse_args(argv)
    app, svc = assemble()
    print(f"assembled {app} and {svc}")
    if not args.no_package:
        print(package(app, svc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
