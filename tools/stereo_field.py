#!/usr/bin/env python3
"""stereo_field — see and place the three stereo layers (synth, stage, room) on ONE scale.

Unit everywhere: displayed disparity = x_left − x_right in CANVAS px (1920 per eye),
+ = in front of the screen, − = behind. Same unit as `sbs align` / `camera out` sliders
and the splat's plane_px / thickness_px.

  stereo_field.py status [--budget 58]
        measure each layer (feature matching between the eyes of the rendered scene) and
        print p5 / p50 / p95, vertical residual, the synth's CONFIGURED range, and the
        program total against a comfort budget (default 58 px = 3 % of the eye width).
  stereo_field.py converge stage|room|both [--at 0] [--pct 50] [--fix-vertical] [--dry-run]
        set that camera's `sbs align` convergence so the chosen percentile of its subject
        lands at --at px (0 = on the screen plane). --fix-vertical also removes the measured
        vertical offset between the eyes (split across left_y / right_y).
  stereo_field.py snap [--to stage|room|both] [--gap 6] [--side behind|front] [--dry-run]
        put the synth just behind the camera subject's far plane (anchor = front), or just
        in front of its near plane (--side front, anchor = back). Thickness is untouched.
  stereo_field.py set plane|thickness|anchor VALUE
        write one splat control directly.

Run with the OpenCV venv, which still lives at depth_sidecar/.venv (the sidecar itself is
archived in archive/room_depth/):
  depth_sidecar/.venv/bin/python3 tools/stereo_field.py status
Nothing runs continuously: re-run converge / snap after re-zooming or moving a camera.
"""
import argparse
import asyncio
import base64
import json
import os
import sys

import cv2
import numpy as np
import simpleobsws
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(HERE, "..", "control_bridge", "config.yaml")))["obs_instances"]["viture"]
URL = f"ws://{CFG.get('host', 'localhost')}:{CFG['port']}"

SYNTH, SPLAT = "Video Capture Device", "stereo splat"
CAMERAS = ("stage", "room")
ALIGN = "camera"   # one merged filter per camera scene since 2026-11-18 (was "sbs align")
EYE_W = 1920.0
MAX_PARALLAX, PIC_LO, PIC_HI = 0.08, 0.010, 0.985   # legacy splat constants (pre plane/thickness)


class OBS:
    def __init__(self):
        self.ws = simpleobsws.WebSocketClient(url=URL, password=CFG["password"])

    async def __aenter__(self):
        await self.ws.connect()
        await self.ws.wait_until_identified()
        return self

    async def __aexit__(self, *a):
        await self.ws.disconnect()

    async def call(self, t, d=None):
        r = await self.ws.call(simpleobsws.Request(t, d), timeout=40)
        if not r.ok():
            raise RuntimeError(f"{t}: {r.requestStatus.comment}")
        return r.responseData or {}

    async def filt(self, src, name):
        return (await self.call("GetSourceFilter", {"sourceName": src, "filterName": name}))["filterSettings"]

    async def setf(self, src, name, st):
        await self.call("SetSourceFilterSettings", {"sourceName": src, "filterName": name,
                                                    "filterSettings": st, "overlay": True})

    async def nudge_save(self):
        # websocket filter edits do not mark the project changed; this does (see camera_chain.py)
        await self.call("CreateScene", {"sceneName": "zz_save_nudge"})
        await asyncio.sleep(0.5)
        await self.call("RemoveScene", {"sceneName": "zz_save_nudge"})

    async def shot(self, name):
        r = await self.call("GetSourceScreenshot", {"sourceName": name, "imageFormat": "png",
                                                    "imageWidth": 3840, "imageHeight": 1080})
        buf = np.frombuffer(base64.b64decode(r["imageData"].split(",", 1)[1]), np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)


