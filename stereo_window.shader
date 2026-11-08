// stereo_window — one common stereo window for the whole full-SBS program.
//
// Each layer (synth pillar box, stage/room sbs-align masks) crops its own eyes
// by a different amount, so the three feeds end up with different widths. This
// filter sits LAST on the program scene and blacks out the same bands in BOTH
// eyes for ALL layers: margin_outer px on the OUTER edges (left of L, right of R —
// where near content spills), margin_inner on the INNER edges (far content),
// margin_y px top and bottom. Set them to the largest crop any layer needs (see
// tools/stereo_window.py auto). Units: px of this surface (canvas px).
uniform float margin_outer<
    string label = "Outer edges: left of L eye / right of R eye (px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 900.0;
    float step = 1.0;
> = 240.0;
uniform float margin_inner<
    string label = "Inner edges: right of L eye / left of R eye (px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 900.0;
    float step = 1.0;
> = 240.0;
uniform float margin_y<
    string label = "Vertical margin top/bottom (px)";
    string widget_type = "slider";
    float minimum = 0.0;
    float maximum = 500.0;
    float step = 1.0;
> = 0.0;

float4 mainImage(VertData v_in) : TARGET
{
    float2 uv = v_in.uv;
    float is_left = step(uv.x, 0.5);
    float eye_w = uv_size.x * 0.5;
    float x = (uv.x - (1.0 - is_left) * 0.5) * 2.0 * eye_w;   // px within this eye
    float y = uv.y * uv_size.y;
    float m_left  = lerp(margin_inner, margin_outer, is_left);   // this eye's left edge
    float m_right = lerp(margin_outer, margin_inner, is_left);   // this eye's right edge
    if (x < m_left || x > eye_w - m_right || y < margin_y || y > uv_size.y - margin_y)
        return float4(0.0, 0.0, 0.0, 1.0);
    return image.Sample(textureSampler, uv);
}
