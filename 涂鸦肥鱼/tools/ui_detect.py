"""UI 元素检测：用 OmniParser 的图标检测器找出界面上的可交互元素。

与 gd_detect.py 的区别：
  gd_detect  开放词汇，按语义找物体（人/车/狗），慢（3-5秒）
  本脚本     固定类别 icon，专找 UI 元素（按钮/图标/输入框/卡片），快（0.6秒）

用法:
  python ui_detect.py <图片> [输出.png] [conf]

输出:
  IMAGE WxH  ENGINE OmniParser-icon
  FOUND n
  id  conf  x  y  w  h  x2  y2
"""
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np
from PIL import Image, ImageDraw

WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omniparser_icon_detect.pt")

_model = None


def get_model():
    global _model
    if _model is None:
        from ultralytics import YOLO
        _model = YOLO(WEIGHTS)
    return _model


def detect(image_path, conf=0.15):
    model = get_model()
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    result = model.predict(image_path, verbose=False, conf=conf)[0]
    boxes = []
    for b in result.boxes:
        x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
        boxes.append({
            "conf": float(b.conf[0]),
            "x": int(round(x1)), "y": int(round(y1)),
            "x2": int(round(x2)), "y2": int(round(y2)),
            "w": int(round(x2 - x1)), "h": int(round(y2 - y1)),
        })
    # 阅读顺序：先按行（80px 一带），再按列
    boxes.sort(key=lambda d: (round(d["y"] / 80), d["x"]))
    return img, (w, h), boxes


def render(img, boxes, out_path):
    canvas = img.copy()
    d = ImageDraw.Draw(canvas)
    palette = [(0, 224, 255), (255, 214, 0), (0, 230, 118), (255, 64, 129), (140, 120, 255)]
    for i, b in enumerate(boxes, 1):
        c = palette[(i - 1) % len(palette)]
        d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=c, width=3)
        tag = f'#{i} {b["conf"]:.2f}'
        ty = b["y"] - 16 if b["y"] > 18 else b["y2"]
        d.rectangle([b["x"], ty, b["x"] + 8 * len(tag), ty + 16], fill=(10, 12, 20))
        d.text((b["x"] + 2, ty + 3), tag, fill=c)
    canvas.save(out_path)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    image = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "ui-out.png"
    conf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15

    img, (w, h), boxes = detect(image, conf)
    if boxes:
        render(img, boxes, out)

    print(f"IMAGE {w}x{h}  ENGINE OmniParser-icon  conf={conf}")
    print(f"FOUND {len(boxes)}")
    print(f"{'id':<5}{'conf':<8}{'x':<7}{'y':<7}{'w':<7}{'h':<7}{'x2':<7}{'y2':<7}")
    for i, b in enumerate(boxes, 1):
        print(f'{i:<5}{b["conf"]:<8.3f}{b["x"]:<7}{b["y"]:<7}{b["w"]:<7}'
              f'{b["h"]:<7}{b["x2"]:<7}{b["y2"]:<7}')
    print(f"OUT {out}")


if __name__ == "__main__":
    main()
