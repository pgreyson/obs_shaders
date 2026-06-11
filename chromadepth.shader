// chromadepth : mono → half-SBS stereo from color, black at infinity.
//
// Z-ORDERED GATHER (depth march) — the fragment-shader equivalent of the
// reference splat in tools/stereo_oracle.py (oracle v17, user-signed-off
// 2026-06-10). For each destination pixel, scan the source window that
// could project onto it, find the surface whose own disparity lands it
// here, show the nearest. Occlusion resolves identically in both eyes and
// hard-edged shapes translate RIGIDLY: no carve, no fill, no pillowing.
//
// DEPTH FIELD (ported 1:1 from the oracle's rules, each one bought with a
// measured failure):
//   • luma³ baseline + floor-lifted cosine hue wheel + delta confidence
//   • donor-restricted lift: a pixel may inherit a NEARER neighbor's depth
//     only from a SOLID donor (chroma-confident or bright achromatic), and
//     only if the pixel is lit (above the black backdrop) AND is a same-hue
//     fade of that donor or an achromatic AA mix. Backdrop never lifts —
//     a black pixel raised to near depth z-buffers invisibly over content
//     and bites chunks out of neighboring shapes.
//   • contact-mix reassignment: a chroma-confident pixel whose hue differs
//     from a confident neighbor's by >0.06 is a BLEND of two touching
//     surfaces; its own wheel depth belongs to neither (it splats as a
//     detached micro-ledge in the gap). It is reassigned wholesale to the
//     hue-nearest solid donor (ties toward nearer).
//   • dead-zone edge taper: zero disparity within f0 of the display edge,
//     smooth ramp over 1.5·f0 inward; display inset by f0 (both eyes see
//     identical content at the window edge — straight frame, no strain)
//   • reveals = black: the infinity backdrop seen through the disocclusion
//     gap. Shapes stay flat — the gap belongs to the BACKGROUND.
//   • squared depth curve, 8% ceiling; crossed disparity pops forward;
//     analog border crop + clamp-extend; silhouette test at fixed ±3 px
//
// Sliders (all 0..1, CV-driven via the control bridge):
//   depth         — parallax amount (squared curve, max 8% of the eye view)
//   hue_rotation  — which hue sits at the near plane
//   color_weight  — 0 = pure lumadepth, 1 = vivid pixels ride the hue wheel

uniform float depth<
    string label = "Depth";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float hue_rotation<
    string label = "Hue rotation (which hue is near)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float color_weight<
    string label = "Color weight (0 = lumadepth, 1 = chromadepth)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 1.0;

// Test-harness probe (leave at 0 for normal rendering): 1 = render the
// exact depth field as grayscale, texel-aligned — lets the harness diff
// the GPU field against the oracle's post-lift depths directly.
uniform float debug_mode<
    string label = "Debug (0 = off, 1 = field probe, 2 = path trace)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 2.0;
    float step = 1.0;
> = 0.0;

#define MAX_PARALLAX 0.08
// 48 single-tap coarse steps: step ≈ 2.3 px of source at full depth —
// slightly over the ~2 px lifted-fade sliver width, but the bracket
// walk-up below recovers stepped-over slivers (verified: strays stay
// in the single digits). Cheap: coarse steps are 1 tap.
#define MARCH_STEPS  48
#define DEPTH_GAP    0.18
#define PIC_LO 0.010
#define PIC_HI 0.985

float glsl_mod(float x, float y) {
    return x - y * floor(x / y);
}

float hue_of(float3 c, float delta, float cmax) {
    float h = 0.0;
    if (delta > 0.001) {
        if (cmax == c.r)
            h = glsl_mod((c.g - c.b) / delta, 6.0);
        else if (cmax == c.g)
            h = (c.b - c.r) / delta + 2.0;
        else
            h = (c.r - c.g) / delta + 4.0;
        h /= 6.0;
    }
    return h;
}

float hue_dist(float a, float b) {
    float d = abs(a - b);
    return min(d, 1.0 - d);
}

float wheel_depth(float h) {
    // Floor-lifted cosine wheel: no hue ever lands exactly ON the black
    // infinity plane.
    return 0.12 + 0.88 * (0.5 + 0.5 * cos(6.2831853 * (h + hue_rotation)));
}

// Oracle-exact depth — used by the EXACT field (splat verification): the
// oracle has no cap, and at the wheel peak (red, d = 1.0) the 0.995 cap
// shifts a pixel's displacement by 0.1 dest bins — enough to push a top
// subsample across a bin boundary and light a ghost column (measured).
float depth_of_uncapped(float3 c, float delta, float hue, float luma) {
    float confidence = smoothstep(0.06, 0.30, delta) * color_weight;
    float luma_base = luma * luma * luma;
    return lerp(luma_base, wheel_depth(hue), confidence);
}

