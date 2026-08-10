// palette_quantize : continuous probabilistic palette — a smooth hue
// likelihood with N peaks that colors flow toward. NO quantization.
//
// Sits BETWEEN "synth color" and "depth bake". Because in this rig color IS
// depth (the bake's wheel_depth(hue)), concentrating hue upstream
// concentrates the depth field too: the N hue peaks become N soft depth
// strata. This is a color instrument AND a depth-structure instrument.
//
// MODEL — von Mises comb / circular softmax. Define a smooth likelihood over
// the hue wheel with N equally spaced peaks:
//     L(h) ∝ exp(kappa · cos(2π·N·(h − rotation)))
// (the circular analog of a Gaussian mixture — literally softmax of a cosine
// logit). Rather than assigning/rounding, each pixel's hue flows CONTINUOUSLY
// up the log-likelihood gradient toward nearby high-probability hue. That
// gradient is one sine, so the whole map is closed form:
//     h_out = h − pull · sin(2π·N·(h − rotation)) / (2π·N)
// For pull < 1 this is a smooth, monotonic diffeomorphism — it never snaps;
// colors just concentrate toward the peaks and thin out between them, so the
// output hue distribution grows N soft peaks. No arrays, no buckets: N is a
// continuous, unbounded parameter (fractional N fades a peak in/out).
//
// Parameters map to the distribution directly:
//   size  — number of likelihood peaks N (continuous)
//   rotation — peak phase (which hues the peaks sit on; feeds the bake's
//              "which hue is forward" coupling)
//   hue_pull — softmax temperature / concentration: 0 = flat likelihood =
//              passthrough, →1 = sharp peaks = strong pull (hard collapse
//              only in the limit)
//   skew  — second-harmonic asymmetry: 0 = symmetric (Gaussian-ish) peaks,
//           up = skewed (lognormal-ish) basins
// The same sine-attractor shapes luma into luma_peaks continuous tonal peaks
// (luma_pull), and chroma is pulled continuously toward chroma_target.
// black_floor crushes the tonal field downward (the old black-point gesture:
// darkest content → far plane). ALL *_pull at 0 (defaults) = exact
// passthrough (repo convention).

uniform float size<
    string label = "Hue peaks N (continuous)";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 16.0;
    float step = 0.1;
> = 6.0;

uniform float rotation<
    string label = "Rotation (peak phase around the wheel)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float hue_pull<
    string label = "Hue pull (softmax temp: 0 = off, 1 = sharp)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float skew<
    string label = "Skew (0 = symmetric, up = lognormal-ish peaks)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float luma_peaks<
    string label = "Luma peaks (continuous tonal strata)";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 8.0;
    float step = 0.1;
> = 3.0;

uniform float luma_pull<
    string label = "Luma pull (0 = off)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float black_floor<
    string label = "Black floor (crush tonal field down — old black point)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float chroma_pull<
    string label = "Chroma pull toward target (0 = off)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float chroma_target<
    string label = "Chroma target (saturation the pull moves toward)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 1.0;

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
    float3 c = image.Sample(textureSampler, uv).rgb;

    // All pulls off -> exact passthrough, no HSV round trip.
    if (hue_pull < 0.001 && luma_pull < 0.001 && chroma_pull < 0.001)
        return float4(c, 1.0);

    // ---- decompose
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float delta = cmax - cmin;
    float h = hue_of(c, delta, cmax);
    float s = delta / max(cmax, 1e-5);
    float v = cmax;

    // ---- hue: continuous flow up the von Mises comb log-likelihood, THEN
    // a rigid rotation of the whole palette. Concentrating toward FIXED
    // peaks (phase 0) and rotating afterwards means `rotation` sweeps every
    // color a full turn around the wheel (0..1 = 360°), instead of only
    // phasing the peaks (which just wobbles each color in its own basin).
    //   h' = h - pull * (sin θ + skew*0.5*sin 2θ) / (2π N),  θ = 2π N h
    //   h_out = frac(h' + rotation)
    // Monotonic for pull < 1 (never snaps); colors concentrate on the N
    // peaks. skew's second harmonic tilts the basins (lognormal-ish).
    if (hue_pull > 0.001) {
        float n = max(size, 1e-3);
        float theta = TAU * n * h;
        float grad = sin(theta) + skew * 0.5 * sin(2.0 * theta);
        h = frac(h - hue_pull * grad / (TAU * n) + rotation);
    }

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
    return float4(clamp(outc, 0.0, 1.0), 1.0);
}
