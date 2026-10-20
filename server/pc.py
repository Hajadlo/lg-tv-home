"""Gaming PC state and the Play-on-PC job (wake, wait, make Sunshine listen)."""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("tvhome.pc")

SSH_PORT = 22
SUNSHINE_PORT = 47989
WOL_PORTS = (9, 7)
MOONLIGHT_APP = "com.limelight.webos"
CLOSE_APP = Path(__file__).resolve().parent.parent / "tv" / "close-app"


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"bad MAC {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, broadcasts: list[str] | tuple[str, ...] = ()) -> None:
    """Wake-on-LAN straight onto the LAN (the PC shares this server's LAN)."""
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in [*broadcasts, "255.255.255.255"]:
            for port in WOL_PORTS:
                try:
                    sock.sendto(packet, (addr, port))
                except OSError as exc:
                    log.warning("wake packet to %s:%d failed: %s", addr, port, exc)
    log.info("sent Wake-on-LAN for %s", mac)


def restart_sunshine(ssh_host: str, unit: str) -> bool:
    """`ssh <host> systemctl --user restart <unit>`; False when not configured or it failed."""
    if not (ssh_host and unit):
        log.info("Sunshine is closed; no ssh_host/sunshine_unit configured to restart it")
        return False
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", ssh_host,
           "systemctl", "--user", "restart", unit]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Sunshine restart failed: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("Sunshine restart exited %d: %s", result.returncode, result.stderr.strip()[:200])
    else:
        log.info("restarted %s on the PC", unit)
    return result.returncode == 0