float depth_of(float3 c, float delta, float hue, float luma) {
    // Cap below 1.0 (LOCATOR/march only): the march detects a surface by
    // h = t − d turning negative; at d exactly 1.0, h(t=1) = 0 and the
    // strict crossing test never fires — the NEAREST surface (hue at the
    // wheel peak) becomes undetectable and renders black. 0.995 keeps
    // h(1) > 0; the verification re-derives exact uncapped depth anyway.
    return min(depth_of_uncapped(c, delta, hue, luma), 0.995);
}

// All sampling stays inside the real picture (analog border excluded);
// the sampler wraps at uv 0/1, so clamp.
float2 safe_uv(float x, float y) {
    return float2(clamp(x, PIC_LO, PIC_HI), y);
}

// Dead zone (width f0) of ZERO disparity at the view edges, then a smooth
// ramp to full depth inward — edge content pixel-identical in both eyes.
float edge_taper(float x, float f0) {
    float edge_dist = min(x - (PIC_LO + f0), (PIC_HI - f0) - x) - f0;
    return smoothstep(0.0, 1.5 * f0 + 0.001, edge_dist);
}

// ---- oracle-ported depth field ------------------------------------------
// Evaluates the effective depth at a source position: own depth, then the
// donor-restricted lift and the contact-mix reassignment over a sparse
// neighborhood (±1, ±2 px on each axis + ±1 diagonals — the oracle's 5×5
// reach at the taps that matter for 1-2 px AA/analog edges).

// ---- exact field (verification only) ----
// All state in named SCALARS with float 0/1 flags: bool arrays + dynamic
// indexing miscompiled in the shaderfilter transpile (the stability writes
// silently never took effect) and array locals spilled to scratch (~3×
// frame cost, measured 99 ms).

// Load one tap into named scalars; mark mutual center↔tap instability.
#define XTAP(H, DL, LM, DD, U, OX, OY)                                       \
{                                                                            \
    float3 cn = image.Sample(textureSampler,                                 \
        safe_uv(x + (OX) * px_w, y + (OY) * px_h)).rgb;                      \
    float cmax_n = max(cn.r, max(cn.g, cn.b));                               \
    DL = cmax_n - min(cn.r, min(cn.g, cn.b));                                \
    H = hue_of(cn, DL, cmax_n);                                              \
    LM = dot(cn, float3(0.299, 0.587, 0.114));                               \
    DD = depth_of_uncapped(cn, DL, H, LM);                                   \
    U = 0.0;                                                                 \
    if (delta > 0.06 && DL > 0.06 && hue_dist(hue, H) > 0.06) {              \
        U = 1.0;                                                             \
        unstable = true;                                                     \
    }                                                                        \
}

// Mutual instability witness between two taps (each within the other's 5×5
// in the oracle): two chroma-confident pixels with materially different hue
// mark EACH OTHER unstable — the oracle's "& ~unstable" donor gate. Without
// it, at a vertical seam the center's own unstable column (vertical taps,
// hd = 0) wins the reassignment as a bucket-0 self-donor and the seam pixel
// keeps its meaningless intermediate wheel depth — a micro-ledge splatted
// into the disocclusion gap (lit ghosts mid-gap, measured).
#define WP(DA, HA, UA, DB, HB, UB)                                           \
    if (DA > 0.06 && DB > 0.06 && hue_dist(HA, HB) > 0.06)                   \
        { UA = 1.0; UB = 1.0; }

// Witness-extension tap: a ±2 axis donor's instability witnesses can sit
// OUTSIDE the center's 5×5 (the oracle computes instability in the DONOR's
// own frame). One step further out catches the harmful case — a contact-
// mix donor at reach 2 whose mixing partner is at reach 3 (e.g. the green
// body two px right of a red|green seam: its ±2 donor is the mid-mix
// pixel, witnessed only by the red-side pixel at −3). Hue+delta only.
#define XWIT(UT, HT, DLT, OX, OY)                                            \
{                                                                            \
    float3 cw = image.Sample(textureSampler,                                 \
        safe_uv(x + (OX) * px_w, y + (OY) * px_h)).rgb;                      \
    float cmax_w = max(cw.r, max(cw.g, cw.b));                               \
    float delta_w = cmax_w - min(cw.r, min(cw.g, cw.b));                     \
    if (delta_w > 0.06 && DLT > 0.06 &&                                      \
        hue_dist(hue_of(cw, delta_w, cmax_w), HT) > 0.06)                    \
        UT = 1.0;                                                            \
}

// Donor gates for one tap: solid = chroma-confident or bright achromatic
// body, AND stable. Unstable pixels are contact mixes; their depth belongs
// to no surface and must never donate (lift OR reassignment).
#define GATE(H, DL, LM, DD, U)                                               \
{                                                                            \
    bool solid_n = ((smoothstep(0.06, 0.30, DL) * color_weight >= 0.5) ||    \
                    (LM > 0.8)) && (U < 0.5);                                \
    float hd = hue_dist(hue, H);                                             \
    if (solid_n && ((hd < 0.17 && DL > 0.06) || (delta < 0.06)))             \
        lift = max(lift, DD);                                                \
    if (solid_n && DL > 0.06) {                                              \
        float score = floor(hd / 0.02) * 10.0 - DD;                          \
        if (score < best_score) { best_score = score; best_d = DD; }         \
    }                                                                        \
}

