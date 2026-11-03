"""YOLO-World 开放词汇检测：给一个词，找出图中的目标并输出精确坐标。

用法:
  python detect_blocks.py <image> "词1,词2" [out.png] [conf]

坐标直接打进文本，不需要视觉模型去"看准"。
"""
import sys
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np
from PIL import Image, ImageDraw

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolov8s-worldv2.pt")

_model = None


def get_model():
    global _model
    if _model is None:
        from ultralytics import YOLOWorld
        _model = YOLOWorld(MODEL_PATH)
    return _model


def detect(image_path, words, conf=0.15):
    model = get_model()
    model.set_classes(words)

    img = Image.open(image_path).convert("RGB")
    w, h = img.size

    results = model.predict(img, conf=conf, verbose=False)
    boxes = []
    for r in results:
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            boxes.append({
                "cls": words[int(b.cls[0])] if int(b.cls[0]) < len(words) else f"id{int(b.cls[0])}",
                "conf": float(b.conf[0]),
                "x": int(round(x1)), "y": int(round(y1)),
                "x2": int(round(x2)), "y2": int(round(y2)),
                "w": int(round(x2 - x1)), "h": int(round(y2 - y1)),
            })
    boxes.sort(key=lambda d: (d["y"] // 60, d["x"]))
    return img, (w, h), boxes


def render(img, boxes, out_path):
    canvas = img.copy()
    d = ImageDraw.Draw(canvas)
    palette = [(0, 224, 255), (255, 214, 0), (0, 230, 118), (255, 64, 129), (140, 120, 255)]
    for i, b in enumerate(boxes, 1):
        c = palette[(i - 1) % len(palette)]
        d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=c, width=3)
        tag = f'#{i} {b["cls"]} {b["conf"]:.2f}'
        d.rectangle([b["x"], b["y"] - 16, b["x"] + 8 * len(tag), b["y"]], fill=(10, 12, 20))
        d.text((b["x"] + 2, b["y"] - 14), tag, fill=c)
    canvas.save(out_path)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    image = sys.argv[1]
    words = [w.strip() for w in sys.argv[2].replace("，", ",").split(",") if w.strip()]
    out = sys.argv[3] if len(sys.argv) > 3 else "detected.png"
    conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.15

    img, (w, h), boxes = detect(image, words, conf)
    render(img, boxes, out)

    print(f"IMAGE {w}x{h}  words={words}  conf={conf}")
    print(f"FOUND {len(boxes)}")
    print(f"{'id':<4}{'class':<22}{'conf':<7}{'x':<7}{'y':<7}{'w':<7}{'h':<7}{'x2':<7}{'y2':<7}")
    for i, b in enumerate(boxes, 1):
        print(f'{i:<4}{b["cls"]:<22}{b["conf"]:<7.3f}{b["x"]:<7}{b["y"]:<7}'
              f'{b["w"]:<7}{b["h"]:<7}{b["x2"]:<7}{b["y2"]:<7}')
    print(f"OUT {out}")


if __name__ == "__main__":
    main()
