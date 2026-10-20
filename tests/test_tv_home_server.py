"""TV Home backend tests. Run: ./check.sh (or pytest tests/test_tv_home_server.py with Pillow).

Network sources, the wake packet and the Sunshine restart are all fakes here.
Needs Pillow (the server shapes images with it); check.sh adds it from the line below.
check.sh: --with pillow
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import google_oauth  # noqa: E402
import jf_remote  # noqa: E402
import images  # noqa: E402
import jellyfin  # noqa: E402
import net  # noqa: E402
import pc as pc_mod  # noqa: E402
import steam  # noqa: E402
import tvconfig  # noqa: E402
import tvhome_server  # noqa: E402
import innertube  # noqa: E402
import weather  # noqa: E402
import youtube  # noqa: E402
import yt_cookies  # noqa: E402
import yt_watched  # noqa: E402
from refresher import Refresher  # noqa: E402

SERIES = "0123456789abcdef0123456789abcdef"


def episode(item_id, series=SERIES, season=3, number=2, name="The Truth", **extra):
    raw = {"Id": item_id, "Type": "Episode", "Name": name, "SeriesName": "Harbour Lights", "SeriesId": series,
           "ParentIndexNumber": season, "IndexNumber": number, "ProductionYear": 1991,
           "RunTimeTicks": 30 * jellyfin.TICKS_PER_MINUTE, "ImageTags": {"Primary": "p"},
           "BackdropImageTags": [], "ParentBackdropItemId": series, "ParentBackdropImageTags": ["b"],
           "ParentLogoItemId": series, "ParentLogoImageTag": "l", "SeriesPrimaryImageTag": "sp",
           "UserData": {"PlaybackPositionTicks": 0}}
    raw.update(extra)
    return raw


def movie(item_id, **extra):
    raw = {"Id": item_id, "Type": "Movie", "Name": "Paper Moons", "ProductionYear": 1999,
           "RunTimeTicks": 139 * jellyfin.TICKS_PER_MINUTE,
           "ImageTags": {"Primary": "p", "Thumb": "t", "Logo": "l"}, "BackdropImageTags": ["b"],
           "UserData": {"PlaybackPositionTicks": 0}}
    raw.update(extra)
    return raw


class ItemMappingTest(unittest.TestCase):
    def test_episode_uses_series_title_and_series_images(self):
        item = jellyfin.map_item(episode("ep1"))
        self.assertEqual(item["kind"], "episode")
        self.assertEqual(item["title"], "Harbour Lights")
        self.assertEqual(item["subtitle"], "S3 · E2 · The Truth")
        self.assertEqual(item["backdrop"], f"/api/img/{SERIES}/backdrop")
        self.assertEqual(item["logo"], f"/api/img/{SERIES}/logo")
        self.assertEqual(item["poster"], f"/api/img/{SERIES}/poster")
        self.assertEqual(item["thumb"], "/api/img/ep1/primary")
        self.assertIsNone(item["progress"])
        self.assertIsNone(item["minutesLeft"])

    def test_episode_own_backdrop_wins(self):
        item = jellyfin.map_item(episode("ep1", BackdropImageTags=["own"]))
        self.assertEqual(item["backdrop"], "/api/img/ep1/backdrop")

    def test_progress_and_minutes_left(self):
        raw = movie("m1", UserData={"PlaybackPositionTicks": 100 * jellyfin.TICKS_PER_MINUTE + 1})
        item = jellyfin.map_item(raw)
        self.assertAlmostEqual(item["progress"], 0.719, places=3)
        self.assertEqual(item["minutesLeft"], 39)
        self.assertEqual(item["thumb"], "/api/img/m1/thumb")
        self.assertIsNone(item["subtitle"])

    def test_missing_images_are_null_and_contract_keys_present(self):
        raw = movie("m2", ImageTags={}, BackdropImageTags=[])
        item = jellyfin.map_item(raw)
        self.assertEqual(set(item), {"id", "kind", "title", "subtitle", "year", "progress", "minutesLeft",
                                     "backdrop", "thumb", "poster", "logo"})
        for key in ("backdrop", "thumb", "poster", "logo"):
            self.assertIsNone(item[key])

    def test_season_and_series(self):
        season = jellyfin.map_item({"Id": "s1", "Type": "Season", "Name": "Quiet Machines (2016) The Selection",
                                    "SeriesName": "Quiet Machines", "SeriesId": "gp", "ImageTags": {}})
        self.assertEqual((season["kind"], season["title"], season["subtitle"]),
                         ("series", "Quiet Machines", "The Selection"))
        series = jellyfin.map_item({"Id": "gp", "Type": "Series", "Name": "Quiet Machines",
                                    "UserData": {"UnplayedItemCount": 1}, "ImageTags": {}})
        self.assertEqual(series["subtitle"], "1 unwatched episode")

    def test_unknown_types_are_skipped(self):
        self.assertIsNone(jellyfin.map_item({"Id": "x", "Type": "Audio", "Name": "Song"}))


class DedupeTest(unittest.TestCase):
    def test_resume_episode_not_repeated_in_next_up_or_latest(self):
        resume = [episode("ep1", UserData={"PlaybackPositionTicks": 5})]
        next_up = [episode("ep1"), episode("ep9", series="other", name="Other")]
        latest = [{"Id": SERIES, "Type": "Series", "Name": "Harbour Lights"}, movie("m1")]
        rows = jellyfin.build_rows(resume, next_up, latest)
        self.assertEqual([i["id"] for i in rows["resume"]], ["ep1"])
        self.assertEqual([i["id"] for i in rows["nextUp"]], ["ep9"])
        self.assertEqual([i["id"] for i in rows["latest"]], ["m1"])

    def test_resumed_movie_dropped_from_latest_and_rows_capped(self):
        resume = [movie("m1", UserData={"PlaybackPositionTicks": 5})]
        latest = [movie("m1")] + [movie(f"m{i}") for i in range(2, 30)]
        rows = jellyfin.build_rows(resume, [], latest, limit=12)
        self.assertNotIn("m1", [i["id"] for i in rows["latest"]])
        self.assertEqual(len(rows["latest"]), 12)


class WeatherTest(unittest.TestCase):
    def test_wmo_codes_map_to_contract_set(self):
        allowed = {"clear", "partly", "cloudy", "fog", "drizzle", "rain", "snow", "storm"}
        for wmo in list(weather.WMO) + [None, 1234]:
            self.assertIn(weather.map_code(wmo)[0], allowed)
        self.assertEqual(weather.map_code(0)[0], "clear")
        self.assertEqual(weather.map_code(2)[0], "partly")
        self.assertEqual(weather.map_code(45)[0], "fog")
        self.assertEqual(weather.map_code(53)[0], "drizzle")
        self.assertEqual(weather.map_code(81)[0], "rain")
        self.assertEqual(weather.map_code(85)[0], "snow")
        self.assertEqual(weather.map_code(95)[0], "storm")

    @staticmethod
    def hourly(codes, mm=None):
        times = [f"2026-10-20T{h:02d}:00" for h in range(24)]
        return {"time": times, "weather_code": codes, "precipitation": mm or [0.0] * 24,
                "precipitation_probability": [0] * 24}

    def test_note_rain_from(self):
        codes = [3] * 24
        codes[17] = 61
        mm = [0.0] * 24
        mm[17] = 0.8
        note = weather.rain_note(self.hourly(codes, mm), datetime(2026, 10, 20, 13, 15))
        self.assertEqual(note, "Rain from 17:00")

    def test_note_rain_until(self):
        codes = [61] * 21 + [3] * 3
        mm = [1.0] * 21 + [0.0] * 3
        note = weather.rain_note(self.hourly(codes, mm), datetime(2026, 10, 20, 19, 42))
        self.assertEqual(note, "Rain until 21:00")

    def test_note_dry_is_null(self):
        self.assertIsNone(weather.rain_note(self.hourly([1] * 24), datetime(2026, 10, 20, 9, 0)))

    def test_summarize(self):
        forecast = {"current": {"time": "2026-10-20T13:15", "temperature_2m": 11.4, "weather_code": 61},
                    "daily": {"temperature_2m_max": [14.2], "temperature_2m_min": [6.6]},
                    "hourly": self.hourly([61] * 15 + [2] * 9, [0.5] * 15 + [0.0] * 9)}
        self.assertEqual(weather.summarize(forecast), {"tempC": 11, "code": "rain", "label": "Light rain",
                                                       "highC": 14, "lowC": 7, "note": "Rain until 15:00"})

    def test_fetch_uses_the_configured_location(self):
        seen = []
        fetch = weather.make_fetch(lambda: {"latitude": 1.5, "longitude": 2.5},
                                   forecast=lambda lat, lon: seen.append((lat, lon)) or {
                                       "current": {"time": "2026-10-20T12:00", "temperature_2m": 3, "weather_code": 0}})
        self.assertEqual(fetch()["code"], "clear")
        self.assertEqual(seen, [(1.5, 2.5)])


class RefresherTest(unittest.TestCase):
    def test_slow_source_serves_last_good_value(self):
        calls = []
        release = threading.Event()

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                release.wait(5)
            return len(calls)

        r = Refresher("slow", fetch, interval=3600)
        self.assertIsNone(r.get())
        r.refresh_now()
        self.assertEqual(r.get(), 1)
        t = threading.Thread(target=r.refresh_now)
        t.start()
        start = time.monotonic()
        self.assertEqual(r.get(), 1)
        self.assertLess(time.monotonic() - start, 0.1)
        release.set()
        t.join()
        self.assertEqual(r.get(), 2)

    def test_failure_keeps_value_and_records_error(self):
        results = [{"a": 1}, RuntimeError("down")]

        def fetch():
            value = results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        r = Refresher("flaky", fetch, interval=3600)
        r.refresh_now()
        self.assertFalse(r.refresh_now())
        self.assertEqual(r.get(), {"a": 1})
        self.assertIn("down", r.status()["lastError"])

    def test_never_loaded_is_null(self):
        def fetch():
            raise TimeoutError("slow")
        r = Refresher("dead", fetch, interval=3600)
        r.refresh_now()
        self.assertIsNone(r.get())


class FakeDesktop:
    """Simulated PC: probes read state; wake and restart record calls."""

    def __init__(self, on=False, sunshine=False, boots_after=None, sunshine_after=None, restart_fixes=True):
        self.now = 0.0
        self.on, self.sunshine = on, sunshine
        self.boots_after = boots_after
        self.sunshine_after = sunshine_after
        self.restart_fixes = restart_fixes
        self.wakes = []
        self.prepared = []
        self.restarts = []
        self.woken_at = None

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s
        if self.woken_at is not None and self.boots_after is not None and \
                self.now - self.woken_at >= self.boots_after:
            self.on = True
        if self.on and self.sunshine_after is not None and self.now >= self.sunshine_after:
            self.sunshine = True

    def probe(self, port):
        return self.on and (port == pc_mod.SSH_PORT or self.sunshine)

    def wake(self):
        self.wakes.append(self.now)
        if self.woken_at is None:
            self.woken_at = self.now

    def restart(self):
        self.restarts.append(self.now)
        if self.restart_fixes:
            self.sunshine = True
        return True

    def pc(self):
        return pc_mod.PC(probe=self.probe, wake=self.wake, restart=self.restart,
                         prepare_tv=self.prepare_tv, clock=self.clock, sleep=self.sleep)

    def prepare_tv(self):
        self.prepared.append(self.now)


def run_job(fake):
    pc = fake.pc()
    pc._job()
    return pc


class PcJobTest(unittest.TestCase):
    def test_magic_packet(self):
        packet = pc_mod.magic_packet("00:11:22:33:44:55")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("001122334455"))

    def test_already_ready_does_nothing(self):
        fake = FakeDesktop(on=True, sunshine=True)
        pc = run_job(fake)
        self.assertEqual(pc.job.phase, "ready")
        self.assertEqual((fake.wakes, fake.restarts), ([], []))
        self.assertTrue(pc.status()["ready"])

    def test_tv_is_prepared_once_before_ready(self):
        fake = FakeDesktop(boots_after=40, sunshine_after=55)
        phases = []
        pc = fake.pc()
        real_prepare = pc._prepare_tv
        pc._prepare_tv = lambda: (phases.append(pc.job.phase), real_prepare())
        pc._job()
        self.assertEqual(len(fake.prepared), 1)
        self.assertNotEqual(phases[0], "ready")
        self.assertEqual(pc.job.phase, "ready")

    def test_off_pc_is_woken_and_sunshine_comes_up_by_itself(self):
        fake = FakeDesktop(boots_after=40, sunshine_after=55)
        pc = run_job(fake)
        self.assertEqual(pc.job.phase, "ready")
        self.assertEqual(fake.restarts, [])
        self.assertGreaterEqual(len(fake.wakes), 2)  # resent while waiting
        self.assertEqual(pc.status(), {"state": "on", "sunshine": "listening", "ready": True,
                                       "phase": "ready", "error": None})

    def test_sunshine_closed_restarts_once_after_grace(self):
        fake = FakeDesktop(on=True, sunshine=False)
        pc = run_job(fake)
        self.assertEqual(pc.job.phase, "ready")
        self.assertEqual(len(fake.restarts), 1)
        self.assertGreaterEqual(fake.restarts[0], pc.t.sunshine_grace)
        self.assertEqual(fake.wakes, [])

    def test_restart_that_does_not_help_fails_without_second_restart(self):
        fake = FakeDesktop(on=True, sunshine=False, restart_fixes=False)
        pc = run_job(fake)
        self.assertEqual(pc.job.phase, "failed")
        self.assertEqual(len(fake.restarts), 1)
        self.assertIn("Sunshine", pc.status()["error"])

    def test_pc_never_boots(self):
        fake = FakeDesktop(boots_after=None)
        pc = run_job(fake)
        self.assertEqual(pc.job.phase, "failed")
        self.assertEqual(fake.restarts, [])
        self.assertGreaterEqual(fake.now, pc.t.boot_timeout)
        self.assertEqual(pc.status()["state"], "off")

    def test_concurrent_presses_join_one_job_and_report_waking(self):
        gate = threading.Event()
        wakes = []
        state = {"on": False}

        def probe(port):
            return state["on"]

        def sleep(_):
            gate.wait(5)
            state["on"] = True

        pc = pc_mod.PC(probe=probe, wake=lambda: wakes.append(1), restart=lambda: True,
                       prepare_tv=lambda: None, sleep=sleep)
        pc.probe()
        first = pc.play()
        second = pc.play()
        self.assertEqual(first["state"], "waking")
        self.assertEqual(second["state"], "waking")
        threads = [t for t in threading.enumerate() if t.name == "pc-play"]
        self.assertEqual(len(threads), 1)
        gate.set()
        threads[0].join(5)
        self.assertEqual(len(wakes), 1)
        self.assertEqual(pc.status()["phase"], "ready")
        self.assertEqual(pc.home_status(), {"state": "on", "sunshine": "listening"})


class ImageCacheTest(unittest.TestCase):
    ID = "fedcba9876543210fedcba9876543210"

    def test_caches_on_disk_and_404s_cleanly(self):
        calls = []

        def fetch(item_id, image_type, width):
            calls.append((item_id, image_type, width))
            if image_type == "Logo":
                raise jellyfin.NotFound(item_id)
            return b"\xff\xd8jpeg", "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            cache = images.ImageCache(Path(tmp), fetch)
            self.assertEqual(cache.get(self.ID, "poster", 320), (b"\xff\xd8jpeg", "image/jpeg"))
            cache.get(self.ID, "poster", 320)
            self.assertEqual(calls, [(self.ID, "Primary", 320)])
            with self.assertRaises(jellyfin.NotFound):
                cache.get(self.ID, "logo", None)
            with self.assertRaises(jellyfin.NotFound):
                cache.get(self.ID, "logo", None)  # remembered miss, no second fetch
            self.assertEqual(len(calls), 2)
            with self.assertRaises(jellyfin.NotFound):
                cache.get("../../etc", "poster", None)
            with self.assertRaises(jellyfin.NotFound):
                cache.get(self.ID, "banner", None)

    def test_width_buckets(self):
        self.assertEqual(images.bucket_width("300"), 320)
        self.assertEqual(images.bucket_width("320"), 320)
        self.assertEqual(images.bucket_width("99999"), images.MAX_WIDTH)
        self.assertIsNone(images.bucket_width("abc"))
        self.assertIsNone(images.bucket_width(None))


class HttpTest(unittest.TestCase):
    """Whole server over HTTP with fake sources; /api/home stays fast while sources hang."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        ui = Path(cls.tmp.name) / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("<!doctype html><title>t</title>")
        (ui / "bg.png").write_bytes(b"\x89PNG....")
        cls.hang = threading.Event()

        def slow_jellyfin():
            cls.hang.wait(30)
            raise TimeoutError("jellyfin hung")

        def weather_ok():
            return {"tempC": 11, "code": "rain", "label": "Light rain", "highC": 14, "lowC": 7, "note": None}

        fake = FakeDesktop(on=False)
        app = tvhome_server.App(ui, slow_jellyfin, weather_ok, fake.pc(),
                                images.ImageCache(Path(cls.tmp.name) / "img", None),
                                hdd=lambda: {"hddFreeGb": None, "hddTotalGb": None})
        app.weather.refresh_now()
        app.jellyfin.start()
        cls.app = app
        cls.server = tvhome_server.make_server(app, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.hang.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_home_is_fast_with_hung_source(self):
        start = time.monotonic()
        status, headers, body = self.get("/api/home")
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        home = json.loads(body)
        self.assertEqual(set(home), {"now", "features", "weather", "jellyfin", "pc", "system", "youtube", "steam"})
        self.assertIsNone(home["youtube"])
        self.assertIsNone(home["jellyfin"])
        self.assertEqual(home["weather"]["code"], "rain")
        self.assertEqual(home["pc"], {"state": "off", "sunshine": "unknown"})

    def test_static_and_cache_headers(self):
        status, headers, body = self.get("/")
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-cache"))
        status, headers, _ = self.get("/bg.png")
        self.assertIn("max-age", headers["Cache-Control"])
        self.assertEqual(self.get("/../../etc/passwd")[0], 404)
        self.assertEqual(self.get("/nope.js")[0], 404)
        self.assertEqual(self.get("/api/nope")[0], 404)

    def test_image_404_and_options(self):
        self.assertEqual(self.get(f"/api/img/{ImageCacheTest.ID}/poster?w=300")[0], 404)
        status, headers, _ = self.get("/api/pc/play", method="OPTIONS")
        self.assertEqual(status, 204)
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_health(self):
        status, _, body = self.get("/api/health")
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(health["weather"]["ok"])
        self.assertFalse(health["jellyfin"]["loaded"])


# -- slice 2: YouTube, Google OAuth, Steam, web images ------------------------

CH_A = "UCfixturechannelAAxxxxxx"
CH_B = "UCfixturechannelBBxxxxxx"


def feed_xml(channel_id, title, entries):
    """entries: (video_id, published, short?)"""
    parts = []
    for video, published, short in entries:
        href = f"https://www.youtube.com/shorts/{video}" if short else f"https://www.youtube.com/watch?v={video}"
        parts.append(f"""<entry><id>yt:video:{video}</id><yt:videoId>{video}</yt:videoId>
 <yt:channelId>{channel_id}</yt:channelId><title>Video {video}</title>
 <link rel="alternate" href="{href}"/><author><name>{title}</name></author>
 <published>{published}</published>
 <media:group><media:community><media:statistics views="1234"/></media:community></media:group></entry>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
 xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>{''.join(parts)}</feed>""".encode()


def vid(n):
    return f"vid{n:08d}"


class FakeWeb:
    """net.request stand-in: answers by URL prefix and records every call."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, url, method="GET", headers=None, data=None, timeout=15, follow=True):
        self.calls.append({"url": url, "method": method, "headers": headers or {}, "data": data})
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                answer = answer(url, method, headers or {}, data) if callable(answer) else answer
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return net.Response(404, {}, b"")


def shorts_route(shorts):
    def answer(url, method, headers, data):
        video = url.rsplit("/", 1)[1]
        if video in shorts:
            return net.Response(200)
        return net.Response(303, {"location": f"https://www.youtube.com/watch?v={video}"})
    return answer


def jpeg(w, h):
    return b"\xff\xd8\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9


class YouTubeFeedTest(unittest.TestCase):
    def test_parse_feed_fields_and_short_link(self):
        xml = feed_xml(CH_A, "Marques Brownlee", [(vid(1), "2026-10-19T18:00:00+00:00", False),
                                                  (vid(2), "2026-10-18T18:00:00+00:00", True)])
        entries = youtube.parse_feed(xml)
        self.assertEqual(entries[0], {"id": vid(1), "title": f"Video {vid(1)}", "channel": "Marques Brownlee",
                                      "channelId": CH_A, "published": "2026-10-19T18:00:00+00:00",
                                      "views": 1234, "short": None})
        self.assertTrue(entries[1]["short"])

    def test_check_short_answers(self):
        web = FakeWeb({"https://www.youtube.com/shorts/": shorts_route({vid(1)})})
        self.assertTrue(youtube.check_short(vid(1), web))
        self.assertFalse(youtube.check_short(vid(2), web))
        self.assertEqual(web.calls[0]["method"], "HEAD")
        self.assertIn("SOCS", web.calls[0]["headers"]["Cookie"])
        consent = FakeWeb({"https://www.youtube.com/shorts/": net.Response(
            302, {"location": "https://consent.youtube.com/m?continue=..."})})
        self.assertIsNone(youtube.check_short(vid(3), consent))

    def test_thumb_placeholder_is_rejected(self):
        self.assertFalse(youtube.thumb_ok(jpeg(120, 90)))
        self.assertTrue(youtube.thumb_ok(jpeg(1280, 720)))


class YouTubeSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, web, ids=(CH_A, CH_B), limit=30):
        return youtube.YouTubeSource(youtube.StaticChannels(list(ids)), self.cache, fetch=web, workers=2,
                                     limit=limit)

    def test_newest_first_without_shorts_and_capped(self):
        a = [(vid(i), f"2026-10-{10 + i:02d}T10:00:00+00:00", i == 5) for i in range(1, 8)]
        b = [(vid(20 + i), f"2026-10-{10 + i:02d}T12:00:00+00:00", False) for i in range(1, 8)]
        web = FakeWeb({youtube.FEED_URL.format(CH_A): net.Response(200, {}, feed_xml(CH_A, "A", a)),
                       youtube.FEED_URL.format(CH_B): net.Response(200, {}, feed_xml(CH_B, "B", b)),
                       "https://www.youtube.com/shorts/": shorts_route({vid(26)})})
        out = self.source(web, limit=6)()
        self.assertEqual((out["channelCount"], out["auth"]), (2, "ok"))
        ids = [v["id"] for v in out["videos"]]
        # vid(26) is a Short found by HEAD, vid(5) a Short by its feed link.
        self.assertEqual(ids, [vid(27), vid(7), vid(6), vid(25), vid(24), vid(4)])
        self.assertEqual(out["videos"][0]["thumb"], f"/api/yt/{vid(27)}/thumb")
        heads = [c["url"] for c in web.calls if c["method"] == "HEAD"]
        self.assertNotIn(youtube.SHORTS_URL.format(vid(5)), heads)  # the feed said so
        self.assertLessEqual(len(heads), 7)  # only what it needs, not all 13

    def test_shorts_answers_are_cached_on_disk(self):
        a = [(vid(1), "2026-10-11T10:00:00+00:00", False)]
        web = FakeWeb({youtube.FEED_URL.format(CH_A): net.Response(200, {}, feed_xml(CH_A, "A", a)),
                       "https://www.youtube.com/shorts/": shorts_route(set())})
        self.source(web, ids=[CH_A])()
        self.source(web, ids=[CH_A])()
        self.assertEqual(len([c for c in web.calls if c["method"] == "HEAD"]), 1)

    def test_failed_feed_keeps_last_entries_and_all_failed_raises(self):
        state = {"down": False}

        def feed(url, method, headers, data):
            if state["down"]:
                return OSError("network down")
            return net.Response(200, {"etag": '"v1"'}, feed_xml(
                CH_A, "A", [(vid(1), "2026-10-11T10:00:00+00:00", False)]))
        web = FakeWeb({youtube.FEED_URL.format(CH_A): feed, youtube.FEED_URL.format(CH_B): OSError("down"),
                       "https://www.youtube.com/shorts/": shorts_route(set())})
        src = self.source(web)
        self.assertEqual([v["id"] for v in src()["videos"]], [vid(1)])
        state["down"] = True
        self.assertEqual([v["id"] for v in src()["videos"]], [vid(1)])  # that channel's last entries
        self.assertTrue([c for c in web.calls if c["headers"].get("If-None-Match") == '"v1"'])
        dead = self.source(FakeWeb({"https://": OSError("down")}))
        with self.assertRaises(OSError):
            dead()
        r = Refresher("youtube", src, 3600)
        r.refresh_now()
        r._fetch = dead
        self.assertFalse(r.refresh_now())
        self.assertEqual([v["id"] for v in r.get()["videos"]], [vid(1)])  # stale value served


class FakeAuth:
    def __init__(self, path):
        self.path = path

    def token_mtime(self):
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None


class SubscriptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = [1000.0]
        self.sent = []
        self.answers = []

    def tearDown(self):
        self.tmp.cleanup()

    def subs(self):
        def fetch_subs(auth):
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return youtube.Subscriptions(FakeAuth(self.dir / "token.json"), self.dir / "subs.json",
                                     fetch_subs=fetch_subs, notify=lambda t, m: self.sent.append(t),
                                     every=3600, retry=60, clock=lambda: self.now[0])

    def test_missing_auth_without_list(self):
        self.answers = [google_oauth.AuthMissing("no token")]
        self.assertEqual(self.subs().get(), (None, "missing"))
        self.assertEqual(self.sent, ["TV Home: YouTube login missing"])

    def test_expired_serves_last_list_and_notifies_once(self):
        chans = [{"id": CH_A, "title": "A"}]
        self.answers = [chans, google_oauth.AuthExpired("invalid_grant"), google_oauth.AuthExpired("invalid_grant")]
        s = self.subs()
        self.assertEqual(s.get(), (chans, "ok"))
        self.now[0] += 3600
        self.assertEqual(s.get(), (chans, "expired"))
        self.now[0] += 60
        self.assertEqual(s.get(), (chans, "expired"))
        self.assertEqual(self.sent, ["TV Home: YouTube login expired"])
        # after a restart: the list and the "already notified" flag come from disk
        self.answers = [google_oauth.AuthExpired("invalid_grant")]
        self.assertEqual(self.subs().get(), (chans, "expired"))
        self.assertEqual(len(self.sent), 1)

    def test_new_token_file_triggers_refresh_and_rearms(self):
        chans = [{"id": CH_B, "title": "B"}]
        self.answers = [google_oauth.AuthMissing("no token"), chans]
        s = self.subs()
        self.assertEqual(s.get()[1], "missing")
        self.assertFalse(s.wants_refresh())
        (self.dir / "token.json").write_text("{}")
        self.assertTrue(s.wants_refresh())
        self.assertEqual(s.get(), (chans, "ok"))  # no waiting for the retry interval
        self.assertIsNone(json.loads((self.dir / "subs.json").read_text())["notified"])

    def test_transient_error_keeps_list(self):
        chans = [{"id": CH_A, "title": "A"}]
        self.answers = [chans, google_oauth.GoogleError("HTTP 503")]
        s = self.subs()
        s.get()
        self.now[0] += 3600
        self.assertEqual(s.get(), (chans, "ok"))
        self.assertIn("503", s.last_error)
        self.assertEqual(self.sent, [])

    def test_youtube_payload_when_never_authorised(self):
        self.answers = [google_oauth.AuthMissing("no client")]
        out = youtube.YouTubeSource(self.subs(), self.dir, fetch=FakeWeb())()
        self.assertEqual(out, {"updated": None, "channelCount": 0, "auth": "missing", "watched": "unavailable",
                               "videos": [], "continue": []})


class GoogleAuthTest(unittest.TestCase):
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.client = d / "client.json"
        self.token = d / "token.json"
        self.client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec",
                                                         "token_uri": self.TOKEN_URL}}))
        self.token.write_text(json.dumps({"refresh_token": "r1", "scope": google_oauth.SCOPE,
                                          "token_type": "Bearer", "obtained_at": "2026-10-20T12:00:00+00:00"}))
        self.now = [0.0]

    def tearDown(self):
        self.tmp.cleanup()

    def auth(self, web):
        return google_oauth.GoogleAuth(self.client, self.token, fetch=web, clock=lambda: self.now[0])

    def test_refresh_then_reuse_until_expiry(self):
        n = [0]

        def token(url, method, headers, data):
            n[0] += 1
            self.assertEqual((data["grant_type"], data["refresh_token"], data["client_id"], data["client_secret"]),
                             ("refresh_token", "r1", "cid", "sec"))
            return net.Response(200, {}, json.dumps({"access_token": f"a{n[0]}", "expires_in": 3599}).encode())
        auth = self.auth(FakeWeb({self.TOKEN_URL: token}))
        self.assertEqual(auth.access_token(), "a1")
        self.now[0] += 3000
        self.assertEqual(auth.access_token(), "a1")
        self.now[0] += 600
        self.assertEqual(auth.access_token(), "a2")

    def test_invalid_grant_is_expired_and_missing_token_is_missing(self):
        web = FakeWeb({self.TOKEN_URL: net.Response(
            400, {}, b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}')})
        with self.assertRaises(google_oauth.AuthExpired):
            self.auth(web).access_token()
        self.token.unlink()
        with self.assertRaises(google_oauth.AuthMissing):
            self.auth(web).access_token()

    def test_server_error_is_transient(self):
        web = FakeWeb({self.TOKEN_URL: net.Response(503, {}, b"oops")})
        with self.assertRaises(google_oauth.GoogleError):
            self.auth(web).access_token()

    def test_subscriptions_paginate_and_retry_once_on_401(self):
        tokens = iter(["stale", "fresh"])
        pages = {None: {"items": [{"snippet": {"title": "A", "resourceId": {"channelId": CH_A}}}],
                        "nextPageToken": "p2"},
                 "p2": {"items": [{"snippet": {"title": "B", "resourceId": {"channelId": CH_B}}}]}}

        def api(url, method, headers, data):
            if headers["Authorization"] == "Bearer stale":
                return net.Response(401, {}, b'{"error": {"code": 401, "errors": [{"reason": "authError"}]}}')
            page = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("pageToken")
            return net.Response(200, {}, json.dumps(pages[page]).encode())

        def token(url, method, headers, data):
            return net.Response(200, {}, json.dumps({"access_token": next(tokens), "expires_in": 3600}).encode())
        web = FakeWeb({google_oauth.SUBSCRIPTIONS_URL: api, self.TOKEN_URL: token})
        got = google_oauth.fetch_subscriptions(self.auth(web), fetch=web)
        self.assertEqual(got, [{"id": CH_A, "title": "A"}, {"id": CH_B, "title": "B"}])

    def test_device_flow_polls_until_approved_and_saves_the_shared_format(self):
        answers = iter([net.Response(428, {}, b'{"error": "authorization_pending"}'),
                        net.Response(403, {}, b'{"error": "slow_down"}'),
                        net.Response(200, {}, b'{"access_token": "a", "refresh_token": "r2", "scope": "s", '
                                              b'"token_type": "Bearer", "expires_in": 3599}')])
        web = FakeWeb({self.TOKEN_URL: lambda *a: next(answers)})
        sleeps = []
        client = google_oauth.load_client(self.client)
        tok = google_oauth.poll_device_flow(client, {"device_code": "d", "interval": 5, "expires_in": 1800},
                                            fetch=web, sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(sleeps, [5, 5, 10])
        google_oauth.save_token(self.token, tok)
        saved = json.loads(self.token.read_text())
        self.assertEqual(set(saved), {"refresh_token", "scope", "token_type", "obtained_at"})
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)


LOGINUSERS = '''"users"
{
	"76561198000000001"
	{
		"AccountName"		"a"
		"Timestamp"		"1792484185"
	}
	"76561198000000002"
	{
		"AccountName"		"b"
		"timestamp"		"1783842332"
	}
}
'''

LOCALCONFIG = r'''"UserLocalConfigStore"
{
	// a comment
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"apps"
				{
					"570"	{ "LastPlayed" "1787994190" "Playtime" "4321" }
					"2225070" { "LastPlayed" "1788697821" "Playtime" "928" }
					"1493710" { "LastPlayed" "1791000000" }
					"11020" { "LastPlayed" "1785575258" "Playtime" "300" }
					"22490" { "LastPlayed" "1785092627" "Playtime" "72" }
					"760"	{ "cloud" { "quota_bytes" "1" } }
					"1182900" { "LastPlayed" "1791900000" "Playtime" "42" "note" "say \"hi\"" }
				}
			}
		}
	}
}
'''


def manifest(appid, name, last):
    return steam.parse_vdf(f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n'
                           f'\t"LastPlayed"\t\t"{last}"\n}}\n')


class SteamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, store=None):
        store = store or {}

        def details(url, method, headers, data):
            appid = url.split("appids=")[1].split("&")[0]
            info = store.get(appid)
            body = {appid: {"success": True, "data": info} if info else {"success": False}}
            return net.Response(200, {}, json.dumps(body).encode())
        self.web = FakeWeb({"https://store.steampowered.com/api/appdetails": details})
        return steam.StoreNames(self.dir / "names.json", fetch=self.web)

    def test_vdf_parse_and_active_account(self):
        lc = steam.parse_vdf(LOCALCONFIG)
        apps = steam.get_ci(lc, "userlocalconfigstore", "software", "valve", "steam", "apps")
        self.assertEqual(apps["1182900"]["note"], 'say "hi"')
        self.assertEqual(steam.active_account(steam.parse_vdf(LOGINUSERS)), 39734273)
        most_recent = LOGINUSERS.replace('"AccountName"\t\t"b"', '"AccountName"\t\t"b"\n\t\t"MostRecent"\t\t"1"')
        self.assertEqual(steam.active_account(steam.parse_vdf(most_recent)),
                         76561198000000002 - steam.STEAMID64_BASE)

    def test_recent_order_tools_names_and_limit(self):
        apps = steam.played_apps(steam.parse_vdf(LOCALCONFIG), [
            manifest(2225070, "Trackmania", 1788697821), manifest(570, "Dota 2", 1787994190),
            manifest(1182900, "A Plague Tale: Requiem", 1791981679), manifest(3658110, "Proton 10.0", 0),
            manifest(1628350, "Steam Linux Runtime 3.0 (sniper)", 1790000000)])
        self.assertEqual(apps[1182900]["lastPlayed"], 1791981679)  # the manifest's newer stamp wins
        names = self.names({"11020": {"name": "TrackMania Nations Forever", "type": "game"},
                            "22490": {"name": "Some DLC", "type": "dlc"}})
        src = steam.SteamSource(lambda: True, self.dir / "steam.json", names, read=lambda: (None, None))
        games = src.build(apps)
        self.assertEqual([g["appid"] for g in games], [1182900, 2225070, 570, 11020])
        self.assertEqual(games[1], {"appid": 2225070, "name": "Trackmania",
                                    "lastPlayed": steam.iso_local(1788697821), "playtimeH": 15.5,
                                    "cover": "/api/steam/2225070/cover", "hero": "/api/steam/2225070/hero",
                                    "logo": "/api/steam/2225070/logo", "header": "/api/steam/2225070/header"})
        self.assertEqual(games[3]["name"], "TrackMania Nations Forever")
        # installed games need no store lookup; Proton, the runtime and the DLC are left out
        looked_up = sorted(c["url"].split("appids=")[1].split("&")[0] for c in self.web.calls)
        self.assertEqual(looked_up, ["11020", "22490"])
        src.limit = 2
        self.assertEqual(len(src.build(apps)), 2)
        before = len(self.web.calls)  # names are cached on disk: a new process asks the store nothing
        steam.SteamSource(lambda: True, self.dir / "s2.json",
                          steam.StoreNames(self.dir / "names.json", fetch=self.web)).build(apps)
        self.assertEqual(len(self.web.calls), before)

    def test_persisted_result_is_served_while_desktop_sleeps(self):
        on = [True]
        reads = []

        def read():
            reads.append(1)
            return steam.parse_vdf(LOCALCONFIG), [manifest(2225070, "Trackmania", 1788697821)]
        names = self.names({"11020": {"name": "TMNF", "type": "game"}})
        src = steam.SteamSource(lambda: on[0], self.dir / "steam.json", names, read=read)
        first = src()
        self.assertTrue(first["recent"])
        on[0] = False
        restarted = steam.SteamSource(lambda: on[0], self.dir / "steam.json", names, read=read)
        self.assertEqual(restarted(), first)
        self.assertFalse(restarted.due())
        self.assertEqual(len(reads), 1)
        on[0] = True
        self.assertTrue(restarted.due())

    def test_read_desktop_commands(self):
        commands = []

        def run(cmd):
            commands.append(cmd)
            if "loginusers" in cmd:
                return LOGINUSERS + steam.SEP + '\n"libraryfolders" { "0" { "path" "/home/x/Steam Lib" } }'
            return LOCALCONFIG + steam.SEP + '\n"AppState" { "appid" "570" "name" "Dota 2" }'
        lc, mans = steam.read_desktop(run)
        self.assertIn("userdata/39734273/config/localconfig.vdf", commands[1])
        self.assertIn("'/home/x/Steam Lib'/steamapps/appmanifest_*.acf", commands[1])
        self.assertEqual(mans[0]["AppState"]["name"], "Dota 2")
        self.assertIsNotNone(steam.get_ci(lc, "UserLocalConfigStore", "Software"))

    def test_asset_candidates(self):
        urls = steam.asset_candidates(570, "cover")
        self.assertTrue(urls[0].endswith("/570/library_600x900_2x.jpg"))
        self.assertEqual(steam.asset_candidates(570, "header", "https://x/h.jpg")[-1], "https://x/h.jpg")


class UrlImageCacheTest(unittest.TestCase):
    def test_fallback_miss_and_stale(self):
        good = jpeg(480, 360)
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            web = FakeWeb({"https://i.ytimg.com/vi/aaaaaaaaaaa/maxresdefault.jpg": net.Response(200, {}, jpeg(120, 90)),
                           "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg": net.Response(200, {}, good)})
            cache = images.UrlImageCache(Path(tmp), fetch=web, fresh_s=100, clock=lambda: now[0])
            key, urls = "yt_aaaaaaaaaaa_thumb", youtube.thumb_candidates("aaaaaaaaaaa")
            self.assertEqual(cache.get(key, urls, youtube.thumb_ok)[0], good)
            cache.get(key, urls, youtube.thumb_ok)
            self.assertEqual(len(web.calls), 2)  # the second answer came from disk
            other = youtube.thumb_candidates("bbbbbbbbbbb")
            with self.assertRaises(jellyfin.NotFound):
                cache.get("yt_bbbbbbbbbbb_thumb", other, youtube.thumb_ok)
            n = len(web.calls)
            with self.assertRaises(jellyfin.NotFound):
                cache.get("yt_bbbbbbbbbbb_thumb", other, youtube.thumb_ok)
            self.assertEqual(len(web.calls), n)  # remembered miss
            now[0] += 1000  # past freshness, network down: the stale copy; nothing cached: Unavailable
            web.routes = {"https://": OSError("down")}
            self.assertEqual(cache.get(key, urls, youtube.thumb_ok)[0], good)
            with self.assertRaises(images.Unavailable):
                cache.get("yt_ccccccccccc_thumb", youtube.thumb_candidates("ccccccccccc"))


class Slice2HttpTest(unittest.TestCase):
    def test_home_youtube_steam_and_image_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "steam.json").write_text(json.dumps({"updated": "2026-10-20T14:00:00+02:00", "recent": []}))
            steam_src = steam.SteamSource(lambda: False, d / "steam.json",
                                          steam.StoreNames(d / "names.json", fetch=FakeWeb()))
            web = FakeWeb({youtube.FEED_URL.format(CH_A): net.Response(200, {}, feed_xml(
                CH_A, "A", [(vid(1), "2026-10-11T10:00:00+00:00", False)])),
                "https://www.youtube.com/shorts/": shorts_route(set())})
            yt_src = youtube.YouTubeSource(youtube.StaticChannels([CH_A]), d, fetch=web)
            thumb = jpeg(1280, 720)
            img_web = FakeWeb({"https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg": net.Response(200, {}, thumb)})
            app = tvhome_server.App(d, lambda: None, lambda: None, FakeDesktop().pc(),
                                    images.ImageCache(d / "img", None), hdd=lambda: {},
                                    youtube_source=yt_src, steam_source=steam_src,
                                    web_images=images.UrlImageCache(d / "web", fetch=img_web))
            app.youtube.refresh_now()
            server = tvhome_server.make_server(app, "127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def get(path):
                try:
                    with urllib.request.urlopen(base + path, timeout=5) as resp:
                        return resp.status, resp.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()
            try:
                home = json.loads(get("/api/home")[1])
                self.assertEqual(home["steam"]["updated"], "2026-10-20T14:00:00+02:00")  # persisted, PC off
                self.assertEqual([v["id"] for v in home["youtube"]["videos"]], [vid(1)])
                self.assertEqual(get("/api/yt/dQw4w9WgXcQ/thumb"), (200, thumb))
                self.assertEqual(get("/api/steam/570/cover")[0], 404)  # every CDN answers 404
                self.assertEqual(get("/api/steam/570/banner")[0], 404)
                self.assertEqual(get("/api/yt/short/thumb")[0], 404)
            finally:
                server.shutdown()
                server.server_close()


class ConfigGatingTest(unittest.TestCase):
    """config.toml decides which sources run; /api/home tells the UI which tabs to build."""

    def test_features_follow_the_config(self):
        empty = tvconfig.features(tvconfig.from_dict({}))
        self.assertEqual({k: empty[k] for k in ("jellyfin", "steam", "weather")},
                         {"jellyfin": False, "steam": False, "weather": False})
        cfg = tvconfig.from_dict({
            "jellyfin": {"url": "http://127.0.0.1:8096", "user": "viewer"},
            "weather": {"latitude": 0.0, "longitude": 0.0},
            "steam": {"pc_ip": "192.0.2.5", "pc_mac": "00:11:22:33:44:55"},  # no Moonlight host: off
        })
        f = tvconfig.features(cfg)
        self.assertTrue(f["jellyfin"] and f["weather"])
        self.assertFalse(f["steam"])
        self.assertEqual(cfg["tv"]["apps"]["moonlight"], "com.limelight.webos")  # defaults stay
        self.assertTrue(tvconfig.features(tvconfig.from_dict(
            {"youtube": {"subscriptions_csv": "~/subs.csv"}}))["youtube"])

    def test_config_file_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('[jellyfin]\nurl = "http://127.0.0.1:8096"\nuser = "viewer"\n[tv]\nip = "192.0.2.20"\n')
            cfg = tvconfig.load(p)
            self.assertEqual(cfg["tv"]["ip"], "192.0.2.20")
            self.assertEqual(cfg["tv"]["app_id"], "com.example.tvhome")
            self.assertTrue(tvconfig.features(cfg)["jellyfin"])
            p.write_text("[tv\n")
            with self.assertRaises(tvconfig.ConfigError):
                tvconfig.load(p)

    def test_example_config_parses_and_turns_everything_on(self):
        cfg = tvconfig.load(Path(__file__).resolve().parent.parent / "config.example.toml")
        f = tvconfig.features(cfg)
        self.assertTrue(f["jellyfin"] and f["youtube"] and f["weather"])
        self.assertFalse(f["steam"])  # needs the Moonlight host uuid filled in
        self.assertEqual(cfg["tv"]["apps"]["youtube"], "youtube.leanback.v4")

    def test_takeout_csv_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "subscriptions.csv"
            p.write_text("Channel Id,Channel Url,Channel Title\n"
                         "UCfixturechannel01xxxxxx,http://www.youtube.com/channel/UCfixturechannel01xxxxxx,One\n"
                         "bogus,x,y\n")
            self.assertEqual(youtube.csv_channels(p).get(), ([{"id": "UCfixturechannel01xxxxxx", "title": "One"}], "ok"))

    def test_disabled_sources_are_null_and_their_routes_gone(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            app = tvhome_server.App(d, lambda: calls.append("jf") or {"resume": []}, lambda: calls.append("wx"),
                                    FakeDesktop(on=True, sunshine=True).pc(), images.ImageCache(d / "img", None),
                                    hdd=lambda: {}, features={"jellyfin": False, "youtube": True,
                                                              "steam": False, "weather": False})
            app.start()
            server = tvhome_server.make_server(app, "127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def call(path, method="GET"):
                req = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None)
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, None
            try:
                status, home = call("/api/home")
                self.assertEqual(status, 200)
                self.assertEqual(home["features"], {"jellyfin": False, "youtube": True, "steam": False,
                                                    "weather": False})
                for key in ("jellyfin", "weather", "pc", "steam"):
                    self.assertIsNone(home[key], key)
                self.assertEqual(call("/api/pc")[0], 404)
                self.assertEqual(call("/api/pc/play", "POST")[0], 404)
                self.assertEqual(call("/api/jellyfin/open/abc", "POST")[0], 404)
                self.assertEqual(call("/tv/bridge.js")[0], 404)  # no [server] url, no bridge
                time.sleep(0.2)
                self.assertEqual(calls, [])  # disabled sources are never fetched
                self.assertIsNone(app.pc.last)  # and the PC is never probed
            finally:
                server.shutdown()
                server.server_close()


# -- smooth pass: image shaping with Pillow, logoDark --------------------------

def real_jpeg(w, h, color=(200, 40, 40)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG")
    return out.getvalue()


def logo_png(w, h, box, color):
    """Transparent w x h PNG with an opaque rectangle `box` in `color`."""
    from PIL import Image
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color + (255,)), box[:2])
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def size_of(data):
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


class ShapeTest(unittest.TestCase):
    def test_resize_width_steps(self):
        self.assertEqual(images.resize_width("300"), 300)
        self.assertEqual(images.resize_width("301"), 320)
        self.assertIsNone(images.resize_width("0"))
        self.assertIsNone(images.resize_width("x"))

    def test_jpeg_scaled_down_never_up(self):
        data, ctype = images.shape(real_jpeg(1280, 720), 320)
        self.assertEqual((ctype, size_of(data)), ("image/jpeg", (320, 180)))
        self.assertIsNone(images.shape(real_jpeg(300, 450), 320))  # already small enough: served as is

    def test_logo_info_dark_and_trim(self):
        light = logo_png(640, 360, (100, 100, 300, 200), (240, 240, 240))
        dark = logo_png(640, 360, (100, 100, 300, 200), (10, 10, 10))
        self.assertEqual(images.logo_info(light), {"dark": False, "box": [100, 100, 300, 200]})
        self.assertEqual(images.logo_info(dark), {"dark": True, "box": None})
        data, ctype = images.shape(light, 100, trim=True)  # trimmed to 200x100, then scaled to 100 wide
        self.assertEqual((ctype, size_of(data)), ("image/png", (100, 50)))

    def test_shaper_falls_back_to_the_original(self):
        src = (b"not an image", "image/jpeg")
        self.assertEqual(images.Shaper().get("k", src, 320), src)
        self.assertEqual(images.Shaper().get("k", src, None), src)

    def test_logo_info_cache_waits_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logo-info.json"
            loads = []

            def load(url):
                loads.append(url)
                if url == "/missing":
                    raise jellyfin.NotFound(url)
                return logo_png(64, 64, (0, 0, 64, 64), (5, 5, 5))
            info = images.LogoInfo(path, load)
            self.assertTrue(info.dark("/a", wait_s=5))
            self.assertIsNone(info.dark("/missing", wait_s=5))
            self.assertTrue(info.dark("/a"))  # cached: no second load
            self.assertEqual(loads, ["/a", "/missing"])
            again = images.LogoInfo(path, load)  # read back from disk
            self.assertTrue(again.dark("/a"))
            self.assertEqual(len(loads), 2)


class SmoothHttpTest(unittest.TestCase):
    def test_widths_trimmed_logos_and_logo_dark(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            item = jellyfin.map_item(movie("a" * 32))
            rows = {"resume": [item], "nextUp": [], "latest": []}
            game = steam.to_game(570, "Dota 2", 1760000000, 60)
            (d / "steam.json").write_text(json.dumps({"updated": None, "recent": [game]}))
            steam_src = steam.SteamSource(lambda: False, d / "steam.json",
                                          steam.StoreNames(d / "names.json", fetch=FakeWeb()))
            jf_logo = logo_png(800, 300, (100, 50, 500, 150), (230, 230, 230))
            dark_logo = logo_png(640, 360, (0, 0, 640, 360), (0, 0, 0))

            def jf_fetch(item_id, kind, width):
                if kind == "Logo":
                    return jf_logo, "image/png"
                raise jellyfin.NotFound(item_id)
            web = FakeWeb({"https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg": net.Response(200, {}, real_jpeg(1280, 720)),
                           "https://shared.steamstatic.com/store_item_assets/steam/apps/570/logo.png":
                           net.Response(200, {}, dark_logo),
                           "https://shared.steamstatic.com/store_item_assets/steam/apps/570/library_600x900_2x.jpg":
                           net.Response(200, {}, real_jpeg(600, 900))})
            app = tvhome_server.App(d, lambda: rows, lambda: None, FakeDesktop().pc(),
                                    images.ImageCache(d / "img", jf_fetch), hdd=lambda: {},
                                    steam_source=steam_src, web_images=images.UrlImageCache(d / "web", fetch=web),
                                    logo_cache=d / "logo-info.json")
            app.jellyfin.refresh_now()
            home = app.home()
            self.assertIs(home["jellyfin"]["resume"][0]["logoDark"], False)
            self.assertIs(home["steam"]["recent"][0]["logoDark"], True)
            self.assertNotIn("logoDark", app.jellyfin.get()["resume"][0])  # the shared cached value is untouched
            data, _ = app.image(f"/api/img/{'a' * 32}/logo", "480")
            self.assertEqual(size_of(data), (400, 100))  # transparent padding trimmed
            self.assertEqual(size_of(app.image("/api/yt/dQw4w9WgXcQ/thumb", "320")[0]), (320, 180))
            self.assertEqual(size_of(app.image("/api/yt/dQw4w9WgXcQ/thumb", None)[0]), (1280, 720))
            self.assertEqual(size_of(app.image("/api/steam/570/cover", "300")[0]), (300, 450))
            self.assertIsNone(app.image("/api/nope", None))


# -- watched state from the desktop browser's login (cookies + InnerTube) ------

FIXTURES = Path(__file__).resolve().parent / "testdata" / "innertube"  # synthetic, see make_innertube_fixtures.py
CHANNEL = "Couch Channel"
BRAND_DS = "200000000000000000002||100000000000000000001"  # the fixtures' fake brand channel
MOZ_COOKIES = """CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '',
 name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER,
 isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0,
 schemeMap INTEGER DEFAULT 0, isPartitionedAttributeSet INTEGER DEFAULT 0)"""
LOGIN = [(".youtube.com", "LOGIN_INFO", "fake-login"), (".youtube.com", "SAPISID", "fake-sapisid"),
         (".youtube.com", "__Secure-1PAPISID", "fake-1p"), (".youtube.com", "__Secure-3PAPISID", "fake-3p"),
         (".youtube.com", "SID", "fake-sid")]


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def login_cookies():
    return [{"host": h, "name": n, "value": v, "path": "/", "expiry": None, "secure": True, "httpOnly": False}
            for h, n, v in LOGIN]


def cookie_tar(rows, extra_members=()):
    """A tar like the desktop's: cookies.sqlite plus a -wal that still holds the newest row."""
    import io
    import sqlite3
    import tarfile
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "cookies.sqlite"
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute(MOZ_COOKIES)
        con.commit()
        for i, (host, name, value, expiry, attrs) in enumerate(rows):
            con.execute("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, lastAccessed,"
                        " creationTime, isSecure, isHttpOnly) VALUES (?,?,?,?,'/',?,?,?,1,0)",
                        (attrs, name, value, host, expiry, i, i))
            con.commit()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:  # while `con` is open: the WAL is not checkpointed
            for name in ("cookies.sqlite", "cookies.sqlite-wal"):
                tar.add(Path(tmp) / name, arcname=name)
            for member, data in extra_members:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
        con.close()
        return buf.getvalue()


class CookieExtractionTest(unittest.TestCase):
    NOW = 1_800_000_000

    def test_keeps_only_unexpired_default_container_youtube_cookies(self):
        import tarfile
        future_ms, future_s = (self.NOW + 86400) * 1000, self.NOW + 86400
        rows = [(".youtube.com", "SAPISID", "a", future_ms, ""),          # Firefox 132+: milliseconds
                (".youtube.com", "SID", "b", future_s, ""),               # older: seconds
                (".google.com", "SID", "h", future_s, ""),                # never sent to youtube.com: dropped
                (".youtube.com", "OLD", "c", self.NOW - 10, ""),          # expired
                (".youtube.com", "BOX", "d", future_s, "^userContextId=2"),  # a container tab
                ("www.youtube.com", "HOSTONLY", "e", future_s, ""),
                (".example.com", "X", "f", future_s, ""),
                (".youtube.com", "LOGIN_INFO", "g", future_s, "")]       # last insert: only in the WAL
        evil = tarfile.TarInfo("../escape")
        evil.size = 1
        link = tarfile.TarInfo("cookies.sqlite-shm")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        out = yt_cookies.extract(cookie_tar(rows, [(evil, b"x"), (link, None)]), now=self.NOW)
        got = {(c["host"], c["name"]): (c["value"], c["expiry"]) for c in out}
        self.assertEqual(got, {(".youtube.com", "SAPISID"): ("a", future_s), (".youtube.com", "SID"): ("b", future_s),
                               (".youtube.com", "LOGIN_INFO"): ("g", future_s)})
        self.assertTrue(yt_cookies.has_login(out))
        self.assertFalse(yt_cookies.has_login([c for c in out if c["name"] != "LOGIN_INFO"]))

    def test_store_syncs_only_while_desktop_on_and_hourly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg" / "yt-cookies.json"
            on, now, calls = [False], [0.0], []
            tar = cookie_tar([(h, n, v, self.NOW * 2, "") for h, n, v in LOGIN])

            def fetch_tar():
                calls.append(1)
                return tar, "abcd.Default (release)"
            store = yt_cookies.CookieStore(path, lambda: on[0], fetch_tar, every=3600, retry=600,
                                           clock=lambda: now[0])
            self.assertFalse(store.sync_if_due())  # desktop off: never woken, nothing copied
            self.assertEqual((calls, store.cookies()), ([], []))
            on[0] = True
            self.assertTrue(store.sync_if_due())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            saved = json.loads(path.read_text())
            self.assertEqual((saved["profile"], saved["login"], len(saved["cookies"])),
                             ("abcd.Default (release)", True, 5))
            now[0] = 1800
            self.assertFalse(store.sync_if_due())  # not yet an hour
            now[0] = 3600
            self.assertTrue(store.sync_if_due())
            self.assertEqual(len(calls), 2)

    def test_failed_copy_keeps_the_last_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "yt-cookies.json"
            answers = [(cookie_tar([(h, n, v, self.NOW * 2, "") for h, n, v in LOGIN]), "p"),
                       OSError("ssh pc exited 255")]

            def fetch_tar():
                a = answers.pop(0)
                if isinstance(a, Exception):
                    raise a
                return a
            now = [0.0]
            store = yt_cookies.CookieStore(path, lambda: True, fetch_tar, every=3600, retry=600, clock=lambda: now[0])
            store.sync_if_due()
            now[0] = 3600
            self.assertFalse(store.sync_if_due())
            self.assertIn("255", store.last_error)
            self.assertEqual(len(store.cookies()), 5)

    def test_remote_command_only_reads(self):
        for word in ("rm ", "mv ", "cp ", " > ", "sqlite3", "touch", "tee"):
            self.assertNotIn(word, yt_cookies.REMOTE)
        self.assertIn("tar -cf -", yt_cookies.REMOTE)


class SapisidHashTest(unittest.TestCase):
    JAR = {"SAPISID": "fake-sapisid", "__Secure-1PAPISID": "fake-1p", "__Secure-3PAPISID": "fake-3p"}

    def test_three_hashes_like_yt_dlp(self):
        auth = innertube.authorization(self.JAR, ts=1700000000)
        parts = auth.split(" ")
        self.assertEqual(parts[0:2], ["SAPISIDHASH", "1700000000_6b2c32afdc7a2c4f00b844c84a58147e96fba5d6"])
        self.assertEqual(parts[2:4], ["SAPISID1PHASH", "1700000000_f727a04f821b8fc39325677da69e33c745657df8"])
        self.assertEqual(parts[4], "SAPISID3PHASH")

    def test_user_session_part_and_3p_fallback(self):
        auth = innertube.authorization(self.JAR, ts=1700000000, user_session_id="100000000000000000001")
        self.assertTrue(auth.startswith("SAPISIDHASH 1700000000_1f3a9259022164303b9cb24bc586324aac38fea1_u "))
        only_3p = innertube.authorization({"__Secure-3PAPISID": "fake-sapisid"}, ts=1700000000)
        self.assertEqual(only_3p.split(" ")[:2], ["SAPISIDHASH", "1700000000_6b2c32afdc7a2c4f00b844c84a58147e96fba5d6"])
        self.assertIsNone(innertube.authorization({"SID": "x"}))

    def test_client_headers_carry_cookie_session_and_channel(self):
        c = innertube.Client(login_cookies() + [{"host": ".google.com", "name": "SID", "value": "g"}],
                             clock=lambda: 1700000000)
        c.auth_user, c.page_id = 1, "200000000000000000002"
        h = c.headers()
        self.assertEqual((h["X-Goog-AuthUser"], h["X-Goog-PageId"], h["X-Origin"]),
                         ("1", "200000000000000000002", "https://www.youtube.com"))
        self.assertIn("SAPISID=fake-sapisid", h["Cookie"])
        self.assertNotIn("=g", h["Cookie"])  # .google.com cookies do not go to youtube.com
        with self.assertRaises(innertube.SessionError):
            innertube.Client([{"host": ".youtube.com", "name": "SAPISID", "value": "x"}]).headers()


class InnerTubeParseTest(unittest.TestCase):
    def test_subscriptions_feed(self):
        data = fixture("fe-subscriptions.json")
        videos = innertube.parse_videos(data)
        self.assertEqual(len(videos), 8)  # the Shorts shelf is skipped
        by_id = {v["id"]: v for v in videos}
        self.assertEqual(by_id["fakeVidPart"], {"id": "fakeVidPart", "title": "How tides work",
                                                "channel": by_id["fakeVidPart"]["channel"], "progress": 0.27})
        self.assertEqual(by_id["fakeVidDone"]["progress"], 1.0)
        self.assertEqual(by_id["fakeVidTenP"]["progress"], 0.1)
        self.assertEqual(sum(v["progress"] == 1.0 for v in videos), 2)
        self.assertEqual(sum(v["progress"] is None for v in videos), 4)
        self.assertEqual(innertube.continuation(data), "FAKE_SUBS_CONTINUATION")
        self.assertEqual(innertube.datasync_id(data), BRAND_DS)
        self.assertFalse(innertube.logged_out(data))

    def test_history(self):
        data = fixture("fe-history.json")
        videos = innertube.parse_videos(data)
        self.assertTrue(all(v["progress"] is not None for v in videos))  # history always has the bar
        self.assertIn(("fakeVidPart", 0.27), [(v["id"], v["progress"]) for v in videos])
        self.assertGreaterEqual(sum(v["progress"] == 1.0 for v in videos), 3)
        self.assertNotIn("a Short", [v["title"] for v in videos])
        self.assertEqual(innertube.continuation(data), "FAKE_HISTORY_CONTINUATION")

    def test_older_video_renderer_resume_overlay(self):
        data = {"contents": {"x": [{"videoRenderer": {"videoId": "dQw4w9WgXcQ", "title": {"runs": [{"text": "T"}]},
                "thumbnailOverlays": [{"thumbnailOverlayResumePlaybackRenderer": {"percentDurationWatched": 55}}]}}]}}
        self.assertEqual(innertube.parse_videos(data), [{"id": "dQw4w9WgXcQ", "title": "T", "channel": "", "progress": 0.55}])

    def test_channels_and_accounts(self):
        data = fixture("fe-channels.json")
        self.assertEqual([c["title"] for c in innertube.parse_channels(data)], ["Channel 1", "Channel 2", "Channel 3"])
        self.assertIsNone(innertube.continuation(data))  # the chip bar's token is not a next page
        accounts = innertube.parse_accounts(fixture("accounts-list.json"))
        self.assertEqual([(a["name"], a["selected"], a["pageId"]) for a in accounts],
                         [("Main Account", True, None), (CHANNEL, False, "200000000000000000002")])
        self.assertEqual(innertube.user_session_id(accounts[1]["datasyncId"]), "100000000000000000001")
        self.assertEqual(innertube.user_session_id("100000000000000000001||"), "100000000000000000001")

    def test_merge_progress_takes_highest_bar(self):
        merged = yt_watched.merge_progress([{"id": "a", "progress": 0.3}, {"id": "b", "progress": None}],
                                           [{"id": "a", "progress": 1.0}, {"id": "a", "progress": 0.1}])
        self.assertEqual(merged, {"a": 1.0})
        self.assertEqual(yt_watched.WatchState({"a": 0.95, "b": 0.89, "c": 1.0}).hidden(), {"a", "c"})


class FakeInnerTube:
    """POST /youtubei/v1/... stand-in answering from the fixtures; the brand channel lives at session index 1."""

    def __init__(self):
        self.calls = []
        self.signed_out = False
        self.down = False
        self.datasync = BRAND_DS

    def __call__(self, url, method="GET", headers=None, data=None, timeout=15, follow=True):
        body = json.loads(data)
        self.calls.append({"url": url, "headers": headers, "body": body})
        if self.down:
            raise OSError("network down")
        ctx = {"responseContext": {"mainAppWebResponseContext": {"datasyncId": self.datasync,
                                                                 "loggedOut": self.signed_out}}}
        if "/account/accounts_list" in url:
            if self.signed_out or headers["X-Goog-AuthUser"] not in ("0", "1"):
                answer = {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}
            elif headers["X-Goog-AuthUser"] == "0":
                answer = {**ctx, "actions": [{"accountItemRenderer": {"accountName": {"simpleText": "Work"},
                          "isSelected": True, "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                              "supportedTokens": [{"datasyncIdToken": {"datasyncIdToken": "9||"}}]}}}}]}
            else:
                answer = fixture("accounts-list.json")
            return net.Response(200, {}, json.dumps(answer).encode())
        name = {"FEsubscriptions": "fe-subscriptions.json", "FEhistory": "fe-history.json",
                "FEchannels": "fe-channels.json"}[body["browseId"]]
        return net.Response(200, {}, json.dumps({**fixture(name), **ctx}).encode())

    def browses(self):
        return [c["body"].get("browseId") for c in self.calls if "/browse" in c["url"]]


class CookieYouTubeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = [10_000.0]
        self.sent = []
        self.web = FakeInnerTube()
        self.write_cookies(login_cookies())

    def tearDown(self):
        self.tmp.cleanup()

    def write_cookies(self, cookies):
        net.write_json(self.dir / "yt-cookies.json", {"synced": "x", "cookies": cookies})

    def make(self):
        store = yt_cookies.CookieStore(self.dir / "yt-cookies.json", lambda: False)
        return yt_watched.CookieYouTube(store, self.dir / "yt-watched.json", channel_name=CHANNEL, fetch=self.web,
                                        notify=lambda t, m: self.sent.append(m), clock=lambda: self.now[0])

    def test_finds_the_channel_and_reads_feed_and_history(self):
        cy = self.make()
        state = cy.watch_state()
        self.assertEqual(cy.status_word, "ok")
        self.assertEqual(state.progress["fakeVidPart"], 0.27)
        self.assertIn("fakeVidDone", state.progress)
        self.assertGreaterEqual(len(state.hidden()), 3)
        self.assertNotIn("fakeVidPart", state.hidden())
        browse = [c for c in self.web.calls if "/browse" in c["url"]]
        self.assertEqual({c["headers"]["X-Goog-PageId"] for c in browse}, {"200000000000000000002"})
        self.assertEqual({c["headers"]["X-Goog-AuthUser"] for c in browse}, {"1"})
        for c in browse:  # every hash carries the brand channel's user session ("_u")
            self.assertTrue(all(p.endswith("_u") for p in c["headers"]["Authorization"].split(" ")[1::2]))
        # only read endpoints were called
        self.assertEqual({c["url"].split("/youtubei/v1/")[1].split("?")[0] for c in self.web.calls},
                         {"account/accounts_list", "browse"})
        self.assertEqual(self.web.browses(), ["FEsubscriptions", "FEhistory"])
        self.assertEqual([c["title"] for c in cy.channels()], ["Channel 1", "Channel 2", "Channel 3"])

    def test_at_most_every_30_minutes(self):
        cy = self.make()
        cy.watch_state()
        self.now[0] += 29 * 60
        cy.watch_state()
        self.assertEqual(len(self.web.browses()), 2)
        self.now[0] += 60
        cy.watch_state()
        self.assertEqual(len(self.web.browses()), 4)

    def test_no_cookies_unavailable_and_one_ntfy_until_recovery(self):
        self.write_cookies([])
        cy = self.make()
        self.assertIsNone(cy.watch_state())
        self.assertEqual(cy.status_word, "unavailable")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Open youtube.com once in the browser", self.sent[0])
        self.now[0] += 1800
        cy.watch_state()
        self.assertEqual(len(self.sent), 1)  # not repeated
        self.assertEqual(self.make().notified, "nocookies")  # survives a restart
        self.write_cookies(login_cookies())
        self.now[0] += 1800
        self.assertIsNotNone(cy.watch_state())
        self.assertIsNone(json.loads((self.dir / "yt-watched.json").read_text())["notified"])  # re-armed

    def test_signed_out_and_wrong_channel(self):
        cy = self.make()
        cy.watch_state()
        self.web.signed_out = True
        self.now[0] += 1800
        self.assertIsNone(cy.watch_state())
        self.assertIn("signed out", self.sent[0])
        self.web.signed_out = False
        self.web.datasync = "300000000000000000003||100000000000000000001"
        self.now[0] += 1800
        self.assertIsNone(cy.watch_state())
        self.assertIn("another channel", cy.last_error)
        self.assertEqual(len(self.sent), 1)

    def test_no_such_channel(self):
        store = yt_cookies.CookieStore(self.dir / "yt-cookies.json", lambda: False)
        cy = yt_watched.CookieYouTube(store, self.dir / "w.json", channel_name="Nobody", fetch=self.web,
                                      notify=lambda t, m: self.sent.append(m), clock=lambda: self.now[0])
        self.assertIsNone(cy.watch_state())
        self.assertIn("cannot find the YouTube channel Nobody", self.sent[0])

    def test_transient_failure_keeps_last_state_then_alerts(self):
        cy = self.make()
        first = cy.watch_state()
        self.web.down = True
        self.now[0] += 1800
        self.assertIs(cy.watch_state(), first)  # within the 2 h grace
        self.assertEqual(self.sent, [])
        self.now[0] += 2 * 3600
        self.assertIsNone(cy.watch_state())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("network down", self.sent[0])


class WatchedMergeTest(unittest.TestCase):
    class Watched:
        def __init__(self, progress):
            self.state = None if progress is None else yt_watched.WatchState(progress)

        def watch_state(self):
            return self.state

    def run_source(self, progress):
        with tempfile.TemporaryDirectory() as tmp:
            a = [(vid(i), f"2026-10-{10 + i:02d}T10:00:00+00:00", False) for i in range(1, 6)]
            web = FakeWeb({youtube.FEED_URL.format(CH_A): net.Response(200, {}, feed_xml(CH_A, "A", a)),
                           "https://www.youtube.com/shorts/": shorts_route(set())})
            src = youtube.YouTubeSource(youtube.StaticChannels([CH_A]), Path(tmp), fetch=web, limit=3,
                                        watched=self.Watched(progress))
            return src(), web

    def test_watched_hidden_partial_progress_and_limit_refilled(self):
        out, web = self.run_source({vid(5): 1.0, vid(4): 0.92, vid(3): 0.4})
        self.assertEqual(out["watched"], "ok")
        self.assertEqual([(v["id"], v["progress"]) for v in out["videos"]],
                         [(vid(3), 0.4), (vid(2), None), (vid(1), None)])
        heads = [c["url"] for c in web.calls if c["method"] == "HEAD"]
        self.assertNotIn(youtube.SHORTS_URL.format(vid(5)), heads)  # hidden before any Shorts check

    def test_unavailable_falls_back_to_unfiltered(self):
        out, _ = self.run_source(None)
        self.assertEqual(out["watched"], "unavailable")
        self.assertEqual([(v["id"], v["progress"]) for v in out["videos"]],
                         [(vid(5), None), (vid(4), None), (vid(3), None)])


class SubscriptionsCookieRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sent = []
        self.oauth_calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def subs(self, cookie_answer, oauth_answer):
        def cookie_channels():
            if isinstance(cookie_answer, Exception):
                raise cookie_answer
            return cookie_answer

        def fetch_subs(auth):
            self.oauth_calls += 1
            if isinstance(oauth_answer, Exception):
                raise oauth_answer
            return oauth_answer
        return youtube.Subscriptions(FakeAuth(self.dir / "token.json"), self.dir / "subs.json",
                                     fetch_subs=fetch_subs, notify=lambda t, m: self.sent.append(t),
                                     cookie_channels=cookie_channels)

    def test_cookie_route_makes_oauth_optional(self):
        chans = [{"id": CH_A, "title": "A"}]
        s = self.subs(chans, google_oauth.AuthExpired("invalid_grant"))
        self.assertEqual(s.get(), (chans, "ok"))
        self.assertEqual((self.oauth_calls, self.sent), (0, []))
        self.assertEqual(s.state["source"], "cookies")

    def test_cookie_failure_falls_back_to_oauth_without_ntfy(self):
        chans = [{"id": CH_B, "title": "B"}]
        s = self.subs(innertube.SessionError("signed out"), chans)
        self.assertEqual(s.get(), (chans, "ok"))
        self.assertEqual(s.state["source"], "oauth")
        self.assertIn("signed out", s.cookie_error)
        s2 = self.subs(innertube.SessionError("signed out"), google_oauth.AuthExpired("invalid_grant"))
        self.assertEqual(s2.get(), (chans, "expired"))  # the last list from disk
        self.assertEqual(self.sent, [])  # the cookie route sends its own alert


if __name__ == "__main__":
    unittest.main()


class FakeJellyfinRemote:
    """Sessions appear after `appear_after` polls; PlayNow takes after `takes_after` sends."""

    def __init__(self, item_type="Episode", ticks=0, appear_after=2, takes_after=1, device="LG Smart TV"):
        self.user_id = "u1"
        self.item = {"Type": item_type, "Name": "The Truth", "UserData": {"PlaybackPositionTicks": ticks}}
        self.appear_after, self.takes_after, self.device = appear_after, takes_after, device
        self.polls, self.posts, self.playing = 0, [], None

    def get_json(self, path, **params):
        if path.startswith("/Users/"):
            return self.item
        self.polls += 1
        if self.polls <= self.appear_after:
            return [{"Id": "other", "Client": "Jellyfin Android", "DeviceName": "Pixel",
                     "SupportsRemoteControl": True}]
        return [{"Id": "tv", "Client": "Jellyfin Web", "DeviceName": self.device, "SupportsRemoteControl": True,
                 "LastActivityDate": "2026-10-20T19:00:00Z",
                 "NowPlayingItem": {"Id": self.playing} if self.playing else None}]

    def post(self, path, body=None, **params):
        self.posts.append((path, body, params))
        if path.endswith("/Playing") and len(self.posts) >= self.takes_after:
            self.playing = params["itemIds"]


def run_open(fake, item_id="ep1", **kw):
    op = jf_remote.Opener(lambda: fake, sleep=lambda s: None, **kw)
    op._generation = 1
    op.last = {"itemId": item_id, "state": "waiting"}
    op._job(item_id, 1)
    return op


class JellyfinRemoteTest(unittest.TestCase):
    def test_pick_session_wants_the_tvs_web_client(self):
        self.assertIsNone(jf_remote.pick_session([{"Client": "Jellyfin Web", "DeviceName": "Firefox",
                                                     "SupportsRemoteControl": True}]))
        tv = {"Id": "tv", "Client": "Jellyfin Web", "DeviceName": "LG Smart TV", "SupportsRemoteControl": True}
        self.assertEqual(jf_remote.pick_session([tv])["Id"], "tv")

    def test_waits_for_the_session_then_plays_from_the_saved_position(self):
        fake = FakeJellyfinRemote(ticks=12_000_000_000)
        op = run_open(fake, timeout=1e9)
        self.assertEqual(op.last["state"], "playing")
        path, body, params = fake.posts[0]
        self.assertEqual(path, "/Sessions/tv/Playing")
        self.assertEqual(params, {"playCommand": "PlayNow", "itemIds": "ep1", "startPositionTicks": 12_000_000_000})

    def test_unstarted_item_sends_no_start_position(self):
        fake = FakeJellyfinRemote(ticks=0, appear_after=0)
        run_open(fake, timeout=1e9)
        self.assertIsNone(fake.posts[0][2]["startPositionTicks"])

    def test_play_is_resent_until_the_app_takes_it(self):
        fake = FakeJellyfinRemote(appear_after=0, takes_after=3)
        op = run_open(fake, timeout=1e9)
        self.assertEqual(op.last["state"], "playing")
        self.assertEqual(len(fake.posts), 3)

    def test_series_shows_its_page_twice_and_never_plays(self):
        fake = FakeJellyfinRemote(item_type="Series", appear_after=0)
        op = run_open(fake, timeout=1e9)
        self.assertEqual(op.last["state"], "shown")
        self.assertEqual([p[0] for p in fake.posts], ["/Sessions/tv/Command"] * 2)
        self.assertEqual(fake.posts[0][1]["Name"], "DisplayContent")
        self.assertEqual(fake.posts[0][1]["Arguments"]["ItemId"], "ep1")

    def test_no_tv_session_fails_cleanly(self):
        clock = iter(range(0, 1000, 5))
        fake = FakeJellyfinRemote(appear_after=10**6)
        op = run_open(fake, timeout=20, clock=lambda: next(clock))
        self.assertEqual(op.last["state"], "failed")
        self.assertEqual(fake.posts, [])

    def test_a_newer_request_cancels_the_older_job(self):
        fake = FakeJellyfinRemote(appear_after=10**6)
        op = jf_remote.Opener(lambda: fake, sleep=lambda s: setattr(op, "_generation", 2))
        op._generation, op.last = 1, {"itemId": "old", "state": "waiting"}
        op._job("old", 1)
        self.assertEqual(fake.posts, [])


class ContinueWatchingTest(unittest.TestCase):
    def test_lockup_byline_gives_the_channel(self):
        lv = {"contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": "abcdefghijk", "metadata": {
            "lockupMetadataViewModel": {"title": {"content": "T"}, "metadata": {"contentMetadataViewModel": {
                "metadataRows": [{"metadataParts": [{"text": {"content": "Lars"}}, {"text": {"content": "6.6K views"}}]}]}}}}}
        self.assertEqual(innertube._lockup_video(lv)["channel"], "Lars")

    def test_unfinished_keeps_history_order_and_skips_glances_and_finished(self):
        hist = [{"id": i, "title": i, "channel": "c", "progress": None, "recent": True} for i in ("a", "b", "c", "d", "a")]
        hist.insert(1, {"id": "m", "title": "Song", "channel": "Artist - Topic", "progress": None, "recent": True})
        hist.append({"id": "old", "title": "Old", "channel": "c", "progress": None, "recent": False})
        state = yt_watched.WatchState({"a": 0.5, "b": 0.95, "c": 0.1, "d": 0.3, "m": 0.4, "old": 0.5}, 0.0, hist)
        self.assertEqual([v["id"] for v in state.unfinished()], ["a", "d"])
        self.assertEqual(state.unfinished()[0]["progress"], 0.5)

    def test_continue_videos_prefer_rss_details_and_fall_back_to_history(self):
        rss = [{"id": "a", "title": "A", "channel": "Chan", "channelId": "UC1", "published": "2026-10-20T10:00:00+00:00",
                "views": 5}]
        got = youtube.continue_videos([{"id": "a", "title": "x", "channel": "y", "progress": 0.4},
                                       {"id": "b", "title": "B", "channel": "Other", "progress": 0.2}], rss)
        self.assertEqual((got[0]["channel"], got[0]["published"], got[0]["progress"]), ("Chan", "2026-10-20T10:00:00+00:00", 0.4))
        self.assertEqual((got[1]["title"], got[1]["channel"], got[1]["published"], got[1]["thumb"]),
                         ("B", "Other", None, "/api/yt/b/thumb"))

    def test_history_sections_mark_the_last_week(self):
        def sec(day, vid):
            return {"itemSectionRenderer": {"header": {"itemSectionHeaderRenderer": {"title": {"simpleText": day}}},
                    "contents": [{"lockupViewModel": {"contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": vid}}]}}
        data = {"contents": {"sectionListRenderer": {"contents": [sec("Today", "aaaaaaaaaaa"), sec("Sunday", "bbbbbbbbbbb"),
                                                                    sec("Oct 13", "ccccccccccc")]}}}
        self.assertEqual([(v["id"][0], v["recent"]) for v in innertube.parse_history(data)],
                         [("a", True), ("b", True), ("c", False)])

    def test_music_is_dropped_and_the_row_capped_at_five(self):
        calls = []

        def fetch(url, **kw):
            vid = url.rsplit("=", 1)[-1]
            calls.append(vid)
            cat = b"Music" if vid.startswith("m") else b"Gaming"
            return net.Response(200, {}, b'..."category":"' + cat + b'"...')
        src = youtube.YouTubeSource(youtube.StaticChannels([]), Path(tempfile.mkdtemp()), fetch=fetch)
        vids = [{"id": i} for i in ("m1", "g1", "m2", "g2", "g3", "g4", "g5", "g6")]
        self.assertEqual([v["id"] for v in src.drop_music(vids)], ["g1", "g2", "g3", "g4", "g5"])
        calls.clear()
        src.drop_music(vids)
        self.assertEqual(calls, [])  # cached

