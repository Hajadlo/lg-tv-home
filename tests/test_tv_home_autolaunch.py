"""Policy tests for tv/autolaunch.py (no TV, no aiowebostv)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tv"))

from autolaunch import HOME_ID, LG_HOME_ID, Policy, classify  # noqa: E402

ON = {"state": "Active"}
STANDBY = {"state": "Active Standby"}
SCREEN_OFF = {"state": "Screen Off"}
YT = "youtube.leanback.v4"


def launches(recs):
    return [r for r in recs if r.get("action") == "launch"]


def booted(app=YT, takeover=False):
    """A policy that has seen the TV on with `app` in front."""
    p = Policy(takeover=takeover)
    p.on_reconnect(ON, app, 0)
    return p


def test_classify_states():
    assert classify(ON) == "on"
    assert classify(STANDBY) == "off"
    assert classify({"state": "Suspend"}) == "off"
    assert classify({"state": "Active", "processing": "Request Power Off"}) == "off"
    assert classify(SCREEN_OFF) == "screen"
    assert classify(None) == "unknown"


def test_watcher_start_with_tv_on_does_not_launch():
    p = Policy()
    recs = p.on_reconnect(ON, YT, 0)
    assert not [r for r in recs if r["event"] == "power-on"]
    assert launches(p.tick(10)) == []


def test_power_on_resuming_last_app_launches_home():
    p = booted()
    p.on_power(STANDBY, 1)
    p.on_power(ON, 100)
    assert launches(p.tick(101)) == []          # still settling
    assert len(launches(p.tick(105))) == 1


def test_power_on_to_live_tv_launches_home():
    p = booted()
    p.on_power(STANDBY, 1)
    p.on_power(ON, 100)
    p.on_app("com.webos.app.livetv", 100.5)
    assert len(launches(p.tick(105))) == 1


def test_power_on_into_a_different_app_is_left_alone():
    # Netflix button, a cast, or an HDMI device waking the TV over CEC.
    p = booted()
    p.on_power(STANDBY, 1)
    p.on_power(ON, 100)
    p.on_app("netflix", 101)
    assert launches(p.tick(105)) == []


def test_user_switch_during_settle_is_respected_even_if_they_come_back():
    p = booted()
    p.on_power(STANDBY, 1)
    p.on_power(ON, 100)
    p.on_app("com.webos.app.hdmi1", 101)
    p.on_app(YT, 102)
    assert launches(p.tick(105)) == []


def test_power_on_with_home_in_front_does_nothing():
    p = booted(app=HOME_ID)
    p.on_power(STANDBY, 1)
    p.on_power(ON, 100)
    assert launches(p.tick(105)) == []


def test_screen_off_and_back_is_not_a_power_on():
    p = booted()
    p.on_power(SCREEN_OFF, 1)
    p.on_power(ON, 50)
    assert launches(p.tick(60)) == []


def test_network_blip_while_watching_is_not_a_power_on():
    p = booted()
    p.on_disconnect(10)
    recs = p.on_reconnect(ON, YT, 20)
    assert not [r for r in recs if r["event"] == "power-on"]
    assert launches(p.tick(30)) == []


def test_full_power_cycle_without_quick_start_launches_home():
    p = booted()
    p.on_power({"state": "Active", "processing": "Request Power Off"}, 5)
    p.on_disconnect(6)
    p.on_reconnect(ON, "com.webos.app.livetv", 60)
    assert len(launches(p.tick(65))) == 1


def test_takeover_off_ignores_lg_home():
    p = booted()
    assert launches(p.on_app(LG_HOME_ID, 10)) == []


def test_takeover_relaunches_home_and_measures():
    p = booted(takeover=True)
    assert len(launches(p.on_app(LG_HOME_ID, 10))) == 1
    done = [r for r in p.on_app(HOME_ID, 10.4) if r["event"] == "takeover-done"]
    assert done and abs(done[0]["lg_home_visible_s"] - 0.4) < 1e-6


def test_leaving_home_for_lg_home_opens_bypass_window():
    p = booted(app=HOME_ID, takeover=True)
    assert launches(p.on_app(LG_HOME_ID, 10)) == []           # lgHome() from Home
    p.on_app("netflix", 20)
    assert launches(p.on_app(LG_HOME_ID, 60)) == []           # within 120 s
    assert len(launches(p.on_app(YT, 70) + p.on_app(LG_HOME_ID, 200))) == 1  # after it
