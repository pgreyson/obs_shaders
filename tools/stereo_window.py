#!/usr/bin/env python3
"""stereo_window: one common stereo window on main's program scene.

  stereo_window.py auto            derive margins from the layers: synth pillar box
                                   ((1-aspect)/2 of the eye on every edge), each camera layer's
                                   shift + its per-edge mask_* sliders; take the max per edge
  stereo_window.py set LL LR RL RR TOP BOTTOM   explicit margins (canvas px) per edge:
                                   left-eye-left, left-eye-right, right-eye-left, right-eye-right, top, bottom
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
    KEYS = ("left_eye_left", "left_eye_right", "right_eye_left", "right_eye_right", "margin_top", "margin_bottom")
    needs = []   # (layer, dict(edge->px), enabled)
    pb = await filt(SYNTH, "pillar box")
    if pb is not None:
        pbm = (1.0 - float(pb.get("aspect_correction", 0.75))) / 2.0 * EYE_W
        needs.append((SYNTH, dict(left_eye_left=pbm, left_eye_right=pbm, right_eye_left=pbm, right_eye_right=pbm, margin_top=0.0, margin_bottom=0.0), enabled.get(SYNTH)))
    for lay in CAMERA_LAYERS:
        al = await filt(lay, "sbs align")
        if al is None:
            continue
        lx, rx, ly, ry = (float(al.get(k, 0.0)) for k in ("left_x", "right_x", "left_y", "right_y"))
        x0 = max(0.0, lx, rx); x1 = -min(0.0, lx, rx); y0 = max(0.0, ly, ry); y1 = -min(0.0, ly, ry)
        g = lambda k: float(al.get(k, 0.0))
        needs.append((lay, dict(left_eye_left=x0 + g("mask_left_eye_left"), left_eye_right=x1 + g("mask_left_eye_right"),
                                right_eye_left=x0 + g("mask_right_eye_left"), right_eye_right=x1 + g("mask_right_eye_right"),
                                margin_top=y0 + g("mask_top"), margin_bottom=y1 + g("mask_bottom")), enabled.get(lay)))
    for n, d, en in needs:
        print(f"  {n:22s} " + " ".join(f"{k.replace('_eye_','.').replace('margin_','')}={v:5.0f}" for k, v in d.items()) + f"  {'(on)' if en else '(off)'}")

    if cmd == "auto":
        on = [t for t in needs if t[2]] or needs
        vals = {k: round(max(t[1][k] for t in on)) for k in KEYS}
    elif cmd == "set":
        vals = dict(zip(KEYS, (float(v) for v in sys.argv[2:8])))
    else:
        cur = await filt(SCENE, NAME)
        print("stereo window:", {k: cur.get(k) for k in KEYS} if cur else "(not installed)")
        await ws.disconnect(); return

    have = [f["filterName"] for f in (await call("GetSourceFilterList", {"sourceName": SCENE}))["filters"]]
    if NAME not in have:
        await call("CreateSourceFilter", {"sourceName": SCENE, "filterName": NAME, "filterKind": "shader_filter",
                                          "filterSettings": dict(from_file=True, shader_file_name=SHADER, **vals)})
    else:
        await call("SetSourceFilterSettings", {"sourceName": SCENE, "filterName": NAME, "filterSettings": vals, "overlay": True})
    n = len((await call("GetSourceFilterList", {"sourceName": SCENE}))["filters"])
    await call("SetSourceFilterIndex", {"sourceName": SCENE, "filterName": NAME, "filterIndex": n - 1})
    await asyncio.sleep(1.5)
    cur = await filt(SCENE, NAME)
    print("stereo window ->", vals, f"| visible L eye {EYE_W-vals['left_eye_left']-vals['left_eye_right']:.0f} px, R eye {EYE_W-vals['right_eye_left']-vals['right_eye_right']:.0f} px, height {1080-vals['margin_top']-vals['margin_bottom']:.0f} | last_error={cur.get('last_error','')!r}")
    await ws.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
