#!/usr/bin/env python3
"""
run_stereo_tests.py — capture chromadepth.shader output in OBS, diff against
stereo_oracle ground truth, and report pass/fail metrics + render perf.

Usage:
  .venv python3 tools/run_stereo_tests.py            # full matrix
  .venv python3 tools/run_stereo_tests.py --quick    # depth 0.5/0.84, scene 1

Setup expected (created earlier, persisted in the scene collection):
  viture OBS instance ws://127.0.0.1:4455
  "Test Pattern" image_source (hidden scene item) + "chromadepth" filter

Per config: push mono pattern PNG -> set filter params -> screenshot ->
split half-SBS -> compare vs render_eyes(pattern(...), eye_w=960).

Metrics per eye:
  meandiff  — mean abs error (target < 0.01)
  ghost     — shader lit / oracle black, the worst visible class (target 0
              within rounding: < 20)
  stray     — shader black / oracle lit, outside a 3px oracle-edge band
              (target < 50; rim inside the band is tolerated)
PERF: GetStats averageFrameRenderTime on the viture instance (target < 33 ms
— note BOTH instances must hit this on the Mac mini for the rig to hold
30 fps; the projector runs the same shader).

Exit code 0 = all targets met.
"""
import argparse
import asyncio
import base64
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from stereo_oracle import pattern, render_eyes, render_eyes_warp  # noqa: E402

import simpleobsws  # noqa: E402

WS_URL = "ws://127.0.0.1:4455"
WS_PASSWORD = "fcXnj9dDSRctKq8N"
SOURCE = "Test Pattern"
# "stereo splat" = the native plugin (true forward splat + z-buffer);
# "chromadepth" = the legacy fragment march (disabled in the chain but
# kept as rollback — pass --filter chromadepth to compare against it).
FILTER = "stereo splat"
MONO_PNG = "/tmp/harness_mono.png"

TARGET_MEANDIFF = 0.01
TARGET_GHOST = 20
TARGET_STRAY = 50
TARGET_FRAME_MS = 33.0


def edge_mask(o, rad=3):
    lit = o.max(axis=2) > 0.05
    e = np.zeros_like(lit)
    for d in range(1, rad + 1):
        for ax in (0, 1):
            e |= lit != np.roll(lit, d, axis=ax)
            e |= lit != np.roll(lit, -d, axis=ax)
    return e


def compare(cap, oL, oR):
    out = []
    for c, o in ((cap[:, :960], oL), (cap[:, 960:], oR)):
        ghost = int(((c.max(axis=2) > 0.15) & (o.max(axis=2) < 0.05)).sum())
        hole = (c.max(axis=2) < 0.05) & (o.max(axis=2) > 0.15)
        stray = int((hole & ~edge_mask(o)).sum())
        out.append({"meandiff": float(np.abs(c - o).mean()),
                    "ghost": ghost, "hole": int(hole.sum()), "stray": stray})
    return out


async def run(configs):
    ws = simpleobsws.WebSocketClient(url=WS_URL, password=WS_PASSWORD)
    await ws.connect()
    await ws.wait_until_identified()
    failures = 0
    last_scene = None
    src_q = None
    for scene, soft, noise, depth, rot, cw, sigma, mode in configs:
        if (scene, soft, noise) != last_scene:
            src = pattern(scene, softness_px=soft, noise_amp=noise)
            # Quantize the oracle's source to the PNG's 8 bits: the shader
            # can only ever see the PNG, and float-vs-8-bit dust at exact
            # grid boundaries measured as whole ghost rows.
            src_q = np.round(np.clip(src, 0, 1) * 255.0) / 255.0
            Image.fromarray((src_q * 255).astype(np.uint8)).save(MONO_PNG)
            await ws.call(simpleobsws.Request("SetInputSettings", {
                "inputName": SOURCE, "inputSettings": {"file": MONO_PNG}}))
            last_scene = (scene, soft, noise)
            await asyncio.sleep(0.6)
        # depth drives the march filter; hue_rotation/color_weight drive
        # the bake filter (the field owns them).
        await ws.call(simpleobsws.Request("SetSourceFilterSettings", {
            "sourceName": SOURCE, "filterName": "depth bake",
            "filterSettings": {"hue_rotation": rot, "color_weight": cw}}))
        await ws.call(simpleobsws.Request("SetSourceFilterSettings", {
            "sourceName": SOURCE, "filterName": FILTER,
            "filterSettings": {"depth": depth, "mode": mode,
                               "fill": 3, "ssaa": False}}))
        # regional-depth smoothing: both separable passes get the same sigma
        for fname in ("field smooth h", "field smooth v"):
            await ws.call(simpleobsws.Request("SetSourceFilterSettings", {
                "sourceName": SOURCE, "filterName": fname,
                "filterSettings": {"smoothing": sigma}}))
        await asyncio.sleep(0.8)
        r = await ws.call(simpleobsws.Request("GetSourceScreenshot", {
            "sourceName": SOURCE, "imageFormat": "png",
            "imageWidth": 1920, "imageHeight": 1080}))
        b64 = r.responseData["imageData"].split(",", 1)[1]
        cap = np.asarray(Image.open(io.BytesIO(base64.b64decode(b64)))
                         .convert("RGB")).astype(float) / 255.0
        if mode == 1:
            oL, oR = render_eyes_warp(src_q, depth, hue_rotation=rot,
                                      color_weight=cw, eye_w=960,
                                      field_sigma=sigma)
        else:
            # convention baseline: 3px micro-fill + 2x vertical SSAA
            oL, oR = render_eyes(src_q, depth, hue_rotation=rot,
                                 color_weight=cw, eye_w=960,
                                 field_sigma=sigma, micro_fill=3, ss_v=1)
        for eye, m in zip("LR", compare(cap, oL, oR)):
            ok = (m["meandiff"] < TARGET_MEANDIFF
                  and m["ghost"] < TARGET_GHOST and m["stray"] < TARGET_STRAY)
            if not ok:
                failures += 1
            print(f"{'PASS' if ok else 'FAIL'} scene{scene} soft{soft} "
                  f"n{noise} d{depth} rot{rot} cw{cw} s{sigma} "
                  f"{'warp' if mode else 'splat'} {eye}: "
                  f"diff {m['meandiff']:.4f} ghost {m['ghost']} "
                  f"hole {m['hole']} stray {m['stray']}")
    r = await ws.call(simpleobsws.Request("GetStats"))
    ms = r.responseData["averageFrameRenderTime"]
    perf_ok = ms < TARGET_FRAME_MS
    if not perf_ok:
        failures += 1
    print(f"{'PASS' if perf_ok else 'FAIL'} perf: averageFrameRenderTime "
          f"{ms:.2f} ms (target < {TARGET_FRAME_MS})")
    await ws.disconnect()
    return failures


