#!/usr/bin/env python3
"""
stereo_oracle.py — numpy ground-truth renderer for chromadepth.shader.

Renders the EXACT expected half/full-SBS output for the test patterns in
test_pattern.shader: forward splat + z-buffer (the thing a fragment shader
can only approximate). Conventions (frozen per user spec 2026-06):
  black = infinity (zero disparity) · crossed disparity = pop forward
  squared depth curve f0 = depth^2 * 0.08 · display inset by f0 + edge taper
  reveals = backdrop black, shapes rigid/flat in both eyes (no carve/fill)
  analog border crop PIC_LO/PIC_HI

PATTERNS below is the single source of truth; test_pattern.shader mirrors it
(comment-locked — change both together).
"""
import numpy as np

W, H = 1920, 1080
MAX_PARALLAX = 0.08
PIC_LO, PIC_HI = 0.010, 0.985

# ---- pattern spec (mirrored in test_pattern.shader) ----
RECTS = [  # x0,y0,x1,y1, r,g,b
    (0.15, 0.15, 0.35, 0.45, 0.90, 0.10, 0.10),
    (0.35, 0.30, 0.55, 0.60, 0.10, 0.90, 0.10),
    (0.60, 0.20, 0.80, 0.50, 0.20, 0.30, 0.95),
    (0.55, 0.55, 0.75, 0.85, 0.95, 0.95, 0.10),
    (0.25, 0.60, 0.45, 0.90, 0.95, 0.10, 0.95),
    # edge-adjacent rects: sit AT the left/right frame edges at depth, so the
    # taper/window behavior is part of the signed-off reference
    (0.01, 0.05, 0.14, 0.35, 0.95, 0.55, 0.10),
    (0.86, 0.62, 0.99, 0.95, 0.95, 0.95, 0.95),
]
BARS = [  # y0,y1, r,g,b   (bar 2 is the black-at-infinity invariant bar)
    (0.0, 0.2, 1.00, 1.00, 1.00),
    (0.2, 0.4, 0.50, 0.50, 0.50),
    (0.4, 0.6, 0.00, 0.00, 0.00),
    (0.6, 0.8, 0.90, 0.10, 0.10),
    (0.8, 1.0, 0.10, 0.90, 0.90),
]
LINES = [  # x_center, width_px, r,g,b
    (0.20,  2, 0.95, 0.10, 0.10),
    (0.40,  4, 0.10, 0.95, 0.10),
    (0.60,  8, 0.20, 0.30, 0.95),
    (0.80, 16, 0.95, 0.95, 0.95),
]
BLOBS = [  # cx,cy,r, r,g,b
    (0.30, 0.40, 0.15, 0.95, 0.55, 0.10),
    (0.65, 0.55, 0.20, 0.10, 0.85, 0.90),
    (0.50, 0.75, 0.10, 0.60, 0.20, 0.95),
]


def _soften(img, softness_px):
    """Separable binomial blur ≈ softness_px of edge softening."""
    if softness_px < 1.0:
        return img
    k = np.array([1.0, 2.0, 1.0]) / 4.0
    n = max(1, int(round(softness_px / 2)))
    for _ in range(n):
        for ax in (0, 1):
            img = (np.roll(img, -1, axis=ax) * k[0]
                   + img * k[1] + np.roll(img, 1, axis=ax) * k[2])
    return img


