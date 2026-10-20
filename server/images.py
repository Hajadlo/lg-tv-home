"""Jellyfin image proxy with a small on-disk cache, so the TV needs no token.

Also shapes images for the TV with Pillow (optional: without it originals are
served): scaling down to the requested width, trimming a logo's transparent
padding, and telling whether a logo is drawn dark.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from jellyfin import NotFound

log = logging.getLogger("tvhome.images")

# Proxy kind -> Jellyfin image type. `primary` is an extra the backend uses for
# an episode's own still (Jellyfin's Primary on an episode is landscape).
KINDS = {"backdrop": "Backdrop", "thumb": "Thumb", "poster": "Primary", "logo": "Logo", "primary": "Primary"}
ITEM_ID = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F-]{36}$")
WIDTH_STEP = 160
MAX_WIDTH = 3840
FRESH_S = 7 * 86400
MISS_S = 600
MAX_CACHE_BYTES = 400 * 1024 * 1024


def bucket_width(raw: str | None) -> int | None:
    """Round a ?w= hint up to a 160 px step so the cache stays small."""
    try:
        w = int(raw) if raw else 0
    except ValueError:
        return None
    if w <= 0:
        return None
    return min(MAX_WIDTH, -(-w // WIDTH_STEP) * WIDTH_STEP)


def sniff_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


class ImageCache:
    def __init__(self, root: Path, fetch: Callable[[str, str, int | None], tuple[bytes, str]] | None,
                 clock: Callable[[], float] = time.time):
        self.root = root
        self.fetch = fetch
        self._clock = clock
        self._misses: dict[str, float] = {}
        self._lock = threading.Lock()
        self._writes = 0

    def get(self, item_id: str, kind: str, width: int | None) -> tuple[bytes, str]:
        """Return (bytes, content type); raise NotFound for a missing image.

        A stale cached copy is served when Jellyfin cannot be reached.
        """
        if kind not in KINDS or not ITEM_ID.match(item_id):
            raise NotFound(f"{item_id}/{kind}")
        key = f"{item_id.lower()}_{kind}_{width or 0}"
        path = self.root / key[:2] / key
        now = self._clock()
        with self._lock:
            missed = self._misses.get(key)
        if missed and now - missed < MISS_S:
            raise NotFound(key)
        cached = None
        try:
            if now - path.stat().st_mtime < FRESH_S:
                data = path.read_bytes()
                return data, sniff_type(data)
            cached = path.read_bytes()
        except FileNotFoundError:
            pass
        if self.fetch is None:
            if cached is not None:
                return cached, sniff_type(cached)
            raise NotFound(key)
        try:
            data, _ = self.fetch(item_id, KINDS[kind], width)
        except NotFound:
            with self._lock:
                self._misses[key] = now
            raise
        except Exception:
            if cached is not None:
                return cached, sniff_type(cached)
            raise
        self._store(path, data)
        return data, sniff_type(data)

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{threading.get_ident()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._writes += 1
        if self._writes % 50 == 0:
            self.prune()

    def prune(self, limit: int = MAX_CACHE_BYTES) -> None:
        prune_dir(self.root, limit)


def prune_dir(root: Path, limit: int) -> None:
    """Delete the oldest files under root/xx/ until the total is under limit."""
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.glob("*/*") if p.is_file()]
    total = sum(size for _, size, _ in files)
    for _, size, p in sorted(files):
        if total <= limit:
            break
        p.unlink(missing_ok=True)
        total -= size


class Unavailable(Exception):
    """The image source could not be reached and nothing is cached (HTTP 502)."""


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, or None."""
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return None


