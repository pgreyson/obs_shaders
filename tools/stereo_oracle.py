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
    elif scene == 6:
        # SYNTH-LIKE GRADIENTS: multi-scale plasma — smoothly varying hue,
        # saturation and luma with both gentle and steep gradient regions
        # and NO hard edges. Real synth output is curved oscillating
        # gradients everywhere; the linear ramps of scene 2 only test one
        # constant gradient magnitude.
        a = np.sin(2 * np.pi * (u * 1.7 + 0.35 * np.sin(2 * np.pi * v * 1.3)))
        b = np.sin(2 * np.pi * (v * 2.3 + 0.50 * np.sin(2 * np.pi * u * 0.7)))
        c = np.sin(2 * np.pi * (u * 0.9 + v * 1.1) +
                   2.0 * np.sin(2 * np.pi * (u * 0.4 - v * 0.6)))
        img = np.stack([0.5 + 0.5 * a,
                        0.5 + 0.5 * (0.6 * b + 0.4 * c),
                        0.5 + 0.5 * c], axis=2)
        # push some regions toward dark so the luma^3 baseline and the
        # lit/backdrop gates see realistic low-end content
        env = 0.25 + 0.75 * (0.5 + 0.5 * np.sin(2 * np.pi * (u * 0.5 + v * 0.3)))
        img = np.clip(img * env[..., None], 0, 1)
    if scene == 5 and noise_amp > 0:
        rng = np.random.default_rng(seed)
        img = np.clip(img + (rng.random((H, W, 3)) - 0.5) * 0.2 * noise_amp, 0, 1)
    return img


def field_source(src):
    """Depth-source prefilter: one 3×3 binomial ([1,2,1] both axes). The
    FIELD pipeline (depth, hue, confidence, gates, lit floor) reads this;
    displayed colors stay the original. Analog noise (±0.1) attenuates ~4×
    — below the confidence deadband — so per-pixel gate flicker cannot
    become displacement speckle at high depth. Clean hard edges become 2 px
    soft ramps, which is exactly the regime the donor/reassignment rules
    were built for."""
    k = np.array([1.0, 2.0, 1.0]) / 4.0
    out = src
    for ax in (0, 1):
        out = (np.roll(out, -1, axis=ax) * k[0] + out * k[1]
               + np.roll(out, 1, axis=ax) * k[2])
    return out


def field_smooth(d, field_sigma, range_sigma=0.0):
    """REGIONAL-DEPTH smoothing (user-selected convention 2026-06-10 for
    analog/textured content): Gaussian-smooth the FINAL depth field — after
    dilation/lift/reassignment, before taper/splat. Depth cliffs become
    ramps wider than the disocclusion gap they would open, so textured
    content stops punching 1-6px black pepper holes and sloped contacts
    stop tearing into staircase zippers (both measured on a real captured
    frame; pure-oracle renders showed them, so this is a convention fix,
    not a shader bug fix).

    Smoothing DEPTH (not the source colors) keeps every σ=0 rule exactly
    as signed off, never drags blended hues through the depth wheel, and
    measured 3.7× fewer residual black holes than source-side blur at σ=4
    on the real frame. σ is continuous: 0 = rigid-shape sign-off behavior
    for graphic content, ≈4 = the regional look the user picked.

    Kernel: separable 21-tap discrete Gaussian, σ_eff = sqrt(round(2σ²)/2)
    (quantized so tiny σ snaps to off), radius R=10, EDGE-CLAMPED (not
    wrapped). This is BIT-THE-SAME construction as chromadepth_smooth.
    shader's two passes — change both together.

    8-BIT CHAIN MODEL: on the rig the field rides the alpha channel
    (a = 0.5 + d/2) through RGBA8 filter hops — quantized to 1/255 after
    the bake, after the h pass, and after the v pass. A smoothed field is
    ramps EVERYWHERE, so float-vs-8-bit staircase dust lands on every
    silhouette (same class as the oracle-source PNG quantization fix).
    Model the hops exactly: encode, quantize, blur h, quantize, blur v,
    quantize, decode — shader pass order (h then v) matters once
    quantization sits between the passes."""
    R = 10
    n = int(round(2.0 * field_sigma * field_sigma))
    if n <= 0:
        return d
    sig2 = n * 0.5
    i = np.arange(-R, R + 1, dtype=float)
    w = np.exp(-(i * i) / (2.0 * sig2))
    w /= w.sum()

    def q8(x):
        return np.round(np.clip(x, 0.0, 1.0) * 255.0) / 255.0

    e = q8(0.5 + 0.5 * np.clip(d, 0.0, 1.0))      # bake's encoded alpha
    for ax in (1, 0):                              # shader order: h then v
        pad = [(R, R) if a == ax else (0, 0) for a in (0, 1)]
        p = np.pad(e, pad, mode="edge")
        acc = np.zeros_like(e)
        wacc = np.full_like(e, 0.0)
        for k in range(2 * R + 1):
            sl = (slice(k, k + e.shape[0]) if ax == 0 else slice(None),
                  slice(k, k + e.shape[1]) if ax == 1 else slice(None))
            tap = p[sl]
            if range_sigma > 0.0:
                # range weight in DEPTH units (alpha delta x2), matching
                # chromadepth_smooth.shader's bilateral term: smoothing
                # applies WITHIN surfaces; cliffs deeper than ~range_sigma
                # keep their geometry (silhouettes stay rigid).
                dd = 2.0 * (tap - e)
                wk = w[k] * np.exp(-(dd * dd) /
                                   (2.0 * range_sigma * range_sigma))
            else:
                wk = w[k]
            acc += wk * tap
            wacc += wk
        e = q8(acc / wacc)
    return 2.0 * (e - 0.5)


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


