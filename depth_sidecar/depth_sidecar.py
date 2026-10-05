#!/usr/bin/env python3
"""depth_sidecar: live depth map of the StereoPi camera feed, published
into OBS as a Syphon source.

Transport: OBS owns the Blackmagic capture (its camera extension streams
to OBS but not to plain CLI processes — verified 2026-10). The sidecar
polls source screenshots over obs-websocket (~12 fps), runs MiDaS_small
on MPS over the LEFT eye, EMA-smooths, and publishes 960x1080 grayscale
depth (near = bright) as Syphon server "stereopi-depth". Add a Syphon
Client source in OBS to consume it.

  .venv/bin/python3 depth_sidecar.py                 # run (Ctrl-C stops)
  .venv/bin/python3 depth_sidecar.py --source "StereoPi AV" --fps 12

Long sessions: launch detached (CC background tasks die at session end):
  nohup .venv/bin/python3 depth_sidecar.py > /tmp/depth_sidecar.log 2>&1 &

Upgrade path to 30 fps: pin OBS's virtual camera to the camera scene and
capture that via AVFoundation (virtual cam is readable from CLI, unlike
the BMD extension); the engine and Syphon side stay unchanged.
"""
import argparse, asyncio, base64, io, os, sys, time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, '..', 'control_bridge')


