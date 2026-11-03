"""UI 元素检测：用 OmniParser 的图标检测器，结果写成 JSON。

供 scan.py 以子进程调用（跑在系统 Python 里，那里有 torch + ultralytics）。

注意导入顺序：先 import torch 再 import ultralytics。
反过来会报 shm.dll 加载失败——这是这台机器上 torch 2.14 的已知现象。
"""
import json
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS = os.path.join(HERE, "omniparser_icon_detect.pt")


def main():
    image, out_json = sys.argv[1], sys.argv[2]
    conf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15

    import torch  # noqa: F401  必须先于 ultralytics
    from ultralytics import YOLO

    model = YOLO(WEIGHTS)
    result = model.predict(image, verbose=False, conf=conf)[0]

    rows = []
    for b in result.boxes:
        x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
        rows.append({
            "x": int(round(x1)), "y": int(round(y1)),
            "x2": int(round(x2)), "y2": int(round(y2)),
            "w": int(round(x2 - x1)), "h": int(round(y2 - y1)),
            "conf": round(float(b.conf[0]), 4),
        })

    rows.sort(key=lambda d: (round(d["y"] / 60), d["x"]))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    print(f"UI {len(rows)}")


if __name__ == "__main__":
    main()
