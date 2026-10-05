// stereo_soften — post-splat visual softness, identical in both eyes.
//
// synth_color's `soften` runs BEFORE the depth bake; since color IS
// depth in this rig, pre-bake blur feathers color edges into DEPTH
// RAMPS, and the splat then smears those ramps in opposite directions
// per eye — the eyes disagree about edge shapes and fusion suffers.
// This filter sits AFTER the stereo splat: both eyes get the exact
// same blur, depth stays crisp. Keep synth_color soften at 0.
//
// Half-SBS aware: samples are clamped inside each eye's half so the
// blur never bleeds across the center seam (or wraps at the borders).
// Kernel is a dense 13-tap two-ring disc — smooth at every strength,
// no tap-separation ghosting (the synth_color 4-tap trap).

uniform float soften<
    string label = "Soften (0 = off)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

float2 clamp_eye(float2 uv, float xmin, float xmax, float2 half_px) {
    return float2(clamp(uv.x, xmin + half_px.x, xmax - half_px.x),
                  clamp(uv.y, half_px.y, 1.0 - half_px.y));
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float3 c = image.Sample(textureSampler, uv).rgb;
    if (soften <= 0.001)
        return float4(c, 1.0);

    float is_left = step(uv.x, 0.5);
    float xmin = lerp(0.5, 0.0, is_left);
    float xmax = lerp(1.0, 0.5, is_left);
    float2 half_px = 0.5 * uv_pixel_interval;

    float r = (0.75 + 3.25 * soften);          // outer ring radius, px
    float2 ro = r * uv_pixel_interval;         // outer ring step
    float2 ri = 0.5 * ro;                      // inner ring step
    float s45 = 0.7071068;

    // inner ring: 4 axis taps at r/2
    float3 inner =
          image.Sample(textureSampler, clamp_eye(uv + float2( ri.x, 0.0), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(-ri.x, 0.0), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(0.0,  ri.y), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(0.0, -ri.y), xmin, xmax, half_px)).rgb;

    // outer ring: 8 taps (4 axis + 4 diagonal) at r
    float3 outer =
          image.Sample(textureSampler, clamp_eye(uv + float2( ro.x, 0.0), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(-ro.x, 0.0), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(0.0,  ro.y), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(0.0, -ro.y), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2( ro.x * s45,  ro.y * s45), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(-ro.x * s45,  ro.y * s45), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2( ro.x * s45, -ro.y * s45), xmin, xmax, half_px)).rgb
        + image.Sample(textureSampler, clamp_eye(uv + float2(-ro.x * s45, -ro.y * s45), xmin, xmax, half_px)).rgb;

    float3 b = 0.30 * c + 0.40 * (inner * 0.25) + 0.30 * (outer * 0.125);
    c = lerp(c, b, soften);
    return float4(c, 1.0);
}
