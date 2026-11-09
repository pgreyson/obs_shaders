#!/usr/bin/env python3
"""stereo_window: one common stereo window on main's program scene.

  stereo_window.py auto [--vertical]
                                   derive the window from the layers: synth pillar box
                                   ((1-aspect)/2 of the eye), each camera layer's shift mask ∩ its own
                                   window_width/shift/offset; intersect, equalise the eye widths.
                                   Top/bottom margins stay 0: each camera layer's sbs align already masks
                                   both eyes to the same vertical span, so cropping the whole program
                                   (and the synth under it) only loses height. --vertical crops anyway.
  stereo_window.py set WIDTH SHIFT OFFSET TOP BOTTOM   explicit values (canvas px): one window
                                   width for both eyes, shared shift (+ = right), L/R offset (+ = floats in
                                   front of the screen), top and bottom crops
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
    KEYS = ("window_width", "window_shift", "window_offset", "margin_top", "margin_bottom")
    # per layer: the visible window of each eye as [x0, x1] plus top/bottom crop (canvas px)
    needs = []
    pb = await filt(SYNTH, "pillar box")
    if pb is not None:
        pbm = (1.0 - float(pb.get("aspect_correction", 0.75))) / 2.0 * EYE_W
        needs.append((SYNTH, [pbm, EYE_W - pbm], [pbm, EYE_W - pbm], 0.0, 0.0, enabled.get(SYNTH)))
    for lay in CAMERA_LAYERS:
        al = await filt(lay, "sbs align")
        if al is None:
            continue
        g = lambda k, d=0.0: float(al.get(k, d))
        lx, rx, ly, ry = g("left_x"), g("right_x"), g("left_y"), g("right_y")
        x0 = max(0.0, lx, rx); x1 = EYE_W + min(0.0, lx, rx)
        L, Rw = [x0, x1], [x0, x1]
        if g("window_width") > 0.5:
            for win, sgn in ((L, 0.5), (Rw, -0.5)):
                c = EYE_W / 2 + g("window_shift") + sgn * g("window_offset")
                win[0] = max(win[0], c - g("window_width") / 2); win[1] = min(win[1], c + g("window_width") / 2)
        top = max(0.0, ly, ry) + g("mask_top"); bot = -min(0.0, ly, ry) + g("mask_bottom")
        needs.append((lay, L, Rw, top, bot, enabled.get(lay)))
    for n, L, Rw, top, bot, en in needs:
        print(f"  {n:22s} L eye [{L[0]:4.0f},{L[1]:4.0f}]  R eye [{Rw[0]:4.0f},{Rw[1]:4.0f}]  top {top:3.0f} bottom {bot:3.0f}  {'(on)' if en else '(off)'}")

    if cmd == "auto":
        on = [t for t in needs if t[5]] or needs
        L = [max(t[1][0] for t in on), min(t[1][1] for t in on)]
        Rw = [max(t[2][0] for t in on), min(t[2][1] for t in on)]
        width = round(min(L[1] - L[0], Rw[1] - Rw[0]))
        cL, cR = (L[0] + L[1]) / 2, (Rw[0] + Rw[1]) / 2        # equalise widths around each eye's centre
        vert = "--vertical" in sys.argv
        vals = dict(window_width=width, window_shift=round((cL + cR) / 2 - EYE_W / 2), window_offset=round(cL - cR),
                    margin_top=round(max(t[3] for t in on)) if vert else 0,
                    margin_bottom=round(max(t[4] for t in on)) if vert else 0)
    elif cmd == "set":
        vals = dict(zip(KEYS, (float(v) for v in sys.argv[2:7])))
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
    print("stereo window ->", vals, f"| visible {vals['window_width']:.0f}x{1080-vals['margin_top']-vals['margin_bottom']:.0f} per eye | last_error={cur.get('last_error','')!r}")
    await ws.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
