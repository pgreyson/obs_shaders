// synth_color_palette : synth_color + palette_quantize in ONE pass (2026-11-18).
//
// The two filters ran back to back and both are per-pixel colour maps, so the
// merge is exact: first synth_color's conditioning (soften -> chroma smooth ->
// black level -> contrast -> gamma -> saturation -> hue rotate), then
// palette_quantize's concentration (hue peaks / pull / skew / rotation, luma
// peaks, chroma pull) on the result. Same controls, same names. Why: every
// pass on the 3840x1080 canvas costs 2-4 ms on this machine regardless of
// what it computes. synth_color.shader and palette_quantize.shader stay in the
// repo as the reference (and the full commentary) for each half.
//
// Sits FIRST in the synth chain, before "depth bake". Colour IS depth in this
// rig, so every control here is also a depth-content control.

// ==== 1. conditioning (synth_color) =======================================
uniform float black_level<
    string label = "Black level crush";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float contrast<
    string label = "Contrast";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float gamma<
    string label = "Gamma";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float saturation<
    string label = "Saturation";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float hue_shift<
    string label = "Hue rotate";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float chroma_smooth<
    string label = "Chroma smoothing";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float soften<
    string label = "Soften";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

// ==== 2. palette (palette_quantize) =======================================
uniform float size<
    string label = "Hue peaks N";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 16.0;
    float step = 0.1;
> = 6.0;

uniform float rotation<
    string label = "Rotation";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float hue_pull<
    string label = "Hue pull";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float skew<
    string label = "Skew";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float luma_peaks<
    string label = "Luma peaks";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 8.0;
    float step = 0.1;
> = 3.0;

uniform float luma_pull<
    string label = "Luma pull";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float black_floor<
    string label = "Black floor";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float chroma_pull<
    string label = "Chroma pull";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float chroma_target<
    string label = "Chroma target";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 1.0;

#define LUMA_W float3(0.299, 0.587, 0.114)

