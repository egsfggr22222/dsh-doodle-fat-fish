"""GroundingDINO 开放词汇检测：输入一句话/一个词，输出图中目标的精确坐标。

用法:
  python gd_detect.py <image> "a video card" [out.png] [box_thr] [text_thr]

坐标直接打印成文本，视觉模型不需要"看准"，只需比对。
依赖: transformers, torch, pillow (CPU 可跑)
"""
import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np
import torch
from PIL import Image, ImageDraw
from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection

# 权重随插件一起分发，不依赖 ~/.cache/huggingface：
# 有本地副本就用本地，没有才回落到 HuggingFace。
_LOCAL_WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights", "grounding-dino-tiny")
MODEL_ID = _LOCAL_WEIGHTS if os.path.isdir(_LOCAL_WEIGHTS) else "IDEA-Research/grounding-dino-tiny"

_proc = None
_model = None


def get_models():
    global _proc, _model
    if _model is None:
        _proc = AutoProcessor.from_pretrained(MODEL_ID)
        _model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID)
        _model.eval()
    return _proc, _model


@torch.no_grad()
def detect(image_path, text, box_thr=0.25, text_thr=0.20):
    proc, model = get_models()
    img = Image.open(image_path).convert("RGB")
    w, h = img.size

    # GroundingDINO 要求提示以句号分隔
    prompt = text.strip()
    if not prompt.endswith("."):
        prompt += "."

    inputs = proc(images=img, text=prompt, return_tensors="pt")
    outputs = model(**inputs)
    res = proc.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        threshold=box_thr,
        text_threshold=text_thr,
        target_sizes=[(h, w)],
    )[0]

    boxes = []
    for score, label, box in zip(res["scores"], res["labels"], res["boxes"]):
        x1, y1, x2, y2 = [float(v) for v in box]
        boxes.append({
            "label": str(label),
            "score": float(score),
            "x": int(round(x1)), "y": int(round(y1)),
            "x2": int(round(x2)), "y2": int(round(y2)),
            "w": int(round(x2 - x1)), "h": int(round(y2 - y1)),
        })
    boxes.sort(key=lambda d: -d["score"])
    return img, (w, h), boxes, prompt


def render(img, boxes, out_path):
    canvas = img.copy()
    d = ImageDraw.Draw(canvas)
    palette = [(0, 224, 255), (255, 214, 0), (0, 230, 118), (255, 64, 129), (140, 120, 255)]
    ordered = sorted(boxes, key=lambda b: (b["y"] // 60, b["x"]))
    for i, b in enumerate(ordered, 1):
        c = palette[(i - 1) % len(palette)]
        d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=c, width=4)
        tag = f'#{i} {b["score"]:.2f}'
        tw = 9 * len(tag)
        ty = b["y"] - 18 if b["y"] > 20 else b["y2"]
        d.rectangle([b["x"], ty, b["x"] + tw, ty + 18], fill=(10, 12, 20))
        d.text((b["x"] + 3, ty + 4), tag, fill=c)
    canvas.save(out_path)
    return ordered


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    image = sys.argv[1]
    text = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else "gd-out.png"
    box_thr = float(sys.argv[4]) if len(sys.argv) > 4 else 0.25
    text_thr = float(sys.argv[5]) if len(sys.argv) > 5 else 0.20

    img, (w, h), boxes, prompt = detect(image, text, box_thr, text_thr)
    ordered = render(img, boxes, out)

    print(f"IMAGE {w}x{h}  model={MODEL_ID}")
    print(f"PROMPT {prompt!r}  box_thr={box_thr} text_thr={text_thr}")
    print(f"FOUND {len(boxes)}")
    print(f"{'id':<4}{'score':<8}{'label':<26}{'x':<7}{'y':<7}{'w':<7}{'h':<7}{'x2':<7}{'y2':<7}")
    for i, b in enumerate(ordered, 1):
        print(f'{i:<4}{b["score"]:<8.3f}{b["label"][:25]:<26}'
              f'{b["x"]:<7}{b["y"]:<7}{b["w"]:<7}{b["h"]:<7}{b["x2"]:<7}{b["y2"]:<7}')
    print(f"OUT {out}")


if __name__ == "__main__":
    main()
