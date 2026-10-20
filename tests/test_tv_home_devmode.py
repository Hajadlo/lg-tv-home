"""Parsing tests for tv/devmode-renew (no TV, no network)."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_loader = SourceFileLoader("devmode_renew", str(Path(__file__).resolve().parent.parent / "tv/devmode-renew"))
renew = importlib.util.module_from_spec(importlib.util.spec_from_loader("devmode_renew", _loader))
_loader.exec_module(renew)

TOKEN = "a" * 40 + "B7" * 12


def test_token_is_picked_out_of_ares_output():
    out = "[Info] Set target device : tv\n" + TOKEN + "\n"
    assert renew.parse_token(out) == TOKEN


def test_no_token_in_error_output():
    assert renew.parse_token("ares-novacom ERR! [ssh exec failure]: timeout\n") is None
    assert renew.parse_token("") is None


def test_success_reports_remaining_hours():
    s = renew.parse_session('{"result":"success","errorCode":"200","errorMsg":"999:30:00"}')
    assert s["ok"] and s["hours"] == 999.5


def test_failure_keeps_the_raw_answer():
    s = renew.parse_session('{"result":"fail","errorCode":"ERR_005","errorMsg":"Check user session"}')
    assert not s["ok"] and s["hours"] is None and s["raw"]["errorCode"] == "ERR_005"


def test_non_json_is_a_failure():
    s = renew.parse_session("<html>maintenance</html>")
    assert not s["ok"]
