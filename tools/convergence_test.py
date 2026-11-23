#!/usr/bin/env python3
"""convergence_test.py — MEASURE what the depth bake + splat do to each eye.

  depth_sidecar/.venv/bin/python3 tools/convergence_test.py [--thickness PX] [--set key=value ...]
                                                            [--bake key=value ...] [--with-color]

Runs off-air on the hidden "Test Pattern" source of the main instance (:4455) with the
LIVE depth bake and splat settings (plus overrides), and restores that source afterwards.

The pattern is three rows of flat-colour content (so depth is known and simple) carrying
a random vertical-stripe brightness texture. The texture does not change hue or
saturation, so it does not change depth; it only makes every point trackable. Each eye is
matched against the same eye rendered at thickness 0, which gives the horizontal shift of
every point in each eye in px:

  row 1  BANDS   8 flat hues, hard edges        -> 8 depth planes, steps between them
  row 2  RAMP    continuous hue sweep            -> a smooth depth slope
  row 3  FADE    the 8 bands fading to black     -> depth must not change as colour dims

For a correct stereo pair, at every point:
  symmetry   left shift = -right shift                      (|L+R| -> 0, inside flat bands)
  disparity  L-R = thickness * (depth - anchor)             (inside flat bands)
  straight   the shift does not vary down a column          (else edges bend, and they
             bend opposite ways in the two eyes: "mirrored concavity")
  order      points keep their left-to-right order          (else the picture is folded)
  found      the texture is still recognisable in both eyes (information only: band edges
             always hide or stretch some of it; compare the figure between runs)

Prints one line per row and per check, PASS/FAIL against the limits below, and writes
/tmp/convergence_test.png (left eye | right eye, shift maps underneath).
Exit code 0 = all pass.

How far to trust it: 'straight' is robust (it is 0.0 px when nothing bends). Where the
picture is stretched or squeezed by a steep depth slope the 40 px matcher starts to
mismatch, so at high thickness 'symmetry', 'disparity' and 'order' overstate the damage;
use them to compare two settings, not as absolute figures. Strong smoothing also keeps
240 px bands from ever reaching their own depth, which 'disparity' reports as an error.
"""
import argparse, asyncio, base64, io, sys, warnings
from pathlib import Path

import numpy as np
import yaml
from PIL import Image
import simpleobsws

warnings.filterwarnings("ignore", category=RuntimeWarning)   # all-NaN columns are expected
ROOT = Path(__file__).resolve().parent.parent
T, LIVE = "Test Pattern", "Video Capture Device"
PNG, OUT = "/tmp/convergence_pattern.png", "/tmp/convergence_test.png"
W, H = 1920, 1080
ROWS = {"BANDS": (0, 360), "RAMP": (360, 720), "FADE": (720, 1080)}
WIN, STEP_X, STEP_Y, SEARCH = 40, 8, 12, 330

LIM_SYM = 3.0        # px, p95 of |L+R|
LIM_DISP = 4.0       # px, p95 of |measured - expected| inside flat bands
LIM_STRAIGHT = 3.0   # px, p95 over columns of the shift's spread down the column
LIM_FOLD = 0.5       # % of points out of order


def hsv(h, s, v):
    r = np.clip(np.abs(h * 6 - 3) - 1, 0, 1)
    g = np.clip(2 - np.abs(h * 6 - 2), 0, 1)
    b = np.clip(2 - np.abs(h * 6 - 4), 0, 1)
    return v[..., None] * (1 - s + s * np.stack([r, g, b], -1))


