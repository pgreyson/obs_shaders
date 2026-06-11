// chromadepth : mono → half-SBS stereo from color, black at infinity.
// PASS 2 of the chromadepth pair — the MARCH. Expects its input to be the
// output of chromadepth_bake.shader ("depth bake" filter ordered before
// this one): rgb = original color, a = the baked lifted/reassigned field
// depth (uncapped, prefiltered source — see the bake header). Every depth
// read here is ONE texture tap; all donor/witness/gate work happens once
// per source pixel in the bake. Output alpha is forced to 1.0 — input
// alpha is DATA, not opacity.
//
// Z-ORDERED GATHER (depth march) — the fragment-shader equivalent of the
// reference splat in tools/stereo_oracle.py. For each destination pixel,
// scan the source window that could project onto it; a crossing is
// accepted only by SPLAT VERIFICATION (the oracle's exact bin test).
// March structure (pre-scan + far early-out, interior fast-accept,
// bracket restart, walk-up, jump-verify, 2-round bisection, VERIFY3,
// thin-feature rescue, src-row snap) is unchanged from the converged
// b7ae55e build — every piece bought with a measured failure; see git
// history for the war stories.
//
// Sliders (CV-driven via the control bridge):
//   depth — parallax amount (squared curve, max 8% of the eye view).
//   hue_rotation / color_weight moved to the BAKE filter (the field owns
//   them).

uniform float depth<
    string label = "Depth";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

// Test-harness probe (leave at 0): 1 = show the baked field as grayscale;
// 2 = path trace (each exit returns float4(path, crossings/8, dmax, 1)).
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
// walk-up below recovers stepped-over slivers (verified: strays stay in
// the single digits).
#define MARCH_STEPS  48
#define PIC_LO 0.010
#define PIC_HI 0.985

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

// Baked field, UNCAPPED — splat-verification candidates read at texel
// centers, where .a is the exact per-pixel field value.
float field_exact(float x, float y) {
    return image.Sample(textureSampler, safe_uv(x, y)).a;
}

// Capped for the march h-test: at d exactly 1.0, h(t=1) = 0 and the
// strict crossing test never fires — the NEAREST surface becomes
// undetectable. The verification uses the uncapped value anyway.
float depth_field(float x, float y, float f0) {
    return min(image.Sample(textureSampler, safe_uv(x, y)).a, 0.995)
           * edge_taper(x, f0);
}

// With the bake, the quick read IS the field read (one tap, bilinear
// between texel centers — fine for LOCATING; acceptance is texel-exact).
float depth_quick(float x, float y, float f0) {
    return depth_field(x, y, f0);
}

// Oracle bin test for one candidate source pixel: does any of its 4
// subsamples (±0.375/±0.125 px, taper evaluated AT THE SUBSAMPLE — in the
// edge ramp the disparity gradient reaches ~1 px/px), displaced by the
// pixel's own field depth, land inside THIS fragment's dest bin?
// Returns 1 when AT LEAST TWO subsamples land in the bin (a single
// straggler subsample is the width of the 8-bit-vs-float quantization —
// accepting it paints a ghost bin just outside the oracle's run).
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

