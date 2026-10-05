#!/usr/bin/env python3
"""eval_depth: objective quality metrics for the live depth map, judged
against the live camera image. Static scene assumed.

  .venv/bin/python3 eval_depth.py [label]

Metrics (direction of better):
  edge_align  depth edges that coincide with image edges / all depth edges  (up)
  edge_steep  95th pct depth gradient at image edges — crispness            (up)
  bg_noise    depth laplacian energy where the image is featureless        (down)
  temporal    mean |depth(t+1.5s) - depth(t)| on a static scene            (down)
  range       occupied depth range p98-p2                                   (context)
"""
import asyncio, base64, io, sys, time, os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'control_bridge'))
import yaml, simpleobsws


async def grab(ws, name, width=960):
    r = await ws.call(simpleobsws.Request('GetSourceScreenshot',
        {'sourceName': name, 'imageFormat': 'png', 'imageWidth': width}))
    raw = base64.b64decode(r.responseData['imageData'].split(',', 1)[1])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


async def main():
    label = sys.argv[1] if len(sys.argv) > 1 else ''
    import subprocess
    if subprocess.run(['pgrep', '-f', 'depth_sidecar.py'],
                      capture_output=True).returncode != 0:
        print(f"{label:28s} SKIPPED: sidecar not running (would measure a frozen frame)")
        return
    cfg = yaml.safe_load(open(os.path.join(HERE, '..', 'control_bridge', 'config.yaml')))
    c = cfg['obs_instances']['viture']
    ws = simpleobsws.WebSocketClient(url=f"ws://{c['host']}:{c['port']}", password=c['password'])
    await ws.connect(); await ws.wait_until_identified()

    cam = await grab(ws, 'StereoPi AV', 1920)
    left = cv2.cvtColor(cam[:, :960], cv2.COLOR_BGR2GRAY).astype(np.float32)
    ds = []
    for _ in range(4):
        ds.append(cv2.cvtColor(await grab(ws, 'StereoPi Depth', 960),
                               cv2.COLOR_BGR2GRAY).astype(np.float32) / 255)
        await asyncio.sleep(1.2)
    await ws.disconnect()
    d1 = ds[0]
    tdiffs = [float(np.abs(ds[i+1] - ds[i]).mean() * 1000) for i in range(3)]

    left = cv2.resize(left, (d1.shape[1], d1.shape[0]))
    # CLAHE so dim image edges count
    lg = cv2.createCLAHE(3.0, (8, 8)).apply(left.astype(np.uint8)).astype(np.float32)
    ig = cv2.magnitude(cv2.Sobel(lg, cv2.CV_32F, 1, 0), cv2.Sobel(lg, cv2.CV_32F, 0, 1))
    dg = cv2.magnitude(cv2.Sobel(d1, cv2.CV_32F, 1, 0), cv2.Sobel(d1, cv2.CV_32F, 0, 1))

    img_edge = ig > np.percentile(ig, 90)
    img_edge_d = cv2.dilate(img_edge.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    dep_edge = dg > np.percentile(dg, 90)

    edge_align = (dep_edge & img_edge_d).sum() / max(dep_edge.sum(), 1)
    edge_steep = float(np.percentile(dg[img_edge], 95))
    flat = ig <= np.percentile(ig, 50)
    lap = cv2.Laplacian(d1, cv2.CV_32F)
    bg_noise = float(np.abs(lap[flat]).mean() * 1000)
    temporal = float(np.median(tdiffs))
    rng = float(np.percentile(d1, 98) - np.percentile(d1, 2))

    print(f"{label:28s} edge_align={edge_align:.3f}  edge_steep={edge_steep:.3f}  "
          f"bg_noise={bg_noise:.2f}  temporal={temporal:.2f}  range={rng:.2f}")

asyncio.run(main())