def measure(img):
    """-> dict(n, p5, p50, p95, dy) of displayed disparity (canvas px), or None if unmeasurable."""
    g = cv2.cvtColor(img[..., :3], cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(3.0, (8, 8)).apply(g)
    w = g.shape[1] // 2
    sift = cv2.SIFT_create(4000)
    kl, dl = sift.detectAndCompute(g[:, :w], None)
    kr, dr = sift.detectAndCompute(g[:, w:], None)
    if dl is None or dr is None or len(kl) < 20 or len(kr) < 20:
        return None
    good = [m[0] for m in cv2.BFMatcher().knnMatch(dl, dr, k=2) if len(m) == 2 and m[0].distance < 0.7 * m[1].distance]
    if len(good) < 15:
        return None
    pl = np.array([kl[m.queryIdx].pt for m in good])
    pr = np.array([kr[m.trainIdx].pt for m in good])
    dy = pl[:, 1] - pr[:, 1]
    ok = np.abs(dy - np.median(dy)) < 6
    d = (pl[:, 0] - pr[:, 0])[ok]
    # reject gross horizontal outliers (mismatches) around the bulk
    med = np.median(d); mad = np.median(np.abs(d - med)) + 1.0
    d2 = d[np.abs(d - med) < 8 * mad + 20]
    if len(d2) < 12:
        return None
    p = np.percentile(d2, [5, 50, 95])
    return {"n": int(len(d2)), "p5": float(p[0]), "p50": float(p[1]), "p95": float(p[2]),
            "dy": float(np.median(dy[ok])), "d": d2}


async def measure_layer(o, name, tries=3):
    """Median of a few frames (analog/camera noise moves individual matches)."""
    ms = []
    for _ in range(tries):
        m = measure(await o.shot(name))
        if m:
            ms.append(m)
    if not ms:
        return None
    d = np.concatenate([m["d"] for m in ms])
    p = np.percentile(d, [5, 50, 95])
    return {"n": int(np.median([m["n"] for m in ms])), "p5": float(p[0]), "p50": float(p[1]), "p95": float(p[2]),
            "dy": float(np.median([m["dy"] for m in ms])), "d": d}


def synth_config_range(st):
    """(far_px, near_px, description) of the splat as configured."""
    if "thickness_px" in st:
        t, p, a = float(st["thickness_px"]), float(st.get("plane_px", 0.0)), float(st.get("anchor", 1.0))
        return p - t * a, p + t * (1.0 - a), f"plane {p:+.0f}  thickness {t:.0f}  anchor {a:.2f}"
    depth, c = float(st.get("depth", 0.0)), float(st.get("convergence", 0.0))
    f0 = depth * depth * MAX_PARALLAX
    span = (PIC_HI - f0) - (PIC_LO + f0)
    k = 2.0 * f0 * EYE_W / span
    return -c * k, (1.0 - c) * k, f"legacy depth {depth:.3f}  convergence {c:.3f}"


def strip(lo, hi, vmin, vmax, width=61, ch="█"):
    cells = [" "] * width
    f = lambda v: int(round((v - vmin) / (vmax - vmin) * (width - 1)))
    z = f(0.0)
    if 0 <= z < width:
        cells[z] = "│"
    if hi >= lo:
        for i in range(max(0, f(lo)), min(width - 1, f(hi)) + 1):
            cells[i] = ch
    return "".join(cells)


async def cmd_status(args):
    async with OBS() as o:
        items = {i["sourceName"]: i["sceneItemEnabled"] for i in (await o.call("GetSceneItemList", {"sceneName": "Scene"}))["sceneItems"]}
        rows = []
        st = await o.filt(SYNTH, SPLAT)
        far, near, desc = synth_config_range(st)
        rows.append(("synth (configured)", far, None, near, None, None, items.get(SYNTH), desc))
        m = await measure_layer(o, SYNTH)
        if m:
            rows.append(("synth (measured)", m["p5"], m["p50"], m["p95"], m["dy"], m["n"], items.get(SYNTH), "textured content only"))
        for cam in CAMERAS:
            m = await measure_layer(o, cam)
            al = await o.filt(cam, ALIGN)
            note = f"convergence {float(al.get('convergence', 0.0)):+.1f}"
            if m:
                rows.append((cam, m["p5"], m["p50"], m["p95"], m["dy"], m["n"], items.get(cam), note))
            else:
                rows.append((cam, None, None, None, None, 0, items.get(cam), note + "  (no matches: dark / keyed out?)"))
        vals = [v for r in rows for v in (r[1], r[3]) if v is not None]
        vmin, vmax = min(vals + [-10]) - 10, max(vals + [10]) + 10
        print(f"displayed disparity, canvas px (1920/eye): + in front of screen, − behind;  │ = screen plane")
        print(f"{'layer':20s} {'far/p5':>7s} {'p50':>6s} {'near/p95':>8s} {'vert':>6s} {'n':>5s}  {'':5s} {strip(1, 0, vmin, vmax)}")
        on_vals = []
        for name, lo, mid, hi, dy, n, en, note in rows:
            if lo is None:
                print(f"{name:20s} {'-':>7s} {'-':>6s} {'-':>8s} {'-':>6s} {n:5d}  {'on ' if en else 'off'}   {note}")
                continue
            if en and not name.startswith("synth (measured"):
                on_vals += [lo, hi]
            print(f"{name:20s} {lo:+7.0f} {('%+6.0f' % mid) if mid is not None else '     -'} {hi:+8.0f} "
                  f"{('%+6.1f' % dy) if dy is not None else '     -'} {(n if n is not None else 0):5d}  {'on ' if en else 'off'} "
                  f"{strip(lo, hi, vmin, vmax)}  {note}")
        if on_vals:
            total = max(on_vals) - min(on_vals)
            flag = "OK" if total <= args.budget else "OVER"
            print(f"\nprogram (enabled layers): {min(on_vals):+.0f} … {max(on_vals):+.0f} px = {total:.0f} px "
                  f"({total / EYE_W * 100:.1f} % of eye width); budget {args.budget:.0f} px ({args.budget / EYE_W * 100:.1f} %) -> {flag}")


async def cmd_converge(args):
    cams = CAMERAS if args.layer == "both" else (args.layer,)
    async with OBS() as o:
        for cam in cams:
            m = await measure_layer(o, cam)
            if not m:
                print(f"{cam}: cannot measure (no feature matches); skipped")
                continue
            al = await o.filt(cam, ALIGN)
            cur = float(al.get("convergence", 0.0))
            at_now = float(np.percentile(m["d"], args.pct))
            new = max(-300.0, min(300.0, cur + (args.at - at_now)))
            st = {"convergence": round(new * 2) / 2}
            msg = f"{cam}: p{args.pct:g} at {at_now:+.1f} px (range {m['p5']:+.0f}…{m['p95']:+.0f}) -> convergence {cur:+.1f} → {st['convergence']:+.1f}"
            if args.fix_vertical and abs(m["dy"]) > 0.5:
                st["left_y"] = round((float(al.get("left_y", 0.0)) - m["dy"] / 2) * 4) / 4
                st["right_y"] = round((float(al.get("right_y", 0.0)) + m["dy"] / 2) * 4) / 4
                msg += f"; vertical {m['dy']:+.1f} px -> left_y {st['left_y']:+.2f} right_y {st['right_y']:+.2f}"
            print(msg)
            if args.dry_run:
                continue
            await o.setf(cam, ALIGN, st)
            await asyncio.sleep(1.0)
            m2 = await measure_layer(o, cam)
            if m2:
                print(f"   now p5 {m2['p5']:+.0f}  p50 {m2['p50']:+.0f}  p95 {m2['p95']:+.0f}  vertical {m2['dy']:+.1f}")
        if not args.dry_run:
            await o.nudge_save()


async def cmd_snap(args):
    cams = CAMERAS if args.to == "both" else (args.to,)
    async with OBS() as o:
        ms = [m for m in [await measure_layer(o, c) for c in cams] if m]
        if not ms:
            print("cannot measure the camera layer(s); nothing changed")
            return
        st = await o.filt(SYNTH, SPLAT)
        if "thickness_px" not in st:
            print("the running splat plugin still has the legacy depth/convergence controls; "
                  "snap needs the plane/thickness build (relaunch main after plugin/build.sh)")
            return
        if args.side == "behind":
            new = {"anchor": 1.0, "plane_px": round(min(m["p5"] for m in ms) - args.gap)}
            what = f"synth FRONT plane at {new['plane_px']:+.0f} px = camera far plane {min(m['p5'] for m in ms):+.0f} − gap {args.gap:g}"
        else:
            new = {"anchor": 0.0, "plane_px": round(max(m["p95"] for m in ms) + args.gap)}
            what = f"synth BACK plane at {new['plane_px']:+.0f} px = camera near plane {max(m['p95'] for m in ms):+.0f} + gap {args.gap:g}"
        print(what + f" (thickness {float(st['thickness_px']):.0f} unchanged)")
        if not args.dry_run:
            await o.setf(SYNTH, SPLAT, new)
            await o.nudge_save()


async def cmd_set(args):
    key = {"plane": "plane_px", "thickness": "thickness_px", "anchor": "anchor"}[args.what]
    async with OBS() as o:
        st = await o.filt(SYNTH, SPLAT)
        if "thickness_px" not in st:
            print("legacy splat plugin running (no plane/thickness yet)")
            return
        await o.setf(SYNTH, SPLAT, {key: args.value})
        far, near, desc = synth_config_range({**st, key: args.value})
        print(f"{desc} -> synth spans {far:+.0f} … {near:+.0f} px")
        await o.nudge_save()


PROGRAM, ZERO = "Scene", "display zero"


ZERO_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "control_surface", ".display_zero.json")


