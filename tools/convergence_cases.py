#!/usr/bin/env python3
"""convergence_cases.py — the stereo regression suite: one focused case per defect.

  depth_sidecar/.venv/bin/python3 tools/convergence_cases.py [--thickness 120]

THE HABIT: when a stereo defect is seen, it becomes a case here BEFORE it is fixed —
a panel of the test pattern that isolates it (or, if it only shows on real material, the
captured frame + settings as a fixture in test_patterns/fixtures/), a number that
measures it, and a limit. Run the whole suite after every change to the bake, the splat
or their settings logic: a fix for one case must not break another.

Each case is FIXED (must pass: a failure is a REGRESSION and the exit code is 1) or OPEN
(known, not solved yet: reported, does not fail the run; says so when it starts passing).

Runs OFF-AIR on the hidden "Test Pattern" source of main (:4455) with FIXED settings, not
the live ones, so results repeat. Writes test_patterns/cases.png (the on-air test pattern
filter can show the same picture) and /tmp/convergence_cases.png: one row per case, left
eye | right eye, reference setting then tested setting, with the measured number.

Pattern panels (1920x1080). Lit areas carry dark hairlines every 16 px: they do not change
depth and make any stretch, fold or lean visible.

  STEP     blue | red, hard edge            two depth planes
  RAMP     blue -> red sweep                a depth slope
  FADE     red fading to black              depth must not change as colour dims
  STRIPES  stripes cut to black at          black beside a lit stripe must not drag it
           different heights
  BARS     red bars on black                bar width must be the same in both eyes
  FRINGE   violet bars whose soft edge      one object: its fringe must be on the same
           runs pink > red > brown > black  side of the bar in both eyes
  RULERS   tick rulers, edge to edge        both eyes must show the same ticks at the
                                            left and right frame edges
"""
import argparse, asyncio, base64, json, sys
from pathlib import Path

import cv2
import numpy as np
import yaml
import simpleobsws

ROOT = Path(__file__).resolve().parent.parent
PNG = ROOT / "test_patterns" / "cases.png"
OUT = "/tmp/convergence_cases.png"
T, LIVE = "Test Pattern", "Video Capture Device"
W, H = 1920, 1080
PIC_LO, PIC_HI, INSET_PX = 0.010, 0.985, 60.0
NEAR, FAR = 0.02, 0.55                     # hues: red is near, blue is far on the cosine wheel at rotation 0
COLS = [(100, 620), (700, 1220), (1300, 1820)]
ROWS = [(130, 520), (570, 960)]
PANEL = {"STEP": (0, 0), "RAMP": (1, 0), "FADE": (2, 0), "BARS": (0, 1), "FRINGE": (1, 1), "STRIPES": (2, 1)}
FIX = ROOT / "test_patterns" / "fixtures"
RULERS = [(40, 90), (990, 1040)]


def box(name):
    c, r = PANEL[name]
    return COLS[c][0], ROWS[r][0], COLS[c][1], ROWS[r][1]


def hsv(h, s, v):
    h = np.asarray(h, float) % 1.0
    r = np.clip(np.abs(h * 6 - 3) - 1, 0, 1)
    g = np.clip(2 - np.abs(h * 6 - 2), 0, 1)
    b = np.clip(2 - np.abs(h * 6 - 4), 0, 1)
    v = np.asarray(v, float)
    return v[..., None] * (1 - s + s * np.stack([r, g, b], -1))


