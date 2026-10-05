#!/usr/bin/env python3
"""sbs_mode: switch the OBS chain between mono-synth input (default) and
SBS stereo input (e.g. StereoPi over the Elgato).

  python3 tools/sbs_mode.py on      # SBS input: bypass depth synthesis
  python3 tools/sbs_mode.py off     # mono synth: full chain restored
  python3 tools/sbs_mode.py status

SBS mode disables the depth-synthesis block (depth bake, field smooth h/v,
stereo splat), enables "sbs align" (per-eye x/y trim for the camera pair),
zeroes synth color's `soften` (its corner-tap blur renders as 4 offset
ghost copies on camera detail), and sets pillar box aspect_correction to
1.0 (passthrough — the Pi's halves don't carry the analog 4:3 stretch).
synth color's grading + palette (and their CV) stay live and apply
identically to both eyes. Splat depth (ch0) and bake hue_rotation (ch1)
knobs are dormant in SBS mode.

Prior state (filter enables, soften, aspect) is snapshotted to a sidecar
before changing anything and restored exactly on 'off' (never
blanket-enable: the chain carries deliberately-disabled filters). Run
from control_bridge/ or anywhere.
"""
import asyncio, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, '..', 'control_bridge')
sys.path.insert(0, BRIDGE)
import yaml, simpleobsws

STATE = os.path.join(HERE, '.sbs_mode_state.json')
SOURCE = "Video Capture Device"
DEPTH_BLOCK = ["depth bake", "field smooth h", "field smooth v", "stereo splat", "stereo soften"]
ALIGN = "sbs align"
FALLBACK = {f: True for f in DEPTH_BLOCK}          # sane mono-mode defaults
FALLBACK[ALIGN] = False
FALLBACK_ASPECT = 0.75
FALLBACK_SOFTEN = {"viture": 1.0, "projector": 0.53}   # synth-feed look

cfg = yaml.safe_load(open(os.path.join(BRIDGE, 'config.yaml')))

async def connect(c):
    ws = simpleobsws.WebSocketClient(url=f"ws://{c['host']}:{c['port']}", password=c['password'])
    await ws.connect(); await ws.wait_until_identified()
    return ws

async def mode_on():
    snap = {}
    for nm, c in cfg['obs_instances'].items():
        ws = await connect(c)
        fl = (await ws.call(simpleobsws.Request("GetSourceFilterList",
              {"sourceName": SOURCE}))).responseData["filters"]
        enabled = {f["filterName"]: f["filterEnabled"] for f in fl}
        pb = (await ws.call(simpleobsws.Request("GetSourceFilter",
              {"sourceName": SOURCE, "filterName": "pillar box"}))).responseData["filterSettings"]
        sc = (await ws.call(simpleobsws.Request("GetSourceFilter",
              {"sourceName": SOURCE, "filterName": "synth color"}))).responseData["filterSettings"]
        snap[nm] = {"enabled": {f: enabled.get(f, True) for f in DEPTH_BLOCK + [ALIGN]},
                    "aspect": pb.get("aspect_correction", FALLBACK_ASPECT),
                    "soften": sc.get("soften", FALLBACK_SOFTEN.get(nm, 0.5))}
        for f in DEPTH_BLOCK:
            await ws.call(simpleobsws.Request("SetSourceFilterEnabled",
                {"sourceName": SOURCE, "filterName": f, "filterEnabled": False}))
        await ws.call(simpleobsws.Request("SetSourceFilterEnabled",
            {"sourceName": SOURCE, "filterName": ALIGN, "filterEnabled": True}))
        await ws.call(simpleobsws.Request("SetSourceFilterSettings",
            {"sourceName": SOURCE, "filterName": "synth color",
             "filterSettings": {"soften": 0.0}, "overlay": True}))
        await ws.call(simpleobsws.Request("SetSourceFilterSettings",
            {"sourceName": SOURCE, "filterName": "pillar box",
             "filterSettings": {"aspect_correction": 1.0}, "overlay": True}))
        print(f"[{nm}] SBS mode ON: depth block off, sbs align on, soften 0, pillar box passthrough")
        await ws.disconnect()
    json.dump(snap, open(STATE, 'w'))

async def mode_off():
    snap = json.load(open(STATE)) if os.path.exists(STATE) else {}
    for nm, c in cfg['obs_instances'].items():
        s = snap.get(nm, {"enabled": FALLBACK, "aspect": FALLBACK_ASPECT,
                          "soften": FALLBACK_SOFTEN.get(nm, 0.5)})
        ws = await connect(c)
        for f, en in s["enabled"].items():
            await ws.call(simpleobsws.Request("SetSourceFilterEnabled",
                {"sourceName": SOURCE, "filterName": f, "filterEnabled": bool(en)}))
        await ws.call(simpleobsws.Request("SetSourceFilterSettings",
            {"sourceName": SOURCE, "filterName": "synth color",
             "filterSettings": {"soften": float(s.get("soften", FALLBACK_SOFTEN.get(nm, 0.5)))},
             "overlay": True}))
        await ws.call(simpleobsws.Request("SetSourceFilterSettings",
            {"sourceName": SOURCE, "filterName": "pillar box",
             "filterSettings": {"aspect_correction": float(s["aspect"])}, "overlay": True}))
        print(f"[{nm}] SBS mode OFF: snapshot restored "
              f"({'from sidecar' if nm in snap else 'fallback defaults'})")
        await ws.disconnect()
    if os.path.exists(STATE):
        os.remove(STATE)

async def status():
    for nm, c in cfg['obs_instances'].items():
        ws = await connect(c)
        fl = (await ws.call(simpleobsws.Request("GetSourceFilterList",
              {"sourceName": SOURCE}))).responseData["filters"]
        sc = (await ws.call(simpleobsws.Request("GetSourceFilter",
              {"sourceName": SOURCE, "filterName": "synth color"}))).responseData["filterSettings"]
        chain = " -> ".join(f"{f['filterName']}{'' if f['filterEnabled'] else '[OFF]'}" for f in fl)
        print(f"[{nm}] soften={sc.get('soften', '?')}  {chain}")
        await ws.disconnect()

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    asyncio.run({'on': mode_on, 'off': mode_off, 'status': status}[cmd]())