// ---- locator field (march/bisection only) ----
// Old-style accumulate-in-place taps WITHOUT the stability exclusion: it
// only LOCATES the crossing; the ±1 px candidate slack in verification
// absorbs its error, and its lift can only OVER-donate, which makes the
// far early-out strictly safer (never a false far-skip).
#define LOC_TAP(OX, OY)                                                      \
{                                                                            \
    float3 cn = image.Sample(textureSampler,                                 \
        safe_uv(x + (OX) * px_w, y + (OY) * px_h)).rgb;                      \
    float cmax_n = max(cn.r, max(cn.g, cn.b));                               \
    float delta_n = cmax_n - min(cn.r, min(cn.g, cn.b));                     \
    float hue_n = hue_of(cn, delta_n, cmax_n);                               \
    float luma_n = dot(cn, float3(0.299, 0.587, 0.114));                     \
    float d_n = depth_of(cn, delta_n, hue_n, luma_n);                        \
    float hd = hue_dist(hue, hue_n);                                         \
    bool solid_n = (smoothstep(0.06, 0.30, delta_n) * color_weight >= 0.5)   \
                   || (luma_n > 0.8);                                        \
    if (solid_n && ((hd < 0.17 && delta_n > 0.06) || (delta < 0.06)))        \
        lift = max(lift, d_n);                                               \
    if (delta > 0.06 && delta_n > 0.06 && hd > 0.06)                         \
        unstable = true;                                                     \
    if (solid_n && delta_n > 0.06) {                                         \
        float score = floor(hd / 0.02) * 10.0 - d_n;                         \
        if (score < best_score) { best_score = score; best_d = d_n; }        \
    }                                                                        \
}

