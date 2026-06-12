// chromadepth_bake.shader : pass 1 of the chromadepth pair — DEPTH BAKE.
//
// Per SOURCE pixel, compute the final lifted/reassigned field depth (the
// oracle's complete donor/witness/gate pipeline, previously re-derived per
// READ inside chromadepth.shader's march) and emit
//     float4(original_rgb, field_depth)
// so the march pass (chromadepth.shader, "stereo displace") can fetch the
// exact field with ONE texture tap. The field depends only on the source
// pixel, so baking it once removes the O(reads-per-fragment) redundancy
// that capped the rig at ~33 ms/frame.
//
// NOISE PREFILTER (oracle field_source(), commit d0aad24): every color the
// FIELD pipeline reads — center, donor taps, witness taps — is the source
// blurred by one 3x3 binomial ([1,2,1] both axes / 16). Analog noise
// (±0.1) attenuates ~4x, below the confidence deadband, so per-pixel gate
// flicker cannot become displacement speckle at high depth. Displayed
// colors stay ORIGINAL: only .a is derived from the blurred source.
// The blur is sampled with 4 BILINEAR taps at the (±0.5, ±0.5) texel
// corners: each corner tap is the exact mean of its 4 surrounding texels,
// and their average is exactly the [1,2,1]x[1,2,1]/16 kernel.
//
// Depth is stored UNCAPPED (the oracle has no cap); the march applies its
// 0.995 h-test cap at read time. Output range [0,1] fits the chain.
//
// FIELD RULES (ported 1:1 from tools/stereo_oracle.py, every rule bought
// with a measured failure — see chromadepth.shader history for the war
// stories):
//   • luma³ baseline + floor-lifted cosine hue wheel + delta confidence
//   • donor-restricted lift: only SOLID donors (chroma-confident or bright
//     achromatic, AND stable); lit same-hue fades / achromatic AA only;
//     backdrop never lifts.
//   • contact-mix reassignment: chroma-confident pixel marked unstable by
//     a materially different confident hue in its window is a BLEND of two
//     surfaces — reassigned wholesale to the hue-nearest solid donor.
//
// Sliders (CV-driven via the control bridge — these moved here from the
// march because the field owns them):
//   hue_rotation  — which hue sits at the near plane
//   color_weight  — 0 = pure lumadepth, 1 = vivid pixels ride the hue wheel

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

// 1 = render the baked field as grayscale (diff GPU field vs oracle).
uniform float debug_mode<
    string label = "Debug (0 = off, 1 = field probe)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 1.0;
> = 0.0;

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

// Oracle depth — UNCAPPED (the march caps at read for its h-test).
float depth_of_uncapped(float3 c, float delta, float hue, float luma) {
    float confidence = smoothstep(0.06, 0.30, delta) * color_weight;
    float luma_base = luma * luma * luma;
    return lerp(luma_base, wheel_depth(hue), confidence);
}

// All sampling stays inside the real picture (analog border excluded);
// the sampler wraps at uv 0/1, so clamp. y is left free: the oracle's
// prefilter/donor rolls wrap vertically and so does the sampler.
float2 safe_uv(float x, float y) {
    return float2(clamp(x, PIC_LO, PIC_HI), y);
}

// 3x3 binomial of the source at (x, y) — oracle field_source(). Four
// bilinear corner taps: exact [1,2,1]x[1,2,1]/16 in 4 samples (x, y must
// be a texel center, which every call site guarantees).
float3 blur3(float x, float y) {
    // ±0.5 texel corners = exact 3×3 binomial. DO NOT widen by moving
    // these offsets: at ±1.0 the bilinear lands ON the diagonal texel
    // centers — no interpolation, center pixel excluded, an aliasing
    // X-kernel (user saw black speckle). Widening the prefilter must be
    // done with additional taps, prototyped in the oracle first.
    float hw = 0.5 * uv_pixel_interval.x;
    float hh = 0.5 * uv_pixel_interval.y;
    return 0.25 * (image.Sample(textureSampler, safe_uv(x - hw, y - hh)).rgb
                 + image.Sample(textureSampler, safe_uv(x + hw, y - hh)).rgb
                 + image.Sample(textureSampler, safe_uv(x - hw, y + hh)).rgb
                 + image.Sample(textureSampler, safe_uv(x + hw, y + hh)).rgb);
}

