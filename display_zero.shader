// display_zero — one global stereo offset for the whole program.
//
// The Viture glasses (the reference display) do not show identical left/right
// images at a comfortable distance: a flat picture needs a crossed offset
// before the eyes relax. This filter sits on main's PROGRAM scene and adds
// that offset to everything at once, so 0 px on every other control (splat
// plane, camera convergence, stereo window) means the relaxed plane.
//
// zero_px shifts the left eye right by zero/2 and the right eye left by
// zero/2: displayed disparity (left - right) of every layer rises by exactly
// zero_px (+ = toward the viewer). It is baked into the vcam, so the Twitch,
// YouTube and projector feeds carry it too. The strip each eye loses at its
// outer edge renders black. 0 = exact passthrough.
// Find the value: splat thickness 0, plane 0, sweep until the flat picture is
// relaxed. tools/stereo_field.py zero N [--keep] sets it from the shell.

uniform float zero_px<
    string label = "Display zero (px, + = toward viewer)";
    string widget_type = "slider";
    float minimum = -100.0;
    float maximum = 300.0;
    float step = 0.5;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    if (abs(zero_px) < 0.01)
        return image.Sample(textureSampler, uv);
    float is_left = step(uv.x, 0.5);
    float off = (is_left > 0.5 ? 0.5 : -0.5) * zero_px / uv_size.x;
    float sx = uv.x - off;
    float xmin = is_left > 0.5 ? 0.0 : 0.5;
    float xmax = xmin + 0.5;
    if (sx < xmin || sx > xmax)
        return float4(0.0, 0.0, 0.0, 1.0);
    float hp = 0.5 / uv_size.x;
    sx = clamp(sx, xmin + hp, xmax - hp);
    return image.Sample(textureSampler, float2(sx, uv.y));
}
