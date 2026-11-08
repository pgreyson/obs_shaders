// stereo_window — one common stereo window for the whole full-SBS program.
//
// Each layer (synth pillar box, stage/room sbs-align masks) crops its own eyes
// by a different amount, so the three feeds end up with different widths. This
// filter sits LAST on the program scene and blacks out the same bands in BOTH
// eyes for ALL layers: one window width, a shared shift, a left/right offset
// (floating-window depth) and top/bottom crops. Set them to the largest crop any layer needs (see
// tools/stereo_window.py auto). Units: px of this surface (canvas px).
uniform float window_width<  string label = "Window width, both eyes (px)";               string widget_type = "slider"; float minimum = 100.0; float maximum = 1920.0; float step = 1.0; > = 1920.0;
uniform float window_shift<  string label = "Window shift, both eyes (px, + = right)";     string widget_type = "slider"; float minimum = -600.0; float maximum = 600.0; float step = 1.0; > = 0.0;
uniform float window_offset< string label = "L/R offset (px, + = floats in front)";        string widget_type = "slider"; float minimum = -400.0; float maximum = 400.0; float step = 1.0; > = 0.0;
uniform float margin_top<    string label = "Top (px)";                                    string widget_type = "slider"; float minimum = 0.0; float maximum = 500.0; float step = 1.0; > = 0.0;
uniform float margin_bottom< string label = "Bottom (px)";                                 string widget_type = "slider"; float minimum = 0.0; float maximum = 500.0; float step = 1.0; > = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float is_left = step(uv.x, 0.5);
    float eye_w = uv_size.x * 0.5;
    float x = (uv.x - (1.0 - is_left) * 0.5) * 2.0 * eye_w;   // px within this eye
    float y = uv.y * uv_size.y;
    float c  = eye_w * 0.5 + window_shift + (is_left > 0.5 ? 0.5 : -0.5) * window_offset;
    float x0 = c - window_width * 0.5, x1 = c + window_width * 0.5;
    if (x < x0 || x > x1 || y < margin_top || y > uv_size.y - margin_bottom)
        return float4(0.0, 0.0, 0.0, 1.0);
    return image.Sample(textureSampler, uv);
}