// ---- field pipeline, all state in named SCALARS ----
// bool arrays + dynamic indexing miscompile in the shaderfilter transpile
// (stability writes silently dropped) and array locals spill to scratch
// (~3x frame cost, measured 99 ms in the march era). Keep the XTAP/WP
// pattern.

// Load one tap into named scalars; mark mutual center<->tap instability.
#define XTAP(H, DL, LM, DD, U, OX, OY)                                       \
{                                                                            \
    float3 cn = blur3(x + (OX) * px_w, y + (OY) * px_h);                     \
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
    edge_max = max(edge_max,                                                 \
                   max(abs(cn.r - c.r),                                      \
                       max(abs(cn.g - c.g), abs(cn.b - c.b))));              \
}

// Mutual instability witness between two taps (each within the other's 5x5
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
// OUTSIDE the center's 5x5 (the oracle computes instability in the DONOR's
// own frame). One step further out catches the harmful case — a contact-
// mix donor at reach 2 whose mixing partner is at reach 3. Hue+delta only.
#define XWIT(UT, HT, DLT, OX, OY)                                            \
{                                                                            \
    float3 cw = blur3(x + (OX) * px_w, y + (OY) * px_h);                     \
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

// Final lifted/reassigned field depth at texel center (x, y), computed on
// the PREFILTERED source — bit-faithful to the oracle's donor rules.
float field_depth(float x, float y) {
    float px_w = uv_pixel_interval.x;
    float px_h = uv_pixel_interval.y;

    float3 c = blur3(x, y);
    float cmax = max(c.r, max(c.g, c.b));
    float delta = cmax - min(c.r, min(c.g, c.b));
    float hue = hue_of(c, delta, cmax);
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    float d = depth_of_uncapped(c, delta, hue, luma);

    // Below the lit floor no gate can fire (no lift, no reassignment) —
    // backdrop is most of a synth frame. (Oracle bdist on the blurred src.)
    if (cmax <= 0.04)          // backdrop (black) never lifts/reassigns
        return d;
    bool unstable = false;
    float lift = 0.0;
    float best_score = 1e9;
    float best_d = d;
    float edge_max = 0.0;

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
    // distance 2 of each other — a strict subset of the oracle's full 5x5
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

    // SOFT EDGE GATE (v20, mirrors stereo_oracle compute_field): the
    // donor machinery exists for CONTACTS; its binary gates manufacture
    // depth cliffs out of SMOOTH gradients (black reveal arcs in luma
    // wells at depth — user repro). Blend machinery-vs-raw by a
    // smoothstep over local contrast: raw on smooth content, full
    // machinery at contacts, continuous between (a HARD threshold just
    // creates new cliffs along its own contour). Early-out below the
    // smoothstep floor also skips the witness/gate work.
    float d_raw = d;
    if (edge_max <= 0.10)
        return d;
    float ew = smoothstep(0.10, 0.25, edge_max);

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

    return lerp(d_raw, d, ew);
}

float4 mainImage(VertData v_in) : TARGET
{
    // Snap to the texel center: the blur corner trick and the donor grid
    // both assume texel-aligned evaluation (and the march reads .a at
    // texel centers).
    float xq = (floor(v_in.uv.x / uv_pixel_interval.x) + 0.5)
               * uv_pixel_interval.x;
    float yq = (floor(v_in.uv.y / uv_pixel_interval.y) + 0.5)
               * uv_pixel_interval.y;

    float d = field_depth(xq, yq);

    if (debug_mode > 0.5)
        return float4(d, d, d, 1.0);

    // Displayed color stays ORIGINAL — only the field is prefiltered.
    // ALPHA ENCODING a = 0.5 + d/2. Empirically (march probes 3/4): with
    // a ≥ 0.5 the chain passes rgb AND alpha completely unmodified; but
    // content stored with a ≈ 0 arrives BLACK at the next filter (the
    // ramp band measured black at a = luma³ ≈ 0.005, lit at a = 0.502 —
    // some OBS path punishes near-zero alpha). Stay in the safe half.
    float3 orig = image.Sample(textureSampler, safe_uv(xq, yq)).rgb;
    return float4(orig, 0.5 + 0.5 * saturate(d));
}
