// halfsbs_pillarbox (with right-eye sync indicator)
//
// Squeezes each eye region back to correct aspect ratio with black
// pillarbox bars (undoes the analog→HDMI 4:3→16:9 stretch). Also
// overlays a small nested-square sync indicator in the upper-right of
// the right-eye view — only visible to the right eye in a correctly-
// routed L/R display. Useful sanity check when wearing 3D glasses.
//
// indicator_aspect compensates for the canvas's horizontal stretch
// relative to the source so the indicator is square in DISPLAY pixels:
//   ≈ canvas_height / canvas_width
//   Viture (3840×1080):  0.28
//   Projector (1920×1080): 0.56

uniform float aspect_correction<
    string label = "Aspect Correction";
    string widget_type = "slider";
    float minimum = 0.5;
    float maximum = 1.0;
    float step = 0.01;
> = 0.75;

// When checked, the indicator is rendered at half width in source space
// so the 2x downstream horizontal stretch (half-SBS source → full-SBS
// canvas, e.g. Viture) ends up square on the display. Uncheck when the
// output goes 1:1 to a half-SBS display (e.g. a 3D projector that
// natively consumes half-SBS).
uniform bool half_sbs<
    string label = "Half SBS (compensate for 2x downstream stretch)";
> = true;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;

    // ============ Right-eye sync indicator (overlay) ============
    // Small light-grey square flush with the upper-right corner, sized to
    // be barely noticeable — viewer-facing cue that 3D glasses are routed
    // correctly. Source pixels: 10 tall always; 5 wide when half_sbs (2x
    // downstream stretch → 10 display px), 10 wide otherwise.
    {
        float size_y_uv = 10.0 / 1080.0;
        float size_x_uv = (half_sbs ? 5.0 : 10.0) / 1920.0;
        if (uv.x >= 1.0 - size_x_uv && uv.y <= size_y_uv) {
            return float4(0.75, 0.75, 0.75, 1.0);
        }
    }

    // ============ Pillarbox aspect correction ============
    // Determine which eye (left half or right half)
    float eye_left  = step(uv.x, 0.5);
    float eye_start = eye_left * 0.0 + (1.0 - eye_left) * 0.5;
    float eye_width = 0.5;

    // Position within this eye (0.0 to 1.0)
    float eye_uv_x = (uv.x - eye_start) / eye_width;

    // Squeeze to correct aspect ratio, centered
    float padding = (1.0 - aspect_correction) * 0.5;
    float corrected_x = (eye_uv_x - padding) / aspect_correction;

    // Black bars outside content area
    if (corrected_x < 0.0 || corrected_x > 1.0)
        return float4(0.0, 0.0, 0.0, 1.0);

    // Map back to source UV
    float source_x = eye_start + corrected_x * eye_width;
    return image.Sample(textureSampler, float2(source_x, uv.y));
}
