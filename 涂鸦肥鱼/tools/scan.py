"""三合一扫描：OCR + UI 检测 + 开放词汇检测，各跑各的，汇到一张图上。

设计要点：
  - OCR 与检测器依赖不同环境（paddleocr 要 numpy<2，ultralytics 要新版 torch），
    所以三种检测不放在同一个进程里：本脚本是「总调度」，分别起子进程跑。
  - 每个子进程只做一种检测，把自己结果写成 JSON。
  - 三种结果合并后，在原图上用三色描出来，并输出结构化描述。

颜色：
  红  OCR        文字
  蓝  UI         可交互元素
  绿  开放词汇    语义物体

用法:
  python scan.py <图片> <输出目录> [--object-terms "person,car"]

输出:
  scan.png    三色叠加图
  scan.json   结构化描述（每层的每一条：坐标 + 内容 + 含义）
"""
import argparse
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_PY = os.path.join(HERE, "..", "ocr_env", "Scripts", "python.exe")
SYS_PY = sys.executable

COLOR_TEXT = (255, 64, 64)
COLOR_UI = (64, 160, 255)
COLOR_OBJ = (64, 220, 120)


# 词表由调用方（大语言模型）负责挑选：它看过 COCO 80 类清单，
# 知道模型认什么，直接把英文词传进来。程序这里不做任何翻译 ——
# 硬编码映射表既维护不过来，也会悄悄吞掉没收录的词。


def expand_terms(raw_terms):
    """原样透传，只去重和去空白。翻译是调用方的事。"""
    seen = set()
    result = []
    for term in raw_terms:
        t = str(term).strip()
        if not t:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        result.append(t)
    return result


def run_child(python_exe, script, args, json_path, timeout=600):
    """起子进程跑一个检测脚本，读它输出的 JSON 文件。"""
    try:
        proc = subprocess.run(
            [python_exe, os.path.join(HERE, script)] + args,
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, (err[-1] if err else "failed")
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f), ""
    except Exception as e:
        return None, f"bad json: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("outdir")
    ap.add_argument("--object-terms", default="")
    ap.add_argument("--min-conf", type=float, default=0.5)   # PaddleOCR 常用 0.5
    ap.add_argument("--ui-conf", type=float, default=0.25)   # YOLO 官方默认 0.25
    ap.add_argument("--obj-conf", type=float, default=0.25)  # YOLO 官方默认 0.25
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    img = Image.open(args.image).convert("RGB")
    W, H = img.size

    terms = expand_terms(args.object_terms.split(","))
    if terms:
        print(f"TERMS {terms}")
    warnings = []
    timings = {}

    # ── 1. OCR ──
    t = time.time()
    ocr_json = os.path.join(args.outdir, "_ocr.json")
    texts, err = run_child(
        OCR_PY, "det_ocr.py",
        [args.image, ocr_json, str(args.min_conf)], ocr_json,
    )
    if err != "":
        texts = []
        warnings.append(f"ocr: {err}")
    timings["ocr_ms"] = round((time.time() - t) * 1000)

    # ── 2. UI ──
    t = time.time()
    ui_json = os.path.join(args.outdir, "_ui.json")
    uis, err = run_child(
        SYS_PY, "det_ui.py",
        [args.image, ui_json, str(args.ui_conf)], ui_json,
    )
    if err != "":
        uis = []
        warnings.append(f"ui: {err}")
    timings["ui_ms"] = round((time.time() - t) * 1000)

    # ── 3. 开放词汇 ──
    t = time.time()
    obj_json = os.path.join(args.outdir, "_obj.json")
    if terms:
        objects, err = run_child(
            SYS_PY, "det_obj.py",
            [args.image, obj_json, ",".join(terms), str(args.obj_conf)], obj_json,
        )
        if err != "":
            objects = []
            warnings.append(f"object: {err}")
    else:
        objects = []
    timings["object_ms"] = round((time.time() - t) * 1000)

    # 清理子进程留下的临时 json
    for p in (ocr_json, ui_json, obj_json):
        try:
            os.remove(p)
        except OSError:
            pass

    # ── 画三色叠加 ──
    canvas = img.copy()
    d = ImageDraw.Draw(canvas)
    for t0 in uis:
        d.rectangle([t0["x"], t0["y"], t0["x2"], t0["y2"]], outline=COLOR_UI, width=2)
    for o in objects:
        d.rectangle([o["x"], o["y"], o["x2"], o["y2"]], outline=COLOR_OBJ, width=3)
    for t0 in texts:
        d.rectangle([t0["x"], t0["y"], t0["x2"], t0["y2"]], outline=COLOR_TEXT, width=2)

    scan_png = os.path.join(args.outdir, "scan.png")
    canvas.save(scan_png)

    doc = {
        "image": {"width": W, "height": H, "path": os.path.abspath(args.image)},
        "timings_ms": timings,
        "warnings": warnings,
        "layers": {
            "text": {
                "color": "#ff4040",
                "count": len(texts),
                "meaning": "红色：图片上写着的文字，items 里带原文",
                "items": texts,
            },
            "ui": {
                "color": "#40a0ff",
                "count": len(uis),
                "meaning": "蓝色：可交互的界面元素（按钮/图标/输入框/卡片）",
                "items": uis,
            },
            "object": {
                "color": "#40dc78",
                "count": len(objects),
                "meaning": "绿色：按词检出的语义物体",
                "terms": terms,
                "items": objects,
            },
        },
        "scan_png": scan_png,
    }
    scan_json = os.path.join(args.outdir, "scan.json")
    with open(scan_json, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    print(f"IMAGE {W}x{H}")
    print(f"TEXT {len(texts)}  UI {len(uis)}  OBJECT {len(objects)}  {timings}")
    if warnings:
        for w in warnings:
            print(f"WARN {w}")
    print(f"PNG {scan_png}")
    print(f"JSON {scan_json}")


if __name__ == "__main__":
    main()