def close_moonlight(app_id: str = MOONLIGHT_APP) -> None:
    """Close a suspended Moonlight on the TV so the next launch is a cold start
    (only then does it stream the PC straight away). Best effort."""
    try:
        result = subprocess.run([str(CLOSE_APP), app_id], capture_output=True, text=True,
                                timeout=25, check=False)
        log.info("close Moonlight: %s", (result.stdout or result.stderr).strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("close Moonlight failed: %s", exc)


@dataclass
class Timing:
    boot_timeout: float = 180.0       # wait this long for a woken PC to answer
    rewake_every: float = 30.0        # resend the magic packet while waiting
    sunshine_grace: float = 20.0      # PC was already on: restart after this long closed
    sunshine_grace_boot: float = 45.0 # PC just booted: give the session time to start it
    sunshine_after_restart: float = 45.0
    poll: float = 2.0
    idle_poll: float = 15.0


@dataclass
class Probe:
    on: bool
    sunshine: bool


@dataclass
class JobState:
    phase: str = "idle"  # idle | waking | starting-sunshine | restarting-sunshine | ready | failed
    error: str | None = None
    started: float | None = None
    finished: float | None = None
    restarted: bool = False
    events: list[str] = field(default_factory=list)


class PC:
    """Cached reachability plus at most one Play job at a time.

    Requests never probe the network themselves: a monitor thread keeps
    `last` fresh (every `idle_poll`, every `poll` while a job runs).
    """

    def __init__(self, probe: Callable[[int], bool] | None = None, wake: Callable[[], None] = lambda: None,
                 restart: Callable[[], bool] = lambda: False,
                 prepare_tv: Callable[[], None] = close_moonlight, timing: Timing | None = None,
                 host: str = "", sunshine_port: int = SUNSHINE_PORT,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.host = host
        self.sunshine_port = sunshine_port
        self._probe_port = probe or (lambda port: tcp_open(host, port))
        self._wake = wake
        self._restart = restart
        self._prepare_tv = prepare_tv
        self.t = timing or Timing()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._job_thread: threading.Thread | None = None
        self.job = JobState()
        self.last: Probe | None = None
        self.last_probe_at: float | None = None
        self._kick = threading.Event()

    # -- probing ---------------------------------------------------------
    def probe(self) -> Probe:
        sunshine = self._probe_port(self.sunshine_port)
        on = sunshine or self._probe_port(SSH_PORT)
        result = Probe(on=on, sunshine=sunshine)
        with self._lock:
            self.last = result
            self.last_probe_at = self._clock()
        return result

    def monitor(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.probe()
            except Exception as exc:  # never let the monitor die
                log.warning("PC probe failed: %s", exc)
            interval = self.t.poll if self.running() else self.t.idle_poll
            self._kick.wait(interval)
            self._kick.clear()

    def start_monitor(self) -> threading.Event:
        stop = threading.Event()
        threading.Thread(target=self.monitor, args=(stop,), name="pc-monitor", daemon=True).start()
        return stop

    # -- status ----------------------------------------------------------
    def running(self) -> bool:
        return self._job_thread is not None and self._job_thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            last, phase, error = self.last, self.job.phase, self.job.error
        if self.running() and not (last and last.on):
            state = "waking"
        elif last is None:
            state = "off"
        else:
            state = "on" if last.on else "off"
        if last is None or not last.on:
            sunshine = "unknown"
        else:
            sunshine = "listening" if last.sunshine else "down"
        return {"state": state, "sunshine": sunshine, "ready": sunshine == "listening",
                "phase": phase, "error": error}

    def home_status(self) -> dict:
        s = self.status()
        return {"state": s["state"], "sunshine": s["sunshine"]}

    # -- the Play job ----------------------------------------------------
    def play(self) -> dict:
        """Start the job, or join the one already running; returns at once."""
        with self._lock:
            if self._job_thread is None or not self._job_thread.is_alive():
                self.job = JobState(phase="starting", started=self._clock())
                self._job_thread = threading.Thread(target=self._run_job, name="pc-play", daemon=True)
                self._job_thread.start()
        self._kick.set()
        return self.status()

    def _set(self, phase: str, error: str | None = None, event: str | None = None) -> None:
        with self._lock:
            self.job.phase = phase
            self.job.error = error
            if event:
                self.job.events.append(event)
            if phase in ("ready", "failed"):
                self.job.finished = self._clock()
        if event:
            log.info("pc job: %s", event)

    def _wait_for(self, predicate: Callable[[Probe], bool], timeout: float,
                  every: Callable[[float], None] | None = None) -> bool:
        start = self._clock()
        while True:
            p = self.probe()
            if predicate(p):
                return True
            elapsed = self._clock() - start
            if elapsed >= timeout:
                return False
            if every:
                every(elapsed)
            self._sleep(self.t.poll)

    def _ready(self, event: str) -> None:
        try:
            self._prepare_tv()
        except Exception:
            log.exception("preparing the TV failed")
        self._set("ready", event=event)

    def _run_job(self) -> None:
        try:
            self._job()
        except Exception as exc:
            log.exception("pc job crashed")
            self._set("failed", error=f"{type(exc).__name__}: {exc}")

    def _job(self) -> None:
        p = self.probe()
        booted = False
        if not p.on:
            self._set("waking", event="PC off: sending wake packet")
            self._wake()
            last_wake = [0.0]

            def rewake(elapsed: float) -> None:
                if elapsed - last_wake[0] >= self.t.rewake_every:
                    last_wake[0] = elapsed
                    self._wake()

            if not self._wait_for(lambda q: q.on, self.t.boot_timeout, rewake):
                self._set("failed", error="PC did not come up within 3 minutes",
                          event="gave up waiting for the PC")
                return
            booted = True
        if self.probe().sunshine:
            self._ready("Sunshine listening")
            return
        grace = self.t.sunshine_grace_boot if booted else self.t.sunshine_grace
        self._set("starting-sunshine", event=f"PC on, waiting up to {grace:.0f}s for Sunshine")
        if self._wait_for(lambda q: q.sunshine, grace):
            self._ready("Sunshine listening")
            return
        self._set("restarting-sunshine", event="Sunshine closed: restarting its unit once")
        with self._lock:
            self.job.restarted = True
        self._restart()
        if self._wait_for(lambda q: q.sunshine, self.t.sunshine_after_restart):
            self._ready("Sunshine listening after restart")
        else:
            self._set("failed", error="Sunshine is not listening after a restart",
                      event="Sunshine still closed after restart")
