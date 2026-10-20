"""Pair the TV Home server with the LG TV's network control (SSAP) once.

Retries the pairing prompt until someone presses Accept on the TV (with the
remote), then stores the client key in ~/.config/tv-home/ssap-client-key
(mode 600; never print it). The TV's address is [tv] ip in config.toml.
Run: uv run -q --no-project --with aiowebostv python -I tv/ssap_pair.py [host]
"""
import asyncio
import os
import sys
import time
from pathlib import Path

from aiowebostv import WebOsClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tvconfig  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else tvconfig.load()["tv"]["ip"]
if not HOST:
    sys.exit("ssap_pair: set [tv] ip in config.toml or pass the TV's address")
KEY_FILE = tvconfig.CONFIG_DIR / "ssap-client-key"
DEADLINE = time.monotonic() + 20 * 60


async def main() -> int:
    if KEY_FILE.exists():
        print(f"already paired: {KEY_FILE}")
        return 0
    while time.monotonic() < DEADLINE:
        client = WebOsClient(HOST)
        try:
            print("sending pairing prompt to the TV", flush=True)
            await asyncio.wait_for(client.connect(), timeout=75)
            if client.client_key:
                KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(client.client_key)
                print(f"paired; key saved to {KEY_FILE}")
                return 0
        except Exception as exc:  # TV off, prompt ignored or timed out
            print(f"not paired yet: {type(exc).__name__}: {exc}", flush=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(10)
    print("gave up after 20 minutes")
    return 1


sys.exit(asyncio.run(main()))
