// stereo_displace : create stereo depth from monocular input
// obs-shaderfilter port of structure_tools/glsl/fx/0_stereo_displace.glsl
//
// Takes a flat (monocular) video synthesis input and produces half-SBS
// stereo by displacing pixels horizontally based on color/luminance.
// Same principle as ChromaDepth glasses but rendered digitally: color
// or brightness is mapped to depth, then each pixel is shifted in
// opposite horizontal directions for L/R eyes. The displacement between
// eyes IS parallax — your brain reads it as depth.
//
// Depth modes (mode slider):
//   Luminance (left half of slider): brightness = depth.
//   ChromaDepth (right half): hue = depth (red near → violet far).
//
// Overlap cropping: the base coordinate is restricted to [margin, 1-margin]
// where margin = max_disp/2, cropping to the region both eyes can see.

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

// GLSL-style mod (HLSL fmod differs in sign for negative inputs)
float glsl_mod(float x, float y) {
    return x - y * floor(x / y);
}

// Convert RGB to hue (0.0 to 1.0)
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

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float f0 = lerp(0.0, 0.08, depth * depth);   // squared curve, max ~8%
    float f1 = lerp(1.0, 4.0, contrast * contrast);
    float f2 = mode;                              // 0=luminance, 1=chromadepth

    // Overlap crop margin based on max displacement
    float margin = f0 * 0.5;

    // Map output UV to base position in mono source frame
    float local_x;
    if (uv.x < 0.5)
        local_x = uv.x * 2.0;
    else
        local_x = (uv.x - 0.5) * 2.0;
    float base_x = margin + local_x * (1.0 - 2.0 * margin);

    // Sample depth from the base position in the mono source
    float3 color = image.Sample(textureSampler, float2(base_x, uv.y)).rgb;

    // Compute depth value (0.0 to 1.0)
    float d;
    if (f2 < 0.5) {
        float luma = dot(color, float3(0.299, 0.587, 0.114));
        float offset = f2 * 2.0;
        d = frac(luma + offset);
    } else {
        float hue = rgb2hue(color);
        float offset = (f2 - 0.5) * 2.0;
        d = 1.0 - frac(hue + offset);
    }

    // Expand depth contrast — push values away from midpoint
    d = clamp(0.5 + (d - 0.5) * f1, 0.0, 1.0);

    // Displace in opposite directions per eye
    float displacement = (d - 0.5) * f0;
    float source_x;
    if (uv.x < 0.5)
        source_x = base_x + displacement;
    else
        source_x = base_x - displacement;

    source_x = clamp(source_x, 0.0, 1.0);
    return image.Sample(textureSampler, float2(source_x, uv.y));
}
