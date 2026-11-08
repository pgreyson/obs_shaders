// sbs_align — per-eye x/y offset for SBS stereo input
//
// The StereoPi's two sensors are not mechanically aligned with pixel
// precision; residual vertical offset between the eyes fights stereo
// fusion (vertical disparity is the one the visual system can't
// resolve). This filter shifts each eye's image independently within
// its own half of the SBS frame.
//
// Units are SOURCE pixels of whatever the filter sits on — on a nested
// SCENE (main's camera_chain) that is CANVAS px, i.e. 2x camera px. Range
// ±400 so a near object (hundreds of px raw disparity) can be converged. Positive x moves that eye's image right,
// positive y moves it down ("left frame is higher" → raise left_y, or
// lower right_y, until rows match). Revealed area at the trailing
// edges renders black; samples are clamped inside the eye's half so
// one eye never bleeds into the other (the sampler wraps at uv 0/1).
//
// Sits FIRST in the chain (after the test pattern) so everything
// downstream — palette, pillar box — sees aligned frames. Only
// meaningful on SBS input; harmless but pointless on mono synth.

uniform float left_x<
    string label = "Left eye X offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float left_y<
    string label = "Left eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float right_x<
    string label = "Right eye X offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

uniform float right_y<
    string label = "Right eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -400.0;
    float maximum = 400.0;
    float step = 0.25;
> = 0.0;

// Stereo window: after shifting, each eye has content the other eye lacks
// (the strip that slid in from off-frame on one side, and the black revealed
// on the other). Those pixels have no counterpart and fight fusion, so mask
// them in BOTH eyes: only the region visible to both survives.
uniform bool mask_common<
    string label = "Mask to the region both eyes can see";
> = true;
// The shift mask only covers the convergence offset itself. Content OFF the
// convergence plane still spills out of one eye: objects NEARER than the plane
// (crossed disparity) are missing from the other eye at the OUTER edges — the
// left edge of the left eye and the right edge of the right eye; objects
// FARTHER than the plane are missing at the INNER edges (right edge of the
// left eye, left edge of the right eye). Set margin_outer to the largest near
// disparity in the scene and margin_inner to the largest far one (px of this
// surface). Applied only where it belongs — never to all four edges.
uniform float margin_outer<
    string label = "Stereo margin, OUTER edges (near content, px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 400.0;
    float step = 1.0;
> = 0.0;
uniform float margin_inner<
    string label = "Stereo margin, INNER edges (far content, px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 400.0;
    float step = 1.0;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float is_left = step(uv.x, 0.5);
    float2 off = float2(lerp(right_x, left_x, is_left),
                        lerp(right_y, left_y, is_left));

    if (mask_common) {
        // position within this eye, in px of the eye (half the surface wide)
        float2 eye_px = float2(uv_size.x * 0.5, uv_size.y);
        float2 p = float2((uv.x - (1.0 - is_left) * 0.5) * 2.0, uv.y) * eye_px;
        // after a shift of +d, an eye shows content on [max(0,d), W+min(0,d)];
        // the common window is the intersection over both eyes
        float x0 = max(max(0.0, left_x), max(0.0, right_x));
        float x1 = eye_px.x + min(min(0.0, left_x), min(0.0, right_x));
        // outer edge = left side of the LEFT eye / right side of the RIGHT eye
        x0 += lerp(margin_inner, margin_outer, is_left);
        x1 -= lerp(margin_outer, margin_inner, is_left);
        float y0 = max(max(0.0, left_y), max(0.0, right_y));
        float y1 = eye_px.y + min(min(0.0, left_y), min(0.0, right_y));
        if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1)
            return float4(0.0, 0.0, 0.0, 1.0);
    }

    // shift content by +off pixels = sample -off pixels upstream
    float2 suv = uv - off / uv_size;

    // this eye's horizontal span
    float xmin = lerp(0.5, 0.0, is_left);
    float xmax = lerp(1.0, 0.5, is_left);

    // revealed area (no source content for this eye) → black
    if (suv.x < xmin || suv.x > xmax || suv.y < 0.0 || suv.y > 1.0)
        return float4(0.0, 0.0, 0.0, 1.0);

    // keep samples half a texel inside the eye half: no wrap, no bleed
    float2 half_px = 0.5 / uv_size;
    suv.x = clamp(suv.x, xmin + half_px.x, xmax - half_px.x);
    suv.y = clamp(suv.y, half_px.y, 1.0 - half_px.y);

    return image.Sample(textureSampler, suv);
}
