"""坐标网格与逐级放大。

两个动作：
  grid   整图铺网格，每格间距 N 个**原图像素**，每个交点标自己的原图坐标
  zoom   以 (cx,cy) 为中心取一块、放大显示，但网格**仍按原图像素**画，
         所以放大后读到的新数字还是原图坐标，不需要任何换算

放大只是把同一段原图坐标拆得更细：
  原图   |-------75-------|            标 0, 75
  放大2x |-------75-------|  屏幕同长   标 0, 37, 75
  放大4x |-------75-------|            标 0, 19, 38, 56, 75

用法:
  python grid_zoom.py grid <图> <输出.png> [--grid 75]
  python grid_zoom.py zoom <图> <输出.png> --cx 937 --cy 1087 --factor 4 --grid 75

输出:
  crop     本次显示的原图区域 (x,y,w,h)，拿它算下一级中心
  grid     本次实际上画的格距（原图像素）
  OUT      图片路径
"""
import argparse
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from PIL import Image, ImageDraw, ImageFont

GRID_COLOR = (0, 224, 255)
LABEL_BG = (8, 10, 16)
LABEL_FG = (255, 214, 0)
MAX_SIDE = 1600          # 放大后的显示上限，避免生成超大图


def pick_step(span, base, screen_scale):
    """选一个格距（单位：原图像素），目标是屏幕上一格约 70~90 像素。

    screen_scale = 显示像素 / 原图像素。
    所以一格的屏幕宽度 = step * screen_scale，要控制在 ~80 像素：
        step ≈ 80 / screen_scale
    但不超过 base（整图时不比 base 密）。
    """
    if screen_scale <= 0:
        return float(base)
    ideal = 80.0 / screen_scale
    step = min(float(base), ideal)
    # 落到好看档位
    for nice in (75, 50, 40, 25, 20, 10, 5, 2, 1):
        if step >= nice:
            return float(nice)
    return 1.0


def draw_grid(img, ox, oy, scale, step, label_font):
    """在已放大的图上画网格。ox/oy 是这块区域左上角在原图里的坐标。"""
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # 竖线：原图坐标从 ox 开始，按 step 递增
    start_x = (int(ox / step) + 1) * step
    x = start_x
    while x <= ox + W / scale:
        px = (x - ox) * scale
        draw.line([(px, 0), (px, H)], fill=GRID_COLOR, width=1)
        draw.text((px + 2, 2), f"{int(x)}", font=label_font, fill=LABEL_FG,
                  stroke_width=2, stroke_fill=LABEL_BG)
        x += step

    start_y = (int(oy / step) + 1) * step
    y = start_y
    while y <= oy + H / scale:
        py = (y - oy) * scale
        draw.line([(0, py), (W, py)], fill=GRID_COLOR, width=1)
        draw.text((2, py + 1), f"{int(y)}", font=label_font, fill=LABEL_FG,
                  stroke_width=2, stroke_fill=LABEL_BG)
        y += step


def load_font(size):
    for name in ("consola.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["grid", "zoom"])
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--grid", type=float, default=75.0)
    ap.add_argument("--cx", type=float, default=0)
    ap.add_argument("--cy", type=float, default=0)
    ap.add_argument("--factor", type=float, default=4.0)
    args = ap.parse_args()

    src = Image.open(args.image).convert("RGB")
    W, H = src.size

    if args.action == "grid":
        ox, oy = 0.0, 0.0
        view_w, view_h = float(W), float(H)
        # 整图时按 max_side 缩放显示
        scale = min(1.0, MAX_SIDE / max(W, H))
        step = args.grid
    else:
        factor = max(1.0, args.factor)
        view_w = W / factor
        view_h = H / factor
        ox = min(max(0.0, args.cx - view_w / 2), W - view_w)
        oy = min(max(0.0, args.cy - view_h / 2), H - view_h)
        scale = min(factor, MAX_SIDE / max(view_w, view_h), 8.0)
        # 放大后网格自动加密：屏幕上一格始终约 80 像素
        step = pick_step(view_w, args.grid, scale)

    box = (int(round(ox)), int(round(oy)),
           int(round(ox + view_w)), int(round(oy + view_h)))
    crop = src.crop(box)
    shown = crop.resize((max(1, int(round(view_w * scale))),
                         max(1, int(round(view_h * scale)))), Image.LANCZOS)

    font = load_font(max(11, int(13 * min(scale, 2))))
    draw_grid(shown, ox, oy, scale, step, font)
    shown.save(args.out)

    print(f"ACTION {args.action}")
    print(f"CROP {int(ox)} {int(oy)} {int(round(view_w))} {int(round(view_h))}")
    print(f"SCALE {scale:.3f}  STEP {step:.2f}  SHOWN {shown.size[0]}x{shown.size[1]}")
    print(f"OUT {args.out}")


if __name__ == "__main__":
    main()
