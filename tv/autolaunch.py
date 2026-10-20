"""Launch the TV Home app when the LG TV turns on (SSAP watcher on the TV Home server).

Run: uv run -q --no-project --with aiowebostv python -I autolaunch.py [--takeover]
The TV address and app id come from config.toml ([tv] ip, app_id, takeover).

Power-on is read from the TV's power state, not reachability: with Quick
Start+ the TV keeps its network up in standby. The watcher never steals focus
from an app the user picked after power-on (an app other than the one webOS
resumed, e.g. a Netflix-button or cast power-on, or an HDMI device via CEC).

--takeover (also `[tv] takeover = true`): when LG's own launcher
(com.webos.app.home) comes to the front, relaunch Home. Leaving Home for LG's
launcher (tvHome.lgHome() or the remote's Home key while in Home) opens a
bypass window instead, so other apps stay reachable.

Events go to stdout as JSON lines (journald keeps them for the unit).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tvconfig  # noqa: E402

HOME_ID = "com.example.tvhome"  # replaced from config.toml in main()
LG_HOME_ID = "com.webos.app.home"
KEY_FILE = tvconfig.CONFIG_DIR / "ssap-client-key"

OFF_STATES = {"Power Off", "Suspend", "Active Standby"}
# Screen-only states: the TV is on, the panel is not. Coming back from these
# is not a power-on.
SCREEN_STATES = {"Screen Off", "Screen Saver"}
# What webOS may put in front by itself while booting or resuming.
PASSIVE_APPS = {
    None, "", LG_HOME_ID, "com.webos.app.livetv", "com.webos.app.bootlogo",
    "com.webos.app.masterbrand", "com.webos.app.screensaver", "com.webos.app.cpcover",
}


def classify(power: dict | None) -> str:
    """Map an SSAP power payload to on / off / screen / unknown."""
    if not power:
        return "unknown"
    state = power.get("state")
    processing = power.get("processing") or ""
    if state in OFF_STATES or "Power Off" in processing or "Suspend" in processing \
            or "Active Standby" in processing:
        return "off"
    if state in SCREEN_STATES:
        return "screen"
    if state == "Active":
        return "on"
    return "unknown"


@dataclass
class Policy:
    """Decides when to launch Home. Pure: feed events, get actions back."""

    takeover: bool = False
    bypass_s: float = 120.0
    settle_s: float = 4.0
    power: str = "startup"            # startup | on | off | screen | unknown
    app: str | None = None
    app_before_off: str | None = None
    pending_since: float | None = None   # power-on seen, decision pending
    apps_since_on: list = field(default_factory=list)
    bypass_until: float = 0.0
    lg_home_since: float | None = None

    # --- events ---
    def on_power(self, payload: dict | None, now: float) -> list[dict]:
        new = classify(payload)
        old = self.power
        if new == old:
            return []
        self.power = new
        out = [{"event": "power", "from": old, "to": new, "raw": payload}]
        if new == "off":
            self.app_before_off = self.app
            self.pending_since = None
        if new == "on" and old == "off":
            self.pending_since = now
            self.apps_since_on = []
            out.append({"event": "power-on", "app": self.app})
        return out

    def on_disconnect(self, now: float) -> list[dict]:
        # A dropped socket alone is not "off": a Wi-Fi blip must not look like
        # a power-on afterwards. on_reconnect decides from the front app.
        if self.power != "off":
            self.power = "unknown"
        return [{"event": "disconnected"}]

    def on_reconnect(self, payload: dict | None, app: str | None, now: float) -> list[dict]:
        was = self.power
        self.app = app
        new = classify(payload)
        self.power = new
        out = [{"event": "connected", "power": new, "app": app, "was": was}]
        if new != "on":
            return out
        if was == "off" or (was == "unknown" and app in PASSIVE_APPS):
            self.pending_since = now
            self.apps_since_on = []
            out.append({"event": "power-on", "app": app, "via": "reconnect"})
        return out

    def on_app(self, app: str | None, now: float) -> list[dict]:
        if app == self.app:
            return []
        prev, self.app = self.app, app
        out = [{"event": "app", "from": prev, "to": app}]
        if self.pending_since is not None:
            self.apps_since_on.append(app)
            return out
        if app == HOME_ID and self.lg_home_since is not None:
            out.append({"event": "takeover-done", "lg_home_visible_s": round(now - self.lg_home_since, 3)})
            self.lg_home_since = None
        if app != LG_HOME_ID or not self.takeover or self.power != "on":
            return out
        if prev == HOME_ID:
            self.bypass_until = now + self.bypass_s
            out.append({"event": "bypass", "until_s": self.bypass_s})
            return out
        if now < self.bypass_until:
            out.append({"event": "bypass-active", "left_s": round(self.bypass_until - now, 1)})
            return out
        self.lg_home_since = now
        out.append({"event": "takeover", "action": "launch", "from": prev})
        return out

    def tick(self, now: float) -> list[dict]:
        """Resolve a pending power-on once the front app has settled."""
        if self.pending_since is None or now - self.pending_since < self.settle_s:
            return []
        self.pending_since = None
        app = self.app
        resumed = {self.app_before_off} | PASSIVE_APPS
        chosen = [a for a in self.apps_since_on if a not in resumed]
        if app == HOME_ID:
            return [{"event": "power-on-skip", "why": "home already in front"}]
        if chosen or app not in resumed:
            return [{"event": "power-on-skip", "why": "user picked an app", "app": app}]
        return [{"event": "power-on-launch", "action": "launch", "app": app}]


def emit(rec: dict) -> None:
    rec = {"t": round(time.time(), 3), **rec}
    print(json.dumps(rec, default=str), flush=True)


async def launch_home(client, reason: str) -> None:
    for attempt in range(3):
        try:
            await client.request("system.launcher/launch", {"id": HOME_ID, "params": {"reason": reason}})
            emit({"event": "launched", "reason": reason, "attempt": attempt + 1})
            return
        except Exception as exc:  # TV still booting
            emit({"event": "launch-failed", "reason": reason, "error": str(exc)[:200]})
            await asyncio.sleep(2)


async def run(args) -> None:
    from aiowebostv import WebOsClient

    key = KEY_FILE.read_text().strip()
    policy = Policy(takeover=args.takeover, bypass_s=args.bypass_seconds, settle_s=args.settle_seconds)
    emit({"event": "start", "takeover": args.takeover, "bypass_s": args.bypass_seconds})

    async def act(recs, client):
        for rec in recs:
            emit(rec)
            if rec.get("action") == "launch" and not args.dry_run:
                await launch_home(client, rec["event"])

    while True:
        client = WebOsClient(args.host, client_key=key)
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(args.retry_seconds)
            continue

        async def on_state(state, client=client):
            now = time.monotonic()
            await act(policy.on_power(state.power_state, now), client)
            await act(policy.on_app(state.current_app_id, now), client)

        await act(policy.on_reconnect(client.tv_state.power_state, client.tv_state.current_app_id,
                                      time.monotonic()), client)
        await client.register_state_update_callback(on_state)
        try:
            while client.is_connected():
                await act(policy.tick(time.monotonic()), client)
                await asyncio.sleep(0.5)
        finally:
            await act(policy.on_disconnect(time.monotonic()), client)
            try:
                await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(args.retry_seconds)


def main(argv=None) -> int:
    global HOME_ID
    cfg = tvconfig.load()
    HOME_ID = cfg["tv"]["app_id"]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=cfg["tv"]["ip"])
    p.add_argument("--takeover", action="store_true", help="relaunch Home when LG's launcher comes up")
    p.add_argument("--bypass-seconds", type=float, default=120.0)
    p.add_argument("--settle-seconds", type=float, default=4.0)
    p.add_argument("--retry-seconds", type=float, default=5.0)
    p.add_argument("--dry-run", action="store_true", help="log decisions, launch nothing")
    args = p.parse_args(argv)
    args.takeover = args.takeover or bool(cfg["tv"]["takeover"])
    if not args.host:
        print("autolaunch: set [tv] ip in config.toml", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
