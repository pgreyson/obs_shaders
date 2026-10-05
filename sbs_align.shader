// sbs_align — per-eye x/y offset for SBS stereo input
//
// The StereoPi's two sensors are not mechanically aligned with pixel
// precision; residual vertical offset between the eyes fights stereo
// fusion (vertical disparity is the one the visual system can't
// resolve). This filter shifts each eye's image independently within
// its own half of the SBS frame.
//
// Units are SOURCE pixels. Positive x moves that eye's image right,
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
    float minimum = -100.0;
    float maximum = 100.0;
    float step = 0.25;
> = 0.0;

uniform float left_y<
    string label = "Left eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -100.0;
    float maximum = 100.0;
    float step = 0.25;
> = 0.0;

uniform float right_x<
    string label = "Right eye X offset (px)";
    string widget_type = "slider";
    float minimum = -100.0;
    float maximum = 100.0;
    float step = 0.25;
> = 0.0;

uniform float right_y<
    string label = "Right eye Y offset (px)";
    string widget_type = "slider";
    float minimum = -100.0;
    float maximum = 100.0;
    float step = 0.25;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    float is_left = step(uv.x, 0.5);
    float2 off = float2(lerp(right_x, left_x, is_left),
                        lerp(right_y, left_y, is_left));

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