def pattern(scene, noise_amp=0.0, softness_px=2.0, seed=7, backdrop=0.0):
    """Render the mono source pattern, HxWx3 float in [0,1]."""
    yy, xx = np.mgrid[0:H, 0:W]
    u, v = (xx + 0.5) / W, (yy + 0.5) / H
    img = np.full((H, W, 3), backdrop)
    if scene == 0:
        for y0, y1, r, g, b in BARS:
            m = (v >= y0) & (v < y1)
            img[m] = (r, g, b)
    elif scene in (1, 5):
        # Coverage-correct AA: supersampled HARD edges, averaged down, then
        # a post-blur for softness. Touching rects transition DIRECTLY into
        # each other. (Per-rect alpha ramps each fall to zero at their own
        # edge — two abutting rects then dip to near-black along the shared
        # seam: a phantom dark line in the MONO source that the stereo
        # displacement faithfully carries around. User caught it as "why is
        # there a black hairline at a direct yellow→green transition".)
        S = 4
        uu = (np.arange(W * S) + 0.5) / (W * S)
        vv = (np.arange(H * S) + 0.5) / (H * S)
        img_hr = np.full((H * S, W * S, 3), backdrop)
        for x0, y0, x1, y1, r, g, b in RECTS:
            mx = (uu >= x0) & (uu < x1)
            my = (vv >= y0) & (vv < y1)
            img_hr[np.ix_(my, mx)] = (r, g, b)
        img = img_hr.reshape(H, S, W, S, 3).mean(axis=(1, 3))
        img = _soften(img, softness_px)
    elif scene == 2:
        hp = (u * 6.0) % 6.0
        c = np.clip(np.abs((hp[..., None] + np.array([0.0, 4.0, 2.0])) % 6.0 - 3.0) - 1.0, 0, 1)
        gray = np.repeat(u[..., None], 3, axis=2)
        img = np.where((v < 0.5)[..., None], c, gray)
    elif scene == 3:
        for xc, wpx, r, g, b in LINES:
            m = np.abs(u - xc) < (wpx * 0.5 / W)
            img[m] = (r, g, b)
    elif scene == 4:
        for cx, cy, rad, r, g, b in BLOBS:
            d = np.sqrt(((u - cx) * (W / H)) ** 2 + (v - cy) ** 2) / rad * 0  # placeholder
            d = np.sqrt((u - cx) ** 2 + ((v - cy) / (W / H) * (W / H)) ** 2)
            a = np.clip((rad - d) / (3.0 / W * rad * W / 100 + 0.01), 0, 1)
            img = img * (1 - a[..., None]) + a[..., None] * np.array([r, g, b])
    if scene == 5 and noise_amp > 0:
        rng = np.random.default_rng(seed)
        img = np.clip(img + (rng.random((H, W, 3)) - 0.5) * 0.2 * noise_amp, 0, 1)
    return img


# ---- depth model (bit-faithful to chromadepth.shader) ----
def depth_for(img, hue_rotation=0.0, color_weight=1.0):
    cmax = img.max(axis=2)
    cmin = img.min(axis=2)
    delta = cmax - cmin
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    hue = np.zeros_like(cmax)
    m = delta > 0.001
    dm = np.where(m, delta, 1.0)
    hr = ((g - b) / dm) % 6.0
    hg = (b - r) / dm + 2.0
    hb = (r - g) / dm + 4.0
    hue = np.where(m & (cmax == r), hr, np.where(m & (cmax == g), hg, np.where(m, hb, 0.0))) / 6.0
    luma = img @ [0.299, 0.587, 0.114]
    chroma_depth = 0.12 + 0.88 * (0.5 + 0.5 * np.cos(2 * np.pi * (hue + hue_rotation)))
    conf = np.clip((delta - 0.06) / (0.30 - 0.06), 0, 1)
    conf = conf * conf * (3 - 2 * conf)
    return luma ** 3 * (1 - conf * color_weight) + chroma_depth * conf * color_weight


def conf_for(img, color_weight=1.0):
    """Effective chroma confidence — how sure we are this pixel's color IS a
    surface (vs an ambiguous low-chroma transition/AA mix)."""
    delta = img.max(axis=2) - img.min(axis=2)
    conf = np.clip((delta - 0.06) / (0.30 - 0.06), 0, 1)
    return conf * conf * (3 - 2 * conf) * color_weight


def hue_delta(img):
    """Per-pixel hue (0..1) and chroma amplitude, same math as depth_for."""
    cmax = img.max(axis=2)
    cmin = img.min(axis=2)
    delta = cmax - cmin
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    m = delta > 0.001
    dm = np.where(m, delta, 1.0)
    hr = ((g - b) / dm) % 6.0
    hg = (b - r) / dm + 2.0
    hb = (r - g) / dm + 4.0
    hue = np.where(m & (cmax == r), hr,
                   np.where(m & (cmax == g), hg, np.where(m, hb, 0.0))) / 6.0
    return hue, delta


def edge_taper(x, f0):
    # Dead zone (width f0) of ZERO disparity at the view edges — edge
    # content is pixel-identical in both eyes (no partial clipping/flatten
    # mismatch = no strain) — then a smooth ramp to full depth inward.
    d = np.minimum(x - (PIC_LO + f0), (PIC_HI - f0) - x) - f0
    t = np.clip(d / (1.5 * f0 + 0.001), 0, 1)
    return t * t * (3 - 2 * t)


