"""差分位移 + 连通区域框选（去重版）。

流程：
  1. 差分位移：整图左移 1px、下移 1px 后与原图相减
  2. 差异像素做形态学闭合，把断开的笔画连回一个整体
  3. 连通域分析 → 合并重叠/相邻的块（去重影）
  4. 输出：
       diff.png     白底红像素差异图，每块标编号
       boxes.png    原图上画所有块的最小框
       picked.png   原图上只框选定的那一块（--pick N）

不做裁剪、不放大、不抠图，输出全部是整图。

去重影说明：
  位移差分会让粗笔画的左右/上下边缘各产生一条差异带，
  形成"一个元素两个框"。这里用两种手段抑制：
    - 先闭合（dilate→erode）把同一元素的两条带连成一个整体
    - 再按重叠率归并：两个框重叠超过阈值就留大的
"""
import argparse
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np
from PIL import Image, ImageDraw

try:
    from scipy import ndimage
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


def shift_diff_mask(arr, dx=1, dy=1, tol=40):
    """左移 dx、下移 dy 后与原图相减，返回差异掩码。"""
    h, w = arr.shape[:2]
    shifted = np.full_like(arr, 255)
    shifted[dy:h, 0:w - dx] = arr[0:h - dy, dx:w]
    return np.abs(arr.astype(int) - shifted.astype(int)).sum(2) > tol


