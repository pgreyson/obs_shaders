#!/usr/bin/env python3
"""after_launch: what main needs once it is up and that OBS does not restore by itself.

  - the fullscreen Program projector on the VITURE display (OBS does not save projectors here)
  - the virtual camera, if `--startvirtualcam` did not take (seen 2026-11-21 after a relaunch:
    the relays and the phone viewer then show a frozen picture)

Waits up to 60 s for main's websocket. Safe to run on a fresh main only: run twice and there
are two projector windows.   control_bridge/.venv/bin/python3 tools/after_launch.py
"""
import asyncio, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws

PW = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))['obs_instances']['viture']['password']


async def main():
    ws = None
    for _ in range(30):
        try:
            ws = simpleobsws.WebSocketClient(url="ws://127.0.0.1:4455", password=PW)
            await asyncio.wait_for(ws.connect(), 3)
            await asyncio.wait_for(ws.wait_until_identified(), 3)
            break
        except Exception:
            ws = None
            await asyncio.sleep(2)
    if not ws:
        raise SystemExit("main (:4455) did not answer")
    call = lambda t, d=None: ws.call(simpleobsws.Request(t, d))
    await asyncio.sleep(4)          # let the scene collection finish loading
    mons = (await call("GetMonitorList")).responseData["monitors"]
    vit = [m["monitorIndex"] for m in mons if "VITURE" in m["monitorName"].upper()]
    if vit:
        r = await call("OpenVideoMixProjector", {"videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
                                                 "monitorIndex": vit[0]})
        print("program projector on VITURE:", "opened" if r.ok() else r.requestStatus.comment)
    else:
        print("no VITURE display; monitors:", [m["monitorName"] for m in mons])
    if not (await call("GetVirtualCamStatus")).responseData["outputActive"]:
        r = await call("StartVirtualCam")
        print("virtual camera was off:", "started" if r.ok() else r.requestStatus.comment)
    await ws.disconnect()

asyncio.run(main())