// Try the 3 source pixel centers bracketing SCENTER against this
// fragment's bin; leaves the nearest passer in win_d/win_x (win_d < 0 if
// none). The >= comparison: equal-depth ties go to the LARGEST x
// (candidates ascend), bit-matching the oracle's stable z-sort.
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
            return float4(image.Sample(textureSampler,                      \
                              safe_uv(win_x, src_y)).rgb, 1.0);              \
        }                                                                    \
    }                                                                        \
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    if (debug_mode > 0.5 && debug_mode < 1.5) {
        // Field probe: grayscale baked depth at this texel.
        float xq = (floor(uv.x / uv_pixel_interval.x) + 0.5)
                   * uv_pixel_interval.x;
        float yq = (floor(uv.y / uv_pixel_interval.y) + 0.5)
                   * uv_pixel_interval.y;
        float dp = field_exact(xq, yq);
        return float4(dp, dp, dp, 1.0);
    }
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
    // Snap to the texel-center row the oracle reads, in the oracle's own
    // arithmetic FORM, with a −1e-4 tie-break toward the LOWER row (the
    // f64-dust rationale lives in git history at b7ae55e).
    float pxh = uv_pixel_interval.y;
    float Hpx = 1.0 / pxh;
    float src_row = floor(f0 * Hpx
                          + (floor(uv.y * Hpx) + 0.5) * (1.0 - 2.0 * f0)
                          - 1e-4);
    float src_y = (src_row + 0.5) * pxh;
    // One dest pixel covers span/960 of source uv (= 2*span*pxw); a splat
    // lands in THIS fragment's bin iff |dest − p| < span*pxw.
    float pxw = uv_pixel_interval.x;
    float half_bin = (vis_hi - vis_lo) * pxw;

    if (f0 < 0.0005)
        return float4(image.Sample(textureSampler, safe_uv(p, src_y)).rgb,
                      1.0);

    // Coarse pre-scan: bound the depth present in this pixel's window.
    // The own-position tap is taken at the BIN-TOP TEXEL CENTER (the pixel
    // the early-out would paint) — at continuous p the bilinear blend
    // straddles texels and dilutes the field.
    float bc = (floor((p + half_bin) / pxw - 0.125) + 0.5) * pxw;
    float dmax = depth_field(bc, src_y, f0);
    for (int k = 1; k <= 6; k++)
        dmax = max(dmax, depth_quick(p + dir * (float(k) / 6.0) * f0,
                                     src_y, f0));
    // Above f0 ≈ 0.044 the k/6 spacing exceeds ~14 px and a hard 16 px
    // feature can sit wholly between taps — midpoint taps halve the
    // spacing.
    if (f0 > 0.0437) {
        for (int k = 1; k <= 6; k++)
            dmax = max(dmax, depth_quick(
                p + dir * ((float(k) - 0.5) / 6.0) * f0, src_y, f0));
    }

    if (dmax < 0.02) {
        // Far content is undisplaced; among equal depths the oracle's
        // stable z-sort shows the pixel under the bin's upper edge (bc).
        if (dbg2) return float4(0.1, 0.0, dmax, 1.0);
        return float4(image.Sample(textureSampler, safe_uv(bc, src_y)).rgb,
                      1.0);
    }

    // Interior fast-accept: if bc is the NEAREST surface anywhere in this
    // pixel's window (nothing can z-beat it) and its own splat lands in
    // this bin, it wins outright — skips the march for body interiors.
    {
        float du_bc = field_exact(bc, src_y);
        if (du_bc * edge_taper(bc, f0) >= dmax - 0.005 &&
            splat_hits(bc, du_bc, p, dir, f0, half_bin, pxw) > 0.5) {
            if (dbg2) return float4(0.2, 0.0, dmax, 1.0);
            return float4(image.Sample(textureSampler,
                                       safe_uv(bc, src_y)).rgb, 1.0);
        }
    }

    // March disparity layers from the highest plausible down to the
    // farthest. Surface at s = p + dir*t*f0 covers this pixel when
    // d(s) = t; h = t − d(s) flips sign there.
    float t_hi = min(1.0, dmax + 0.06);
    float t_prev = t_hi;
    float s_prev = p + dir * t_hi * f0;
    float d_prev = depth_field(s_prev, src_y, f0);
    float h_prev = t_prev - d_prev;
    if (h_prev <= 0.0) {
        // Pre-scan bound too low (sparse taps straddled a thin or
        // edge-aligned surface). Restart the bracket from t=1, where
        // h = 1 − d ≥ 0.005 holds by construction (read-time cap).
        t_hi = 1.0;
        t_prev = 1.0;
        s_prev = p + dir * f0;
        d_prev = depth_field(s_prev, src_y, f0);
        h_prev = 1.0 - d_prev;
    }

    for (int i = 1; i <= MARCH_STEPS; i++) {
        float t = t_hi * (1.0 - float(i) / float(MARCH_STEPS));
        float s = p + dir * t * f0;
        float h = t - depth_quick(s, src_y, f0);
        if (h_prev > 0.0 && h <= 0.0) {
            n_cross += 1.0;
            float ta = t_prev; float sa = s_prev;
            float ha = t_prev - depth_field(s_prev, src_y, f0);
            float tb = t;      float sb = s;
            float hb = t - depth_field(s, src_y, f0);
            if (ha <= 0.0) {
                // The field sees a lifted fade above this bracket. Recover
                // the true bracket by walking UP in half-steps.
                float t_base = ta;
                tb = ta; sb = sa; hb = ha;   // old top = negative side
                bool re = false;
                for (int u = 1; u <= 4; u++) {
                    float tu = t_base
                               + 0.5 * float(u) * (t_hi / float(MARCH_STEPS));
                    if (tu > 1.0) break;
                    float su = p + dir * tu * f0;
                    float hu = tu - depth_field(su, src_y, f0);
                    if (hu > 0.0) { ta = tu; sa = su; ha = hu; re = true; break; }
                    tb = tu; sb = su; hb = hu;
                }
                if (!re) {
                    // WIDE lifted region: jump to the field's own layer by
                    // bisection on [tb, 1] and verify the splat there — the
                    // exact verification still rejects trailing-silhouette
                    // ramps, so this cannot paint ghosts.
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
                        return float4(image.Sample(textureSampler,
                                          safe_uv(win_x, src_y)).rgb, 1.0);
                    }
                    t_prev = t; s_prev = s;
                    h_prev = max(h, t - depth_field(s, src_y, f0));
                    continue;
                }
            }
            if (hb > 0.0) {
                // Over-read (noise/fade) — no real crossing here.
                t_prev = t; s_prev = s; h_prev = hb;
                continue;
            }
            // 2 bisection iterations: the coarse bracket is ≤ ~2.3 px and
            // the 3 verification candidates span ±1.5 px around s_hit.
            for (int r = 0; r < 2; r++) {
                float tm = 0.5 * (ta + tb);
                float sm = p + dir * tm * f0;
                float hm = tm - depth_field(sm, src_y, f0);
                if (hm > 0.0) { ta = tm; sa = sm; ha = hm; }
                else          { tb = tm; sb = sm; hb = hm; }
            }
            float w = ha / (ha - hb);
            float s_hit = lerp(sa, sb, w);
            // SPLAT VERIFICATION — the exact oracle bin test; among
            // passers the nearest (highest tapered depth) wins.
            {
                VERIFY3(s_hit)
                if (win_d >= 0.0) {
                    if (dbg2) return float4(0.4, n_cross / 8.0, dmax, 1.0);
                    return float4(image.Sample(textureSampler,
                                      safe_uv(win_x, src_y)).rgb, 1.0);
                }
            }
            // no pixel splats into this bin at this layer — keep marching
        }
        t_prev = t;
        s_prev = s;
        h_prev = h;
    }

    // Thin-feature rescue: a 1-2 px line at high depth can fall BETWEEN
    // the coarse march steps and never bracket. Re-test each pre-scan
    // tap's SELF-COVER, nearest layer first (z-order).
    RESCUE(6.0) RESCUE(5.0) RESCUE(4.0)
    RESCUE(3.0) RESCUE(2.0) RESCUE(1.0)

    // No surface projects here: a disocclusion. Show the infinity backdrop
    // (black) — the gap belongs to the BACKGROUND (oracle-locked).
    if (dbg2) return float4(1.0, n_cross / 8.0, dmax, 1.0);
    return float4(0.0, 0.0, 0.0, 1.0);
}