def iou(a, b):
    ix = max(0, min(a["x2"], b["x2"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y2"], b["y2"]) - max(a["y"], b["y"]))
    inter = ix * iy
    if inter == 0:
        return 0.0
    ua = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / ua if ua else 0.0


def merge_overlapping(blocks, thr=0.30):
    """重叠率超过阈值的框合并成一个（取并集），消除重影。"""
    changed = True
    while changed:
        changed = False
        merged = []
        used = [False] * len(blocks)
        for i, a in enumerate(blocks):
            if used[i]:
                continue
            cur = dict(a)
            for j in range(i + 1, len(blocks)):
                if used[j]:
                    continue
                b = blocks[j]
                if iou(cur, b) >= thr:
                    cur = {
                        "x": min(cur["x"], b["x"]),
                        "y": min(cur["y"], b["y"]),
                        "x2": max(cur["x2"], b["x2"]),
                        "y2": max(cur["y2"], b["y2"]),
                    }
                    cur["w"] = cur["x2"] - cur["x"]
                    cur["h"] = cur["y2"] - cur["y"]
                    used[j] = True
                    changed = True
            used[i] = True
            merged.append(cur)
        blocks = merged
    return blocks


def find_blocks(mask, min_w=40, min_h=18, min_px=180, close_iter=2):
    """闭合 + 连通域 + 去重，返回最小外接矩形列表。"""
    if not HAVE_SCIPY:
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            return []
        return [{"x": int(xs.min()), "y": int(ys.min()),
                 "x2": int(xs.max()) + 1, "y2": int(ys.max()) + 1}]

    # 先闭合：把同一元素的断裂笔画连成一个整体
    closed = ndimage.binary_closing(
        ndimage.binary_dilation(mask, iterations=close_iter),
        iterations=close_iter,
    )

    lab, _ = ndimage.label(closed)
    objs = ndimage.find_objects(lab)
    h, w = mask.shape
    raw = []
    for sl in objs:
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        if x1 - x0 < min_w or y1 - y0 < min_h:
            continue
        if (x1 - x0) > w * 0.92 or (y1 - y0) > h * 0.90:
            continue
        if int(mask[sl].sum()) < min_px:
            continue
        raw.append({"x": int(x0), "y": int(y0), "x2": int(x1), "y2": int(y1),
                    "w": int(x1 - x0), "h": int(y1 - y0)})

    blocks = merge_overlapping(raw, thr=0.30)

    # 收紧到真实差异像素的极值（得到最小框）
    for b in blocks:
        sub = mask[b["y"]:b["y2"], b["x"]:b["x2"]]
        ys, xs = np.nonzero(sub)
        if len(xs):
            b["x"] += int(xs.min())
            b["y"] += int(ys.min())
            b["x2"] -= (sub.shape[1] - 1 - int(xs.max()))
            b["y2"] -= (sub.shape[0] - 1 - int(ys.max()))
        b["w"] = b["x2"] - b["x"]
        b["h"] = b["y2"] - b["y"]
        b.pop("id", None)

    blocks = [b for b in blocks if b["w"] >= min_w and b["h"] >= min_h]
    blocks.sort(key=lambda b: (round(b["y"] / 60), b["x"]))
    for i, b in enumerate(blocks, 1):
        b["id"] = i
    return blocks


def draw_diff(mask, blocks, path):
    h, w = mask.shape
    canvas = np.full((h, w, 3), 255, dtype=np.uint8)
    canvas[mask] = [255, 0, 0]
    img = Image.fromarray(canvas)
    d = ImageDraw.Draw(img)
    for b in blocks:
        d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=(0, 150, 255), width=2)
        d.text((b["x"] + 2, max(0, b["y"] + 1)), str(b["id"]), fill=(0, 0, 180))
    img.save(path)


def draw_boxes(src_img, blocks, path, only_id=None):
    canvas = src_img.copy()
    d = ImageDraw.Draw(canvas)
    for b in blocks:
        if only_id is not None and b["id"] != only_id:
            continue
        color = (0, 224, 255) if only_id is not None else (255, 90, 0)
        d.rectangle([b["x"], b["y"], b["x2"], b["y2"]], outline=color, width=4)
        d.text((b["x"] + 3, max(0, b["y"] - 14)), f'#{b["id"]}', fill=color)
    canvas.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out", nargs="?", default=".")
    ap.add_argument("--tol", type=int, default=40)
    ap.add_argument("--min-w", type=int, default=40)
    ap.add_argument("--min-h", type=int, default=18)
    ap.add_argument("--min-px", type=int, default=180)
    ap.add_argument("--close", type=int, default=2, help="闭合迭代数，越大越能抑制重影")
    ap.add_argument("--pick", type=int, default=0)
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGB")
    arr = np.array(img)
    # 输出目录不存在就建：工具层传进来的 scratch 目录可能还没建。
    os.makedirs(args.out, exist_ok=True)
    mask = shift_diff_mask(arr, 1, 1, args.tol)
    blocks = find_blocks(mask, args.min_w, args.min_h, args.min_px, args.close)

    diff_path = os.path.join(args.out, "diff.png")
    boxes_path = os.path.join(args.out, "boxes.png")
    draw_diff(mask, blocks, diff_path)
    draw_boxes(img, blocks, boxes_path)

    print(f"IMAGE {img.width}x{img.height}  BLOCKS {len(blocks)}  close={args.close}")
    print(f"{'id':<5}{'x':<7}{'y':<7}{'w':<7}{'h':<7}{'x2':<7}{'y2':<7}")
    for b in blocks:
        print(f'{b["id"]:<5}{b["x"]:<7}{b["y"]:<7}{b["w"]:<7}{b["h"]:<7}{b["x2"]:<7}{b["y2"]:<7}')
    print(f"DIFF {diff_path}")
    print(f"BOXES {boxes_path}")

    if args.pick:
        target = next((b for b in blocks if b["id"] == args.pick), None)
        if target is None:
            print(f"ERROR pick={args.pick} 超出 1..{len(blocks)}")
            return
        picked_path = os.path.join(args.out, "picked.png")
        draw_boxes(img, blocks, picked_path, only_id=args.pick)
        print(f"PICKED #{target['id']} bbox=({target['x']},{target['y']})-({target['x2']},{target['y2']}) "
              f"size={target['w']}x{target['h']}")
        print(f"OUT {picked_path}")


if __name__ == "__main__":
    main()
