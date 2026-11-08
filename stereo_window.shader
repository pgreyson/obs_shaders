// stereo_window — one common stereo window for the whole full-SBS program.
//
// Each layer (synth pillar box, stage/room sbs-align masks) crops its own eyes
// by a different amount, so the three feeds end up with different widths. This
// filter sits LAST on the program scene and blacks out the same bands in BOTH
// eyes for ALL layers: margin_x px on the left and right of each eye, margin_y
// px top and bottom. Set them to the largest crop any layer needs (see
// tools/stereo_window.py auto). Units: px of this surface (canvas px).
uniform float margin_x<
    string label = "Horizontal margin per eye edge (px)";
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
    if (x < margin_x || x > eye_w - margin_x || y < margin_y || y > uv_size.y - margin_y)
        return float4(0.0, 0.0, 0.0, 1.0);
    return image.Sample(textureSampler, uv);
}