# ---- ground truth render: forward splat + z-buffer ----
def render_eyes(src, depth_slider, hue_rotation=0.0, color_weight=1.0, eye_w=W,
                backdrop=0.0):
    """Return (L, R) eye images, each H x eye_w x 3. Splat at 2x subpixel.

    backdrop = the known uniform background layer behind all content. Holes
    (disocclusions) show this layer — it continues behind every shape. At
    backdrop luma³ depth the layer's own disparity is sub-pixel, so it is
    laid down as a uniform zero-disparity fill. backdrop=0.0 reproduces the
    black-void convention exactly.
    """
    f0 = depth_slider * depth_slider * MAX_PARALLAX
    vis_lo, vis_hi = PIC_LO + f0, PIC_HI - f0
    span = vis_hi - vis_lo
    SS = 4  # subpixel splat density
    xs = (np.arange(int(W * SS)) + 0.5) / (W * SS)          # source x in [0,1]
    keep = (xs >= PIC_LO) & (xs <= PIC_HI)
    xs = xs[keep]
    src_cols = np.clip((xs * W).astype(int), 0, W - 1)
    out = {}
    eyes = {"L": +1.0, "R": -1.0}  # dest = x + sign * D (crossed)
    # vertical crop mapping: output row -> source row
    vy = np.clip(((f0 + (np.arange(H) + 0.5) / H * (1 - 2 * f0)) * H).astype(int), 0, H - 1)
    d_full = depth_for(src, hue_rotation, color_weight)
    # Foreground depth dilation (standard 2D->3D practice): AA/soft edge
    # pixels mix toward gray and would otherwise compute a FAR depth,
    # peeling object edges off as stranded ribbons. Max-filter so edge
    # ramps inherit their object's (nearer) depth.
    # Horizontal-only, radius = the AA ramp width (2 px): wider/2D dilation
    # grabs strips of NEIGHBORING objects' bodies onto the nearer plane —
    # floating hairlines that break fusion at object-object boundaries.
    # Gate (binary — it must snap, not blend; a proportional gate gives
    # transition pixels intermediate depth and they splat into the middle
    # of the disocclusion gap as detached floating debris). A pixel may
    # inherit a nearer neighbor's depth iff:
    #   • it is ambiguous itself (conf < 0.5: gray/dark AA mix toward
    #     background — the white-rect ribbon rescue), OR
    #   • the neighbor is chromatic with the SAME hue — i.e. this pixel is
    #     a dimmer fade of that same surface. (Chroma confidence alone
    #     cannot tell "dim surface" from "dim fade of a bright surface";
    #     hue can. The locked confidence blend gives a dim-red edge fade
    #     depth 0.62 vs its surface's 1.0 — mid-gap debris without this.)
    # Cross-hue inheritance is forbidden: that is dilation stealing the far
    # shape's edge at a shape-shape contact.
    # Taps run both horizontally AND vertically: a top/bottom edge's AA row
    # has its rescuing surface BELOW/ABOVE it, not beside it — horizontal-
    # only taps leave that row at its dim luma³ depth and it splats as a
    # hairline poking out of the shape's corner in one eye. Vertical taps
    # were unsafe with unconditional dilation (they stole neighboring
    # bodies); the gate makes them safe.
    # DONOR-RESTRICTED, full 5×5 window: an ambiguous pixel may only inherit
    # from SOLID pixels (chromatically confident, or bright achromatic body
    # — the white rect). Inheriting from other transition pixels chains a
    # ladder of intermediate depths that belong to no surface: the outer
    # fringe ends up hovering mid-air between body and backdrop (the corner
    # hairline). With solid-only donors a pixel either reaches a real
    # surface depth within the window or keeps its own; the 5×5 reach lets
    # corner fringes (partial in both axes) find their body diagonally.
    # Known limit: dim achromatic solids (mid-gray bodies) don't donate, so
    # their fringes keep luma³ depth — offsets there are a few px at most.
    hue, delta = hue_delta(src)
    conf = conf_for(src, color_weight)
    luma = src @ np.array([0.299, 0.587, 0.114])
    # "Lit" = visibly distinct from the backdrop layer (with a black
    # backdrop this is just max-channel). Backdrop-colored pixels must
    # never lift, whatever the backdrop value — see bite note below.
    bdist = np.abs(src - backdrop).max(axis=2)

    def sh2(a, dy, dx):
        return np.roll(np.roll(a, dy, axis=0), dx, axis=1)

    ALL_SHIFTS = [(dy, dx) for dy in (-2, -1, 0, 1, 2)
                  for dx in (-2, -1, 0, 1, 2) if (dy, dx) != (0, 0)]
    # Hue stability: a CONTACT MIX (blur/AA blend of two touching surfaces)
    # is chroma-confident yet its hue lies between its neighbors' — on the
    # chromadepth wheel that intermediate hue maps to a depth belonging to
    # NEITHER surface, so the mix splats as a detached micro-ledge in the
    # disocclusion gap. Such pixels are detectable: their 5×5 window holds
    # confident pixels of materially different hue. Genuine surfaces, thin
    # lines (uniform hue), and smooth hue ramps (~0.0005 hue/px) all test
    # stable.
    unstable = np.zeros(hue.shape, dtype=bool)
    for dy, dx in ALL_SHIFTS:
        hd = np.abs(hue - sh2(hue, dy, dx))
        hd = np.minimum(hd, 1.0 - hd)
        unstable |= (sh2(delta, dy, dx) > 0.06) & (delta > 0.06) & (hd > 0.06)
    solid = ((conf >= 0.5) | (luma > 0.8)) & ~unstable
    donor_d = np.where(solid, d_full, 0.0)

    # REASSIGNMENT pass: an unstable chromatic pixel is a blend of the two
    # surfaces it sits between — it gets the depth of the hue-NEAREST solid
    # donor (replace, not max: its own wheel depth is meaningless). Hue
    # distances within one 0.02 bucket tie-break to the NEARER donor
    # (foreground assignment, as everywhere else).
    reassign = unstable & (delta > 0.06) & (bdist > 0.04)
    best_score = np.full(hue.shape, np.inf)
    best_d = d_full.copy()
    for dy, dx in ALL_SHIFTS:
        ok = sh2(solid, dy, dx) & (sh2(delta, dy, dx) > 0.06)
        hd = np.abs(hue - sh2(hue, dy, dx))
        hd = np.minimum(hd, 1.0 - hd)
        dn = sh2(d_full, dy, dx)
        score = np.where(ok, np.floor(hd / 0.02) * 10.0 - dn, np.inf)
        better = score < best_score
        best_score = np.where(better, score, best_score)
        best_d = np.where(better, dn, best_d)
    d_full = np.where(reassign & np.isfinite(best_score), best_d, d_full)
    cand = d_full.copy()

    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            if dy == 0 and dx == 0:
                continue
            dn = sh2(donor_d, dy, dx)
            hd = np.abs(hue - sh2(hue, dy, dx))
            hd = np.minimum(hd, 1.0 - hd)
            # 0.17 hue radius: wide enough that a contact-mix pixel (hue
            # midway between two touching surfaces, ~0.10 from each at the
            # closest real pair, yellow/green) attaches to a side instead
            # of floating in the gap at its own intermediate depth; narrow
            # enough that the patterns' distinct adjacent pairs (≥0.20
            # apart) never cross-inherit.
            same_surface = (hd < 0.17) & (sh2(delta, dy, dx) > 0.06)
            # Lift only LIT pixels that are a plausible fade of the donor:
            # same hue (a dim edge of that surface) or achromatic (a gray
            # AA mix / surface-surface blend). Backdrop black NEVER lifts —
            # a black pixel raised to a near depth z-buffers invisibly OVER
            # real content beside the contact and bites chunks out of the
            # neighboring shape's edge. The floor is color distance, NOT
            # luma: a 10% blue fringe is visible at luma 0.04 (blue's luma
            # weight is 0.114) and must still ride with its body.
            ok = (bdist > 0.04) & (same_surface | (delta < 0.06))
            cand = np.maximum(cand, np.where(ok, dn, 0.0))
    d_full = cand
    taper = edge_taper(xs, f0) if f0 > 0 else np.ones_like(xs)
    for eye, sign in eyes.items():
        img = np.full((H, eye_w, 3), backdrop, dtype=float)
        for row in range(H):
            sr = vy[row]
            cols = src_cols
            d = d_full[sr, cols] * taper
            order = np.argsort(d, kind="stable")          # nearest written last
            dest = xs + sign * d * f0
            q = (dest - vis_lo) / span
            px = np.floor(q * eye_w).astype(int)
            ok = (px >= 0) & (px < eye_w)
            o = order[ok[order]]
            img[row, px[o]] = src[sr, cols[o]]
            # Splat holes (disocclusions) stay backdrop black — black IS the
            # infinity plane here. Every shape keeps its rigid silhouette in
            # both eyes: no carve eats the far surface, no fill extends it.
            # The strip between a near edge and the flat far edge in the
            # reveal eye is the backdrop seen through the gap (user-locked
            # 2026-06-10 after carve v8 and da Vinci fill v9 both rejected
            # as deformations of the far shape's edge).
        out[eye] = img
    return out["L"], out["R"]


def render_fullsbs(scene, depth_slider, hue_rotation=0.0, color_weight=1.0,
                   noise_amp=0.0):
    src = pattern(scene, noise_amp)
    L, R = render_eyes(src, depth_slider, hue_rotation, color_weight, eye_w=W)
    return np.concatenate([L, R], axis=1)  # 3840 x 1080 full-SBS


if __name__ == "__main__":
    from PIL import Image
    cases = [
        ("rects_d50", 1, 0.50), ("rects_d100", 1, 1.00),
        ("bars_d50", 0, 0.50), ("ramp_d75", 2, 0.75), ("lines_d100", 3, 1.00),
    ]
    for name, scene, d in cases:
        sbs = render_fullsbs(scene, d)
        Image.fromarray((sbs * 255).astype(np.uint8)).save(f"/tmp/oracle_{name}.png")
        print(f"oracle_{name}.png  scene={scene} depth={d}")