# ---- field computation: prefilter + dilation + reassignment + lift ----
def compute_field(src, hue_rotation=0.0, color_weight=1.0, backdrop=0.0,
                  field_sigma=0.0, range_sigma=0.0):
    """The complete depth-field pipeline (what chromadepth_bake.shader
    bakes into alpha): prefiltered source -> raw depth -> hue-stability ->
    contact-mix reassignment -> donor-restricted lift -> field_smooth(σ)
    regional smoothing. Returns d_full (H x W). Splat colors are NOT
    touched here — callers splat original src colors at these depths."""
    # The entire field pipeline reads the prefiltered source; only the
    # splatted COLORS come from the original src.
    src_f = field_source(src)
    d_full = depth_for(src_f, hue_rotation, color_weight)
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
    hue, delta = hue_delta(src_f)
    conf = conf_for(src_f, color_weight)
    luma = src_f @ np.array([0.299, 0.587, 0.114])
    # "Lit" = visibly distinct from the backdrop layer (with a black
    # backdrop this is just max-channel). Backdrop-colored pixels must
    # never lift, whatever the backdrop value — see bite note below.
    bdist = np.abs(src_f - backdrop).max(axis=2)

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
    # The deployed chain carries the field as 8-bit encoded alpha
    # (a = 0.5 + d/2, chromadepth_bake.shader) — model that quantization
    # ALWAYS, same fairness rationale as quantizing the oracle source to
    # the PNG's 8 bits. Without it, float-vs-8-bit dust at depth
    # boundaries measures as straggler ghosts (~50/eye at d0.5) that no
    # GPU implementation reading the bake could ever avoid.
    cand = (np.round(np.clip(0.5 + 0.5 * np.clip(cand, 0.0, 1.0), 0.0, 1.0)
                     * 255.0) / 255.0 - 0.5) * 2.0
    if field_sigma > 0.0:
        cand = field_smooth(cand, field_sigma, range_sigma)
    return cand


