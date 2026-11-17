"""Check the version-guarded Twitch AdFree patch: both files, re-apply, backup and rollback."""
import hashlib
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

MODULE = runpy.run_path(str(Path(__file__).parent.parent / "tv/twitch-adfree-patch"))
configure, SPEC = MODULE["configure"], MODULE["SPEC"]
ADDON = (Path(__file__).parent.parent / "tv/twitch-tvhome.js").read_bytes()
OLD, NEW = SPEC["index"]["old"], SPEC["index"]["new"]


def fixture(tmp_path):
    index = ('!function(){var r="";window.webOSSystem&&window.webOSSystem.launchParams&&(%s),'
             'location.href="https://lg.tv.twitch.tv/"+r}();' % OLD).encode()
    user = b"/*! license */\n!function(){}();\n"
    spec = {**SPEC, "index": {**SPEC["index"], "original": hashlib.sha256(index).hexdigest()},
            "userScript": {"original": hashlib.sha256(user).hexdigest()}}
    (tmp_path / "webOSUserScripts").mkdir()
    (tmp_path / "index.js").write_bytes(index)
    (tmp_path / "webOSUserScripts/userScript.js").write_bytes(user)
    return index, user, spec


def test_install_reapply_and_restore(tmp_path):
    index, user, spec = fixture(tmp_path)
    assert configure(tmp_path, "check", spec, ADDON) == {"installed": False, "index": False, "userScript": False}
    assert configure(tmp_path, "install", spec, ADDON)["changed"]
    assert (tmp_path / "index.js").read_bytes() == index.replace(OLD.encode(), NEW.encode())
    assert (tmp_path / "webOSUserScripts/userScript.js").read_bytes() == ADDON + user
    assert not configure(tmp_path, "install", spec, ADDON)["changed"]
    assert configure(tmp_path, "check", spec, ADDON)["installed"]
    # a newer add-on replaces the old copy instead of stacking a second one
    newer = ADDON.replace(b"tv-home: begin", b"tv-home: begin v2")
    assert configure(tmp_path, "install", spec, newer)["changed"]
    assert (tmp_path / "webOSUserScripts/userScript.js").read_bytes() == newer + user
    assert (tmp_path / "webOSUserScripts/userScript.js.before-tvhome").read_bytes() == user
    assert (tmp_path / "index.js.before-tvhome").read_bytes() == index
    assert configure(tmp_path, "restore", spec, newer)["changed"]
    assert (tmp_path / "index.js").read_bytes() == index
    assert (tmp_path / "webOSUserScripts/userScript.js").read_bytes() == user


def test_unknown_version_is_left_alone(tmp_path):
    _, user, spec = fixture(tmp_path)
    (tmp_path / "index.js").write_bytes(b"something else")
    with pytest.raises(RuntimeError, match="index.js"):
        configure(tmp_path, "install", spec, ADDON)
    assert (tmp_path / "webOSUserScripts/userScript.js").read_bytes() == user


def test_launch_param_reader_takes_a_string_or_an_object():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    expr = NEW.split("=", 1)[1].replace("window.webOSSystem.launchParams", "P")
    for given, want in (("'{\"channel\":\"gorgc\"}'", "gorgc"), ('{channel:"x"}', "x"), ("'not json'", ""), ("null", "")):
        out = subprocess.run([node, "-e", "var P=%s;console.log(%s)" % (given, expr)],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == want


def test_addon_is_one_marked_block():
    assert ADDON.startswith(b"/* tv-home: begin */") and ADDON.endswith(SPEC["end"].encode())
    assert ADDON.count(SPEC["end"].encode()) == 1
