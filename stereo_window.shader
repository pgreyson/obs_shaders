// stereo_window — one common stereo window for the whole full-SBS program.
//
// Each layer (synth pillar box, stage/room sbs-align masks) crops its own eyes
// by a different amount, so the three feeds end up with different widths. This
// filter sits LAST on the program scene and blacks out the same bands in BOTH
// eyes for ALL layers, one value per edge of each eye plus top/bottom. Set them to the largest crop any layer needs (see
// tools/stereo_window.py auto). Units: px of this surface (canvas px).
uniform float left_eye_left<  string label = "Left eye, left edge (px)";  string widget_type = "slider"; float minimum = 0.0; float maximum = 900.0; float step = 1.0; > = 0.0;
uniform float left_eye_right< string label = "Left eye, right edge (px)"; string widget_type = "slider"; float minimum = 0.0; float maximum = 900.0; float step = 1.0; > = 0.0;
uniform float right_eye_left< string label = "Right eye, left edge (px)"; string widget_type = "slider"; float minimum = 0.0; float maximum = 900.0; float step = 1.0; > = 0.0;
uniform float right_eye_right<string label = "Right eye, right edge (px)";string widget_type = "slider"; float minimum = 0.0; float maximum = 900.0; float step = 1.0; > = 0.0;
uniform float margin_top<     string label = "Top (px)";                   string widget_type = "slider"; float minimum = 0.0; float maximum = 500.0; float step = 1.0; > = 0.0;
uniform float margin_bottom<  string label = "Bottom (px)";                string widget_type = "slider"; float minimum = 0.0; float maximum = 500.0; float step = 1.0; > = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float is_left = step(uv.x, 0.5);
    float eye_w = uv_size.x * 0.5;
    float x = (uv.x - (1.0 - is_left) * 0.5) * 2.0 * eye_w;   // px within this eye
    float y = uv.y * uv_size.y;
    float m_left  = lerp(right_eye_left,  left_eye_left,  is_left);
    float m_right = lerp(right_eye_right, left_eye_right, is_left);
    if (x < m_left || x > eye_w - m_right || y < margin_top || y > uv_size.y - margin_bottom)
        return float4(0.0, 0.0, 0.0, 1.0);
    return image.Sample(textureSampler, uv);
}
