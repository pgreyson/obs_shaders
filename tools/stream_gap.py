#!/usr/bin/env python3
"""stream_gap: leave a gap between the two eye pictures in the STREAMED picture.

A phone in a Cardboard-type viewer has a divider between the lenses, so the two eye pictures
must not meet in the middle. This shrinks each eye in the relays' `full_sbs` scene, keeping its
16:9 shape and its centre (the quarter points of the frame, where the lenses are), so black
appears between the eyes, at the outer edges and above/below. Only the Twitch and YouTube
relays change: main, the Viture and the phone viewer page keep the full picture, and it costs
main nothing (two cropped scene items, no shader).

  tools/stream_gap.py 4          gap = 4 % of the stream width between the eyes (both relays)
  tools/stream_gap.py 0          back to the plain picture
  tools/stream_gap.py show
  tools/stream_gap.py 4 --relay youtube

Run with control_bridge/.venv/bin/python3.
"""
import asyncio, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws

PW = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))['obs_instances']['viture']['password']
RELAYS = {"twitch": 4456, "youtube": 4457}
SCENE, SOURCE = "full_sbs", "Viture Program (VirtualCam)"


async def apply(name, port, gap_pct):
    ws = simpleobsws.WebSocketClient(url=f"ws://127.0.0.1:{port}", password=PW)
    try:
        await asyncio.wait_for(ws.connect(), 4)
        await asyncio.wait_for(ws.wait_until_identified(), 4)
    except Exception:
        print(f"{name}: not running"); return

    async def call(t, d=None):
        r = await ws.call(simpleobsws.Request(t, d))
        if not r.ok():
            raise SystemExit(f"{name}: {t}: {r.requestStatus.comment}")
        return r.responseData or {}

    cw = (await call("GetVideoSettings"))["baseWidth"]
    ch = (await call("GetVideoSettings"))["baseHeight"]
    items = sorted((i for i in (await call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]
                    if i["sourceName"] == SOURCE), key=lambda i: i["sceneItemId"])
    if not items:
        raise SystemExit(f"{name}: no '{SOURCE}' in scene {SCENE}")
    sw = int(items[0]["sceneItemTransform"]["sourceWidth"]) or 3840
    if gap_pct is None:                                   # show
        t = items[0]["sceneItemTransform"]
        if len(items) < 2:
            print(f"{name}: plain picture (no gap)")
        else:
            w = t["boundsWidth"]
            print(f"{name}: gap {100 * (cw / 2 - w) / cw:.1f} % of the width ({cw / 2 - w:.0f} px), each eye {w:.0f} x {t['boundsHeight']:.0f}")
        await ws.disconnect(); return

    async def xf(item_id, t):
        await call("SetSceneItemTransform", {"sceneName": SCENE, "sceneItemId": item_id, "sceneItemTransform": t})

    if gap_pct <= 0:
        for extra in items[1:]:
            await call("RemoveSceneItem", {"sceneName": SCENE, "sceneItemId": extra["sceneItemId"]})
        await xf(items[0]["sceneItemId"], {"cropLeft": 0, "cropRight": 0, "positionX": 0, "positionY": 0,
                                           "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER",
                                           "boundsAlignment": 0, "boundsWidth": cw, "boundsHeight": ch})
        print(f"{name}: plain picture")
    else:
        if len(items) < 2:
            new = (await call("CreateSceneItem", {"sceneName": SCENE, "sourceName": SOURCE}))["sceneItemId"]
            items.append({"sceneItemId": new})
        gap = gap_pct / 100.0 * cw
        w = cw / 2 - gap                                  # eye width: gap between, gap/2 outside
        h = w * 9 / 16
        for k, it in enumerate(items[:2]):
            await xf(it["sceneItemId"], {
                "cropLeft": 0 if k == 0 else sw // 2, "cropRight": sw // 2 if k == 0 else 0,
                "alignment": 5, "boundsType": "OBS_BOUNDS_STRETCH", "boundsAlignment": 0,
                "boundsWidth": w, "boundsHeight": h,
                "positionX": cw / 4 + k * cw / 2 - w / 2, "positionY": ch / 2 - h / 2})
        print(f"{name}: gap {gap:.0f} px between the eyes, each eye {w:.0f} x {h:.0f} (of {cw / 2:.0f} x {cw / 2 * 9 / 16:.0f})")
    # scene item edits over the websocket do not mark the collection changed: nudge a save
    await call("CreateScene", {"sceneName": "zz_save_nudge"})
    await asyncio.sleep(0.5)
    await call("RemoveScene", {"sceneName": "zz_save_nudge"})
    await ws.disconnect()


async def main():
    args = [a for a in sys.argv[1:]]
    relay = "both"
    if "--relay" in args:
        relay = args[args.index("--relay") + 1]; del args[args.index("--relay"):args.index("--relay") + 2]
    if not args:
        raise SystemExit(__doc__)
    gap = None if args[0] == "show" else float(args[0])
    if gap is not None and not 0 <= gap <= 30:
        raise SystemExit("gap must be 0..30 (% of the stream width)")
    for name, port in RELAYS.items():
        if relay in ("both", name):
            await apply(name, port, gap)

asyncio.run(main())
