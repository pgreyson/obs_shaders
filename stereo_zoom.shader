// stereo_zoom : centered zoom on L and R sides of a half-SBS image
// obs-shaderfilter port of structure_tools/glsl/fx/0_stereo_zoom.glsl
//
// Each eye is zoomed independently around its own center (0.25 / 0.75),
// with black fill outside bounds. Apply after a mono→stereo shader.
// The zoom slider is 0-1 (matching CV control); it remaps to 0.1x..4.0x.

uniform float zoom<
    string label = "Zoom (L=out, R=in)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 1.0;
    float step = 0.01;
> = 0.5;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float z = lerp(0.1, 4.0, zoom);

    // Determine which eye we're in (left or right half)
    float eye_center;
    if (uv.x < 0.5)
        eye_center = 0.25;
    else
        eye_center = 0.75;

    // Scale UV around the eye center
    float scaled_x = eye_center + (uv.x - eye_center) / z;
    float scaled_y = 0.5 + (uv.y - 0.5) / z;

    // Black fill when outside the eye's bounds
    bool out_of_bounds = false;
    if (uv.x < 0.5) {
        if (scaled_x < 0.0 || scaled_x > 0.5) out_of_bounds = true;
    } else {
        if (scaled_x < 0.5 || scaled_x > 1.0) out_of_bounds = true;
    }
    if (scaled_y < 0.0 || scaled_y > 1.0) out_of_bounds = true;

    if (out_of_bounds)
        return float4(0.0, 0.0, 0.0, 1.0);

    return image.Sample(textureSampler, float2(scaled_x, scaled_y));
}
