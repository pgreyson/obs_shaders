// synth_color : pre-stereo color conditioning for the analog synth feed.
//
// Sits FIRST in the chain, before "depth bake" — the synth has limited
// onboard control over these functions. NOTE the depth coupling: in this
// rig color IS depth (hue -> wheel, luma^3 -> baseline, chroma delta ->
// confidence), so every control here is also a depth-content control:
//   black_level crushes the analog noise floor BELOW the bake's lit/
//     backdrop gates (cleaner reveals), saturation drives how much
//     content rides the hue wheel vs the luma baseline, hue_shift
//     re-colors AND re-depths simultaneously (unlike the bake's
//     hue_rotation, which remaps depth while leaving colors alone).
//
// All sliders 0..1 for CV mapping. NEUTRAL = the bridge's unpatched
// resting value: 0.5 for contrast/gamma/saturation/hue_shift (0V CV),
// 0.0 for black_level/chroma_smooth/soften. With every control neutral
// the shader is an exact passthrough.
//
// Order of operations: soften -> chroma smooth -> black level ->
// contrast -> gamma -> saturation -> hue rotate.

uniform float black_level<
    string label = "Black level crush (0 = off)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float contrast<
    string label = "Contrast (0.5 = neutral)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float gamma<
    string label = "Gamma (0.5 = neutral)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float saturation<
    string label = "Saturation (0.5 = neutral, 0 = mono)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float hue_shift<
    string label = "Hue rotate (0.5 = neutral, full wheel range)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float chroma_smooth<
    string label = "Chroma smoothing (denoise color, keep luma sharp)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float soften<
    string label = "Soften (small full blur)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

#define LUMA_W float3(0.299, 0.587, 0.114)

// 4 bilinear corner taps at ±r = a cheap, alias-free disc blur (exact
// 3x3 binomial at r = 0.5 texels; wider r trades exactness for reach
// but stays bilinear-interpolated, unlike sparse-tap kernels).
float3 blur4(float2 uv, float r) {
    float2 o = r * uv_pixel_interval;
    return 0.25 * (image.Sample(textureSampler, uv + float2(-o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x, -o.y)).rgb
                 + image.Sample(textureSampler, uv + float2(-o.x,  o.y)).rgb
                 + image.Sample(textureSampler, uv + float2( o.x,  o.y)).rgb);
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float3 c = image.Sample(textureSampler, uv).rgb;

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

    return float4(c, 1.0);
}
