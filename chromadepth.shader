// chromadepth : mono → half-SBS stereo from color, black at infinity.
//
// Z-ORDERED GATHER (depth march) — the fragment-shader equivalent of
// splatting every source pixel to its disparity-shifted position with the
// nearest winning. For each destination pixel, scan the source window that
// could project onto it, find the pixels whose own disparity lands them
// here, show the nearest. Occlusion is resolved identically in both eyes,
// and hard-edged shapes translate RIGIDLY (no pillowing — the march needs
// no fold-free wide blur, because it resolves folds by ordering).
//
// Final form carrying every lesson of the 2026-06 sessions:
//   • cosine color wheel (a sawtooth wheel must cliff at some hue — it
//     showed as a noisy vertical bar that moved with hue_rotation)
//   • luma³ baseline: black=infinity, highlights near; mid-bright washes
//     settle back instead of hovering at the viewer in luma mode
//   • delta-based hue confidence: hue is trusted only where chroma
//     amplitude clears the analog noise floor (ratio-based gates let
//     moderate-sat noise through as displacement jitter)
//   • reveals continue the background (sample own position), NEVER paint
//     synthetic black — black speckle on gradients came from that
//   • silhouette test at fixed ±3 px scale after bisection (analog edges
//     are a few px wide; sub-pixel tests read every soft cliff as a wall)
//   • crossed disparity (left-eye image shifts right = pops forward),
//     settled empirically on this rig
//   • analog border crop + clamp-extend ("more of the same" past edges:
//     full-width content never shortens); sampler wraps at uv 0/1
//   • disparity taper to zero at the display edges + display inset by f0:
//     straight window edges, identical in both eyes
//   • squared depth curve, 8% ceiling: artifact magnitude IS displacement
//     magnitude — the original Structure shader's real secret
//   • far-pixel pre-scan skip (NO "smooth region" naive shortcut — that
//     reintroduced destination-depth edge erosion as zigzag teeth)
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
#define MARCH_STEPS  48
#define DEPTH_GAP    0.18
#define PIC_LO 0.010
#define PIC_HI 0.985

float glsl_mod(float x, float y) {
    return x - y * floor(x / y);
}

float depth_for(float3 c) {
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float delta = cmax - cmin;

    float hue = 0.0;
    if (delta > 0.001) {
        if (cmax == c.r)
            hue = glsl_mod((c.g - c.b) / delta, 6.0);
        else if (cmax == c.g)
            hue = (c.b - c.r) / delta + 2.0;
        else
            hue = (c.r - c.g) / delta + 4.0;
        hue /= 6.0;
    }

    float luma = dot(c, float3(0.299, 0.587, 0.114));

    // Floor-lifted cosine wheel: no hue ever lands exactly ON the black
    // infinity plane (at rotation 0 cyan otherwise glued to the backdrop).
    float chroma_depth = 0.12 + 0.88 * (0.5 + 0.5 * cos(6.2831853 * (hue + hue_rotation)));
    float confidence = smoothstep(0.06, 0.30, delta);
    float luma_base = luma * luma * luma;
    return lerp(luma_base, chroma_depth, confidence * color_weight);
}

// All sampling stays inside the real picture (analog border excluded);
// beyond-edge reads extend the edge pixels. The sampler wraps at uv 0/1.
float2 safe_uv(float x, float y) {
    return float2(clamp(x, PIC_LO, PIC_HI), y);
}

// Disparity tapers to zero at the display-window edges so edge content is
// identical in both eyes and the window edge stays straight.
float edge_taper(float x, float f0) {
    float edge_dist = min(x - (PIC_LO + f0), (PIC_HI - f0) - x);
    return smoothstep(0.0, 1.5 * f0 + 0.001, edge_dist);
}

// Narrow cross kernel: 3 horizontal taps (4 px) for noise stability plus 2
// vertical taps (8 px) for row coherence. Narrow on purpose — the march
// handles depth cliffs by z-ordering, so shapes keep hard edges.
float depth_at(float x, float y, float f0) {
    float px = 4.0 * uv_pixel_interval.x;
    float py = 8.0 * uv_pixel_interval.y;
    float dL = depth_for(image.Sample(textureSampler, safe_uv(x - px, y)).rgb);
    float dC = depth_for(image.Sample(textureSampler, safe_uv(x,      y)).rgb);
    float dR = depth_for(image.Sample(textureSampler, safe_uv(x + px, y)).rgb);
    float dU = depth_for(image.Sample(textureSampler, safe_uv(x, y - py)).rgb);
    float dD = depth_for(image.Sample(textureSampler, safe_uv(x, y + py)).rgb);
    float d = (dL + 2.0 * dC + dR + dU + dD) / 6.0;
    return d * edge_taper(x, f0);
}

// Single-tap depth for the coarse pre-scan.
float depth_quick(float x, float y, float f0) {
    float d = depth_for(image.Sample(textureSampler, safe_uv(x, y)).rgb);
    return d * edge_taper(x, f0);
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
    // Far pixels (most of a synth frame) resolve immediately.
    float dmax = 0.0;
    for (int k = 0; k <= 8; k++)
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
    float d_prev = depth_at(s_prev, src_y, f0);
    float h_prev = t_prev - d_prev;
    if (h_prev <= 0.0) {
        // The pre-scan bound was too low (its sparse taps straddled a thin
        // or edge-aligned surface). Do NOT accept this layer — the surface
        // here projects elsewhere; painting it puts wrong content at shape
        // edges. Restart the bracket from t=1, where h = 1 − d ≥ 0 holds
        // by construction.
        t_hi = 1.0;
        t_prev = 1.0;
        s_prev = p + dir * f0;
        d_prev = depth_at(s_prev, src_y, f0);
        h_prev = 1.0 - d_prev;
    }

    for (int i = 1; i <= MARCH_STEPS; i++) {
        float t = t_hi * (1.0 - float(i) / float(MARCH_STEPS));
        float s = p + dir * t * f0;
        float d = depth_at(s, src_y, f0);
        float h = t - d;
        if (h_prev > 0.0 && h <= 0.0) {
            float ta = t_prev; float sa = s_prev; float ha = h_prev;
            float tb = t;      float sb = s;      float hb = h;
            for (int r = 0; r < 5; r++) {
                float tm = 0.5 * (ta + tb);
                float sm = p + dir * tm * f0;
                float hm = tm - depth_at(sm, src_y, f0);
                if (hm > 0.0) { ta = tm; sa = sm; ha = hm; }
                else          { tb = tm; sb = sm; hb = hm; }
            }
            float w = ha / (ha - hb);
            float s_hit = lerp(sa, sb, w);
            float eps = 3.0 * uv_pixel_interval.x;
            float d_lo = depth_at(s_hit - eps, src_y, f0);
            float d_hi = depth_at(s_hit + eps, src_y, f0);
            if (abs(d_hi - d_lo) <= DEPTH_GAP)
                return image.Sample(textureSampler, safe_uv(s_hit, src_y));
            // silhouette — keep marching
        }
        t_prev = t;
        s_prev = s;
        h_prev = h;
    }

    // No surface projects here (a reveal): show the true backdrop — black,
    // infinity. Continuing the background instead (sampling own position)
    // painted each shape's unshifted silhouette into the reveal: a ghost
    // contour at conflicting disparity that gave the visual system two
    // matches per edge and collapsed fusion entirely on hard-edged content.
    return float4(0.0, 0.0, 0.0, 1.0);
}