float3 blur4(float2 uv, float r) {
    float2 o = r * uv_pixel_interval;
    return 0.25 * (image.Sample(textureSampler, uv + float2(-o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2(-o.x,  o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x,  o.y)).rgb);
}

#define TAU 6.2831853

float glsl_mod(float x, float y) {
    return x - y * floor(x / y);
}

// Max-channel HSV hue — same formula as chromadepth_bake.shader so the
// palette and the depth wheel agree on what "hue" means.
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

// Closed-form HSV -> RGB (no arrays, no branches).
float3 hsv2rgb3(float h, float s, float v) {
    float r = clamp(abs(h * 6.0 - 3.0) - 1.0, 0.0, 1.0);
    float g = clamp(2.0 - abs(h * 6.0 - 2.0), 0.0, 1.0);
    float b = clamp(2.0 - abs(h * 6.0 - 4.0), 0.0, 1.0);
    return v * lerp(float3(1.0, 1.0, 1.0), float3(r, g, b), s);
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float4 src = image.Sample(textureSampler, uv);   // alpha preserved
    float3 c = src.rgb;

    // ================= 1. conditioning =================
    // ---- soften: blend toward a small full blur (sub-pixel to ~2px)
    if (soften > 0.001) {
        float3 b = blur4(uv, 0.5 + 1.5 * soften);
        c = lerp(c, b, soften);
    }

    // ---- chroma smoothing: blur color, keep luma sharp. Stabilizes
    // hue (and therefore wheel depth) on noisy analog feeds without
    // softening the image.
    if (chroma_smooth > 0.001) {
        float3 b = blur4(uv, 0.75 + 3.25 * chroma_smooth);
        float y_sharp = dot(c, LUMA_W);
        float y_blur = dot(b, LUMA_W);
        float3 chroma_blurred = b - y_blur;       // blurred color offset
        float3 smoothed = y_sharp + chroma_blurred;
        c = lerp(c, smoothed, chroma_smooth);
    }

    // ---- black level: crush the analog noise floor (then rescale so
    // white stays white). Crushed pixels fall below the depth bake's
    // lit gates -> they read as true backdrop.
    float bl = black_level * 0.3;
    c = max(c - bl, 0.0) / (1.0 - bl);

    // ---- contrast around mid gray: 0.5 neutral, range ~0.5x..2x
    float cs = exp2((contrast - 0.5) * 2.0);
    c = (c - 0.5) * cs + 0.5;
    c = clamp(c, 0.0, 1.0);

    // ---- gamma: 0.5 neutral, range ~0.35..2.8
    float ge = exp2((0.5 - gamma) * 3.0);
    c = pow(c, float3(ge, ge, ge));

    // ---- saturation: 0.5 neutral, 0 = mono, 1 = 2x
    float y = dot(c, LUMA_W);
    c = y + (c - y) * (saturation * 2.0);
    c = clamp(c, 0.0, 1.0);

    // ---- hue rotate around the luma axis (YIQ): 0.5 neutral, edges =
    // ±half wheel. Rotates the COLORS themselves (and with them the
    // wheel depths) — complementary to the bake's hue_rotation, which
    // remaps depth only.
    float ang = (hue_shift - 0.5) * 6.2831853;
    if (abs(ang) > 0.0001) {
        float i = dot(c, float3(0.596, -0.274, -0.322));
        float q = dot(c, float3(0.211, -0.523, 0.312));
        float y2 = dot(c, LUMA_W);
        float ca = cos(ang);
        float sa = sin(ang);
        float i2 = i * ca - q * sa;
        float q2 = i * sa + q * ca;
        c = float3(y2 + 0.956 * i2 + 0.621 * q2,
                   y2 - 0.272 * i2 - 0.647 * q2,
                   y2 - 1.106 * i2 + 1.703 * q2);
        c = clamp(c, 0.0, 1.0);
    }

    // ================= 2. palette =================
    // All pulls off AND no rotation -> exact passthrough, no HSV round trip.
    // rotation is an always-on global hue spin (below), so it counts as
    // active even when nothing is being concentrated. (rotation near 0 or 1
    // is the neutral no-spin case — the wheel is cyclic.)
    bool rot_active = (rotation > 0.001 && rotation < 0.999);
    if (hue_pull < 0.001 && luma_pull < 0.001 && chroma_pull < 0.001 && !rot_active)
        return float4(c, src.a);   // palette idle: the graded colour goes out as is

    // ---- decompose
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float delta = cmax - cmin;
    float h = hue_of(c, delta, cmax);
    float s = delta / max(cmax, 1e-5);
    float v = cmax;

    // ---- hue: ITERATED flow up the von Mises comb log-likelihood, THEN a
    // rigid rotation of the whole palette. One gradient step barely moves a
    // color (max displacement 1/(2πN)); three unrolled steps let hue_pull
    // actually COLLAPSE colors onto the peaks (≈80-90% gather at high pull),
    // so hue_pull reads as "how limited": 0 = full color, 1 = severely
    // reduced to N hues (N=1 → near-monochrome, N=2 → a complementary pair).
    // Composition of diffeomorphisms stays continuous — sharper, never snaps.
    // skew's second harmonic tilts the basins (lognormal-ish).
    #define HUE_STEP { float th_ = TAU * n * h; \
        h = h - hue_pull * (sin(th_) + skew * 0.5 * sin(2.0 * th_)) / (TAU * n); }
    if (hue_pull > 0.001) {
        float n = max(size, 1e-3);
        HUE_STEP
        HUE_STEP
        HUE_STEP
        // The iterated flow concentrates but never fully converges, so the
        // top of the pull range (0.75..1.0) blends in a hard nearest-peak
        // snap with a narrow smoothstep band at the basin boundary — at
        // hue_pull = 1 the output is truly DISCRETE colors with only that
        // band acting as antialiasing at the edges.
        float snap_amt = smoothstep(0.75, 1.0, hue_pull);
        if (snap_amt > 0.001) {
            float hn = h * n;
            float cell = floor(hn);
            float u = hn - cell;
            float aa = 0.04;                    // AA half-band, fraction of a cell
            float hs = (cell + smoothstep(0.5 - aa, 0.5 + aa, u)) / n;
            h = lerp(h, hs, snap_amt);
        }
    }
    // rotation is applied UNCONDITIONALLY (outside the hue_pull gate) so it
    // spins the palette a full turn (0..1 = 360°) at every collapse amount —
    // including the smooth end where hue_pull is ~0. Concentration happens
    // toward fixed peaks (phase 0), then the whole result rotates.
    h = frac(h + rotation);

    // ---- luma: same sine-attractor toward luma_peaks tonal strata, then
    // crush the field downward by black_floor. Continuous, no banding.
    if (luma_pull > 0.001) {
        float lp = max(luma_peaks, 1e-3);
        float phi = TAU * lp * v;
        v = v - luma_pull * sin(phi) / (TAU * lp);
        float bf = black_floor * 0.5;
        v = max(v - bf, 0.0) / (1.0 - bf);
        v = clamp(v, 0.0, 1.0);
    }

    // ---- chroma: continuous pull toward the target saturation, gated by
    // the pixel's own chroma so achromatic pixels never gain invented color.
    float s2 = lerp(s, chroma_target,
                    chroma_pull * smoothstep(0.02, 0.10, s));
    float3 outc = hsv2rgb3(h, s2, v);
    return float4(clamp(outc, 0.0, 1.0), src.a);
}
