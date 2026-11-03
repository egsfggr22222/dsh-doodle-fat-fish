"""PaddleOCR 文字定位：整图识别，输出每个文本块的精确坐标。

不裁剪、不放大 —— 整图一次识别，坐标直接给出。

用法:
  python paddle_find.py <image> "<查询文字>" [out.png] [--min-conf 0.5]

输出:
  MATCHES n
  id  score  x  y  w  h  x2  y2  text
"""
import argparse
import os
import re
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("FLAGS_call_stack_level", "2")
os.environ.setdefault("GLOG_minloglevel", "3")

import numpy as np
from PIL import Image, ImageDraw


def norm(s):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def score(q, t):
    if not t:
        return 0.0
    if q and q in t:
        return 1.0
    hit = sum(1 for ch in q if ch in t)
    return 0.9 * hit / max(1, len(q))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("query")
    ap.add_argument("out", nargs="?", default="paddle-out.png")
    ap.add_argument("--min-conf", type=float, default=0.4)
    ap.add_argument("--min-score", type=float, default=0.4)
    args = ap.parse_args()

    from paddleocr import PaddleOCR

    # use_angle_cls 处理竖排/倾斜文字；lang=ch 中英文混排
    ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    img = Image.open(args.image).convert("RGB")
    W, H = img.size
    arr = np.array(img)

    result = ocr.ocr(arr, cls=True)
    lines = result[0] if result and result[0] else []

    boxes = []
    for item in lines:
        pts, (text, conf) = item
        if conf < args.min_conf or not text.strip():
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x1, y1 = int(round(min(xs))), int(round(min(ys)))
        x2, y2 = int(round(max(xs))), int(round(max(ys)))
        boxes.append({"text": text.strip(), "conf": float(conf),
                      "x": x1, "y": y1, "x2": x2, "y2": y2,
                      "w": x2 - x1, "h": y2 - y1})

    q = norm(args.query)
    scored = [(score(q, norm(b["text"])), b) for b in boxes]
    scored = [(s, b) for s, b in scored if s >= args.min_score]
    scored.sort(key=lambda t: -t[0])

    print(f"IMAGE {W}x{H}  ENGINE PaddleOCR(ch)  LINES {len(boxes)}")
    print(f"QUERY {args.query!r}  MATCHES {len(scored)}")
    print(f"{'id':<4}{'score':<8}{'conf':<8}{'x':<7}{'y':<7}{'w':<7}{'h':<7}{'x2':<7}{'y2':<7}{'text'}")
    for i, (s, b) in enumerate(scored[:12], 1):
        print(f'{i:<4}{s:<8.3f}{b["conf"]:<8.3f}{b["x"]:<7}{b["y"]:<7}{b["w"]:<7}'
              f'{b["h"]:<7}{b["x2"]:<7}{b["y2"]:<7}{b["text"][:36]}')

    if scored:
        canvas = img.copy()
        d = ImageDraw.Draw(canvas)
        for i, (s, b) in enumerate(scored[:12], 1):
            c = (0, 224, 255) if i == 1 else (255, 214, 0)
            d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=c, width=4)
        canvas.save(args.out)
        print(f"OUT {args.out}")

    # 同时打印全部识别行（便于人工核对，不画图）
    print("--- ALL LINES (conf>=%.2f) ---" % args.min_conf)
    for b in sorted(boxes, key=lambda d: (d["y"] // 30, d["x"]))[:120]:
        print(f'  {b["x"]:>5},{b["y"]:>5} {b["w"]:>4}x{b["h"]:<4} conf={b["conf"]:.2f}  {b["text"][:40]}')


if __name__ == "__main__":
    main()