class UrlImageCache:
    """Disk cache for public images (YouTube thumbnails, Steam art).

    `get(key, candidates)` tries the candidate URLs in order and keeps the first
    image that answers 200 and passes `accept`. When every candidate is missing
    the key is remembered as a miss for `miss_s` and NotFound is raised. When
    the network fails a stale copy is served, else Unavailable is raised.
    `key` must be a safe file name (callers validate ids).
    """

    def __init__(self, root: Path, fetch: Callable | None = None, fresh_s: float = FRESH_S,
                 miss_s: float = 86400, max_bytes: int = 300 * 1024 * 1024,
                 clock: Callable[[], float] = time.time):
        if fetch is None:
            import net
            fetch = net.request
        self.root = root
        self.fetch = fetch
        self.fresh_s = fresh_s
        self.miss_s = miss_s
        self.max_bytes = max_bytes
        self._clock = clock
        self._misses: dict[str, float] = {}
        self._lock = threading.Lock()
        self._writes = 0

    def get(self, key: str, candidates, accept: Callable[[bytes], bool] | None = None) -> tuple[bytes, str]:
        path = self.root / key[:2] / key
        now = self._clock()
        with self._lock:
            missed = self._misses.get(key)
        if missed is not None and now - missed < self.miss_s:
            raise NotFound(key)
        cached = None
        try:
            if now - path.stat().st_mtime < self.fresh_s:
                data = path.read_bytes()
                return data, sniff_type(data)
            cached = path.read_bytes()
        except FileNotFoundError:
            pass
        urls = candidates() if callable(candidates) else candidates
        network_error = None
        for url in urls:
            try:
                resp = self.fetch(url, timeout=15)
            except OSError as exc:
                network_error = exc
                continue
            if resp.status == 200 and resp.body and (accept is None or accept(resp.body)):
                self._store(path, resp.body)
                return resp.body, sniff_type(resp.body)
            if resp.status >= 500 or resp.status == 429:
                network_error = OSError(f"HTTP {resp.status} from {url}")
        if cached is not None:
            return cached, sniff_type(cached)
        if network_error is not None:
            raise Unavailable(str(network_error))
        with self._lock:
            self._misses[key] = now
        raise NotFound(key)

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{threading.get_ident()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._writes += 1
        if self._writes % 50 == 0:
            prune_dir(self.root, self.max_bytes)


# -- shaping with Pillow -------------------------------------------------------

RESIZE_STEP = 20          # public images scale to the requested width, rounded up to this
ALPHA_MIN = 40            # a logo pixel counts as drawn from this alpha up
DARK_LUMINANCE = 0.3      # mean Rec. 709 luma (0-1) of the drawn pixels below which a logo is dark
TRIM_BELOW = 0.8          # trim a logo when its drawn box covers less than this share of it
JPEG_QUALITY = 85


def resize_width(raw: str | None) -> int | None:
    """Round a ?w= hint up to a 20 px step (the UI asks for a few fixed widths)."""
    try:
        w = int(raw) if raw else 0
    except ValueError:
        return None
    if w <= 0:
        return None
    return min(MAX_WIDTH, -(-w // RESIZE_STEP) * RESIZE_STEP)


def _open(data: bytes):
    from PIL import Image  # system python3 has Pillow; without it images pass through as they are
    return Image.open(io.BytesIO(data))


def logo_info(data: bytes) -> dict:
    """{"dark": bool, "box": [x0, y0, x1, y1] | None} for a logo image.

    `dark`: the drawn pixels average below DARK_LUMINANCE, so the logo would
    vanish on the dark page (some Steam logos are black on transparent).
    `box`: the drawn part, when trimming the transparent padding around it
    saves at least a fifth of the area (never for a dark logo). Raises on data
    Pillow cannot read.
    """
    from PIL import ImageStat
    rgba = _open(data).convert("RGBA")
    mask = rgba.getchannel("A").point(lambda a: 255 if a >= ALPHA_MIN else 0)
    box = mask.getbbox()
    if box is None:
        return {"dark": False, "box": None}
    r, g, b = ImageStat.Stat(rgba.convert("RGB"), mask).mean[:3]
    dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < DARK_LUMINANCE
    w, h = rgba.size
    trim = (box[2] - box[0]) * (box[3] - box[1]) < w * h * TRIM_BELOW
    return {"dark": dark, "box": list(box) if trim and not dark else None}


def shape(data: bytes, width: int | None, trim: bool = False) -> tuple[bytes, str] | None:
    """The image trimmed (logos) and scaled down to `width`; None to serve it as it is.

    Never scales up. JPEG stays baseline JPEG (quality 85); anything with
    transparency becomes PNG.
    """
    from PIL import Image
    im = _open(data)
    box = tuple(logo_info(data)["box"] or ()) if trim else ()
    cw, ch = (box[2] - box[0], box[3] - box[1]) if box else im.size
    if not box and (not width or cw <= width):
        return None
    tw, th = (width, max(1, round(ch * width / cw))) if width and cw > width else (cw, ch)
    if not box and im.format == "JPEG":
        im.draft("RGB", (tw, th))  # decode at a reduced DCT scale first: several times faster
    if box:
        im = im.crop(box)
    if im.size != (tw, th):
        im = im.resize((tw, th), Image.Resampling.LANCZOS, reducing_gap=3.0)
    out = io.BytesIO()
    if im.mode in ("RGBA", "LA", "P", "PA") or "transparency" in im.info:
        im.convert("RGBA").save(out, "PNG")
        return out.getvalue(), "image/png"
    im.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), "image/jpeg"


class Shaper:
    """Shaped images kept in memory (least recently used out); the original on any failure."""

    def __init__(self, max_bytes: int = 48 * 1024 * 1024, shape_fn: Callable | None = None):
        self.max_bytes = max_bytes
        self._shape = shape_fn or shape
        self._lru: OrderedDict[tuple, tuple[bytes, str]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str, source: tuple[bytes, str], width: int | None, trim: bool = False) -> tuple[bytes, str]:
        if not width and not trim:
            return source
        k = (key, width, trim, len(source[0]))  # a refreshed source has another length, nearly always
        with self._lock:
            hit = self._lru.get(k)
            if hit is not None:
                self._lru.move_to_end(k)
                return hit
        try:
            shaped = self._shape(source[0], width, trim)
        except Exception as exc:  # no Pillow, or bytes it cannot read
            log.debug("shape %s: %s", key, exc)
            shaped = None
        out = shaped or source
        with self._lock:
            if k not in self._lru:
                self._lru[k] = out
                self._size += len(out[0])
            while self._size > self.max_bytes and self._lru:
                _, old = self._lru.popitem(last=False)
                self._size -= len(old[0])
        return out


class LogoInfo:
    """Whether each logo is drawn dark, worked out once per logo URL and kept on disk.

    `dark(url, wait_s)` answers from the cache at once. An unknown or stale URL
    is queued for one background worker, and the caller may wait up to
    `wait_s` for it. None means not known (yet), no such logo, or unreadable.
    """

    def __init__(self, path: Path, load: Callable[[str], bytes], fresh_s: float = FRESH_S,
                 miss_s: float = MISS_S, clock: Callable[[], float] = time.time,
                 analyse: Callable[[bytes], dict] = logo_info):
        self.path = path
        self._load = load
        self._analyse = analyse
        self.fresh_s = fresh_s
        self.miss_s = miss_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        try:
            self._cache: dict[str, dict] = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            self._cache = {}

    def dark(self, url: str, wait_s: float = 0.0) -> bool | None:
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None and self._clock() - hit["at"] < (self.fresh_s if hit["dark"] is not None else self.miss_s):
                return hit["dark"]
            done = self._pending.get(url)
            if done is None:
                done = self._pending[url] = threading.Event()
                self._queue.put(url)
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="logo-info", daemon=True)
                    self._worker.start()
        if wait_s > 0 and done.wait(wait_s):
            with self._lock:
                hit = self._cache.get(url)
        return hit["dark"] if hit else None

    def _run(self) -> None:
        while True:
            try:
                url = self._queue.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                continue
            self.compute(url)

    def compute(self, url: str) -> None:
        entry = None
        try:
            entry = {"dark": bool(self._analyse(self._load(url))["dark"])}
        except NotFound:
            entry = {"dark": None}
        except Exception as exc:  # source down or unreadable: asked again next time
            log.info("logo %s: %s", url, exc)
        with self._lock:
            if entry is not None:
                entry["at"] = self._clock()
                self._cache[url] = entry
                self._save()
            done = self._pending.pop(url, None)
        if done is not None:
            done.set()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps(self._cache, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("logo cache: %s", exc)
