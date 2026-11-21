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
  tools/stream_gap.py 4 --frame-depth 0     hairline frames at the screen plane

The hairline frames are drawn at the rig's GLOBAL ZERO depth by default (the value in
control_surface/.display_zero.json, canvas px at 1920 per eye, scaled to the eye's size on the
stream): the left eye's frame moves right and the right eye's left by half of it each. Each
frame is its own scene item, and the control surface (control_surface/server.py, stream_frames)
moves the two items whenever Global zero changes, so they follow it live; --frame-depth sets a
fixed depth until Global zero is next moved. If the offset is larger than the gap, the two
frames cross in the middle; the tool says so.

Run with control_bridge/.venv/bin/python3.
"""
import asyncio, os, struct, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws

PW = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))['obs_instances']['viture']['password']
RELAYS = {"twitch": 4456, "youtube": 4457}
SCENE, SOURCE = "full_sbs", "Viture Program (VirtualCam)"
FRAMES = "Eye frames"            # image source: a hairline round each eye, as on a stereo card
FRAME_DIR = os.path.join(HERE, '..', 'obs_config', 'eye_frames')
LINE_PX, LINE_GREY = 2, 96       # on the 1920-wide stream
ZERO_FILE = os.path.join(HERE, '..', 'control_surface', '.display_zero.json')


def global_zero():
    try:
        import json
        return float(json.load(open(ZERO_FILE))["zero_px"])
    except Exception:
        return 0.0


def frame_png(w, h, path):
    """One frame: a transparent picture of w+2L x h+2L with an L-wide outline (the w x h hole
    is where the eye picture shows through)."""
    cw, ch = int(round(w)) + 2 * LINE_PX, int(round(h)) + 2 * LINE_PX
    px = bytes((LINE_GREY, LINE_GREY, LINE_GREY, 255))
    full = bytearray(px * cw)
    side = bytearray(cw * 4)
    side[:LINE_PX * 4] = px * LINE_PX
    side[-LINE_PX * 4:] = px * LINE_PX
    rows = [full if (yy < LINE_PX or yy >= ch - LINE_PX) else side for yy in range(ch)]
    raw = b''.join(b'\x00' + bytes(r) for r in rows)
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', cw, ch, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


async def apply(name, port, gap_pct, frame_depth=None):
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
    all_items = (await call("GetSceneItemList", {"sceneName": SCENE}))["sceneItems"]
    items = sorted((i for i in all_items if i["sourceName"] == SOURCE), key=lambda i: i["sceneItemId"])
    frame_items = [i for i in all_items if i["sourceName"] == FRAMES]
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
        for fi in frame_items:
            await call("RemoveSceneItem", {"sceneName": SCENE, "sceneItemId": fi["sceneItemId"]})
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
        # hairline round each eye: a transparent picture drawn over the two eyes
        depth = global_zero() if frame_depth is None else frame_depth      # canvas px, + = in front
        d = depth * w / 1920.0                                             # on the stream
        png = os.path.abspath(os.path.join(FRAME_DIR, f"eye_frame_{w:.0f}x{h:.0f}.png"))
        print(f"{name}: frames at depth {depth:g} px (global zero {global_zero():g}) = {d:.0f} px on the stream"
              + (f"; wider than the {gap:.0f} px gap, so the frames cross in the middle (gap of {100 * d / cw:.1f} % or more avoids it)" if d > gap else ""))
        frame_png(w, h, png)
        ids = sorted(i["sceneItemId"] for i in frame_items)
        if not ids:
            r = await ws.call(simpleobsws.Request("CreateInput", {"sceneName": SCENE, "inputName": FRAMES,
                              "inputKind": "image_source", "inputSettings": {"file": png}, "sceneItemEnabled": True}))
            if r.ok():
                ids.append(r.responseData["sceneItemId"])
        await call("SetInputSettings", {"inputName": FRAMES, "inputSettings": {"file": png}, "overlay": True})
        while len(ids) < 2:      # one scene item of the same picture per eye
            ids.append((await call("CreateSceneItem", {"sceneName": SCENE, "sourceName": FRAMES}))["sceneItemId"])
        for extra in ids[2:]:
            await call("RemoveSceneItem", {"sceneName": SCENE, "sceneItemId": extra})
        for k, item_id in enumerate(ids[:2]):
            await xf(item_id, {"cropLeft": 0, "cropRight": 0, "alignment": 5, "boundsType": "OBS_BOUNDS_NONE",
                               "scaleX": 1.0, "scaleY": 1.0, "rotation": 0,
                               "positionX": cw / 4 + k * cw / 2 - w / 2 - LINE_PX + (d / 2 if k == 0 else -d / 2),
                               "positionY": ch / 2 - h / 2 - LINE_PX})
        print(f"{name}: gap {gap:.0f} px between the eyes, each eye {w:.0f} x {h:.0f} (of {cw / 2:.0f} x {cw / 2 * 9 / 16:.0f}), hairline frames on")
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
    frame_depth = None
    if "--frame-depth" in args:
        frame_depth = float(args[args.index("--frame-depth") + 1]); del args[args.index("--frame-depth"):args.index("--frame-depth") + 2]
    gap = None if args[0] == "show" else float(args[0])
    if gap is not None and not 0 <= gap <= 30:
        raise SystemExit("gap must be 0..30 (% of the stream width)")
    for name, port in RELAYS.items():
        if relay in ("both", name):
            await apply(name, port, gap, frame_depth)

asyncio.run(main())
