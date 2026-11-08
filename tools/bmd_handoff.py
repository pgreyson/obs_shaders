#!/usr/bin/env python3
"""bmd_handoff: move the Blackmagic UltraStudio (StereoPi camera) between OBS instances.

The Blackmagic camera extension delivers frames to ONE avcapture client at a time;
a second instance selecting the same device gets exact black. So the camera has to
be released in one instance and (re)selected in the other.

  python3 tools/bmd_handoff.py to-stereopi   # main releases, stereopi->full_sbs (:4459) takes it
  python3 tools/bmd_handoff.py to-main       # the reverse (for the depth sidecar / camera_test)
  python3 tools/bmd_handoff.py status
"""
import asyncio, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws

BMD = {"device": "94:14e3c000:00000000:Video", "device_name": "Blackmagic UltraStudio Recorder 3G"}
NONE = {"device": "", "device_name": ""}
SRC = "StereoPi AV"
PW = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))['obs_instances']['viture']['password']
PORTS = {"main": 4455, "stereopi": 4459}


async def ws(port):
    w = simpleobsws.WebSocketClient(url=f"ws://localhost:{port}", password=PW)
    await w.connect()
    if not await w.wait_until_identified(timeout=5):
        raise SystemExit(f"no OBS on ws :{port}")
    return w


async def set_dev(w, settings):
    await w.call(simpleobsws.Request("SetInputSettings", {"inputName": SRC, "inputSettings": settings, "overlay": True}))


async def handoff(src, dst):
    a = await ws(PORTS[src]); b = await ws(PORTS[dst])
    await set_dev(a, NONE)
    await asyncio.sleep(2)
    await set_dev(b, NONE); await asyncio.sleep(1); await set_dev(b, BMD)   # re-select forces a fresh session
    print(f"BMD: {src} released -> {dst} selected (allow ~3 s for frames)")
    await a.disconnect(); await b.disconnect()


async def status():
    for name, port in PORTS.items():
        try:
            w = await ws(port)
        except SystemExit:
            print(f"{name:9s} :{port} not running"); continue
        r = await w.call(simpleobsws.Request("GetInputSettings", {"inputName": SRC}))
        print(f"{name:9s} :{port} {SRC} device = {r.responseData['inputSettings'].get('device') or '(none)'}")
        await w.disconnect()


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    asyncio.run({'to-stereopi': lambda: handoff('main', 'stereopi'),
                 'to-main': lambda: handoff('stereopi', 'main'),
                 'status': status}[cmd]())