// UNTAPERED exact field depth, bit-faithful to the oracle's donor rules —
// used ONLY on splat-verification candidates (it needs the raw value so it
// can re-taper at each subsample position; the oracle tapers at the
// subsample, and in the edge ramp the disparity gradient reaches ~1 px/px).
float field_exact(float x, float y) {
    float px_w = uv_pixel_interval.x;
    float px_h = uv_pixel_interval.y;

    float3 c = image.Sample(textureSampler, safe_uv(x, y)).rgb;
    float cmax = max(c.r, max(c.g, c.b));
    float delta = cmax - min(c.r, min(c.g, c.b));
    float hue = hue_of(c, delta, cmax);
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    float d = depth_of_uncapped(c, delta, hue, luma);

    // Exact early-out: below the lit floor no gate can fire (no lift, no
    // reassignment) — backdrop is most of a synth frame.
    if (cmax <= 0.04)          // backdrop (black) never lifts/reassigns
        return d;
    bool unstable = false;
    float lift = 0.0;
    float best_score = 1e9;
    float best_d = d;

    // Tap layout: 0(-2,0) 1(-1,0) 2(1,0) 3(2,0)  4(0,-2) 5(0,-1) 6(0,1)
    //             7(0,2)  8(-1,-1) 9(1,-1) 10(-1,1) 11(1,1)
    float h0, dl0, lm0, dd0, u0;   float h1, dl1, lm1, dd1, u1;
    float h2, dl2, lm2, dd2, u2;   float h3, dl3, lm3, dd3, u3;
    float h4, dl4, lm4, dd4, u4;   float h5, dl5, lm5, dd5, u5;
    float h6, dl6, lm6, dd6, u6;   float h7, dl7, lm7, dd7, u7;
    float h8, dl8, lm8, dd8, u8;   float h9, dl9, lm9, dd9, u9;
    float h10, dl10, lm10, dd10, u10;
    float h11, dl11, lm11, dd11, u11;

    XTAP(h0, dl0, lm0, dd0, u0, -2.0,  0.0)
    XTAP(h1, dl1, lm1, dd1, u1, -1.0,  0.0)
    XTAP(h2, dl2, lm2, dd2, u2,  1.0,  0.0)
    XTAP(h3, dl3, lm3, dd3, u3,  2.0,  0.0)
    XTAP(h4, dl4, lm4, dd4, u4,  0.0, -2.0)
    XTAP(h5, dl5, lm5, dd5, u5,  0.0, -1.0)
    XTAP(h6, dl6, lm6, dd6, u6,  0.0,  1.0)
    XTAP(h7, dl7, lm7, dd7, u7,  0.0,  2.0)
    XTAP(h8, dl8, lm8, dd8, u8, -1.0, -1.0)
    XTAP(h9, dl9, lm9, dd9, u9,  1.0, -1.0)
    XTAP(h10, dl10, lm10, dd10, u10, -1.0,  1.0)
    XTAP(h11, dl11, lm11, dd11, u11,  1.0,  1.0)

    // Tap-tap witnesses: the COMPLETE set of tap pairs within Chebyshev
    // distance 2 of each other — a strict subset of the oracle's full 5×5
    // witness set, so no FALSE instability is ever introduced. (A trimmed
    // list let the diagonal taps at an unstable seam column slip through
    // as bucket-0 lift donors — measured as gap ghosts.)
    WP(dl0, h0, u0, dl1, h1, u1)   WP(dl0, h0, u0, dl4, h4, u4)
    WP(dl0, h0, u0, dl5, h5, u5)   WP(dl0, h0, u0, dl6, h6, u6)
    WP(dl0, h0, u0, dl7, h7, u7)   WP(dl0, h0, u0, dl8, h8, u8)
    WP(dl0, h0, u0, dl10, h10, u10)
    WP(dl1, h1, u1, dl2, h2, u2)   WP(dl1, h1, u1, dl4, h4, u4)
    WP(dl1, h1, u1, dl5, h5, u5)   WP(dl1, h1, u1, dl6, h6, u6)
    WP(dl1, h1, u1, dl7, h7, u7)   WP(dl1, h1, u1, dl8, h8, u8)
    WP(dl1, h1, u1, dl9, h9, u9)   WP(dl1, h1, u1, dl10, h10, u10)
    WP(dl1, h1, u1, dl11, h11, u11)
    WP(dl2, h2, u2, dl3, h3, u3)   WP(dl2, h2, u2, dl4, h4, u4)
    WP(dl2, h2, u2, dl5, h5, u5)   WP(dl2, h2, u2, dl6, h6, u6)
    WP(dl2, h2, u2, dl7, h7, u7)   WP(dl2, h2, u2, dl8, h8, u8)
    WP(dl2, h2, u2, dl9, h9, u9)   WP(dl2, h2, u2, dl10, h10, u10)
    WP(dl2, h2, u2, dl11, h11, u11)
    WP(dl3, h3, u3, dl4, h4, u4)   WP(dl3, h3, u3, dl5, h5, u5)
    WP(dl3, h3, u3, dl6, h6, u6)   WP(dl3, h3, u3, dl7, h7, u7)
    WP(dl3, h3, u3, dl9, h9, u9)   WP(dl3, h3, u3, dl11, h11, u11)
    WP(dl4, h4, u4, dl5, h5, u5)   WP(dl4, h4, u4, dl8, h8, u8)
    WP(dl4, h4, u4, dl9, h9, u9)
    WP(dl5, h5, u5, dl6, h6, u6)   WP(dl5, h5, u5, dl8, h8, u8)
    WP(dl5, h5, u5, dl9, h9, u9)   WP(dl5, h5, u5, dl10, h10, u10)
    WP(dl5, h5, u5, dl11, h11, u11)
    WP(dl6, h6, u6, dl7, h7, u7)   WP(dl6, h6, u6, dl8, h8, u8)
    WP(dl6, h6, u6, dl9, h9, u9)   WP(dl6, h6, u6, dl10, h10, u10)
    WP(dl6, h6, u6, dl11, h11, u11)
    WP(dl7, h7, u7, dl10, h10, u10)
    WP(dl7, h7, u7, dl11, h11, u11)
    WP(dl8, h8, u8, dl9, h9, u9)   WP(dl8, h8, u8, dl10, h10, u10)
    WP(dl8, h8, u8, dl11, h11, u11)
    WP(dl9, h9, u9, dl10, h10, u10)
    WP(dl9, h9, u9, dl11, h11, u11)
    WP(dl10, h10, u10, dl11, h11, u11)

    // Out-of-window witnesses for the ±2 axis donors.
    XWIT(u0, h0, dl0, -3.0,  0.0)
    XWIT(u3, h3, dl3,  3.0,  0.0)
    XWIT(u4, h4, dl4,  0.0, -3.0)
    XWIT(u7, h7, dl7,  0.0,  3.0)

    GATE(h0, dl0, lm0, dd0, u0)    GATE(h1, dl1, lm1, dd1, u1)
    GATE(h2, dl2, lm2, dd2, u2)    GATE(h3, dl3, lm3, dd3, u3)
    GATE(h4, dl4, lm4, dd4, u4)    GATE(h5, dl5, lm5, dd5, u5)
    GATE(h6, dl6, lm6, dd6, u6)    GATE(h7, dl7, lm7, dd7, u7)
    GATE(h8, dl8, lm8, dd8, u8)    GATE(h9, dl9, lm9, dd9, u9)
    GATE(h10, dl10, lm10, dd10, u10)
    GATE(h11, dl11, lm11, dd11, u11)

    // Contact mix: own hue is meaningless — take the hue-nearest donor.
    if (unstable && delta > 0.06 && best_score < 1e8)
        d = best_d;
    // Donor-restricted lift (dim same-hue fades, achromatic AA).
    d = max(d, lift);

    return d;
}

