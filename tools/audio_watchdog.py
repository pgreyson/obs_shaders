#!/usr/bin/env python3
"""audio_watchdog — re-arm OBS's Elgato audio capture when it silently stalls.

Twice in Nov 2026 every instance's `Mic/Aux` (coreaudio_input_capture on the Elgato 4K S)
stopped delivering audio with NOTHING in the OBS log: meters dead in all four instances,
CoreAudio reporting that no process had the Elgato open, while a recording from any other
process worked. Re-applying the same device_id with SetInputSettings makes the source
shut down and re-open the device (~18 s) and audio returns.

Detection: subscribe to InputVolumeMeters on every instance. A running source reports
`inputLevelsMul` every tick even when silent (zeros); a stalled one is absent from the
event or has an empty level list. Stalled for STALL_S on an instance AND the Elgato
audio device still present in CoreAudio → re-init that instance's Mic/Aux (same device,
never `default` — that is the OWL). At most one re-init per instance per COOLDOWN_S.

Run detached (see launch_dual.sh):
  nohup control_bridge/.venv/bin/python3 tools/audio_watchdog.py > /tmp/audio_watchdog.log 2>&1 &
"""
import asyncio, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "control_bridge"))
sys.path.insert(0, os.path.join(HERE, "..", "projector"))
import yaml, simpleobsws                     # noqa: E402
import coreaudio_outputs as co               # noqa: E402
from ctypes import byref, c_uint32           # noqa: E402

PORTS = {4455: "main", 4456: "twitch", 4457: "youtube", 4458: "projector"}
SOURCE = "Mic/Aux"
STALL_S, COOLDOWN_S, RECONNECT_S = 30.0, 300.0, 15.0
DEVICE_MATCH = "Elgato"


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def password():
    cfg = yaml.safe_load(open(os.path.join(HERE, "..", "control_bridge", "config.yaml")))
    for inst in cfg["obs_instances"].values():
        if inst.get("password"):
            return inst["password"]
    raise SystemExit("no websocket password in control_bridge/config.yaml")


def elgato_present():
    """Enumerate in a fresh subprocess: a long-lived process without a CFRunLoop keeps the
    CoreAudio HAL's device list frozen at its first query (seen 2026-11-13: the watchdog
    reported the Elgato missing for an hour while every new process listed it)."""
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import coreaudio_outputs as co\n"
            "from ctypes import byref, c_uint32\n"
            "addr = co.PropAddr(co.DEVICES, co.GLOBAL, 0)\n"
            "size = c_uint32(co._size(co.SYSTEM_OBJECT, addr)); ids = (c_uint32 * (size.value // 4))()\n"
            "co.CA.AudioObjectGetPropertyData(co.SYSTEM_OBJECT, byref(addr), 0, None, byref(size), ids)\n"
            "print(int(any(%r in (co._cfstring(i, co.NAME) or '') for i in ids)))") % (os.path.join(HERE, "..", "projector"), DEVICE_MATCH)
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15).stdout.strip()
        return out.endswith("1")
    except Exception as e:
        log(f"device enumeration failed ({e!r}); assuming present"); return True


async def watch(port, name, pw):
    last_ok, last_fix, warned = time.time(), 0.0, False
    while True:
        ws = simpleobsws.WebSocketClient(url=f"ws://127.0.0.1:{port}", password=pw,
                                         identification_parameters=simpleobsws.IdentificationParameters(eventSubscriptions=(1 << 16)))
        try:
            await asyncio.wait_for(ws.connect(), 5)
            if not await ws.wait_until_identified(timeout=5):
                raise RuntimeError("not identified")
        except Exception as e:
            if not warned:
                log(f"[{name}:{port}] not reachable ({e!r}); retrying every {RECONNECT_S:.0f}s"); warned = True
            await asyncio.sleep(RECONNECT_S); continue
        warned = False; last_ok = time.time()
        log(f"[{name}:{port}] connected, watching '{SOURCE}'")

        async def on_meters(ev):
            nonlocal last_ok
            for i in ev.get("inputs", []):
                if i.get("inputName") == SOURCE and i.get("inputLevelsMul"):
                    last_ok = time.time(); return
        ws.register_event_callback(on_meters, "InputVolumeMeters")

        try:
            while ws.identified:
                await asyncio.sleep(2)
                stalled = time.time() - last_ok
                if stalled < STALL_S or time.time() - last_fix < COOLDOWN_S:
                    continue
                if not elgato_present():
                    log(f"[{name}:{port}] stalled {stalled:.0f}s but no {DEVICE_MATCH} audio device in CoreAudio — waiting"); last_fix = time.time() - COOLDOWN_S + 60; continue
                r = await ws.call(simpleobsws.Request("GetInputSettings", {"inputName": SOURCE}), timeout=10)
                dev = (r.responseData or {}).get("inputSettings", {}).get("device_id", "") if r.ok() else ""
                if DEVICE_MATCH not in dev:
                    log(f"[{name}:{port}] stalled {stalled:.0f}s, but {SOURCE} is on {dev!r} not the {DEVICE_MATCH} — not touching it"); last_fix = time.time(); continue
                log(f"[{name}:{port}] {SOURCE} stalled {stalled:.0f}s → re-init on {dev}")
                t = time.time(); last_fix = t
                try:
                    r = await ws.call(simpleobsws.Request("SetInputSettings", {"inputName": SOURCE, "inputSettings": {"device_id": dev}, "overlay": True}), timeout=60)
                    log(f"[{name}:{port}] re-init {'ok' if r.ok() else r.requestStatus.comment} in {time.time()-t:.1f}s")
                except Exception as e:
                    log(f"[{name}:{port}] re-init did not return in {time.time()-t:.0f}s ({e!r}) — a macOS microphone-permission dialog may be waiting")
                last_ok = time.time()      # give the fresh capture a full STALL_S before judging it
        except Exception as e:
            log(f"[{name}:{port}] connection lost: {e!r}")
        try:
            await ws.disconnect()
        except Exception:
            pass
        await asyncio.sleep(RECONNECT_S)


async def main():
    pw = password()
    log(f"audio_watchdog: stall {STALL_S:.0f}s, cooldown {COOLDOWN_S:.0f}s, instances {sorted(PORTS)}")
    await asyncio.gather(*(watch(p, n, pw) for p, n in PORTS.items()))


if __name__ == "__main__":
    asyncio.run(main())
