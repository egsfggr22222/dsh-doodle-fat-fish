"""OCR 单项检测：整图识别所有文字，结果写成 JSON。

供 scan.py 以子进程调用（跑在 ocr_env 里，那里有 paddleocr）。
"""
import json
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("FLAGS_call_stack_level", "2")
os.environ.setdefault("GLOG_minloglevel", "3")

import numpy as np
from PIL import Image


def main():
    image, out_json = sys.argv[1], sys.argv[2]
    min_conf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.4

    from paddleocr import PaddleOCR
    ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    img = Image.open(image).convert("RGB")
    result = ocr.ocr(np.array(img), cls=True)

    rows = []
    for item in (result[0] or []):
        pts, (text, conf) = item
        if float(conf) < min_conf or not str(text).strip():
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x1, y1 = int(round(min(xs))), int(round(min(ys)))
        x2, y2 = int(round(max(xs))), int(round(max(ys)))
        rows.append({
            "x": x1, "y": y1, "x2": x2, "y2": y2,
            "w": x2 - x1, "h": y2 - y1,
            "text": str(text).strip(),
            "conf": round(float(conf), 4),
        })

    rows.sort(key=lambda d: (round(d["y"] / 40), d["x"]))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    print(f"OCR {len(rows)}")


if __name__ == "__main__":
    main()
