"""开放词汇物体检测：用 YOLOE 按词找物体，结果写成 JSON。

供 scan.py 以子进程调用（跑在系统 Python 里）。

注意导入顺序：先 import torch 再 import ultralytics。
"""
import json
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS = os.path.join(HERE, "yoloe-11s-seg.pt")

# ultralytics 从「当前工作目录」找 mobileclip_blt.ts。
# 工具调用时 cwd 不是 tools/，不切过去它就去 GitHub 下载，然后 SSL 失败。
os.chdir(HERE)


def main():
    image, out_json = sys.argv[1], sys.argv[2]
    terms = [w.strip() for w in sys.argv[3].split(",") if w.strip()] if len(sys.argv) > 3 else []
    conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.2

    if not terms:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump([], f)
        print("OBJ 0")
        return

    import torch  # noqa: F401  必须先于 ultralytics
    from ultralytics import YOLOE

    model = YOLOE(WEIGHTS)
    model.set_classes(terms, model.get_text_pe(terms))
    result = model.predict(image, verbose=False, conf=conf)[0]

    rows = []
    for b in result.boxes:
        x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
        cls = int(b.cls[0])
        rows.append({
            "x": int(round(x1)), "y": int(round(y1)),
            "x2": int(round(x2)), "y2": int(round(y2)),
            "w": int(round(x2 - x1)), "h": int(round(y2 - y1)),
            "label": terms[cls] if cls < len(terms) else f"cls{cls}",
            "conf": round(float(b.conf[0]), 4),
        })

    rows.sort(key=lambda d: (round(d["y"] / 60), d["x"]))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    print(f"OBJ {len(rows)}")


if __name__ == "__main__":
    main()
