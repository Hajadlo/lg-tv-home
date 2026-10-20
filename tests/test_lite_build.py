"""lite/build.py: the CSS rewrite for older TVs and the page patching."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lite_build", ROOT / "lite" / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_downlevel_css_inset_and_slash_colours():
    css = (":root { --bone: 236 230 218; --sx: 96px; }\n"
           ".a { inset: 0; color: rgb(var(--bone) / 0.5); }\n"
           ".b { color: rgb(var(--bone)); box-shadow: 0 0 1px rgb(var(--accent-rgb) / .55); inset: 0 auto; }\n"
           ".c { --accent-rgb: 0 164 220; }")
    out = build.downlevel_css(css)
    assert "--bone: 236, 230, 218;" in out
    assert "--accent-rgb: 0, 164, 220;" in out
    assert ".a { top: 0; right: 0; bottom: 0; left: 0; color: rgba(var(--bone), 0.5); }" in out
    assert "rgb(var(--bone));" in out
    assert "rgba(var(--accent-rgb), .55)" in out
    assert "inset: 0 auto;" in out  # multi-value inset is left alone (not used by home.css)
    assert "--sx: 96px;" in out


def test_real_home_css_has_nothing_left_for_old_engines():
    out = build.downlevel_css((ROOT / "ui" / "css" / "home.css").read_text())
    assert "inset:" not in out
    assert " / 0" not in out and "/ ." not in out


def test_patch_index_swaps_the_data_layer_and_adds_setup():
    html = build.patch_index((ROOT / "ui" / "index.html").read_text())
    assert "js/api.js" not in html
    assert html.index("lite/sources.js") < html.index("lite/lite.js") < html.index("js/nav.js")
    assert html.index("js/app.js") < html.index("lite/setup.js")
    assert 'data-action="setup"' in html
    assert "lite/compat.css" in html and "lite/compat.js" in html


def test_patch_bridge_sets_default_apps():
    js = build.patch_bridge((ROOT / "tv" / "app" / "bridge.js").read_text())
    assert '"youtube": "youtube.leanback.v4"' in js
    assert '"uiUrl": ""' in js