class MidasEngine:
    TRANSFORMS = {  # model -> official transform attribute
        "MiDaS_small": "small_transform",
        "DPT_SwinV2_T_256": "swin256_transform",
        "DPT_Hybrid": "dpt_transform",
    }

    def __init__(self, model_name="MiDaS_small"):
        import torch
        self.torch = torch
        self.device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        print(f"loading {model_name} on {self.device}...", flush=True)
        self.model = torch.hub.load("intel-isl/MiDaS", model_name, trust_repo=True)
        self.model.to(self.device).eval()
        tf = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True)
        self.transform = getattr(tf, self.TRANSFORMS[model_name])
        print("model ready", flush=True)

    def infer(self, frame):
        """full SBS frame -> float32 depth of left eye [0,1], near = 1."""
        t = self.torch
        left = frame[:, :frame.shape[1] // 2]
        img = cv2.cvtColor(left, cv2.COLOR_BGR2RGB)
        x = self.transform(img).to(self.device)
        with t.no_grad():
            d = self.model(x).squeeze().float().cpu().numpy()
        lo, hi = np.percentile(d, 2), np.percentile(d, 98)
        return np.clip((d - lo) / max(hi - lo, 1e-6), 0.0, 1.0).astype(np.float32)


RECT_FILE = os.path.join(HERE, 'rectify.npz')


def calibrate_rectification(frame):
    """Self-rectify from SIFT matches between the eyes: fundamental matrix
    -> stereoRectifyUncalibrated homographies. Returns (H1, H2) or None.
    Works best on a bright, feature-rich scene."""
    half = frame.shape[1] // 2
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    L = clahe.apply(cv2.cvtColor(frame[:, :half], cv2.COLOR_BGR2GRAY))
    R = clahe.apply(cv2.cvtColor(frame[:, half:], cv2.COLOR_BGR2GRAY))
    sift = cv2.SIFT_create(4000)
    kL, dL = sift.detectAndCompute(L, None)
    kR, dR = sift.detectAndCompute(R, None)
    if dL is None or dR is None or len(kL) < 50 or len(kR) < 50:
        print(f"calibrate: too few features ({len(kL) if kL else 0}/{len(kR) if kR else 0})")
        return None
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    raw = matcher.knnMatch(dL, dR, k=2)
    good = [m for m, n in raw if m.distance < 0.7 * n.distance]
    print(f"calibrate: {len(kL)}/{len(kR)} features, {len(good)} ratio-test matches")
    if len(good) < 40:
        return None
    ptsL = np.float32([kL[m.queryIdx].pt for m in good])
    ptsR = np.float32([kR[m.trainIdx].pt for m in good])
    F, mask = cv2.findFundamentalMat(ptsL, ptsR, cv2.FM_RANSAC, 1.0, 0.999)
    if F is None:
        print("calibrate: no fundamental matrix")
        return None
    inl = mask.ravel().astype(bool)
    print(f"calibrate: {inl.sum()} RANSAC inliers")
    if inl.sum() < 30:
        return None
    ok, H1, H2 = cv2.stereoRectifyUncalibrated(
        ptsL[inl], ptsR[inl], F, (half, frame.shape[0]))
    if not ok:
        print("calibrate: rectification failed")
        return None
    # report residual vertical disparity after rectification
    pl = cv2.perspectiveTransform(ptsL[inl][None], H1)[0]
    pr = cv2.perspectiveTransform(ptsR[inl][None], H2)[0]
    dy = np.abs(pl[:, 1] - pr[:, 1])
    print(f"calibrate: residual |dy| median {np.median(dy):.2f}px (was "
          f"{np.median(np.abs(ptsL[inl][:,1]-ptsR[inl][:,1])):.2f}px)")
    # rectified disparity distribution of the matched features — the
    # uncalibrated homographies shift/scale the zero point, so the SGBM
    # search window must be derived from this, not assumed [0, N)
    dxs = pl[:, 0] - pr[:, 0]
    print(f"calibrate: rectified disparity range p2={np.percentile(dxs,2):.1f} "
          f"p50={np.median(dxs):.1f} p98={np.percentile(dxs,98):.1f}")
    return H1, H2, dxs


class SgbmEngine:
    """True stereo matching on the SBS pair, with cached self-rectification
    homographies (run --calibrate on a bright textured scene first), CLAHE
    texture boost, and WLS left/right-consistency filtering."""

    def __init__(self, dx=0.0, dy=0.0, size=None):
        self.dx, self.dy = dx, dy
        self.H1 = self.H2 = None
        min_disp, num_disp = 0, 96
        if os.path.exists(RECT_FILE):
            z = np.load(RECT_FILE)
            if size is None or tuple(z['size']) == tuple(size):
                self.H1, self.H2 = z['H1'], z['H2']
                print(f"rectification loaded from {RECT_FILE}")
                if 'dxs' in z:
                    # search window from the calibration features' actual
                    # rectified disparities, with headroom for nearer objects
                    lo = float(np.percentile(z['dxs'], 1)) - 16
                    hi = float(np.percentile(z['dxs'], 99)) + 48
                    min_disp = int(np.floor(lo))
                    num_disp = int(np.ceil((hi - lo) / 16) * 16)
                    print(f"disparity window [{min_disp}, {min_disp + num_disp})")
            else:
                print(f"rectification size mismatch ({tuple(z['size'])} vs {size}), ignoring")
        if self.H1 is None:
            print(f"no rectification; falling back to trim shift dx={dx} dy={dy}")
        self.clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        bs = 7
        self.left_m = cv2.StereoSGBM_create(
            minDisparity=min_disp, numDisparities=num_disp, blockSize=bs,
            P1=8 * bs * bs, P2=32 * bs * bs,
            uniquenessRatio=5, speckleWindowSize=120, speckleRange=2,
            disp12MaxDiff=2, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.min_disp = min_disp
        self.right_m = cv2.ximgproc.createRightMatcher(self.left_m)
        self.wls = cv2.ximgproc.createDisparityWLSFilter(self.left_m)
        self.wls.setLambda(8000.0)
        self.wls.setSigmaColor(1.2)

    def infer(self, frame):
        h, w = frame.shape[:2]
        half = w // 2
        L = cv2.cvtColor(frame[:, :half], cv2.COLOR_BGR2GRAY)
        R = cv2.cvtColor(frame[:, half:], cv2.COLOR_BGR2GRAY)
        if self.H1 is not None:
            L = cv2.warpPerspective(L, self.H1, (half, h))
            R = cv2.warpPerspective(R, self.H2, (half, h))
        elif self.dx or self.dy:
            M = np.float32([[1, 0, self.dx], [0, 1, self.dy]])
            R = cv2.warpAffine(R, M, (half, h))
        Lc, Rc = self.clahe.apply(L), self.clahe.apply(R)
        dl = self.left_m.compute(Lc, Rc)
        dr = self.right_m.compute(Rc, Lc)
        disp = self.wls.filter(dl, Lc, disparity_map_right=dr).astype(np.float32) / 16.0
        if self.H1 is not None:  # back to the original left-eye framing
            disp = cv2.warpPerspective(disp, self.H1, (half, h),
                                       flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR)
        valid = disp >= self.min_disp  # SGBM marks invalid as min_disp - 1
        if valid.sum() < 500:
            return np.zeros(L.shape, np.float32)
        lo, hi = np.percentile(disp[valid], 2), np.percentile(disp[valid], 98)
        return np.clip((disp - lo) / max(hi - lo, 1e-6), 0.0, 1.0).astype(np.float32)

    def raw_disparity(self, frame):
        """Disparity + confidence in ORIGINAL left-eye framing, for fusion.
        Confidence: valid disparity AND locally textured (gradient energy)."""
        h, w = frame.shape[:2]
        half = w // 2
        L = cv2.cvtColor(frame[:, :half], cv2.COLOR_BGR2GRAY)
        R = cv2.cvtColor(frame[:, half:], cv2.COLOR_BGR2GRAY)
        if self.H1 is not None:
            Lr = cv2.warpPerspective(L, self.H1, (half, h))
            Rr = cv2.warpPerspective(R, self.H2, (half, h))
        else:
            Lr, Rr = L, R
        Lc, Rc = self.clahe.apply(Lr), self.clahe.apply(Rr)
        dl = self.left_m.compute(Lc, Rc)
        dr = self.right_m.compute(Rc, Lc)
        disp = self.wls.filter(dl, Lc, disparity_map_right=dr).astype(np.float32) / 16.0
        gx = cv2.Sobel(Lc, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(Lc, cv2.CV_32F, 0, 1, ksize=3)
        tex = cv2.boxFilter(gx * gx + gy * gy, -1, (9, 9))
        conf = (disp >= self.min_disp) & (tex > np.percentile(tex, 70))
        if self.H1 is not None:
            inv = cv2.WARP_INVERSE_MAP | cv2.INTER_NEAREST
            disp = cv2.warpPerspective(disp, self.H1, (half, h), flags=inv)
            conf = cv2.warpPerspective(conf.astype(np.uint8), self.H1,
                                       (half, h), flags=inv).astype(bool)
            conf &= disp >= self.min_disp
        return disp, conf


class FusedEngine:
    """MiDaS structure anchored to SGBM measurements: robust linear fit
    disp ~ a*mono + b over stereo-confident pixels, applied to the dense
    mono map. Stereo contributes metric truth, mono contributes coverage."""

    def __init__(self, mono, stereo):
        self.mono, self.stereo = mono, stereo
        self.a = self.b = None      # smoothed fit coefficients
        self.lo = self.hi = None    # smoothed normalization bounds
        self.res_ema = None         # smoothed diffused stereo residual
        print("fused engine: mono structure x stereo anchors + local residual")

    def _smooth(self, attr, val, k=0.9):
        old = getattr(self, attr)
        new = val if old is None else k * old + (1 - k) * val
        setattr(self, attr, new)
        return new

    def infer(self, frame):
        m = self.mono.infer(frame)                     # dense, [0,1]
        disp, conf = self.stereo.raw_disparity(frame)  # sparse-confident
        m_full = cv2.resize(m, (disp.shape[1], disp.shape[0]),
                            interpolation=cv2.INTER_LINEAR)
        n = conf.sum()
        if n > 2000:
            x, y = m_full[conf], disp[conf]
            # two rounds of least squares with outlier rejection
            a, b = np.polyfit(x, y, 1)
            r = np.abs(a * x + b - y)
            keep = r < max(np.percentile(r, 80), 0.5)
            if keep.sum() > 500:
                a, b = np.polyfit(x[keep], y[keep], 1)
            if a > 1e-3:  # accept only a sane positive relation
                a = self._smooth('a', float(a))
                b = self._smooth('b', float(b))
                d = a * m_full + b
                # local residual: where stereo confidently disagrees with the
                # fitted mono map, diffuse that correction along image edges
                guide = cv2.cvtColor(frame[:, :frame.shape[1] // 2], cv2.COLOR_BGR2GRAY)
                guide = cv2.createCLAHE(3.0, (8, 8)).apply(guide)
                guide = (guide / 255.0).astype(np.float32)  # scale-matched to eps
                res = np.zeros_like(d)
                res[conf] = disp[conf] - d[conf]
                lim = np.percentile(np.abs(res[conf]), 95) if n else 0
                res = np.clip(res, -lim, lim)
                w = conf.astype(np.float32)
                res_s = cv2.ximgproc.guidedFilter(guide, res, radius=24, eps=1e-3)
                w_s = cv2.ximgproc.guidedFilter(guide, w, radius=24, eps=1e-3)
                res_d = res_s / np.maximum(w_s, 0.15)
                res_d *= np.clip(w_s / 0.15, 0.0, 1.0)  # fade where unsupported
                self.res_ema = res_d if self.res_ema is None else \
                    0.95 * self.res_ema + 0.05 * res_d
                d = d + 0.7 * self.res_ema
                lo = self._smooth('lo', float(np.percentile(d, 2)))
                hi = self._smooth('hi', float(np.percentile(d, 98)))
                return np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1).astype(np.float32)
        if self.a is not None:  # reuse last good fit rather than jumping to raw mono
            d = self.a * m_full + self.b
            return np.clip((d - self.lo) / max(self.hi - self.lo, 1e-6), 0, 1).astype(np.float32)
        return m_full


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="StereoPi AV", help="OBS source holding the SBS camera")
    ap.add_argument("--instance", default="viture", help="obs_instances key in bridge config")
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--ema", type=float, default=0.75,
                    help="temporal smoothing: new = ema*old + (1-ema)*fresh")
    ap.add_argument("--grab-width", type=int, default=960,
                    help="screenshot width pulled from OBS (SBS, both eyes)")
    ap.add_argument("--engine", choices=("midas", "sgbm", "fused"), default="fused")
    ap.add_argument("--model", default="DPT_SwinV2_T_256",
                    choices=("MiDaS_small", "DPT_SwinV2_T_256", "DPT_Hybrid"))
    ap.add_argument("--calibrate", action="store_true",
                    help="grab one frame, compute self-rectification, save, exit")
    args = ap.parse_args()

    import yaml, simpleobsws, syphon
    from syphon.utils.numpy import copy_image_to_mtl_texture
    from syphon.utils.raw import create_mtl_texture

    cfg = yaml.safe_load(open(os.path.join(BRIDGE, 'config.yaml')))
    c = cfg['obs_instances'][args.instance]
    ws = simpleobsws.WebSocketClient(url=f"ws://{c['host']}:{c['port']}", password=c['password'])
    await ws.connect(); await ws.wait_until_identified()
    print(f"[{args.instance}] connected; polling '{args.source}' at {args.fps} fps", flush=True)

    if args.calibrate:
        gw = args.grab_width
        gh = int(gw * 1080 / 1920)
        r = await ws.call(simpleobsws.Request('GetSourceScreenshot',
            {'sourceName': args.source, 'imageFormat': 'png',
             'imageWidth': gw, 'imageHeight': gh}))
        raw = base64.b64decode(r.responseData['imageData'].split(',', 1)[1])
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        res = calibrate_rectification(frame)
        if res:
            np.savez(RECT_FILE, H1=res[0], H2=res[1], size=(gw // 2, gh), dxs=res[2])
            print(f"saved {RECT_FILE}")
        await ws.disconnect()
        return

    if args.engine in ("sgbm", "fused"):
        # read the user's by-eye alignment from the OBS 'sbs align' filter
        # and convert to a right-eye pre-shift (filter units are px of the
        # 1920-wide source; scale to grab size)
        dx = dy = 0.0
        r = await ws.call(simpleobsws.Request('GetSourceFilter',
            {'sourceName': 'Video Capture Device', 'filterName': 'sbs align'}))
        if r.responseData:
            s = r.responseData['filterSettings']
            scale = args.grab_width / 1920.0
            dx = (s.get('right_x', 0.0) - s.get('left_x', 0.0)) * scale
            dy = (s.get('right_y', 0.0) - s.get('left_y', 0.0)) * scale
        engine = SgbmEngine(dx=dx, dy=dy,
                            size=(args.grab_width // 2, int(args.grab_width * 1080 / 1920)))
        if args.engine == "fused":
            engine = FusedEngine(MidasEngine(args.model), engine)
    else:
        engine = MidasEngine(args.model)
    server = syphon.SyphonMetalServer("stereopi-depth")
    W, H = 960, 1080
    tex = create_mtl_texture(server.device, W, H)

    gw = args.grab_width
    gh = int(gw * 1080 / 1920)
    ema = None
    guide_ema = None
    sharp_ema = None
    frames = 0
    t0 = time.time()
    try:
        while True:
            tick = time.time()
            r = await ws.call(simpleobsws.Request('GetSourceScreenshot',
                {'sourceName': args.source, 'imageFormat': 'jpeg',
                 'imageWidth': gw, 'imageHeight': gh, 'imageCompressionQuality': 85}))
            if r.responseData:
                raw = base64.b64decode(r.responseData['imageData'].split(',', 1)[1])
                frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    left = frame[:, :frame.shape[1] // 2]
                    d = engine.infer(frame)
                    n_eng = int(np.isnan(d).sum())
                    if n_eng:
                        print(f"NaN: engine output {n_eng}px — scrubbing", flush=True)
                        d = np.nan_to_num(d, nan=0.0)
                    ema = d if ema is None else args.ema * ema + (1 - args.ema) * d
                    # edge-guided upsample: snap depth boundaries to the
                    # camera image's own edges instead of bilinear mush
                    raw_gray = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY).astype(np.float32)
                    guide = cv2.createCLAHE(3.0, (8, 8)).apply(
                        raw_gray.astype(np.uint8)).astype(np.float32)
                    guide_ema = guide if guide_ema is None else \
                        args.ema * guide_ema + (1 - args.ema) * guide
                    gf = (guide_ema / 255.0).astype(np.float32)  # [0,1] guide:
                    # eps must match the guide's scale or the filter's internal
                    # cov/(var+eps) division produces NaN blocks in flat regions
                    d_up = cv2.resize(ema, (gf.shape[1], gf.shape[0]),
                                      interpolation=cv2.INTER_LINEAR)
                    d_sharp = cv2.ximgproc.guidedFilter(gf, d_up, radius=3, eps=1e-4)
                    n_gf = int(np.isnan(d_sharp).sum())
                    if n_gf:
                        print(f"NaN: after guided filter {n_gf}px — scrubbing", flush=True)
                        d_sharp = np.nan_to_num(d_sharp, nan=0.0)
                    # black = infinity: near-black image regions have no
                    # recoverable depth; gate them smoothly to the far plane
                    # (matches the rig's chromadepth convention and removes
                    # both model noise and swin window blocks out there)
                    dark = np.clip((26.0 - cv2.GaussianBlur(raw_gray, (9, 9), 0)) / 18.0, 0, 1)
                    d_sharp = d_sharp * (1.0 - dark)
                    sharp_ema = d_sharp if sharp_ema is None else \
                        args.ema * sharp_ema + (1 - args.ema) * d_sharp
                    d8 = (np.clip(sharp_ema, 0, 1) * 255).astype(np.uint8)
                    d8 = cv2.resize(d8, (W, H), interpolation=cv2.INTER_LINEAR)
                    d8 = cv2.flip(d8, 0)  # Syphon consumers assume GL bottom-left origin
                    rgba = cv2.cvtColor(d8, cv2.COLOR_GRAY2RGBA)
                    copy_image_to_mtl_texture(rgba, tex)
                    server.publish_frame_texture(tex)
                    frames += 1
                    if os.environ.get('DS_DEBUG') and frames % 20 == 10:
                        dbg = '/tmp/ds_debug'
                        os.makedirs(dbg, exist_ok=True)
                        cv2.imwrite(f'{dbg}/live_frame.png', left)
                        cv2.imwrite(f'{dbg}/live_engine.png',
                                    (np.clip(d, 0, 1) * 255).astype(np.uint8))
                        cv2.imwrite(f'{dbg}/live_ema.png',
                                    (np.clip(ema, 0, 1) * 255).astype(np.uint8))
                        cv2.imwrite(f'{dbg}/live_sharp.png',
                                    (np.clip(sharp_ema, 0, 1) * 255).astype(np.uint8))
                        cv2.imwrite(f'{dbg}/live_d8.png', d8)
                    if frames % 100 == 0:
                        print(f"{frames / (time.time() - t0):.1f} fps avg", flush=True)
            wait = (1.0 / args.fps) - (time.time() - tick)
            if wait > 0:
                await asyncio.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        await ws.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