def make_pattern():
    img = np.zeros((H, W, 3))
    X, Y = np.meshgrid(np.arange(W, dtype=float), np.arange(H, dtype=float))
    hair = np.where((X % 16) < 3, 0.55, 1.0)

    def put(name, rgb):
        x0, y0, x1, y1 = box(name)
        img[y0:y1, x0:x1] = rgb[y0:y1, x0:x1]

    x0, y0, x1, y1 = box("STEP")
    put("STEP", hsv(np.where(X < (x0 + x1) / 2, FAR, NEAR), 1.0, hair))
    x0, y0, x1, y1 = box("RAMP")
    put("RAMP", hsv(FAR + (NEAR - FAR) * np.clip((X - x0) / (x1 - x0), 0, 1), 1.0, hair))
    x0, y0, x1, y1 = box("FADE")
    fade = np.clip(1.0 - (Y - y0) / (y1 - y0), 0, 1)
    put("FADE", hsv(np.full_like(X, NEAR), 1.0, hair * fade))
    x0, y0, x1, y1 = box("STRIPES")
    rng = np.random.default_rng(7)
    stripe = np.repeat(rng.uniform(0.35, 1.0, W // 8 + 1), 8)[:W][None, :] * np.ones((H, 1))
    # each stripe is CUT to black where it gets dim (what a black-level crush does to real
    # material), so neighbouring stripes go black at different heights
    sv = stripe * np.clip(1.0 - (Y - y0) / (y1 - y0), 0, 1)
    put("STRIPES", hsv(np.full_like(X, NEAR), 1.0, np.where(sv > 0.22, sv, 0.0)))
    x0, y0, x1, y1 = box("BARS")
    bars = ((X >= x0 + 40) & (X < x0 + 200)) | ((X >= x0 + 280) & (X < x0 + 330)) | ((X >= x0 + 420) & (X < x0 + 436))
    put("BARS", hsv(np.full_like(X, NEAR), 1.0, hair * bars))
    # FRINGE: one soft bar whose colour runs violet (core) > pink > red > brown > black
    # toward both edges, as the synth's bars do. The hue sweep crosses red, where the
    # sawtooth wheel has its near/far seam.
    x0, y0, x1, y1 = box("FRINGE")
    rgb = np.zeros((H, W, 3))
    for cx in (x0 + 140, x0 + 380):
        t = np.clip(np.abs(X - cx) / 95.0, 0, 1)                 # 0 core .. 1 outer edge
        hue = 0.76 + 0.34 * t                                     # 0.76 violet .. 1.10 = 0.10 brown
        v = np.where(t < 0.45, 1.0, np.clip(1.0 - (t - 0.45) / 0.55, 0, 1) * 0.75 + 0.0) * (t < 1)
        rgb += hsv(hue, 0.62, v)
    put("FRINGE", np.clip(rgb, 0, 1))
    # rulers: a tick every 20 px, taller every 100, tallest every 500, counted from the left
    for (ry0, ry1) in RULERS:
        hgt = ry1 - ry0
        k = np.floor(X / 20)
        tall = np.where(k % 25 == 0, 1.0, np.where(k % 5 == 0, 0.7, 0.4))
        lit = ((X % 20) < 6) & ((Y - ry0) < tall * hgt) & (Y >= ry0) & (Y < ry1)
        img[lit] = hsv(np.array(0.33), 1.0, np.array(0.9))
    PNG.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(PNG), (np.clip(img, 0, 1)[..., ::-1] * 255).round().astype(np.uint8))


def to_eye(x, y):
    """source px -> eye px (the splat crops a fixed margin and rescales)."""
    r = INSET_PX / 1920.0
    fi = r * (PIC_HI - PIC_LO) / (1 + 2 * r)
    xe = (x / W - (PIC_LO + fi)) / ((PIC_HI - PIC_LO) - 2 * fi) * W
    ye = (y / H - fi) / (1 - 2 * fi) * H
    return int(round(xe)), int(round(ye))


async def render(shots):
    """shots: {name: dict(image=path, splat={...}, bake={...}, color=None|{...})} -> {name: BGR image}.
    Settings are complete (not overlaid on whatever the source had), so a run repeats."""
    pw = yaml.safe_load(open(ROOT / "control_bridge/config.yaml"))["obs_instances"]["viture"]["password"]
    ws = simpleobsws.WebSocketClient(url="ws://127.0.0.1:4455", password=pw)
    await ws.connect(); await ws.wait_until_identified()
    c = lambda n, d=None: ws.call(simpleobsws.Request(n, d or {}))
    fl = lambda s: c("GetSourceFilterList", {"sourceName": s})
    live = {f["filterName"]: f for f in (await fl(LIVE)).responseData["filters"]}
    test = {f["filterName"]: f for f in (await fl(T)).responseData["filters"]}
    oldfile = (await c("GetInputSettings", {"inputName": T})).responseData["inputSettings"].get("file", "")

    async def recreate(name, kind, settings, idx):      # a recreate is what loads the shader file from disk
        await c("RemoveSourceFilter", {"sourceName": T, "filterName": name})
        r = await c("CreateSourceFilter", {"sourceName": T, "filterName": name, "filterKind": kind, "filterSettings": settings})
        assert r.ok(), r.requestStatus
        await c("SetSourceFilterIndex", {"sourceName": T, "filterName": name, "filterIndex": idx})

    def shader(name):                                   # the live filter's shader file, none of its values
        st = live[name]["filterSettings"]
        return {k: st[k] for k in ("shader_file_name", "from_file") if k in st}

    out, cur_image, cur_color = {}, None, "unset"
    try:
        await recreate("depth bake", live["depth bake"]["filterKind"], shader("depth bake"), 1)
        for n in test:
            await c("SetSourceFilterEnabled", {"sourceName": T, "filterName": n, "filterEnabled": n in ("depth bake", "stereo splat")})
        for name, sh in shots.items():
            if sh["image"] != cur_image:
                await c("SetInputSettings", {"inputName": T, "inputSettings": {"file": str(sh["image"])}})
                cur_image = sh["image"]
            col = sh.get("color")
            if col != cur_color:
                if col:
                    await recreate("synth color", live["synth color"]["filterKind"], {**shader("synth color"), **col}, 0)
                await c("SetSourceFilterEnabled", {"sourceName": T, "filterName": "synth color", "filterEnabled": bool(col)})
                cur_color = col
            await c("SetSourceFilterSettings", {"sourceName": T, "filterName": "stereo splat", "filterSettings": sh["splat"], "overlay": False})
            await c("SetSourceFilterSettings", {"sourceName": T, "filterName": "depth bake",
                                                "filterSettings": {**shader("depth bake"), **sh["bake"]}, "overlay": False})
            for _ in range(4):                          # a hidden source renders late: discard a few
                await asyncio.sleep(0.5)
                await c("GetSourceScreenshot", {"sourceName": T, "imageFormat": "jpg", "imageWidth": 320, "imageHeight": 90})
            r = await c("GetSourceScreenshot", {"sourceName": T, "imageFormat": "png", "imageWidth": 2 * W, "imageHeight": H})
            buf = np.frombuffer(base64.b64decode(r.responseData["imageData"].split(",", 1)[1]), np.uint8)
            out[name] = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    finally:
        for n in ("synth color", "depth bake"):
            await recreate(n, test[n]["filterKind"], test[n]["filterSettings"], 0 if n == "synth color" else 1)
        for n, f in test.items():
            await c("SetSourceFilterSettings", {"sourceName": T, "filterName": n, "filterSettings": f["filterSettings"], "overlay": False})
            await c("SetSourceFilterEnabled", {"sourceName": T, "filterName": n, "filterEnabled": f["filterEnabled"]})
        await c("SetInputSettings", {"inputName": T, "inputSettings": {"file": oldfile}})
        await ws.disconnect()
    return out


# ---- measurements on one eye's crop (BGR uint8) ----
def val(crop):
    return crop.max(axis=2).astype(float) / 255.0


def hairlines(crop):
    """Number of dark hairlines across the middle of the crop."""
    v = val(crop)[crop.shape[0] // 2 - 6: crop.shape[0] // 2 + 6].mean(0)
    k = np.convolve(v, np.ones(15) / 15, "same")
    dip = (v < k * 0.9) & (k > 0.1)
    return int((np.diff(dip.astype(int)) == 1).sum())


def lean(crop, top=0.08, bottom=0.62):
    """Sideways drift (px) of the vertical texture between a row near the top and a dim row lower down."""
    v = val(crop)
    def prof(fr):
        y = int(fr * crop.shape[0]); p = v[y - 4:y + 5].mean(0)
        p = p - np.convolve(p, np.ones(41) / 41, "same")
        return p / (np.linalg.norm(p) + 1e-9)
    a, b = prof(top), prof(bottom)
    best = max(range(-90, 91), key=lambda s: float(np.dot(a[100:-100], np.roll(b, -s)[100:-100])))
    return best


def bar_widths(crop):
    lit = val(crop)[crop.shape[0] // 2 - 6: crop.shape[0] // 2 + 6].mean(0) > 0.12
    # close the hairline gaps
    lit = np.convolve(lit.astype(float), np.ones(9), "same") > 0
    ch = np.flatnonzero(np.diff(np.r_[0, lit.astype(int), 0]))
    return [int(b - a) for a, b in zip(ch[::2], ch[1::2]) if b - a > 6]


def holes(crop):
    """% of the lit span, across the middle rows, that is black: tears in the picture."""
    v = val(crop)[crop.shape[0] // 2 - 40: crop.shape[0] // 2 + 40]
    lit = np.flatnonzero(v.mean(0) > 0.08)
    if lit.size < 20:
        return 100.0
    return 100.0 * float((v[:, lit[0]:lit[-1] + 1] < 0.10).mean())


def fringe_side(crop):
    """Where the brown/olive fringe sits relative to the violet/pink bar cores:
    -1 = all of it right of the bars, +1 = all left, 0 = equal on both sides."""
    hsvc = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).astype(float)
    hue, sat, v = hsvc[..., 0] * 2, hsvc[..., 1], hsvc[..., 2]
    core = (hue > 255) & (hue < 345) & (v > 110)
    # the fringe is the dim red / brown / olive part: hue from just past pink round to green-yellow
    fringe = np.where(((hue < 110) | (hue > 345)) & (v > 25) & (v < 215) & (sat > 40) & ~core, v, 0.0)
    left = right = 0.0
    for y in range(crop.shape[0] // 6, crop.shape[0] * 5 // 6, 8):
        ch = np.flatnonzero(np.diff(np.r_[0, core[y].astype(int), 0]))
        for a, b in zip(ch[::2], ch[1::2]):
            if b - a < 10:
                continue
            left += fringe[y, max(0, a - 110):a].sum(); right += fringe[y, b:b + 110].sum()
    return (left - right) / (left + right) if left + right > 0 else 0.0


def black_edges(eye):
    lit = eye.max(axis=2).mean(0) > 6
    return int(np.argmax(lit)), int(np.argmax(lit[::-1]))


def cell(img, title, sub):
    pad = cv2.copyMakeBorder(img, 44, 4, 4, 4, cv2.BORDER_CONSTANT, value=(40, 40, 40))
    cv2.putText(pad, title, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(pad, sub, (8, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 220, 255), 1, cv2.LINE_AA)
    return pad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thickness", type=float, default=120.0)
    ap.add_argument("--pattern-only", action="store_true", help="only (re)write test_patterns/cases.png")
    a = ap.parse_args()
    make_pattern()
    print("pattern:", PNG)
    if a.pattern_only:
        return
    th = a.thickness
    SPLAT = dict(budget_px=0.0, thickness_px=th, plane_px=0.0, anchor=0.5, vignette_pct=0.0, window=True,
                 frame_inset_px=INSET_PX, smooth_sigma=0.0, smooth_follow=6.0, smooth_lit=True, mode=0,
                 fill=1, mesh_step=4, mlaa=True, ssaa=False, sync=True, perspective_px=0.0, debug=0)
    BAKE = dict(hue_rotation=0.0, color_weight=1.0, wheel_shape=0.0, dark_hold=1.0, debug_mode=0.0)

    def shot(splat=None, bake=None):
        return dict(image=PNG, splat={**SPLAT, **(splat or {})}, bake={**BAKE, **(bake or {})}, color=None)

    shots = {
        "on": shot(),
        "follow0": shot({"smooth_follow": 0.0}),
        "dark0": shot(bake={"dark_hold": 0.0}),
        "lit0": shot({"smooth_lit": False}),
        "saw": shot(bake={"wheel_shape": 1.0}),
        "front": shot({"plane_px": th / 2, "vignette_pct": 5.5}),
        "back": shot({"plane_px": -th / 2, "vignette_pct": 5.5}),
        "anchor0": shot({"plane_px": th / 2, "vignette_pct": 5.5, "anchor": 0.0}),
    }
    fixtures = sorted(FIX.glob("*.json"))
    for fj in fixtures:                                  # real frames, with the settings they were seen at
        st = json.loads(fj.read_text())
        clean = lambda d: {k: v for k, v in d.items() if k not in ("shader_file_name", "from_file")}
        shots["fxcos:" + fj.stem] = dict(image=fj.with_suffix(".png"), splat={**SPLAT, **clean(st["stereo splat"]), "sync": True},
                                         bake={**BAKE, **clean(st["depth bake"]), "wheel_shape": 0.0}, color=clean(st["synth color"]))
        shots["fx:" + fj.stem] = dict(image=fj.with_suffix(".png"), splat={**SPLAT, **clean(st["stereo splat"]), "sync": True},
                                      bake={**BAKE, **clean(st["depth bake"])}, color=clean(st["synth color"]))
    img = asyncio.run(render(shots))

    def crop(variant, name, eye, margin=70):
        x0, y0, x1, y1 = box(name)
        ex0, ey0 = to_eye(x0 - margin, y0); ex1, ey1 = to_eye(x1 + margin, y1)
        return img[variant][max(0, ey0):ey1, eye * W + max(0, ex0): eye * W + min(W, ex1)]

    rows, report, regress = [], [], 0

    def judge(status, ok):
        nonlocal regress
        if status == "fixed":
            regress += not ok
            return "PASS" if ok else "REGRESSION"
        return "OPEN (now passing: mark it fixed)" if ok else "OPEN"

    def case(title, status, name, ref, tested, metric, fmt, passes, good):
        """ref = (label, shot) shown for comparison; tested = (label, shot) the verdict is about."""
        cells, verdict = [], ""
        for which, (label, variant) in (("ref", ref), ("test", tested)):
            L, R = crop(variant, name, 0), crop(variant, name, 1)
            mL, mR = metric(L), metric(R)
            txt = fmt(mL, mR)
            if which == "test":
                verdict = judge(status, bool(passes(mL, mR)))
                report.append(f"{verdict:12.12s} {title:30s} {label:22s} {txt}")
            else:
                report.append(f"{'  (compare)':12s} {title:30s} {label:22s} {txt}")
            cells.append(cell(np.hstack([L, np.full((L.shape[0], 6, 3), 90, np.uint8), R]),
                              f"{title}  [{label}]  L | R" + (f"   {verdict}" if which == "test" else ""), f"{txt}    good: {good}"))
        rows.append(np.hstack(cells))

    near = lambda l, r, tol: abs(l) <= tol and abs(r) <= tol
    case("1 STEP: tears at a depth step", "open", "STEP", ("follow 0", "follow0"), ("follow 6", "on"), holes,
         lambda l, r: f"black inside  L {l:.1f} %  R {r:.1f} %", lambda l, r: l < 1.0 and r < 1.0, "0 %")
    case("2 FADE: lean as colour dims", "fixed", "FADE", ("dark hold 0", "dark0"), ("dark hold 1", "on"), lean,
         lambda l, r: f"lean  L {l:+d}  R {r:+d} px", lambda l, r: near(l, r, 3), "0 / 0")
    case("3 STRIPES: black drags lit", "fixed", "STRIPES", ("ignore black off", "lit0"), ("ignore black on", "on"), lean,
         lambda l, r: f"lean  L {l:+d}  R {r:+d} px", lambda l, r: near(l, r, 3), "0 / 0")
    case("4 BARS: width differs per eye", "fixed", "BARS", ("ignore black off", "lit0"), ("ignore black on", "on"), bar_widths,
         lambda l, r: f"widths  L {l}  R {r}", lambda l, r: len(l) == len(r) and all(abs(x - y) <= 2 for x, y in zip(l, r)), "L = R")
    case("5 FRINGE, cosine wheel", "fixed", "FRINGE", ("follow 0", "follow0"), ("follow 6", "on"), fringe_side,
         lambda l, r: f"fringe side  L {l:+.2f}  R {r:+.2f}", lambda l, r: abs(l - r) < 0.25, "L = R (same side in both eyes)")
    case("6 FRINGE, sawtooth wheel", "open", "FRINGE", ("cosine wheel", "on"), ("sawtooth wheel", "saw"), fringe_side,
         lambda l, r: f"fringe side  L {l:+.2f}  R {r:+.2f}", lambda l, r: abs(l - r) < 0.25, "L = R (same side in both eyes)")

    # 7 frame edges: the top ruler at the left and right ends of each eye
    cells = []
    for label, variant in ((f"plane {th / 2:+.0f}", "front"), (f"plane {-th / 2:+.0f}", "back"), (f"plane {th / 2:+.0f} anchor 0", "anchor0")):
        L, R = img[variant][:, :W], img[variant][:, W:]
        ey0 = max(0, to_eye(0, RULERS[0][0] - 10)[1]); ey1 = to_eye(0, RULERS[0][1] + 60)[1]
        ends = [np.hstack([e[ey0:ey1, :330], np.full((ey1 - ey0, 6, 3), 90, np.uint8), e[ey0:ey1, -330:]]) for e in (L, R)]
        bl, br = black_edges(L), black_edges(R)
        ok = abs(bl[0] - br[1]) <= 2 and abs(bl[1] - br[0]) <= 2 and abs(max(bl) - th / 2) <= 3
        verdict = judge("fixed", ok)
        txt = f"black  L {bl[0]}/{bl[1]}  R {br[0]}/{br[1]} px (left/right)"
        report.append(f"{verdict:12.12s} {'7 RULERS: frame edges match':30s} {label:22s} {txt}")
        cells.append(cell(np.vstack([ends[0], np.full((6, ends[0].shape[1], 3), 90, np.uint8), ends[1]]),
                          f"7 RULERS [{label}]  L above R; left end | right end   {verdict}",
                          f"{txt}    good: mirrored, = plane"))
    rows.append(np.hstack(cells))

    # fixtures: real frames, measured over the whole picture; shown as seen, and with the
    # cosine wheel for comparison
    for fj in fixtures:
        cells = []
        for label, key, status in (("same frame, cosine wheel", "fxcos:", None), ("as seen live", "fx:", "open")):
            full = img[key + fj.stem]
            L, R = full[:, :W], full[:, W:]
            sL, sR = fringe_side(L), fringe_side(R)
            verdict = judge(status, abs(sL - sR) < 0.25) if status else "  (compare)"
            txt = f"fringe side  L {sL:+.2f}  R {sR:+.2f}"
            report.append(f"{verdict:12.12s} {'F ' + fj.stem:30.30s} {label:22s} {txt}")
            small = [cv2.resize(e, (720, 405)) for e in (L, R)]
            cells.append(cell(np.hstack([small[0], np.full((405, 6, 3), 90, np.uint8), small[1]]),
                              f"F {fj.stem} [{label}]  L | R   {verdict.strip()}", f"{txt}    good: L = R"))
        rows.append(np.hstack(cells))

    width = max(r.shape[1] for r in rows)
    sheet = np.vstack([cv2.copyMakeBorder(r, 0, 8, 0, width - r.shape[1], cv2.BORDER_CONSTANT, value=(0, 0, 0)) for r in rows])
    cv2.imwrite(OUT, sheet)
    print(f"thickness {th:g} px, plane 0, anchor 0.5, cosine wheel, dark hold 1 unless stated")
    print("\n".join(report))
    print(f"sheet: {OUT}")
    print("ALL FIXED CASES PASS" if not regress else f"{regress} REGRESSION(S)")
    sys.exit(1 if regress else 0)


if __name__ == "__main__":
    main()
