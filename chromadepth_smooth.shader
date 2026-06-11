// chromadepth_smooth.shader : optional pass between "depth bake" and the
// march ("stereo displace"/"chromadepth") — REGIONAL-DEPTH smoothing.
//
// Gaussian-blurs ONLY the baked depth (alpha); rgb passes through from the
// center texel untouched. Smoothing the FINAL field (after the bake's
// donor/lift/reassignment rules) is the convention the user selected
// 2026-06-10: depth cliffs become ramps wider than the disocclusion gap
// they would open, so textured analog content stops punching 1-6px black
// pepper holes beside every bright feature and sloped contacts stop
// tearing into staircase zippers. Both artifacts were reproduced in PURE
// ORACLE renders of a captured synth frame — they are properties of the
// old reveals-stay-black convention on texture, not shader bugs.
//
// Two filter instances of THIS file run back to back (separable blur):
//   "field smooth h"  direction = 0
//   "field smooth v"  direction = 1
//
// Kernel: 21-tap discrete Gaussian, σ_eff = sqrt(round(2σ²)/2) (quantized
// so tiny σ snaps to off), radius 10, edge-clamped taps. BIT-THE-SAME
// construction as stereo_oracle.field_smooth() — change both together.
// smoothing = 0 is an exact passthrough: the σ=0 rigid-shape sign-off
// (and the whole existing harness matrix) is untouched.
//
// Alpha arrives encoded a = 0.5 + d/2 (see chromadepth_bake.shader: the
// chain blacks out rgb where a ≈ 0). The encoding is affine, so blurring
// in encoded space IS blurring the depth — no decode/re-encode needed.

uniform float smoothing<
    string label = "Field smoothing sigma (px, 0 = off)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.5;
    float step = 0.05;
> = 0.0;

uniform float direction<
    string label = "Pass direction (0 = horizontal, 1 = vertical)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 1.0;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    // Texel-center snap, same as the bake: the march reads .a at centers.
    float xq = (floor(v_in.uv.x / uv_pixel_interval.x) + 0.5)
               * uv_pixel_interval.x;
    float yq = (floor(v_in.uv.y / uv_pixel_interval.y) + 0.5)
               * uv_pixel_interval.y;
    float4 center = image.Sample(textureSampler, float2(xq, yq));

    float n = round(2.0 * smoothing * smoothing);
    if (n < 0.5)
        return center;
    float sig2 = n * 0.5;          // σ_eff² = round(2σ²)/2

    float sx = 0.0;
    float sy = 0.0;
    if (direction < 0.5)
        sx = uv_pixel_interval.x;
    else
        sy = uv_pixel_interval.y;

    // Edge-clamp to the half-texel centers (replicates the edge texel —
    // oracle np.pad mode="edge"). The sampler WRAPS at uv 0/1, so without
    // this the blur would smear top rows into bottom (and L/R frame edges
    // together) at radius 10.
    float x_lo = 0.5 * uv_pixel_interval.x;
    float y_lo = 0.5 * uv_pixel_interval.y;

    float acc = 0.0;
    float wsum = 0.0;
    for (int i = -10; i <= 10; i++) {
        float fi = float(i);
        float w = exp(-(fi * fi) / (2.0 * sig2));
        float tx = clamp(xq + fi * sx, x_lo, 1.0 - x_lo);
        float ty = clamp(yq + fi * sy, y_lo, 1.0 - y_lo);
        acc += w * image.Sample(textureSampler, float2(tx, ty)).a;
        wsum += w;
    }
    return float4(center.rgb, acc / wsum);
}
