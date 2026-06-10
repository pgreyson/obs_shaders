// lumadepth : single-purpose stereo from luma, with anchor and falloff controls.
//
// Three CV-exposed sliders, all 0..1:
//   depth        — amount of parallax (linear, max ~12% of canvas width)
//   luma_anchor  — which luma value sits at the screen plane
//                    1.0 → highlights anchor at screen, everything recedes behind (recommended)
//                    0.5 → midtones at screen, bright pops forward / dark recedes (symmetric)
//                    0.0 → darks at screen, brights pop forward aggressively
//   falloff      — curve steepness around the anchor
//                    0.5 → neutral / linear (luma → depth maps 1:1)
//                    < 0.5 → flatter depth field (less spread, depth values squeezed toward anchor)
//                    > 0.5 → steeper (more spread between near and far, more pronounced 3D)
//
// Hard-coded design:
//   • convention: brighter = nearer (matches visual intuition that light objects feel closer)
//   • wide 3-tap pre-blur on the depth map (4px tap spacing) for clean depth edges
//   • no chroma gating needed — unlike chromadepth, luma is stable on dark pixels
//   • dark pixels (black) carry no visible content, so their displacement is "free" — they
//     don't strain the eyes because there's nothing to fuse there, regardless of depth value

uniform float depth<
    string label = "Depth";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

uniform float luma_anchor<
    string label = "Luma anchor (which luma is at screen)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.85;

uniform float falloff<
    string label = "Falloff (depth curve steepness)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

#define MAX_PARALLAX 0.12     // 12% max canvas-width displacement

// Compute the depth value 0..1 for a sampled color, using only luma.
// Convention: brighter pixels are nearer (closer to the screen plane and beyond).
// falloff scales how aggressively depth differentiates from the anchor:
//   falloff = 0   → ~0.3x steepness (flat, depth values cluster near anchor)
//   falloff = 0.5 → 1.0x  (neutral / linear)
//   falloff = 1   → ~3.0x (steep, depth values pushed to extremes from anchor)
float depth_for(float3 c) {
    float luma = dot(c, float3(0.299, 0.587, 0.114));
    float curve_mult = lerp(0.3, 3.0, falloff);
    return clamp(luma_anchor + (luma - luma_anchor) * curve_mult, 0.0, 1.0);
}

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float f0 = depth * MAX_PARALLAX;

    // Source-side margin: sized to the LARGER of the two anchor-direction
    // displacements so source sampling stays in [0,1] for any content.
    float margin = max(luma_anchor, 1.0 - luma_anchor) * f0;

    // Binocular-stretch remap: instead of using the full source [margin,
    // 1-margin] range (which would produce monocular fringes at the eye
    // edges), each eye is mapped to the WORST-CASE binocular base_x range.
    // The crop is asymmetric per eye: dark-content monocular regions sit at
    // each eye's OUTER edge (width 2*anchor*f0 in base_x), bright-content
    // ones at the INNER edge near the seam (width 2*(1-anchor)*f0). The
    // remap stretches the binocular-valid base_x range to fill the eye view.
    //
    // Constant output width by design — no breathing black bars. Trade-off:
    // for non-worst-case content (e.g. highlight pixels when anchor → 1),
    // small monocular regions can still appear at the edges. Lowering
    // luma_anchor toward 0.5 reduces this.
    float outer_crop = 2.0 * luma_anchor * f0;
    float inner_crop = 2.0 * (1.0 - luma_anchor) * f0;

    float local_x;
    float base_x;
    if (uv.x < 0.5) {
        local_x = uv.x * 2.0;
        float left_lo = margin + outer_crop;
        float left_hi = 1.0 - margin - inner_crop;
        base_x = left_lo + local_x * (left_hi - left_lo);
    } else {
        local_x = (uv.x - 0.5) * 2.0;
        float right_lo = margin + inner_crop;
        float right_hi = 1.0 - margin - outer_crop;
        base_x = right_lo + local_x * (right_hi - right_lo);
    }

    // Wide 3-tap depth blur — 4px spacing, ~8px effective kernel
    float px = 4.0 * uv_pixel_interval.x;
    float3 cL = image.Sample(textureSampler, float2(base_x - px, uv.y)).rgb;
    float3 cC = image.Sample(textureSampler, float2(base_x,      uv.y)).rgb;
    float3 cR = image.Sample(textureSampler, float2(base_x + px, uv.y)).rgb;

    float dL = depth_for(cL);
    float dC = depth_for(cC);
    float dR = depth_for(cR);

    // Weighted average (1, 2, 1) / 4
    float d = (dL + 2.0 * dC + dR) / 4.0;

    // Displace around the anchor plane. The user-set luma_anchor IS the zero
    // plane — pixels exactly at that luma sit at screen, brighter pop forward,
    // darker recede. With luma_anchor near 1.0, behaviour is recede-only.
    float displacement = (d - luma_anchor) * f0;
    float source_x;
    if (uv.x < 0.5)
        source_x = base_x + displacement;
    else
        source_x = base_x - displacement;

    // Per-pixel binocular gate: catches residual monocular pixels that the
    // static stretch-remap can't eliminate (it optimizes for worst-case
    // displacement only). For each output pixel, check if the OTHER eye
    // could show this same source content somewhere in ITS valid base_x
    // range (which is asymmetric per eye after the stretch remap, NOT the
    // source-side margin). If not, render black.
    float other_eye_base_x;
    float other_lo, other_hi;
    if (uv.x < 0.5) {
        other_eye_base_x = base_x + 2.0 * displacement;
        other_lo = margin + inner_crop;            // right_lo
        other_hi = 1.0 - margin - outer_crop;      // right_hi
    } else {
        other_eye_base_x = base_x - 2.0 * displacement;
        other_lo = margin + outer_crop;            // left_lo
        other_hi = 1.0 - margin - inner_crop;      // left_hi
    }
    if (other_eye_base_x < other_lo || other_eye_base_x > other_hi) {
        return float4(0.0, 0.0, 0.0, 1.0);
    }

    source_x = clamp(source_x, 0.0, 1.0);
    return image.Sample(textureSampler, float2(source_x, uv.y));
}
