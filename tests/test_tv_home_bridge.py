"""Render tv/app/bridge.js from a test config and run it under node with a fake PalmServiceBridge."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tv"))
sys.path.insert(0, str(ROOT / "server"))

import render  # noqa: E402
import tvconfig  # noqa: E402

BRIDGE = ROOT / "tv/app/bridge.js"
HOST_UUID = "00000000-1111-2222-3333-444444444444"
CFG = tvconfig.from_dict({
    "server": {"url": "http://192.0.2.10:8797"},
    "jellyfin": {"url": "http://127.0.0.1:8096", "user": "viewer"},
    "youtube": {"subscriptions_csv": "subs.csv"},
    "steam": {"pc_ip": "192.0.2.5", "pc_mac": "00:11:22:33:44:55", "moonlight_host_uuid": HOST_UUID,
              "moonlight_app_id": 1234567},
})

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const calls = [];
function makeWindow(withBridge, failFirstUri) {
  const listeners = {};
  const doc = { readyState: 'complete',
    addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
    createEvent: () => ({ initEvent() {} }) };
  const win = { document: doc, setTimeout, clearTimeout, Promise, JSON, Object, Array, Error,
    CustomEvent: function (n) { this.type = n; }, dispatchEvent: () => {}, name: '',
    addEventListener: () => {} };
  win.window = win; win.top = win;
  if (withBridge) {
    win.PalmServiceBridge = function () {
      const self = this;
      this.call = (uri, body) => {
        calls.push({ uri, body: JSON.parse(body) });
        const fail = failFirstUri && uri === failFirstUri;
        setTimeout(() => self.onservicecallback(JSON.stringify(
          fail ? { returnValue: false, errorCode: -1, errorText: 'Denied' } : { returnValue: true })), 0);
      };
      this.cancel = () => {};
    };
    win.PalmSystem = { launchParams: '{"a":1}', activate: () => {} };
  }
  vm.createContext(win);
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), win);
  return win;
}
(async () => {
  const out = {};
  out.browser = typeof makeWindow(false).tvHome;
  const w = makeWindow(true);
  const t = w.tvHome;
  out.isTV = t.isTV;
  out.moon = await t.launch('moonlight');
  out.moonHostOnly = await t.launch('moonlight', { appId: 0 });
  out.jf = await t.launch('jellyfin', { jellyfinItemId: 'x' });
  out.yt = await t.launch('youtube', { videoId: 'dQw4w9WgXcQ' });
  out.ytBad = await t.launch('youtube', { videoId: 'x"&list=1' });
  out.ytHome = await t.launch('youtube');
  for (const s of ['all', 'picture', 'sound', 'network', 'input', 'bogus']) await t.openSettings(s);
  await t.lgHome();
  try { await t.launch('netflix'); out.unknown = 'resolved'; } catch (e) { out.unknown = e.code; }
  out.params = t.launchParams();
  out.calls = calls.splice(0);
  const w2 = makeWindow(true, 'luna://com.webos.applicationManager/launch');
  out.fallback = await w2.tvHome.launch('youtube');
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("bridge")
    h = d / "harness.js"
    h.write_text(HARNESS)
    bridge = d / "bridge.js"
    bridge.write_text(render.render_bridge(BRIDGE.read_text(), render.settings(CFG)))
    r = subprocess.run([node, str(h), str(bridge)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_plain_browser_gets_no_bridge_so_the_mock_loads(result):
    assert result["browser"] == "undefined"
    assert result["isTV"] is True


def test_moonlight_streams_steam_or_only_selects_host(result):
    assert result["moon"]["params"] == {"host_uuid": HOST_UUID, "host_app_id": 1234567}
    assert result["moon"]["deepLinked"] is True
    assert result["moonHostOnly"]["params"] == {"host_uuid": HOST_UUID}


def test_jellyfin_item_falls_back_to_plain_launch(result):
    assert result["jf"]["params"] is None and result["jf"]["deepLinked"] is False


def test_youtube_video_deep_link(result):
    assert result["yt"]["id"] == "youtube.leanback.v4"
    assert result["yt"]["params"] == {"target": "v=dQw4w9WgXcQ"}
    assert result["yt"]["deepLinked"] is True


def test_youtube_bad_id_or_none_opens_home(result):
    assert result["ytBad"]["params"] is None and result["ytBad"]["deepLinked"] is False
    assert "note" in result["ytBad"]
    assert result["ytHome"]["params"] is None and result["ytHome"]["deepLinked"] is False


def test_launch_bodies(result):
    bodies = [c["body"] for c in result["calls"]]
    assert {"id": "com.palm.app.settings"} in bodies
    assert {"id": "com.palm.app.settings", "params": {"target": "picture"}} in bodies
    assert {"id": "com.palm.app.settings", "params": {"target": "sound"}} in bodies
    assert {"id": "com.palm.app.settings", "params": {"target": "system"}} in bodies
    assert {"id": "com.webos.app.quickinputpicker"} in bodies
    assert {"id": "com.webos.app.home", "params": {"launchType": "homeKey"}} in bodies
    assert all(c["uri"] == "luna://com.webos.applicationManager/launch" for c in result["calls"])


def test_unknown_app_rejects(result):
    assert result["unknown"] == "unknown-app"


def test_second_launch_uri_is_tried_when_first_is_denied(result):
    assert result["fallback"]["via"] == "luna://com.webos.service.applicationmanager/launch"


def test_launch_params_are_parsed(result):
    assert result["params"] == {"a": 1}


def test_settings_list_only_configured_apps():
    cfg = tvconfig.from_dict({"server": {"url": "http://192.0.2.10:8797"},
                              "jellyfin": {"url": "http://127.0.0.1:8096", "user": "viewer"}})
    values = render.settings(cfg)
    assert values["uiUrl"] == "http://192.0.2.10:8797/"
    assert values["apps"] == {"jellyfin": "org.jellyfin.webos"}
    rendered = render.render_bridge(BRIDGE.read_text(), values)
    assert rendered.count("var SETTINGS = ") == 1 and "192.0.2.10" in rendered


def test_appinfo_takes_id_and_title(tmp_path):
    cfg = tvconfig.from_dict({"server": {"url": "http://192.0.2.10:8797/"},
                              "tv": {"app_id": "org.example.couch", "title": "Couch"}})
    render.render(tmp_path, cfg)
    info = json.loads((tmp_path / "appinfo.json").read_text())
    assert (info["id"], info["title"]) == ("org.example.couch", "Couch")
    assert (tmp_path / "webOSUserScripts/userScript.js").read_text() == (tmp_path / "bridge.js").read_text()