def main():
    global FILTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--filter", default=FILTER,
                    help="displacement filter name on Test Pattern")
    args = ap.parse_args()
    FILTER = args.filter
    if args.quick:
        # quick now mirrors the LIVE-CONTENT failure modes: gradients
        # (scene 2 — real synth is gradients everywhere), lumadepth mode
        # (cw 0, where the user's CV sits), noise, and the live sigma=4
        # regional-depth configs (config = ..., field_sigma).
        configs = [(1, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 0),
                   (2, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 0),
                   (6, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 0),
                   (6, 2, 0.0, 0.84, 0.0, 1.0, 0.0, 0),
                   (5, 2, 1.0, 0.84, 0.0, 1.0, 0.0, 0),
                   (6, 2, 0.0, 0.5, 0.0, 1.0, 4.0, 0),
                   (6, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1),
                   (6, 2, 0.0, 0.84, 0.0, 1.0, 0.0, 1),
                   (2, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1),
                   (1, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1)]
    else:
        configs = []
        for scene, soft, noise in ((0, 2, 0.0), (1, 2, 0.0), (1, 0, 0.0),
                                   (2, 2, 0.0), (3, 2, 0.0), (4, 2, 0.0),
                                   (5, 2, 1.0), (6, 2, 0.0)):
            for depth in (0.3, 0.5, 0.84, 1.0):
                configs.append((scene, soft, noise, depth, 0.0, 1.0, 0.0, 0))
        configs += [(1, 2, 0.0, 0.5, 0.3, 1.0, 0.0, 0),
                    (1, 2, 0.0, 0.5, 0.0, 0.0, 0.0, 0),
                    (1, 2, 0.0, 0.5, 0.0, 0.5, 0.0, 0),
                    (5, 2, 0.5, 1.0, 0.0, 1.0, 0.0, 0),
                    (2, 2, 0.0, 0.84, 0.0, 0.0, 0.0, 0),
                    (6, 2, 0.0, 0.84, 0.0, 0.0, 0.0, 0),
                    (5, 2, 1.0, 0.5, 0.0, 0.0, 0.0, 0)]
        # sigma=4 regional-depth sweep: every scene class once, plus
        # depth extremes on rects, gradients and noise.
        configs += [(0, 2, 0.0, 0.5, 0.0, 1.0, 4.0, 0),
                    (1, 2, 0.0, 0.5, 0.0, 1.0, 4.0, 0),
                    (1, 2, 0.0, 1.0, 0.0, 1.0, 4.0, 0),
                    (2, 2, 0.0, 0.5, 0.0, 0.0, 4.0, 0),
                    (3, 2, 0.0, 0.84, 0.0, 1.0, 4.0, 0),
                    (4, 2, 0.0, 0.84, 0.0, 1.0, 4.0, 0),
                    (5, 2, 1.0, 0.84, 0.0, 1.0, 4.0, 0),
                    (6, 2, 0.0, 0.84, 0.0, 1.0, 4.0, 0),
                    (5, 2, 1.0, 1.0, 0.0, 0.0, 4.0, 0)]
        # warp-mode sweep (connected stretch): gradients are its primary
        # use case; rects/lines exercise the fold z-resolution.
        configs += [(6, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1),
                    (6, 2, 0.0, 0.84, 0.0, 1.0, 0.0, 1),
                    (6, 2, 0.0, 1.0, 0.0, 1.0, 0.0, 1),
                    (2, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1),
                    (2, 2, 0.0, 0.84, 0.0, 0.0, 0.0, 1),
                    (1, 2, 0.0, 0.5, 0.0, 1.0, 0.0, 1),
                    (1, 2, 0.0, 0.84, 0.0, 1.0, 0.0, 1),
                    (3, 2, 0.0, 0.84, 0.0, 1.0, 0.0, 1),
                    (5, 2, 1.0, 0.84, 0.0, 1.0, 0.0, 1)]
    failures = asyncio.run(run(configs))
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