# ---- ground truth render: forward splat + z-buffer ----
def render_eyes(src, depth_slider, hue_rotation=0.0, color_weight=1.0, eye_w=W,
                backdrop=0.0, field_sigma=0.0, micro_fill=0, range_sigma=0.0,
                ss_v=1):
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
    # PIXEL-FOOTPRINT SPLAT (v19): each source pixel fills its complete
    # destination footprint [dest(left edge), dest(right edge)] at its own
    # depth — the continuous limit of the old 4-subsample splat. Shapes
    # stay exactly as rigid and reveals exactly as black (v10), but steep
    # disparity ramps no longer leave sub-pixel black micro-holes between
    # discrete subsamples (and the GPU can draw it as one line per pixel
    # instead of 4 points — required for dual-instance 30fps).
    u_lo = np.arange(W) / W            # pixel left edges
    u_hi = (np.arange(W) + 1) / W      # pixel right edges
    u_c = (np.arange(W) + 0.5) / W     # pixel centers (PIC crop + z taper)
    keep = (u_c >= PIC_LO) & (u_c <= PIC_HI)
    cols_all = np.arange(W)[keep]
    u_lo, u_hi, u_c = u_lo[keep], u_hi[keep], u_c[keep]
    t_lo = edge_taper(u_lo, f0) if f0 > 0 else np.ones_like(u_lo)
    t_hi = edge_taper(u_hi, f0) if f0 > 0 else np.ones_like(u_hi)
    t_c = edge_taper(u_c, f0) if f0 > 0 else np.ones_like(u_c)
    out = {}
    eyes = {"L": +1.0, "R": -1.0}  # dest = x + sign * D (crossed)
    # vertical crop mapping at the SUPERSAMPLED output grid: ss_v=2 renders
    # 2 sub-rows per output row and box-downsamples — antialiases the
    # row-quantized staircase ("zipper") of sloped silhouettes and reveal
    # edges without touching any depth convention.
    out_h = H * ss_v
    vy = np.clip(((f0 + (np.arange(out_h) + 0.5) / out_h *
                   (1 - 2 * f0)) * H).astype(int), 0, H - 1)
    d_full = compute_field(src, hue_rotation, color_weight, backdrop,
                           field_sigma, range_sigma)
    for eye, sign in eyes.items():
        img = np.full((out_h, eye_w, 3), backdrop, dtype=float)
        for row in range(out_h):
            sr = vy[row]
            d = d_full[sr, cols_all]
            dt = d * t_c  # z key: depth tapered at the pixel CENTER
            # +5e-4 px epsilon: shared bin-boundary convention with the
            # GPU splat (stereo-splat.effect VSSplat); change both
            # together.
            b_lo = np.floor(((u_lo + sign * d * t_lo * f0) - vis_lo) /
                            span * eye_w + 5.0e-4).astype(int)
            b_hi = np.floor(((u_hi + sign * d * t_hi * f0) - vis_lo) /
                            span * eye_w + 5.0e-4).astype(int)
            lo = np.minimum(b_lo, b_hi)
            hi = np.maximum(b_lo, b_hi)
            vis = (hi >= 0) & (lo <= eye_w - 1)
            lo = np.clip(lo[vis], 0, eye_w - 1)
            hi = np.clip(hi[vis], 0, eye_w - 1)
            dt_v = dt[vis]
            cols_v = cols_all[vis]
            # paint far -> near (stable: equal tapered depth resolves to
            # the later = larger source x, the GPU's LEQUAL + draw order)
            order = np.argsort(dt_v, kind="stable")
            k = hi[order] - lo[order] + 1
            idx = np.repeat(np.arange(len(order)), k)
            ends = np.cumsum(k)
            offs = np.arange(ends[-1]) - np.repeat(ends - k, k)
            px = lo[order][idx] + offs
            img[row, px] = src[sr, cols_v[order][idx]]
            # MICRO-FILL (candidate convention): reveal gaps no wider than
            # micro_fill px are texture-scale disocclusions — fill them by
            # continuing the FARTHER flank (da Vinci at texture scale
            # only). Larger gaps stay backdrop per the v10 rigid-shape
            # rule. micro_fill=0 reproduces pure black reveals.
            if micro_fill > 0:
                written = np.zeros(eye_w, dtype=bool)
                written[px] = True
                zrow = np.zeros(eye_w)
                zrow[px] = dt_v[order][idx]
                e = np.diff(written.astype(int))
                starts = np.where(e == -1)[0] + 1   # lit -> gap
                stops = np.where(e == 1)[0] + 1     # gap -> lit
                for a in starts:
                    b_idx = stops[stops > a]
                    if not len(b_idx):
                        continue
                    b = b_idx[0]
                    if b - a <= micro_fill:
                        donor = a - 1 if zrow[a - 1] <= zrow[b] else b
                        img[row, a:b] = img[row, donor]
            # Splat holes (disocclusions) stay backdrop black — black IS the
            # infinity plane here. Every shape keeps its rigid silhouette in
            # both eyes: no carve eats the far surface, no fill extends it.
            # (v10 user-locked; v19 only removes intra-pixel sampling gaps.)
        if ss_v > 1:
            img = img.reshape(H, ss_v, eye_w, 3).mean(axis=1)
        out[eye] = img
    return out["L"], out["R"]