// Locator field: same structure, no stability exclusion — cheap, and only
// ever used to LOCATE crossings (pre-scan bound, brackets, bisection).
float depth_field_raw(float x, float y) {
    float px_w = uv_pixel_interval.x;
    float px_h = uv_pixel_interval.y;

    float3 c = image.Sample(textureSampler, safe_uv(x, y)).rgb;
    float cmax = max(c.r, max(c.g, c.b));
    float delta = cmax - min(c.r, min(c.g, c.b));
    float hue = hue_of(c, delta, cmax);
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    float d = depth_of(c, delta, hue, luma);

    if (cmax <= 0.04)          // backdrop (black) never lifts/reassigns
        return d;
    bool unstable = false;
    float lift = 0.0;
    float best_score = 1e9;
    float best_d = d;

    // Axis taps only: the locator just needs the crossing within ~1 px;
    // corner (diagonal-only) fades shift its estimate sub-pixel and the
    // verification's full-reach exact field still decides acceptance.
    LOC_TAP(-2.0,  0.0) LOC_TAP(-1.0,  0.0)
    LOC_TAP( 1.0,  0.0) LOC_TAP( 2.0,  0.0)
    LOC_TAP( 0.0, -2.0) LOC_TAP( 0.0, -1.0)
    LOC_TAP( 0.0,  1.0) LOC_TAP( 0.0,  2.0)

    if (unstable && delta > 0.06 && best_score < 1e8)
        d = best_d;
    d = max(d, lift);

    return d;
}

float depth_field(float x, float y, float f0) {
    return depth_field_raw(x, y) * edge_taper(x, f0);
}

// Oracle bin test for one candidate source pixel: does any of its 4
// subsamples (±0.375/±0.125 px, taper evaluated AT THE SUBSAMPLE — in the
// edge ramp the disparity gradient reaches ~1 px/px), displaced by the
// pixel's own field depth, land inside THIS fragment's dest bin?
// Returns 1 when AT LEAST TWO subsamples land in the bin. A single
// straggler subsample is exactly the width of the harness's 8-bit-PNG vs
// float-oracle quantization (Δd ≈ 0.0015 shifts dests by up to ~0.13 bins
// at full parallax): accepting it paints one ghost bin just outside the
// oracle's run (measured: full edge columns at cw0/cw0.5/d1.0, R-biased
// because the palette rounds down). Interior and taper-stretch bins always
// collect ≥2 subsamples from one of the three candidates (max stretch ×2
// → ≥3 subsamples per bin); the only losses are run-rim bins — invisible
// black-on-black holes inside the tolerated edge band.
float splat_hits(float sc, float du, float p, float dir, float f0,
                 float half_bin, float pxw) {
    float hits = 0.0;
    for (int k = 0; k < 4; k++) {
        float xk = sc + (0.25 * float(k) - 0.375) * pxw;
        if (xk < PIC_LO || xk > PIC_HI)
            continue;   // oracle drops out-of-picture subsamples
        float dest = xk - dir * du * edge_taper(xk, f0) * f0;
        if (abs(dest - p) < half_bin)
            hits += 1.0;
    }
    return (hits >= 2.0) ? 1.0 : 0.0;
}

// Single-tap depth for the coarse pre-scan.
float depth_quick(float x, float y, float f0) {
    float3 c = image.Sample(textureSampler, safe_uv(x, y)).rgb;
    float cmax = max(c.r, max(c.g, c.b));
    float delta = cmax - min(c.r, min(c.g, c.b));
    float hue = hue_of(c, delta, cmax);
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    return depth_of(c, delta, hue, luma) * edge_taper(x, f0);
}

// Try the 3 source pixel centers bracketing SCENTER against this
// fragment's bin; leaves the nearest passer in win_d/win_x (win_d < 0 if
// none). Declares its own win_d/win_x — use inside an own scope.
// The >= comparison: equal-depth ties go to the LARGEST x (candidates
// ascend), bit-matching the oracle's stable z-sort. In a taper-0 dead
// zone a lit fade and the backdrop BOTH pass at dt = 0 — the oracle shows
// the backdrop (its largest in-bin subsample is written last); a strict >
// kept the lit fade instead and painted a full ghost column (measured at
// d1.0 R col 59, orange edge rect).
#define VERIFY3(SCENTER)                                                     \
    float win_d = -1.0;                                                      \
    float win_x = 0.0;                                                       \
    {                                                                        \
        float scbase = (floor((SCENTER) / pxw) + 0.5) * pxw;                 \
        for (int cnd = -1; cnd <= 1; cnd++) {                                \
            float sc = scbase + float(cnd) * pxw;                            \
            float du = field_exact(sc, src_y);                               \
            if (splat_hits(sc, du, p, dir, f0, half_bin, pxw) > 0.5) {       \
                float dt = du * edge_taper(sc, f0);                          \
                if (dt >= win_d) { win_d = dt; win_x = sc; }                 \
            }                                                                \
        }                                                                    \
    }

