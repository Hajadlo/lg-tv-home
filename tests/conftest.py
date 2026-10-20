"""Keep the tests away from the real ~/.config/tv-home and ~/.cache/tv-home."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="tvhome-tests-")
os.environ["TVHOME_CONFIG_DIR"] = os.path.join(_tmp, "config")
os.environ["TVHOME_CACHE_DIR"] = os.path.join(_tmp, "cache")
os.environ["TVHOME_CONFIG"] = os.path.join(_tmp, "config", "config.toml")
os.environ.pop("NTFY_URL", None)
