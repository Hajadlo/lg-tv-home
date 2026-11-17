"""Check version-guarded SDL filtering, backup preservation and rollback."""
import hashlib
from pathlib import Path
import runpy

import pytest

MODULE = runpy.run_path(str(Path(__file__).parent.parent / "tv/moonlight-controller-filter"))
configure = MODULE["configure"]


def fixture(tmp_path):
    library = tmp_path / "lib/backports/libSDL2-2.0.so.0"
    library.parent.mkdir(parents=True)
    original = b"\x7fELFtable" + bytes.fromhex("99999999") + b"unchanged"
    patched = original[:9] + bytes.fromhex("98999899") + original[13:]
    spec = {"original": hashlib.sha256(original).hexdigest(),
            "patched": hashlib.sha256(patched).hexdigest(), "offset": 9}
    library.write_bytes(original)
    executable = tmp_path / "bin/moonlight"
    executable.parent.mkdir()
    executable.write_bytes(b"\x7fELForiginal video launcher")
    return library, original, patched, spec


def test_install_restore_and_reapply(tmp_path):
    library, original, patched, spec = fixture(tmp_path)
    assert not configure(tmp_path, "check", spec)["installed"]
    assert configure(tmp_path, "install", spec)["changed"]
    assert library.read_bytes() == patched
    assert not configure(tmp_path, "install", spec)["changed"]
    assert configure(tmp_path, "check", spec)["installed"]
    backup = library.with_name(library.name + ".before-c5-filter")
    assert backup.read_bytes() == original
    assert configure(tmp_path, "restore", spec)["changed"]
    assert library.read_bytes() == original
    assert not configure(tmp_path, "restore", spec)["changed"]
    configure(tmp_path, "install", spec)
    assert library.read_bytes() == patched
    assert backup.read_bytes() == original
    assert (tmp_path / "bin/moonlight").read_bytes() == b"\x7fELForiginal video launcher"


def test_unknown_library_and_bad_backup_are_preserved(tmp_path):
    library, original, patched, spec = fixture(tmp_path)
    library.write_bytes(b"different app version")
    with pytest.raises(RuntimeError, match="Unrecognized"):
        configure(tmp_path, "install", spec)
    assert library.read_bytes() == b"different app version"
    library.write_bytes(original)
    backup = library.with_name(library.name + ".before-c5-filter")
    backup.write_bytes(b"unrecognized backup")
    with pytest.raises(RuntimeError, match="backup differs"):
        configure(tmp_path, "install", spec)
    assert library.read_bytes() == original
    library.write_bytes(patched)
    with pytest.raises(RuntimeError, match="Invalid original"):
        configure(tmp_path, "restore", spec)
    assert library.read_bytes() == patched
