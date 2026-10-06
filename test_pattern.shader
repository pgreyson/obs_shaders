// test_pattern : substitute the live feed with a known color test frame.
//
// Sits FIRST on the capture source, BEFORE "synth color" — enabling this
// filter replaces the Elgato frame with a procedural pattern, so the entire
// real chain (synth color -> palette -> depth bake -> splat) processes a
// fully known input. Toggle with the filter's enable checkbox; disabled it
// is exact passthrough of the live video.
//
// Rows: [0.0..0.5)  hue sweep (x) x luma ramp (y)     — the main field
//       [0.5..2/3)  clean full rainbow, sat=1 val=1
//       [2/3..5/6)  saturation ramp 0->1 (tests the palette's chroma gate)
//       [5/6..1.0]  grayscale ramp (must pass every stage un-tinted)

// Random-dot mode: the canonical stereo stimulus (Julesz). Periodic or
// smooth patterns are ambiguous/featureless for binocular matching — a
// depth bulge can be imperceptible on them while random dots make ANY
// disparity field unambiguously visible.
uniform float noise_mode<
    string label = "Random-dot mode (0 = color bands, 1 = dots)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 1.0;
> = 0.0;

uniform float dot_size<
    string label = "Dot size (px)";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 8.0;
    float step = 1.0;
> = 3.0;

float hash12(float2 p) {
    return frac(sin(dot(p, float2(127.1, 311.7))) * 43758.5453);
}

float3 hsv2rgb3(float h, float s, float v) {
    float r = clamp(abs(h * 6.0 - 3.0) - 1.0, 0.0, 1.0);
    float g = clamp(2.0 - abs(h * 6.0 - 2.0), 0.0, 1.0);
    float b = clamp(2.0 - abs(h * 6.0 - 4.0), 0.0, 1.0);
    return v * lerp(float3(1.0, 1.0, 1.0), float3(r, g, b), s);
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    if (noise_mode > 0.5) {
        float2 cell = floor(uv / (dot_size * uv_pixel_interval));
        float n = step(0.5, hash12(cell));
        // mostly binary luma dots with mild hue variety so the chain's
        // color stages still have something to chew on
        float hue = hash12(cell + 7.31);
        float3 cn = lerp(float3(0.08, 0.08, 0.08),
                         hsv2rgb3(hue, 0.35, 1.0), n);
        return float4(cn, 1.0);
    }
    float h = uv.x;
    float3 c;
    if (uv.y < 0.5) {
        c = hsv2rgb3(h, 1.0, 1.0 - (uv.y / 0.5) * 0.95);
    } else if (uv.y < 0.6667) {
        c = hsv2rgb3(h, 1.0, 1.0);
    } else if (uv.y < 0.8333) {
        c = hsv2rgb3(h, (uv.y - 0.6667) / 0.1667, 0.85);
    } else {
        c = float3(uv.x, uv.x, uv.x);
    }
    return float4(c, 1.0);
}