// Self-cover rescue for one stored pre-scan tap (see use site): if the
// surface read by tap KK splats within 3 px of this fragment, run the
// exact 3-candidate verification at the tap position.
#define RESCUE(KK)                                                           \
{                                                                            \
    float sk = p + dir * (KK / 6.0) * f0;                                    \
    float qk = depth_quick(sk, src_y, f0);                                   \
    if (abs(sk - dir * qk * f0 - p) < 3.0 * pxw) {                           \
        VERIFY3(sk)                                                          \
        if (win_d >= 0.0) {                                                  \
            if (dbg2) return float4(0.8, n_cross / 8.0, dmax, 1.0);          \
            return image.Sample(textureSampler, safe_uv(win_x, src_y));      \
        }                                                                    \
    }                                                                        \
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    if (debug_mode > 0.5 && debug_mode < 1.5) {
        // Exact-field probe: grayscale d at this texel (no fold, no march).
        float xq = (floor(uv.x / uv_pixel_interval.x) + 0.5)
                   * uv_pixel_interval.x;
        float yq = (floor(uv.y / uv_pixel_interval.y) + 0.5)
                   * uv_pixel_interval.y;
        float dp = field_exact(xq, yq);
        return float4(dp, dp, dp, 1.0);
    }
    // debug_mode 2: each exit returns float4(path, crossings/8, dmax, 1)
    // path: .1 far  .2 fast-accept  .4 verify  .6 jump  .8 rescue  1 black
    bool dbg2 = debug_mode > 1.5;
    float n_cross = 0.0;

    // Squared depth curve — fine control low, 8% ceiling.
    float f0 = depth * depth * MAX_PARALLAX;

    // Fold into eye-local space. Crossed disparity: left eye gathers
    // leftward (its image shifts right), right eye the opposite.
    float local_x;
    float dir;
    if (uv.x < 0.5) {
        local_x = uv.x * 2.0;
        dir = -1.0;
    } else {
        local_x = (uv.x - 0.5) * 2.0;
        dir = 1.0;
    }

    // Display window: real picture inset by f0 on all four sides
    // (aspect-preserving, identical in both eyes).
    float vis_lo = PIC_LO + f0;
    float vis_hi = PIC_HI - f0;
    float p = vis_lo + local_x * (vis_hi - vis_lo);
    // Snap to the texel-center row the oracle reads (vy = floor(src_y*H)):
    // a vertical bilinear blend of a lit and a black row produces half-lit
    // pixels along top/bottom edges where the oracle is pure black — a
    // ghost class, not just mean noise. Arithmetic FORM matters: computed
    // as f0*H + (row+0.5)*(1−2f0) — the oracle's own expression — minus a
    // 1e-4 tie-break. At d=0.5 the mapping lands EXACTLY on integers every
    // 25 rows; the f64 oracle's dust there is mixed (24.0 exact, but
    // 648.0 computes as 647.99999999999999 → floor 647), and f32 cannot
    // reproduce f64 dust. The tie-break must take the LOWER row: the only
    // exact-grid row that sits on a hard horizontal edge (output 652 →
    // src 648.0, the green rect's bottom) is one the oracle floors DOWN —
    // taking the upper row painted a 205-px ghost row (measured); the
    // rows the oracle floors up are soft-blurred bar boundaries where a
    // one-row offset stays under the diff thresholds. The epsilon is ≫
    // f32 noise (~2e-5) and ≪ the smallest non-exact boundary gap across
    // test depths (~9e-4).
    float pxh = uv_pixel_interval.y;
    float Hpx = 1.0 / pxh;
    float src_row = floor(f0 * Hpx
                          + (floor(uv.y * Hpx) + 0.5) * (1.0 - 2.0 * f0)
                          - 1e-4);
    float src_y = (src_row + 0.5) * pxh;
    // One dest pixel covers span/960 of source uv (= 2*span*pxw); a splat
    // lands in THIS fragment's bin iff |dest − p| < span*pxw (0.86–0.97 px
    // depending on f0 — the bin shrinks as the display window insets).
    float pxw = uv_pixel_interval.x;
    float half_bin = (vis_hi - vis_lo) * pxw;

    if (f0 < 0.0005)
        return image.Sample(textureSampler, safe_uv(p, src_y));

    // Coarse pre-scan: bound the depth present in this pixel's window.
    // Far pixels (most of a synth frame) resolve immediately. The OWN-
    // position tap must use the full lifted field: a shape's dim outer
    // fade reads depth≈0 raw, and a far-skip would draw it UNDISPLACED —
    // a ghost outline of the shape's silhouette in both eyes. It is taken
    // at the BIN-TOP TEXEL CENTER (the pixel the early-out would paint):
    // at continuous p the bilinear blend straddles texels and dilutes the
    // donor gates (luma>0.8 / conf>=0.5), silently dropping the lift —
    // measured as undisplaced fade ghosts. The sparse quick taps stay
    // cheap; their underestimates are caught by the bracket restart below.
    float bc = (floor((p + half_bin) / pxw - 0.125) + 0.5) * pxw;
    float dmax = depth_field(bc, src_y, f0);
    for (int k = 1; k <= 6; k++)
        dmax = max(dmax, depth_quick(p + dir * (float(k) / 6.0) * f0,
                                     src_y, f0));
    // Above f0 ≈ 0.044 the k/6 spacing exceeds ~14 px and a hard 16 px
    // feature (lines scene, no soften) can sit wholly between taps —
    // measured as far-early-outs with dmax = 0 in the white line's dest
    // columns. Midpoint taps halve the spacing; the k/6 grid is kept
    // as-is so every currently-caught alignment stays caught.
    if (f0 > 0.0437) {
        for (int k = 1; k <= 6; k++)
            dmax = max(dmax, depth_quick(
                p + dir * ((float(k) - 0.5) / 6.0) * f0, src_y, f0));
    }

    if (dmax < 0.02) {
        // Far content is undisplaced; the oracle's stable z-sort writes the
        // LARGEST-x subsample in the bin last among equal depths, so the
        // bin shows the pixel under its upper edge (bc) — snap there
        // instead of bilinear-blending two columns at the bin center.
        if (dbg2) return float4(0.1, 0.0, dmax, 1.0);
        return image.Sample(textureSampler, safe_uv(bc, src_y));
    }

    // Interior fast-accept: if bc is the NEAREST surface anywhere in this
    // pixel's window (nothing can z-beat it) and its own splat lands in
    // this bin, it wins outright — skips the march for body interiors,
    // most of the lit frame. Among equal depths the oracle's stable sort
    // takes the largest x, which is bc (the bin-top pixel) by choice.
    {
        float du_bc = field_exact(bc, src_y);
        if (du_bc * edge_taper(bc, f0) >= dmax - 0.005 &&
            splat_hits(bc, du_bc, p, dir, f0, half_bin, pxw) > 0.5) {
            if (dbg2) return float4(0.2, 0.0, dmax, 1.0);
            return image.Sample(textureSampler, safe_uv(bc, src_y));
        }
    }

    // March disparity layers from the highest plausible (pre-scan bound,
    // padded) down to the farthest. Surface at s = p + dir*t*f0 covers this
    // pixel when d(s) = t; h = t − d(s) flips sign there. First flip from
    // the near side = the z-order winner. A crossing whose depth jumps
    // across a fixed ±3 px window is a silhouette — skip it, a deeper
    // surface may still cover this pixel.
    float t_hi = min(1.0, dmax + 0.06);
    float t_prev = t_hi;
    float s_prev = p + dir * t_hi * f0;
    float d_prev = depth_field(s_prev, src_y, f0);
    float h_prev = t_prev - d_prev;
    if (h_prev <= 0.0) {
        // The pre-scan bound was too low (its sparse single taps straddled
        // a thin or edge-aligned surface). Do NOT accept this layer — the
        // surface here projects elsewhere. Restart the bracket from t=1,
        // where h = 1 − d ≥ 0 holds by construction.
        t_hi = 1.0;
        t_prev = 1.0;
        s_prev = p + dir * f0;
        d_prev = depth_field(s_prev, src_y, f0);
        h_prev = 1.0 - d_prev;
    }

    for (int i = 1; i <= MARCH_STEPS; i++) {
        float t = t_hi * (1.0 - float(i) / float(MARCH_STEPS));
        float s = p + dir * t * f0;
        // Coarse steps read raw single-tap depth: the gated field differs
        // from raw only on 1-2 px transition slivers, and the bisection
        // below re-resolves those locally with the full field. Verified
        // equivalent against the oracle (same mean diff / hole counts).
        float h = t - depth_quick(s, src_y, f0);
        if (h_prev > 0.0 && h <= 0.0) {
            n_cross += 1.0;
            float ta = t_prev; float sa = s_prev; float ha = t_prev - depth_field(s_prev, src_y, f0);
            float tb = t;      float sb = s;      float hb = t - depth_field(s, src_y, f0);
            if (ha <= 0.0) {
                // The field sees a lifted fade above this bracket that the
                // raw read missed. Recover the true bracket by walking UP
                // in half-steps with the field (fade slivers are 1-2 px) —
                // rejecting here leaves black speckle around near shapes,
                // accepting blind paints lit ghosts into the gap.
                float t_base = ta;
                tb = ta; sb = sa; hb = ha;   // old top = negative side
                bool re = false;
                for (int u = 1; u <= 4; u++) {
                    float tu = t_base + 0.5 * float(u) * (t_hi / float(MARCH_STEPS));
                    if (tu > 1.0) break;
                    float su = p + dir * tu * f0;
                    float hu = tu - depth_field(su, src_y, f0);
                    if (hu > 0.0) { ta = tu; sa = su; ha = hu; re = true; break; }
                    tb = tu; sb = su; hb = hu;
                }
                if (!re) {
                    // WIDE lifted region (e.g. whole rows lifted to a
                    // bright bar's depth by the vertical donor taps): the
                    // raw march crossed at the RAW layer, but the field
                    // sits far above the entire bracket and the 2-px
                    // sliver walk-up cannot span it. Jump straight to the
                    // field's own layer and verify the splat there — the
                    // exact verification still rejects trailing-silhouette
                    // ramps, so this cannot paint ghosts.
                    // FIELD bisection on [tb, 1]: h(1) = 1 − d ≥ 0.005 (the
                    // locator cap) and h(tb) = hb < 0, so a field crossing
                    // is bracketed. A one-shot (or fixed-point) jump to the
                    // field's layer fails inside the taper ramps — the map
                    // t → d·taper(p+dir·t·f0) has derivative ≈ d there, so
                    // it never converges at full depth (measured: lifted
                    // bar-boundary rows stayed black across the ramps).
                    float ta2 = 1.0;
                    float tb2 = tb;
                    for (int r2 = 0; r2 < 8; r2++) {
                        float tm2 = 0.5 * (ta2 + tb2);
                        float hm2 = tm2 - depth_field(p + dir * tm2 * f0,
                                                      src_y, f0);
                        if (hm2 > 0.0) ta2 = tm2; else tb2 = tm2;
                    }
                    VERIFY3(p + dir * tb2 * f0)
                    if (win_d >= 0.0) {
                        if (dbg2) return float4(0.6, n_cross / 8.0, dmax, 1.0);
                        return image.Sample(textureSampler,
                                            safe_uv(win_x, src_y));
                    }
                    t_prev = t; s_prev = s;
                    h_prev = max(h, t - depth_field(s, src_y, f0));
                    continue;
                }
            }
            if (hb > 0.0) {
                // Raw over-read (noise/fade) — no real crossing here.
                t_prev = t; s_prev = s; h_prev = hb;
                continue;
            }
            // 2 bisection iterations: the coarse bracket is ≤ ~2.3 px and
            // the 3 verification candidates span ±1.5 px around s_hit —
            // sub-pixel s_hit precision buys nothing (measured equal).
            for (int r = 0; r < 2; r++) {
                float tm = 0.5 * (ta + tb);
                float sm = p + dir * tm * f0;
                float hm = tm - depth_field(sm, src_y, f0);
                if (hm > 0.0) { ta = tm; sa = sm; ha = hm; }
                else          { tb = tm; sb = sm; hb = hm; }
            }
            float w = ha / (ha - hb);
            float s_hit = lerp(sa, sb, w);
            // SPLAT VERIFICATION — the EXACT oracle bin test. The oracle
            // splats every source pixel as 4 subsamples at ±0.375/±0.125 px
            // around the center, each displaced by the pixel's field depth
            // tapered AT THE SUBSAMPLE (in the edge ramp the disparity
            // gradient reaches ~1 px/px — the source→dest mapping locally
            // stretches ×2 and only the subsamples keep every dest bin
            // covered), into bins of half-width span*pxw. A crossing is
            // real iff one of the 3 pixel centers bracketing s_hit lands a
            // subsample in THIS fragment's bin; among passers the NEAREST
            // (highest tapered depth) wins — the oracle's z-buffer order.
            // Fade backfill beside a leading edge passes (those pixels
            // really land here); ramp crossings at a trailing silhouette
            // fail (every body pixel lands far away) — no cliff threshold,
            // no edge-side heuristics. Sampling at the pixel CENTER also
            // returns the oracle's exact pixel color.
            {
                VERIFY3(s_hit)
                if (win_d >= 0.0) {
                    if (dbg2) return float4(0.4, n_cross / 8.0, dmax, 1.0);
                    return image.Sample(textureSampler,
                                        safe_uv(win_x, src_y));
                }
            }
            // no pixel splats into this bin at this layer — keep marching
        }
        t_prev = t;
        s_prev = s;
        h_prev = h;
    }


    // Thin-feature rescue: a 1-2 px line at high depth can fall BETWEEN
    // the coarse march steps (2.3-3.2 px at full parallax) and never
    // bracket — its dest bins would stay black (full-height stray
    // columns, measured on the lines scene). The pre-scan taps are denser
    // in practice (they caught it in dmax); re-test each tap's SELF-COVER:
    // if the surface under tap k splats anywhere near this fragment,
    // verify it exactly. Gap fragments filter out in one subtraction
    // (their taps' dests land far away) — the exact calls stay rare.
    // Nearest layer (k=6) first: z-order. (Taps re-sampled here — this
    // path only runs for fallback fragments, i.e. narrow gaps.)
    RESCUE(6.0) RESCUE(5.0) RESCUE(4.0)
    RESCUE(3.0) RESCUE(2.0) RESCUE(1.0)

    // No surface projects here: a disocclusion. Show the infinity backdrop
    // (black) — the gap belongs to the BACKGROUND; shapes stay flat in
    // both eyes (no carve, no fill — oracle-locked).
    if (dbg2) return float4(1.0, n_cross / 8.0, dmax, 1.0);
    return float4(0.0, 0.0, 0.0, 1.0);
}
