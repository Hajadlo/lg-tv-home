"""Argument handling and step dispatch of tools/tv-home/tv/tvctl, with a fake SSAP client."""
import asyncio
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_loader = SourceFileLoader("tvctl", str(Path(__file__).parent.parent / "tv/tvctl"))
tvctl = importlib.util.module_from_spec(importlib.util.spec_from_loader("tvctl", _loader))
_loader.exec_module(tvctl)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def button(self, name):
        self.calls.append(("button", name))

    async def request(self, uri, payload=None):
        self.calls.append(("request", uri, payload))
        if uri == "tv/executeOneShot":
            return {"imageUri": "https://tv/shot.jpg"}
        if uri == "com.webos.applicationManager/getForegroundAppInfo":
            return {"appId": "youtube.leanback.v4", "returnValue": True}
        return {"returnValue": True}


def run(steps, **kw):
    client, out, slept = FakeClient(), [], []

    async def sleep(s):
        slept.append(s)

    asyncio.run(tvctl.run_steps(client, steps, sleep=sleep, out=out.append, **kw))
    return client.calls, [json.loads(line) for line in out], slept


def test_parse_every_verb_in_order():
    steps = tvctl.parse_steps(["key", "green", "wait", "500", "shot", "/x.jpg",
                               "launch", "youtube.leanback.v4", '{"target": "v=abc"}',
                               "ssap", "audio/getVolume", "launch", "com.example.tvhome"])
    assert steps == [("key", "GREEN"), ("wait", 500), ("shot", "/x.jpg"),
                     ("launch", "youtube.leanback.v4", {"target": "v=abc"}),
                     ("ssap", "audio/getVolume", None), ("launch", "com.example.tvhome", None)]


def test_ssap_prefix_dropped_so_the_uri_is_not_doubled():
    assert tvctl.parse_steps(["ssap", "ssap://tv/executeOneShot", "{}"]) == [("ssap", "tv/executeOneShot", {})]


@pytest.mark.parametrize("tokens, message", [
    ([], "no steps"),
    (["eval", "com.example.tvhome"], "an app id and a JS expression"),
    (["press", "HOME"], "unknown step"),
    (["key"], "needs an argument"),
    (["wait", "1.5"], "milliseconds"),
    (["wait", "-3"], "milliseconds"),
    (["launch", "app", "{bad"], "bad JSON"),
    (["ssap", "x/y", "{"], "bad JSON"),
])
def test_bad_arguments(tokens, message):
    with pytest.raises(tvctl.UsageError, match=message):
        tvctl.parse_steps(tokens)


def test_main_exits_2_on_bad_arguments(capsys):
    assert tvctl.main(["wait", "soon"]) == 2
    assert "milliseconds" in capsys.readouterr().err


def test_main_without_pairing_key_exits_1(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(tvctl, "KEY_FILE", tmp_path / "missing")
    assert tvctl.main(["--host", "192.0.2.1", "key", "HOME"]) == 1
    assert "ssap_pair.py" in capsys.readouterr().err


def test_run_steps_dispatch(tmp_path):
    shot = tmp_path / "s.jpg"
    steps = tvctl.parse_steps(["key", "HOME", "wait", "250", "shot", str(shot),
                               "launch", "app.id", '{"a": 1}', "ssap", "audio/getVolume"])
    calls, out, slept = run(steps, fetch=lambda url: b"JPEG:" + url.encode())
    assert calls == [("button", "HOME"),
                     ("request", "tv/executeOneShot", {}),
                     ("request", "system.launcher/launch", {"id": "app.id", "params": {"a": 1}}),
                     ("request", "audio/getVolume", None)]
    assert slept == [0.25]
    assert shot.read_bytes() == b"JPEG:https://tv/shot.jpg"
    assert out[2] == {"shot": str(shot), "bytes": len(b"JPEG:https://tv/shot.jpg")}
    assert out[3]["launch"] == "app.id" and out[4]["reply"] == {"returnValue": True}


def test_launch_without_json_sends_no_params():
    calls, _, _ = run([("launch", "com.example.tvhome", None)])
    assert calls == [("request", "system.launcher/launch", {"id": "com.example.tvhome"})]


def test_fg_and_eval_steps():
    assert tvctl.parse_steps(["fg", "eval", "youtube.leanback.v4", "location.href", "fg"]) == [
        ("fg",), ("eval", "youtube.leanback.v4", "location.href"), ("fg",)]
    evaluated = []

    async def inspect(app, js):
        evaluated.append((app, js))
        return "https://www.youtube.com/tv#/"

    calls, out, _ = run([("fg",), ("eval", "youtube.leanback.v4", "location.href")], inspect=inspect)
    assert calls == [("request", "com.webos.applicationManager/getForegroundAppInfo", None)]
    assert out == [{"fg": "youtube.leanback.v4"},
                   {"eval": "youtube.leanback.v4", "value": "https://www.youtube.com/tv#/"}]
    assert evaluated == [("youtube.leanback.v4", "location.href")]


def test_the_inspector_port_is_read_from_ares_inspect_output():
    assert tvctl.inspector_port("Application Debugging - http://localhost:34003\n") == 34003
    assert tvctl.inspector_port("Application Debugging - http://127.0.0.1:9998/devtools") == 9998
    assert tvctl.inspector_port("Connecting to lgtv ...") is None


def test_eval_stops_the_inspector_and_reports_its_exit(tmp_path, monkeypatch):
    fake = tmp_path / "ares-inspect"
    fake.write_text("#!/bin/sh\necho 'ares-inspect ERR! [Tips]: The app is not running'\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(RuntimeError, match="ares-inspect ended: .*not running"):
        asyncio.run(tvctl.inspect_eval("com.example.tvhome", "1"))