# ---- warp ground truth: connected per-row mesh (stereo splat "Warp") ----
def render_eyes_warp(src, depth_slider, hue_rotation=0.0, color_weight=1.0,
                     eye_w=W, field_sigma=0.0, range_sigma=0.0):
    """Connected-stretch convention: each row is a continuous piecewise-
    linear mapping from source x to destination x — disocclusions STRETCH
    the surface between near and far content instead of opening backdrop
    gaps, folds resolve by z (near wins, tie -> larger source x, matching
    the GPU's LEQUAL + ascending-x draw order). Colors are linearly
    resampled along the stretch (the plugin's fragment stage samples the
    source bilinearly at the interpolated u). Mirrors stereo-splat.effect
    VSWarp/PSWarp — change both together."""
    f0 = depth_slider * depth_slider * MAX_PARALLAX
    vis_lo, vis_hi = PIC_LO + f0, PIC_HI - f0
    span = vis_hi - vis_lo
    vy = np.clip(((f0 + (np.arange(H) + 0.5) / H * (1 - 2 * f0)) * H)
                 .astype(int), 0, H - 1)
    d_full = compute_field(src, hue_rotation, color_weight, 0.0,
                           field_sigma, range_sigma)
    u = np.clip((np.arange(W) + 0.5) / W, PIC_LO, PIC_HI)
    taper = edge_taper(u, f0) if f0 > 0 else np.ones_like(u)
    out = {}
    for eye, sign in (("L", +1.0), ("R", -1.0)):
        img = np.zeros((H, eye_w, 3))
        for row in range(H):
            sr = vy[row]
            d = d_full[sr]
            dest = u + sign * d * taper * f0
            x = np.clip((dest - vis_lo) / span, 0.0, 1.0) * eye_w
            # adaptive per-segment sampling: enough samples that every
            # crossed destination pixel receives one
            x0, x1 = x[:-1], x[1:]
            k = np.maximum(np.ceil(np.abs(x1 - x0)).astype(int) + 1, 2)
            seg = np.repeat(np.arange(W - 1), k)
            # intra-segment parameter 0..1
            ends = np.cumsum(k)
            starts = ends - k
            t = (np.arange(ends[-1]) - np.repeat(starts, k)) / \
                np.repeat(k - 1, k)
            xs_e = x[seg] * (1 - t) + x[seg + 1] * t
            z_e = d[seg] * (1 - t) + d[seg + 1] * t
            u_e = u[seg] * (1 - t) + u[seg + 1] * t
            px = np.clip(xs_e.astype(int), 0, eye_w - 1)
            # paint far -> near (last write wins = nearest); equal depth
            # resolves to the later emission = larger source x, matching
            # the GPU's LEQUAL + ascending-x draw order
            order = np.lexsort((np.arange(len(px)), z_e))
            # POINT color sampling: a stretch zone REPLICATES pixels
            # instead of blending them, so hard color edges stay a single
            # hard transition riding the smooth disparity ramp (linear
            # sampling washed every contact into in-between colors that
            # belong to neither surface — user-rejected smear).
            i_near = np.clip(np.round(u_e * W - 0.5).astype(int), 0, W - 1)
            img[row, px[order]] = src[sr, i_near[order]]
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
