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

#define MAX_PARALLAX 0.08
// 64 single-tap coarse steps: step must stay UNDER the ~2 px lifted-fade
// sliver width or rays whose only surface is the sliver (the pixels
// ringing a popped shape) step over it and render black speckle. At 64,
// step ≈ 1.7 px of source at depth 0.84. Cheap: coarse steps are 1 tap.
#define MARCH_STEPS  64
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

float depth_of(float3 c, float delta, float hue, float luma) {
    float confidence = smoothstep(0.06, 0.30, delta) * color_weight;
    float luma_base = luma * luma * luma;
    // Cap below 1.0: the march detects a surface by h = t − d turning
    // negative; at d exactly 1.0, h(t=1) = 0 and the strict crossing test
    // never fires — the NEAREST surface (hue at the wheel peak) becomes
    // undetectable and renders black. 0.995 keeps h(1) > 0 at sub-pixel
    // disparity cost (≤0.4 px at full depth).
    return min(lerp(luma_base, wheel_depth(hue), confidence), 0.995);
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

#define FIELD_TAP(OX, OY)                                                    \
{                                                                            \
    float3 cn = image.Sample(textureSampler,                                 \
        safe_uv(x + (OX) * px_w, y + (OY) * px_h)).rgb;                      \
    float cmax_n = max(cn.r, max(cn.g, cn.b));                               \
    float delta_n = cmax_n - min(cn.r, min(cn.g, cn.b));                     \
    float hue_n = hue_of(cn, delta_n, cmax_n);                               \
    float luma_n = dot(cn, float3(0.299, 0.587, 0.114));                     \
    float conf_n = smoothstep(0.06, 0.30, delta_n) * color_weight;           \
    float d_n = depth_of(cn, delta_n, hue_n, luma_n);                        \
    float hd = hue_dist(hue, hue_n);                                         \
    bool solid_n = (conf_n >= 0.5) || (luma_n > 0.8);                        \
    if (lit && solid_n &&                                                    \
        ((hd < 0.17 && delta_n > 0.06) || (delta < 0.06)))                   \
        lift = max(lift, d_n);                                               \
    if (delta > 0.06 && delta_n > 0.06 && hd > 0.06)                         \
        unstable = true;                                                     \
    if (solid_n && delta_n > 0.06) {                                         \
        float score = floor(hd / 0.02) * 10.0 - d_n;                         \
        if (score < best_score) { best_score = score; best_d = d_n; }        \
    }                                                                        \
}

float depth_field(float x, float y, float f0) {
    float px_w = uv_pixel_interval.x;
    float px_h = uv_pixel_interval.y;

    float3 c = image.Sample(textureSampler, safe_uv(x, y)).rgb;
    float cmax = max(c.r, max(c.g, c.b));
    float delta = cmax - min(c.r, min(c.g, c.b));
    float hue = hue_of(c, delta, cmax);
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    float d = depth_of(c, delta, hue, luma);

    bool lit = cmax > 0.04;     // backdrop (black) never lifts/reassigns
    // Exact early-out: below the lit floor no gate can fire (no lift, no
    // reassignment) — backdrop is most of a synth frame.
    if (!lit)
        return d * edge_taper(x, f0);
    bool unstable = false;
    float lift = 0.0;
    float best_score = 1e9;
    float best_d = d;

    FIELD_TAP(-2.0,  0.0) FIELD_TAP(-1.0,  0.0)
    FIELD_TAP( 1.0,  0.0) FIELD_TAP( 2.0,  0.0)
    FIELD_TAP( 0.0, -2.0) FIELD_TAP( 0.0, -1.0)
    FIELD_TAP( 0.0,  1.0) FIELD_TAP( 0.0,  2.0)
    FIELD_TAP(-1.0, -1.0) FIELD_TAP( 1.0, -1.0)
    FIELD_TAP(-1.0,  1.0) FIELD_TAP( 1.0,  1.0)

    // Contact mix: own hue is meaningless — take the hue-nearest donor.
    if (unstable && delta > 0.06 && lit && best_score < 1e8)
        d = best_d;
    // Donor-restricted lift (dim same-hue fades, achromatic AA).
    d = max(d, lift);

    return d * edge_taper(x, f0);
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

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

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
    float src_y = f0 + uv.y * (1.0 - 2.0 * f0);

    if (f0 < 0.0005)
        return image.Sample(textureSampler, safe_uv(p, src_y));

    // Coarse pre-scan: bound the depth present in this pixel's window.
    // Far pixels (most of a synth frame) resolve immediately. The OWN-
    // position tap must use the full lifted field: a shape's dim outer
    // fade reads depth≈0 raw, and a far-skip would draw it UNDISPLACED —
    // a ghost outline of the shape's silhouette in both eyes. The sparse
    // taps stay cheap; their underestimates are caught by the bracket
    // restart below.
    float dmax = depth_field(p, src_y, f0);
    for (int k = 1; k <= 8; k++)
        dmax = max(dmax, depth_quick(p + dir * (float(k) / 8.0) * f0, src_y, f0));

    if (dmax < 0.02)
        return image.Sample(textureSampler, safe_uv(p, src_y));

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
            for (int r = 0; r < 5; r++) {
                float tm = 0.5 * (ta + tb);
                float sm = p + dir * tm * f0;
                float hm = tm - depth_field(sm, src_y, f0);
                if (hm > 0.0) { ta = tm; sa = sm; ha = hm; }
                else          { tb = tm; sb = sm; hb = hm; }
            }
            float w = ha / (ha - hb);
            float s_hit = lerp(sa, sb, w);
            // SPLAT VERIFICATION — the acceptance rule that mirrors the
            // oracle exactly. A crossing is real iff a quantized source
            // PIXEL, displaced by its own field depth, lands on this
            // destination pixel: |s_c − dir·d(s_c)·f0 − p| < 0.75 px.
            // Fade backfill beside a leading edge passes (those pixels
            // really land here); ramp crossings at a trailing silhouette
            // fail (every body pixel lands far away) — no cliff
            // threshold, no edge-side heuristics. Sampling at the pixel
            // CENTER also returns the oracle's exact pixel color.
            float pxw = uv_pixel_interval.x;
            float sc0 = (floor(s_hit / pxw) + 0.5) * pxw;
            float sc1 = sc0 + ((s_hit > sc0) ? pxw : -pxw);
            float d0 = depth_field(sc0, src_y, f0);
            if (abs(sc0 - dir * d0 * f0 - p) < 0.75 * pxw)
                return image.Sample(textureSampler, safe_uv(sc0, src_y));
            float d1 = depth_field(sc1, src_y, f0);
            if (abs(sc1 - dir * d1 * f0 - p) < 0.75 * pxw)
                return image.Sample(textureSampler, safe_uv(sc1, src_y));
            // no pixel splats here at this layer — keep marching
        }
        t_prev = t;
        s_prev = s;
        h_prev = h;
    }


    // No surface projects here: a disocclusion. Show the infinity backdrop
    // (black) — the gap belongs to the BACKGROUND; shapes stay flat in
    // both eyes (no carve, no fill — oracle-locked).
    return float4(0.0, 0.0, 0.0, 1.0);
}