def make_pattern():
    rng = np.random.default_rng(7)
    stripe = np.repeat(rng.uniform(0.55, 1.0, W // 6 + 1), 6)[:W]     # brightness texture
    x = (np.arange(W) + 0.5) / W
    band_h = (np.floor(x * 8) + 0.5) / 8
    img = np.zeros((H, W, 3))
    for name, (y0, y1) in ROWS.items():
        n = y1 - y0
        hue = np.tile(x if name == "RAMP" else band_h, (n, 1))
        v = np.tile(stripe, (n, 1))
        if name == "FADE":
            v = v * np.linspace(1.0, 0.0, n)[:, None]
        img[y0:y1] = hsv(hue, 1.0, v)
    Image.fromarray((img * 255).round().astype(np.uint8)).save(PNG)


async def capture(a):
    pw = yaml.safe_load(open(ROOT / "control_bridge/config.yaml"))["obs_instances"]["viture"]["password"]
    ws = simpleobsws.WebSocketClient(url="ws://127.0.0.1:4455", password=pw)
    await ws.connect(); await ws.wait_until_identified()
    c = lambda n, d=None: ws.call(simpleobsws.Request(n, d or {}))
    flist = lambda s: c("GetSourceFilterList", {"sourceName": s})
    live = {f["filterName"]: f for f in (await flist(LIVE)).responseData["filters"]}
    test = {f["filterName"]: f for f in (await flist(T)).responseData["filters"]}
    oldfile = (await c("GetInputSettings", {"inputName": T})).responseData["inputSettings"].get("file", "")
    use = ["depth bake", "stereo splat"] + (["synth color"] if a.with_color else [])
    splat = {**live["stereo splat"]["filterSettings"], **a.set}
    if a.thickness is not None:
        splat["thickness_px"] = a.thickness
    bake = {**live["depth bake"]["filterSettings"], **a.bake, "debug_mode": 0.0}

    async def recreate(name, f, settings, idx):     # a recreate is what loads the shader file from disk
        await c("RemoveSourceFilter", {"sourceName": T, "filterName": name})
        r = await c("CreateSourceFilter", {"sourceName": T, "filterName": name,
                                           "filterKind": f["filterKind"], "filterSettings": settings})
        assert r.ok(), r.requestStatus
        await c("SetSourceFilterIndex", {"sourceName": T, "filterName": name, "filterIndex": idx})

    async def shot(splat_settings, debug):
        await c("SetSourceFilterSettings", {"sourceName": T, "filterName": "stereo splat",
                                            "filterSettings": splat_settings, "overlay": False})
        await c("SetSourceFilterSettings", {"sourceName": T, "filterName": "depth bake",
                                            "filterSettings": {"debug_mode": debug}, "overlay": True})
        for _ in range(4):                          # a hidden source renders late: discard a few
            await asyncio.sleep(0.5)
            await c("GetSourceScreenshot", {"sourceName": T, "imageFormat": "jpg",
                                            "imageWidth": 320, "imageHeight": 90})
        r = await c("GetSourceScreenshot", {"sourceName": T, "imageFormat": "png",
                                            "imageWidth": 2 * W, "imageHeight": H})
        b = base64.b64decode(r.responseData["imageData"].split(",", 1)[1])
        return np.asarray(Image.open(io.BytesIO(b)).convert("RGB")).astype(float) / 255.0

    try:
        await c("SetInputSettings", {"inputName": T, "inputSettings": {"file": PNG}})
        if a.with_color:
            await recreate("synth color", live["synth color"], live["synth color"]["filterSettings"], 0)
        await recreate("depth bake", live["depth bake"], bake, 1)
        for n in test:
            await c("SetSourceFilterEnabled", {"sourceName": T, "filterName": n, "filterEnabled": n in use})
        flat = {**splat, "thickness_px": 0.0}
        base = await shot(flat, 0.0)
        depth = await shot(flat, 1.0)               # bake as grey, undisplaced = depth in eye coordinates
        out = await shot(splat, 0.0)
    finally:
        if a.with_color:
            await recreate("synth color", test["synth color"], test["synth color"]["filterSettings"], 0)
        await recreate("depth bake", test["depth bake"], test["depth bake"]["filterSettings"], 1)
        for n, f in test.items():
            await c("SetSourceFilterSettings", {"sourceName": T, "filterName": n,
                                                "filterSettings": f["filterSettings"], "overlay": False})
            await c("SetSourceFilterEnabled", {"sourceName": T, "filterName": n,
                                               "filterEnabled": f["filterEnabled"]})
        await c("SetInputSettings", {"inputName": T, "inputSettings": {"file": oldfile}})
        await ws.disconnect()
    return base, depth, out, splat, bake


def texture(img):
    """Brightness texture with the slow part (hue bands, fades) divided out."""
    v = img.max(axis=2)
    k = np.ones(48) / 48
    mean = np.apply_along_axis(lambda r: np.convolve(np.pad(r, 24, mode="edge"), k, "same")[24:-24], 1, v)
    return v / np.maximum(mean, 1e-3) - 1.0, v


def track(base_eye, out_eye):
    """Shift of each point of out_eye relative to base_eye. Returns (ys, xs, shift, score)."""
    tb, vb = texture(base_eye)
    to, vo = texture(out_eye)
    ys = np.arange(STEP_Y // 2, H, STEP_Y)
    xs = np.arange(WIN // 2, W - WIN // 2, STEP_X)
    shift = np.full((len(ys), len(xs)), np.nan)
    score = np.zeros((len(ys), len(xs)))
    sw = np.lib.stride_tricks.sliding_window_view
    for i, y in enumerate(ys):
        rb = tb[max(0, y - 2):y + 3].mean(axis=0)
        ro = to[max(0, y - 2):y + 3].mean(axis=0)
        if vb[y].mean() < 0.04:                     # nothing to see on this row
            continue
        B = sw(rb, WIN); B = B - B.mean(1, keepdims=True)
        B = B / np.maximum(np.linalg.norm(B, axis=1, keepdims=True), 1e-6)
        O = sw(ro, WIN)[xs - WIN // 2]; O = O - O.mean(1, keepdims=True)
        O = O / np.maximum(np.linalg.norm(O, axis=1, keepdims=True), 1e-6)
        ncc = O @ B.T                               # [window in out, position in base]
        pos = np.arange(B.shape[0])[None, :] - (xs - WIN // 2)[:, None]
        ncc[np.abs(pos) > SEARCH] = -1
        j = ncc.argmax(1)
        score[i] = ncc[np.arange(len(xs)), j]
        shift[i] = -(j - (xs - WIN // 2))           # out position minus base position
    return ys, xs, shift, score


def analyse(base, depth, out, splat):
    th, anchor = splat["thickness_px"], splat.get("anchor", 1.0)
    res = {}
    eyes = {}
    for e, x0 in (("L", 0), ("R", W)):
        eyes[e] = track(base[:, x0:x0 + W], out[:, x0:x0 + W])
    ys, xs, oL, cL = eyes["L"]
    _, _, oR, cR = eyes["R"]
    # a match further out than any depth can move a point is a false match: the texture
    # there is torn or folded beyond recognition, so it counts as not found
    reach = abs(th) * max(anchor, 1.0 - anchor) / 2.0 + 8.0

    def to_source(o, c):
        """Shifts are measured where a point LANDS; re-index them by where it CAME FROM, so
        the two eyes (and the depth map) are compared at the same point of the picture."""
        s = np.full(o.shape, np.nan)
        for i in range(o.shape[0]):
            with np.errstate(invalid="ignore"):
                ok = (c[i] > 0.6) & (np.abs(o[i]) <= reach)
            if ok.sum() < 4:
                continue
            src = xs[ok] - o[i, ok]
            order = np.argsort(src)
            src, val = src[order], o[i, ok][order]
            v = np.interp(xs, src, val, left=np.nan, right=np.nan)
            gap = np.interp(xs, (src[:-1] + src[1:]) / 2, np.diff(src), left=0, right=0)
            v[gap > 2.5 * STEP_X] = np.nan            # nothing landed from here: hidden or torn
            s[i] = v
        return s

    sL, sR = to_source(oL, cL), to_source(oR, cR)
    good = ~np.isnan(sL) & ~np.isnan(sR)
    d = depth[:, :W, 1]                              # depth where the LEFT base eye shows it
    dwin = np.array([[d[y, x] for x in xs] for y in ys])
    # flat = depth constant over the window and its neighbourhood (inside a band)
    flat = np.array([[np.ptp(d[y, max(0, x - 110):x + 110]) < 0.012 for x in xs] for y in ys])
    failures = 0
    lines = []
    for name, (y0, y1) in ROWS.items():
        # the splat crops a margin: stay clear of row boundaries; FADE: only where still visible
        rows = (ys > y0 + (8 if y0 == 0 else 30)) & (ys < (y1 - 30 if name != "FADE" else y0 + 0.60 * (y1 - y0)))
        visible = ~np.isnan(sL[rows]).all(axis=1)
        g = good[rows]
        L, R = np.where(g, sL[rows], np.nan), np.where(g, sR[rows], np.nan)
        found = 100.0 * g[visible].mean() if visible.any() else 0.0
        fl = flat[rows] & g
        sym = np.percentile(np.abs(L + R)[fl], 95) if fl.any() else 0.0
        exp = th * (dwin[rows] - anchor)
        err = np.abs((L - R) - exp)[fl]
        disp = np.percentile(err, 95) if err.size else 0.0      # RAMP has no flat bands
        with np.errstate(all="ignore"):
            spread = np.nanmax(L, axis=0) - np.nanmin(L, axis=0)   # shift range down each column
            spread_r = np.nanmax(R, axis=0) - np.nanmin(R, axis=0)
        straight = np.nanpercentile(np.concatenate([spread, spread_r]), 95)
        # order: base position (x - shift) must increase with x
        fold = 0.0
        for S in (L, R):
            bp = xs[None, :] - S
            fold += 100.0 * np.nanmean((np.diff(bp, axis=1) < -2)[~np.isnan(np.diff(bp, axis=1))])
        fold /= 2
        checks = [("found", found, None, "%  (band edges always cost some: compare runs)"),
                  ("symmetry", sym, sym <= LIM_SYM, "px"),
                  ("disparity", disp, not (disp > LIM_DISP), "px"),
                  ("straight", straight, straight <= LIM_STRAIGHT, "px"),
                  ("order", fold, fold <= LIM_FOLD, "% folded")]
        for cname, val, ok, unit in checks:
            ok = None if ok is None else bool(ok)
            failures += ok is False
            tag = 'info' if ok is None else 'PASS' if ok else 'FAIL'
            lines.append(f"{tag}  {name:5s} {cname:9s} {val:7.1f} {unit}")
        res[name] = (L, R)
    return failures, lines, (ys, xs, sL, sR, good)


def picture(out, maps):
    ys, xs, sL, sR, good = maps
    def heat(s):
        m = np.where(good, s, np.nan)
        lim = max(8.0, np.nanpercentile(np.abs(m), 99))
        t = np.clip(m / lim, -1, 1)
        rgb = np.stack([np.clip(1 + t, 0, 1), 1 - np.abs(t), np.clip(1 - t, 0, 1)], -1)   # blue - white - red
        rgb[np.isnan(m)] = 0
        return np.asarray(Image.fromarray((rgb * 255).astype(np.uint8)).resize((W // 2, H // 2), Image.NEAREST))
    top = np.asarray(Image.fromarray((out * 255).astype(np.uint8)).resize((W, H // 2)))
    Image.fromarray(np.vstack([top, np.hstack([heat(sL), heat(sR)])])).save(OUT)


def kv(items):
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        out[k] = {"true": True, "false": False}.get(v.lower(), None)
        if out[k] is None:
            out[k] = float(v) if "." in v else int(v)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thickness", type=float, default=None, help="splat thickness_px (default: live value)")
    ap.add_argument("--set", nargs="*", metavar="KEY=VALUE", help="splat setting overrides")
    ap.add_argument("--bake", nargs="*", metavar="KEY=VALUE", help="depth bake setting overrides")
    ap.add_argument("--with-color", action="store_true", help="also run the live synth color filter first")
    a = ap.parse_args()
    a.set, a.bake = kv(a.set), kv(a.bake)
    make_pattern()
    base, depth, out, splat, bake = asyncio.run(capture(a))
    np.savez_compressed("/tmp/convergence_raw.npz", base=base.astype(np.float32),
                        depth=depth.astype(np.float32), out=out.astype(np.float32))
    failures, lines, maps = analyse(base, depth, out, splat)
    print("splat:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in splat.items()
                     if k in ("thickness_px", "plane_px", "anchor", "smooth_sigma", "smooth_follow", "mode", "mesh_step", "fill")})
    print("bake: ", {k: v for k, v in bake.items() if k in ("hue_rotation", "color_weight", "wheel_shape", "dark_hold")})
    print("\n".join(lines))
    picture(out, maps)
    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILED'}   picture: {OUT}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
