// stereo_displace_b : stereo_displace with narrow 3-tap depth blur (1px
// spacing) to reduce aliasing at depth discontinuities.
// obs-shaderfilter port of structure_tools/glsl/fx/0_stereo_displace_b.glsl
//
// Supports both luma and chromadepth modes. The blur smooths the depth
// map before displacement, reducing stairstepping where hard color edges
// in the source become jagged displacement jumps.

uniform float depth<
    string label = "Depth";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float contrast<
    string label = "Contrast";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

uniform float mode<
    string label = "Mode (L=luma, R=chromadepth)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.0;

float glsl_mod(float x, float y) {
    return x - y * floor(x / y);
}

float rgb2hue(float3 c) {
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float delta = cmax - cmin;
    if (delta < 0.001) return 0.0;
    float hue;
    if (cmax == c.r)
        hue = glsl_mod((c.g - c.b) / delta, 6.0);
    else if (cmax == c.g)
        hue = (c.b - c.r) / delta + 2.0;
    else
        hue = (c.r - c.g) / delta + 4.0;
    return hue / 6.0;
}

float depthAt(float3 c, float f2) {
    if (f2 < 0.5) {
        float offset = f2 * 2.0;
        return frac(dot(c, float3(0.299, 0.587, 0.114)) + offset);
    } else {
        float offset = (f2 - 0.5) * 2.0;
        return 1.0 - frac(rgb2hue(c) + offset);
    }
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float f0 = lerp(0.0, 0.08, depth * depth);
    float f1 = lerp(1.0, 4.0, contrast * contrast);
    float f2 = mode;

    float margin = f0 * 0.5;

    float local_x;
    if (uv.x < 0.5)
        local_x = uv.x * 2.0;
    else
        local_x = (uv.x - 0.5) * 2.0;
    float base_x = margin + local_x * (1.0 - 2.0 * margin);

    // 3-tap depth blur (1px spacing) to smooth aliasing at depth edges
    float px = uv_pixel_interval.x;
    float3 cL = image.Sample(textureSampler, float2(base_x - px, uv.y)).rgb;
    float3 cC = image.Sample(textureSampler, float2(base_x,      uv.y)).rgb;
    float3 cR = image.Sample(textureSampler, float2(base_x + px, uv.y)).rgb;

    float dL = depthAt(cL, f2);
    float dC = depthAt(cC, f2);
    float dR = depthAt(cR, f2);

    // Weighted average (1,2,1)/4
    float d = (dL + 2.0 * dC + dR) / 4.0;

    d = clamp(0.5 + (d - 0.5) * f1, 0.0, 1.0);

    float displacement = (d - 0.5) * f0;
    float source_x;
    if (uv.x < 0.5)
        source_x = base_x + displacement;
    else
        source_x = base_x - displacement;

    source_x = clamp(source_x, 0.0, 1.0);
    return image.Sample(textureSampler, float2(source_x, uv.y));
}
