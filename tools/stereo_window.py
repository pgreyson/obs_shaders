#!/usr/bin/env python3
"""stereo_window: one common stereo window on main's program scene.

  stereo_window.py auto            derive margins from the layers: synth pillar box
                                   ((1-aspect)/2 of the eye, both sides), each camera layer's
                                   |shift| + margin_outer / margin_inner and |vertical shift|; take the max
  stereo_window.py set OUTER INNER Y   explicit margins (canvas px): outer = left of L / right of R
                                   (near content), inner = right of L / left of R (far), y = top+bottom
  stereo_window.py status

Installs/updates the 'stereo window' shader filter (stereo_window.shader) as the LAST
filter on scene 'Scene' in main (:4455). Run with control_bridge/.venv/bin/python3.
"""
import asyncio, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws

SHADER = os.path.join(os.path.dirname(HERE), "stereo_window.shader")
PW = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))['obs_instances']['viture']['password']
SCENE, NAME = "Scene", "stereo window"
CAMERA_LAYERS = ("stage", "room")
SYNTH = "Video Capture Device"
EYE_W = 1920.0


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    ws = simpleobsws.WebSocketClient(url="ws://localhost:4455", password=PW)
    await ws.connect(); await ws.wait_until_identified()

    async def call(n, d=None):
        r = await ws.call(simpleobsws.Request(n, d or {}))
        if not r.ok():
            raise SystemExit(f"{n}: {r.requestStatus.comment}")
        return r.responseData or {}

    async def filt(src, name):
        r = await ws.call(simpleobsws.Request("GetSourceFilter", {"sourceName": src, "filterName": name}))
        return r.responseData["filterSettings"] if r.ok() else None

    enabled = {i["sourceName"]: i["sceneItemEnabled"] for i in (await call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]}
    needs = []
    pb = await filt(SYNTH, "pillar box")
    if pb is not None:
        a = float(pb.get("aspect_correction", 0.75))
        pbm = (1.0 - a) / 2.0 * EYE_W
        needs.append((SYNTH, pbm, pbm, 0.0, enabled.get(SYNTH)))
    for lay in CAMERA_LAYERS:
        al = await filt(lay, "sbs align")
        if al is None:
            continue
        sh = max(abs(al.get("left_x", 0.0)), abs(al.get("right_x", 0.0)))
        my = max(abs(al.get("left_y", 0.0)), abs(al.get("right_y", 0.0)))
        needs.append((lay, sh + float(al.get("margin_outer", 0.0)), sh + float(al.get("margin_inner", 0.0)), my, enabled.get(lay)))
    for n, mo, mi, my, en in needs:
        print(f"  {n:22s} needs outer {mo:6.1f}  inner {mi:6.1f}  y {my:6.1f}  {'(on)' if en else '(off)'}")

    if cmd == "auto":
        on = [t for t in needs if t[4]] or needs
        mo = round(max(t[1] for t in on)); mi = round(max(t[2] for t in on)); my = round(max(t[3] for t in on))
    elif cmd == "set":
        mo, mi, my = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    else:
        cur = await filt(SCENE, NAME)
        print("stereo window:", {k: cur.get(k) for k in ("margin_outer", "margin_inner", "margin_y")} if cur else "(not installed)")
        await ws.disconnect(); return

    have = [f["filterName"] for f in (await call("GetSourceFilterList", {"sourceName": SCENE}))["filters"]]
    if NAME not in have:
        await call("CreateSourceFilter", {"sourceName": SCENE, "filterName": NAME, "filterKind": "shader_filter",
                                          "filterSettings": {"from_file": True, "shader_file_name": SHADER, "margin_outer": mo, "margin_inner": mi, "margin_y": my}})
    else:
        await call("SetSourceFilterSettings", {"sourceName": SCENE, "filterName": NAME, "filterSettings": {"margin_outer": mo, "margin_inner": mi, "margin_y": my}, "overlay": True})
    n = len((await call("GetSourceFilterList", {"sourceName": SCENE}))["filters"])
    await call("SetSourceFilterIndex", {"sourceName": SCENE, "filterName": NAME, "filterIndex": n - 1})
    await asyncio.sleep(1.5)
    cur = await filt(SCENE, NAME)
    print(f"stereo window -> outer {mo} inner {mi} y {my}; visible per eye {EYE_W-mo-mi:.0f}x{1080-2*my:.0f}; last_error={cur.get('last_error','')!r}")
    await ws.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
