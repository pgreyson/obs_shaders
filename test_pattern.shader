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
    string label = "Pattern";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 4.0;
    float step = 1.0;
> = 0.0;

uniform float dot_size<
    string label = "Dot size (px)";
    string widget_type = "slider";
    float minimum = 1.0;
    float maximum = 8.0;
    float step = 1.0;
> = 3.0;

// Pattern 4 shows this picture: test_patterns/cases.png, written by
// tools/convergence_cases.py (one panel per stereo defect). Set the file once in
// the filter panel; the regression suite measures the same picture off-air.
uniform texture2d cases_image<
    string label = "Cases image";
>;

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
    if (noise_mode > 3.5)
        return float4(cases_image.Sample(textureSampler, uv).rgb, 1.0);
    if (noise_mode > 2.5) {
        // SYNTH BARS — what the real synth picture is made of, which the flat
        // blocks are not: soft-edged bright bars on black, each with a hue
        // gradient across its body (violet core to pink edge) and a dim halo of
        // a DIFFERENT hue (olive) around it, bending and fading down the frame.
        // Hue depth puts a bar's body and its own halo on different planes, so
        // this is the case where one eye sees the halo left of the bar and the
        // other sees it on the right (seen live 2026-11-22).
        float u = uv.x + 0.035 * sin(uv.y * 9.42);
        float cell = frac(u * 5.0) - 0.5;
        float body = exp(-cell * cell / (2.0 * 0.07 * 0.07));
        float halo = exp(-cell * cell / (2.0 * 0.17 * 0.17));
        float env = 0.25 + 0.75 * abs(cos(uv.y * 4.71));
        float3 cb = hsv2rgb3(lerp(0.90, 0.76, body), 0.65, body * env);
        float3 ch = hsv2rgb3(0.17, 0.70, 0.22 * halo * (1.0 - body) * env);
        return float4(cb + ch, 1.0);
    }
    if (noise_mode > 1.5) {
        // CONVERGENCE BLOCKS — the same layout tools/convergence_test.py measures.
        // Discrete flat hues (each one depth plane, hard edges between them) with
        // a random vertical-stripe brightness texture: it leaves hue and
        // saturation alone, so it does not change depth, and gives the eyes fine
        // vertical marks to fuse. Rows: 8 flat bands / continuous hue ramp /
        // the 8 bands fading to black. In a correct pair every stripe is a
        // straight vertical line in both eyes and each band is one flat plane.
        float stripe = 0.55 + 0.45 * hash12(float2(floor(uv.x / (6.0 * uv_pixel_interval.x)), 3.7));
        float band_h = (floor(uv.x * 8.0) + 0.5) / 8.0;
        if (uv.y < 0.3333)
            return float4(hsv2rgb3(band_h, 1.0, stripe), 1.0);
        if (uv.y < 0.6667)
            return float4(hsv2rgb3(uv.x, 1.0, stripe), 1.0);
        return float4(hsv2rgb3(band_h, 1.0, stripe * (1.0 - (uv.y - 0.6667) / 0.3333)), 1.0);
    }
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
