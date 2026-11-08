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
// Stereo WINDOW on top of the shift mask: the same width in both eyes, moved as a
// pair by window_shift, and slid apart by window_offset — the left eye's window
// moves right by offset/2 while the right eye's moves left by offset/2 (positive =
// window floats in front of the screen, which is what near content at the frame
// edges needs; negative = behind). window_width 0 = no extra crop. Units: px of
// this surface. Top/bottom crops apply to both eyes.
uniform float window_width<
    string label = "Stereo window width, both eyes (px; 0 = off)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 1920.0; float step = 1.0;
> = 0.0;
uniform float window_shift<
    string label = "Stereo window shift, both eyes (px, + = right)";
    string widget_type = "slider";
    float minimum = -600.0; float maximum = 600.0; float step = 1.0;
> = 0.0;
uniform float window_offset<
    string label = "Stereo window L/R offset (px, + = floats in front)";
    string widget_type = "slider";
    float minimum = -400.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
// Feather the window edges (and the shift mask edges) over this many px so a
// layer with a narrower field of view fades into the layer beneath instead of
// ending in a hard line. The fade goes to TRANSPARENT, so downstream filters
// must preserve alpha (synth_color / palette_quantize do; the camera key keeps it).
uniform float vignette<
    string label = "Edge feather / vignette (px, 0 = hard edge)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
uniform float mask_top<
    string label = "Both eyes: extra crop at the TOP (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;
uniform float mask_bottom<
    string label = "Both eyes: extra crop at the BOTTOM (px)";
    string widget_type = "slider";
    float minimum = 0.0; float maximum = 400.0; float step = 1.0;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float is_left = step(uv.x, 0.5);
    float2 off = float2(lerp(right_x, left_x, is_left),
                        lerp(right_y, left_y, is_left));

    float fade = 1.0;
    if (mask_common) {
        // position within this eye, in px of the eye (half the surface wide)
        float2 eye_px = float2(uv_size.x * 0.5, uv_size.y);
        float2 p = float2((uv.x - (1.0 - is_left) * 0.5) * 2.0, uv.y) * eye_px;
        // after a shift of +d, an eye shows content on [max(0,d), W+min(0,d)];
        // the common window is the intersection over both eyes
        float x0 = max(max(0.0, left_x), max(0.0, right_x));
        float x1 = eye_px.x + min(min(0.0, left_x), min(0.0, right_x));
        if (window_width > 0.5) {
            float c = eye_px.x * 0.5 + window_shift + (is_left > 0.5 ? 0.5 : -0.5) * window_offset;
            x0 = max(x0, c - window_width * 0.5);
            x1 = min(x1, c + window_width * 0.5);
        }
        float y0 = max(max(0.0, left_y), max(0.0, right_y)) + mask_top;
        float y1 = eye_px.y + min(min(0.0, left_y), min(0.0, right_y)) - mask_bottom;
        if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1)
            return float4(0.0, 0.0, 0.0, 0.0);   // outside the window: transparent
        if (vignette > 0.5) {
            float v = vignette;
            fade = clamp((p.x - x0) / v, 0.0, 1.0) * clamp((x1 - p.x) / v, 0.0, 1.0)
                 * clamp((p.y - y0) / v, 0.0, 1.0) * clamp((y1 - p.y) / v, 0.0, 1.0);
            fade = fade * fade * (3.0 - 2.0 * fade);   // smoothstep
        }
    }

    // shift content by +off pixels = sample -off pixels upstream
    float2 suv = uv - off / uv_size;

    // this eye's horizontal span
    float xmin = lerp(0.5, 0.0, is_left);
    float xmax = lerp(1.0, 0.5, is_left);

    // revealed area (no source content for this eye) → transparent
    if (suv.x < xmin || suv.x > xmax || suv.y < 0.0 || suv.y > 1.0)
        return float4(0.0, 0.0, 0.0, 0.0);

    // keep samples half a texel inside the eye half: no wrap, no bleed
    float2 half_px = 0.5 / uv_size;
    suv.x = clamp(suv.x, xmin + half_px.x, xmax - half_px.x);
    suv.y = clamp(suv.y, half_px.y, 1.0 - half_px.y);

    float4 c = image.Sample(textureSampler, suv);
    return float4(c.rgb, c.a * fade);
}