async def cmd_zero(args):
    """Global zero: one depth offset for the whole program. Since 2026-11-18 it is FOLDED into the
    layers (the display_zero filter cost a render pass): changing it moves the synth plane, both
    camera convergences and both camera window offsets by the same amount. The running total
    lives in control_surface/.display_zero.json, shared with the touch surface's "global zero".
    --keep only re-labels the total (nothing moves): use it after trimming the layers by hand."""
    try:
        cur = float(json.load(open(ZERO_STATE))["zero_px"])
    except Exception:
        cur = 0.0
    if args.value is None:
        print(f"global zero {cur:+.1f} px (folded into splat plane, camera convergence + window offset)")
        return
    d = args.value - cur
    print(f"global zero {cur:+.1f} → {args.value:+.1f} px")
    if not args.keep and abs(d) > 0.01:
        async with OBS() as o:
            pl = float((await o.filt(SYNTH, SPLAT)).get("plane_px", 0.0))
            await o.setf(SYNTH, SPLAT, {"plane_px": pl + d})
            print(f"  synth plane {pl:+.1f} → {pl + d:+.1f}")
            for cam in CAMERAS:
                st = await o.filt(cam, ALIGN)
                cv, wo = float(st.get("convergence", 0.0)), float(st.get("window_offset", 0.0))
                await o.setf(cam, ALIGN, {"convergence": max(-300.0, min(300.0, cv + d)),
                                          "window_offset": max(-400.0, min(400.0, wo + d))})
                print(f"  {cam} convergence {cv:+.1f} → {cv + d:+.1f}, window offset {wo:+.1f} → {wo + d:+.1f}")
            await o.nudge_save()
    json.dump({"zero_px": args.value}, open(ZERO_STATE, "w"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--budget", type=float, default=58.0)
    c = sub.add_parser("converge"); c.add_argument("layer", choices=("stage", "room", "both"))
    c.add_argument("--at", type=float, default=0.0); c.add_argument("--pct", type=float, default=50.0)
    c.add_argument("--fix-vertical", action="store_true"); c.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("snap"); n.add_argument("--to", choices=("stage", "room", "both"), default="stage")
    n.add_argument("--gap", type=float, default=6.0); n.add_argument("--side", choices=("behind", "front"), default="behind")
    n.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("set"); t.add_argument("what", choices=("plane", "thickness", "anchor")); t.add_argument("value", type=float)
    z = sub.add_parser("zero"); z.add_argument("value", type=float, nargs="?"); z.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    asyncio.run({"zero": cmd_zero, "status": cmd_status, "converge": cmd_converge, "snap": cmd_snap, "set": cmd_set}[args.cmd](args))


if __name__ == "__main__":
    main()
