"""截图专用三合一：底图用**原图**，把 OCR 文字 / UI 框标上去。

⚠ 这张图上看的是「原图 + OCR框 + UI框」——**没有差分**。
  差分只在后台用来分块，不画到图上（画上去会和 OCR/UI 框叠成三色网格，
  把画面搅乱）。块表仍在 act.json 的 blocks 里，需要时能查。

⚠ 底图为什么用原图（以前用差分图，改过来了）：
  差分图是白底 + 红像素，只保留「像素变化的地方」。图标、图片、渐变
  这些不产生差异的东西会整块丢掉，看图像在看一张红色线稿 —— 用户反馈
  「差分图这个图标根本看不清楚」。判读靠的是画面本身，所以底图必须是原图。

和 scan.py 的区别：

  scan.py  三层框各自画在原图上，但 OCR 原文只在 JSON 里，图上没有。
  act.py   同样用原图当底图，但把 OCR 的**文字内容直接标在图上**，
           每条标在它自己的位置，看一张图就能读到「这里写的是什么」。

用法:
  python act.py <图片> <输出目录>

输出:
  act.png    原图 + OCR 文字 + UI 框（本流程唯一要看的那张）
  act.json   结构化结果：差分块表 + 文字层 + UI 层 + 图片尺寸
"""
import argparse
import json
import os
import subprocess
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from PIL import Image, ImageDraw, ImageFont

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_PY = os.path.join(HERE, "..", "ocr_env", "Scripts", "python.exe")
SYS_PY = sys.executable

# ⚠ 底图是原图之后，配色要重挑：界面上本来就可能有红字、蓝按钮，
#   标注框如果撞色就分不清「这是原图的」还是「这是我标的」。
#   所以标注一律用**界面里少见的**高饱和色，且每条文字都带不透明底条，
#   靠底条把自己和底图隔开 —— 这是原图当底图后唯一能保证可读的做法。
COLOR_TEXT = (150, 0, 90)      # 文字：深洋红（界面里几乎没有）
COLOR_UI = (0, 170, 90)        # UI 框：绿（比蓝按钮少见）
COLOR_BLOCK = (230, 120, 0)    # 差分块：橙（和绿/洋红都能分开）
COLOR_BLOCK_TXT = (150, 70, 0)

# 文字标签的底条：不透明浅色。原图当底图时必须不透明，
# 半透明会被底下的深色内容透过、字就糊了。
LABEL_BG = (255, 255, 235)

# ── 坐标网格 ──
#
# 给模型读数用的。模型这边收到的图会被压缩，且它不知道压缩比，
# 所以不能靠「我感觉这里大约多少」。网格上的数字是**原图像素坐标**，
# 压缩只改变数字显示在哪，不改变数字本身 —— 读到什么就是什么。
GRID_STEP = 100                 # 格距（原图像素）
GRID_LINE = (0, 160, 220)       # 网格线：淡蓝，细
GRID_FG = (255, 230, 0)         # 坐标数字：黄，和线条分开
GRID_BG = (20, 24, 32)          # 数字底块：深色，压住底图


def load_font(size):
    """找一个能显示中文的字体，找不到就退回默认位图字体。"""
    for path in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def run_child(python_exe, script, args, json_path, timeout=600):
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
        return None, "bad json: %s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("outdir")
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--ui-conf", type=float, default=0.25)
    # UI 框默认画（检测图标类目标时比差分准）。要关掉传 --no-draw-ui。
    ap.add_argument("--no-draw-ui", action="store_true",
                    help="不画 UI 框")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    img = Image.open(args.image).convert("RGB")
    W, H = img.size
    warnings = []

    # ── 1. 差分：拿到 diff.png 和块表 ──
    # shift_diff.py 自己会写 diff.png，但我们不能用它的，因为要在上面
    # 追加文字标注 —— 先让它产出，再读回来当底图。
    diff_proc = subprocess.run(
        [SYS_PY, os.path.join(HERE, "shift_diff.py"), args.image, args.outdir],
        capture_output=True, timeout=300,
    )
    if diff_proc.returncode != 0:
        err = diff_proc.stderr.decode("utf-8", "replace").strip().splitlines()
        warnings.append("diff: %s" % (err[-1] if err else "failed"))
    diff_path = os.path.join(args.outdir, "diff.png")
    blocks = []
    if os.path.isfile(diff_path):
        # 块表从 stdout 里解析（shift_diff.py 的表格格式固定）。
        out = diff_proc.stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 7 and parts[0].isdigit():
                try:
                    blocks.append({
                        "id": int(parts[0]),
                        "x": int(parts[1]), "y": int(parts[2]),
                        "w": int(parts[3]), "h": int(parts[4]),
                        "x2": int(parts[5]), "y2": int(parts[6]),
                    })
                except ValueError:
                    pass

    # ⚠ 底图是**原图**，不是 diff.png。
    #
    #   差分图只有白底 + 红像素：图标、图片、渐变这些「不产生像素变化」
    #   的东西会整块丢掉，看上去就是一张红色线稿（用户反馈「图标根本
    #   看不清楚」）。判读要的是画面本身，所以底图必须用原图。
    #   差分只贡献块表（版面分块），块框叠在原图上就够了。
    base = img.copy()

    # ── 2. OCR（原图检测）──
    ocr_json = os.path.join(args.outdir, "_ocr.json")
    texts, err = run_child(OCR_PY, "det_ocr.py",
                           [args.image, ocr_json, str(args.min_conf)], ocr_json)
    if err != "":
        texts = []
        warnings.append("ocr: %s" % err)

    # ── 3. UI（原图检测）──
    ui_json = os.path.join(args.outdir, "_ui.json")
    uis, err = run_child(SYS_PY, "det_ui.py",
                         [args.image, ui_json, str(args.ui_conf)], ui_json)
    if err != "":
        uis = []
        warnings.append("ui: %s" % err)

    for p in (ocr_json, ui_json):
        try:
            os.remove(p)
        except OSError:
            pass

    # ── 4. 把文字和 UI 标到原图上 ──
    #
    # ⚠ 这张图是**给 agent 自己读的材料**，不是给人看的成品。
    #   所以要的是「信息都在」，不是「画面好看」：
    #   OCR 每一条都标在它原来的位置上，UI 每个框都画出来。
    #   拥挤没关系 —— 看不清的地方可以用 crops 里的原图块补看。
    #
    #   （曾经为了画面干净把文字按块合并、把 UI 框关掉，那是拿人眼的
    #     审美去衡量机器的输入，方向错了。）
    canvas = base.copy()
    d = ImageDraw.Draw(canvas, "RGBA")
    font = load_font(13)
    # 网格数字用等宽小字：太大压住画面，太小读不出。
    grid_font = load_font(11)

    # UI 框：**默认画**。UI 检测有时比差分更准（尤其是图标类目标），
    # 多一层线索就多一分判对的把握。
    #
    # ⚠ 顺序很关键：UI 框和 OCR 文字都先画在原图上，**网格最后画**。
    #   网格是反色的 —— 它压过 UI 框时会把那条绿线翻成它的反色，
    #   所以网格永远是画面里对比最强的东西，不会被别的标注盖掉。
    #   如果反过来（先画网格再画 UI），绿框会横穿网格线，两条都看不清。
    if not args.no_draw_ui:
        for u in uis:
            d.rectangle([u["x"], u["y"], u["x2"], u["y2"]],
                        outline=COLOR_UI, width=2)

    def draw_tag(px, py, text, color, max_chars=40):
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        try:
            tw, th = d.textbbox((0, 0), text, font=font)[2:]
        except Exception:
            tw, th = len(text) * 7, 13
        px = max(0, min(px, W - tw - 4))
        py = max(0, py)
        # 底条必须不透明：原图有深有浅，半透明会被透过、字就糊了。
        d.rectangle([px - 2, py - 1, px + tw + 2, py + th + 1],
                    fill=LABEL_BG)
        d.text((px, py), text, fill=color, font=font)

    # OCR：每一条都标在它自己的位置。
    # 标签优先写在框上方（不盖住原文），上方没空间才写进框里。
    for t0 in texts:
        label = (t0.get("text") or "").strip()
        if label == "":
            continue
        d.rectangle([t0["x"], t0["y"], t0["x2"], t0["y2"]],
                    outline=COLOR_TEXT, width=1)
        th_guess = max(13, int(t0["y2"] - t0["y"]))
        lp_y = t0["y"] - th_guess - 2
        if lp_y < 0:
            lp_y = t0["y"] + 1
        draw_tag(t0["x"], lp_y, label, COLOR_TEXT)

    # ── 坐标网格：最后画，压在最上层 ──
    #
    # ⚠ 这条是**必须**的，不是装饰。
    #
    #   模型这一端收到的图会被压缩（1920x1080 的图，模型看到的可能是
    #   1536x864 甚至更小），而模型自己不知道压缩比是多少。结果是：它在
    #   图上「看到」某个东西在 (800,400)，那其实是它自己那张小图的
    #   (800,400)，换算到真实屏幕是 (1000,500) —— 偏了 25%。
    #
    #   画上坐标网格之后，模型不必再依赖「我感觉这里大约多少」：
    #   它直接读网格线上的数字，那些数字就是**原图像素坐标**。
    #   压缩只影响数字看起来的位置，不影响数字本身写的是什么。
    #
    #   这就是用户要求的「坐标系」，用它读数比用眼睛估准得多。
    # 画网格：竖线标 x（写在顶部/底部），横线标 y（写在左侧/右侧）。
    #
    # ⚠ 网格线颜色**取底下像素的反色**，不是固定色。
    #
    #   固定色的网格线永远会在一半的画面上糊掉：淡色线在亮天空上看不见，
    #   深色线在暗部看不见。反色（255-r, 255-g, 255-b）保证任何底色上
    #   都形成最大对比 —— 亮处出深线、暗处出亮线，一条线都不丢。
    #
    # ⚠ 不在每个交点标「x,y」：20 列 × 11 行 = 220 个标签，会把画面糊死。
    #   按行列各标一次，读的时候「往上看竖线、往左看横线」就能拼出坐标。
    arr = np.asarray(canvas).astype(np.int16)

    def invert_line(vertical, pos):
        """把一条 1px 宽（为了看清用 2px）的线画成底下像素的反色。"""
        if vertical:
            seg = arr[:, max(0, pos):min(W, pos + 2), :]
            arr[:, max(0, pos):min(W, pos + 2), :] = 255 - seg
        else:
            seg = arr[max(0, pos):min(H, pos + 2), :, :]
            arr[max(0, pos):min(H, pos + 2), :, :] = 255 - seg

    for gx in range(0, W, GRID_STEP):
        invert_line(True, gx)
    for gy in range(0, H, GRID_STEP):
        invert_line(False, gy)

    # 把反色后的像素写回画布，再用普通 draw 标数字。
    canvas = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(canvas, "RGBA")

    for gx in range(0, W, GRID_STEP):
        if gx > 0:
            txt = str(gx)
            try:
                tw, th = d.textbbox((0, 0), txt, font=grid_font)[2:]
            except Exception:
                tw, th = len(txt) * 7, 11
            # 顶部标一次
            d.rectangle([gx + 1, 1, gx + tw + 4, th + 4], fill=GRID_BG)
            d.text((gx + 2, 2), txt, fill=GRID_FG, font=grid_font)
            # 底部再标一次：图下半部分读的时候不用回到顶上找
            d.rectangle([gx + 1, H - th - 5, gx + tw + 4, H - 2], fill=GRID_BG)
            d.text((gx + 2, H - th - 4), txt, fill=GRID_FG, font=grid_font)

    for gy in range(0, H, GRID_STEP):
        if gy > 0:
            txt = str(gy)
            try:
                tw, th = d.textbbox((0, 0), txt, font=grid_font)[2:]
            except Exception:
                tw, th = len(txt) * 7, 11
            # 左侧标一次
            d.rectangle([1, gy + 1, tw + 4, gy + th + 3], fill=GRID_BG)
            d.text((2, gy + 1), txt, fill=GRID_FG, font=grid_font)
            # 右侧再标一次
            d.rectangle([W - tw - 5, gy + 1, W - 2, gy + th + 3], fill=GRID_BG)
            d.text((W - tw - 4, gy + 1), txt, fill=GRID_FG, font=grid_font)

    # ⚠ 差分块**不画**。
    #
    #   差分是后台用来「分块」的中间手段，不该出现在给人看的那张图上
    #   （用户明确要求：只看原图 + OCR + UI 框 + 坐标系）。
    #   块框画上去还会和 OCR 框、UI 框叠成三色网格，把画面搅乱。
    #   块表仍然在 act.json 的 blocks 里，需要时查得到。

    act_png = os.path.join(args.outdir, "act.png")
    canvas.save(act_png)

    # 中间产物不留：diff.png 的像素已经被合进 act.png，boxes.png 是
    # shift_diff.py 顺手生成的、本流程根本不用。
    # 只留 act.png（看一次）和 act.json（结构化数据）。
    for junk in ("boxes.png",):
        try:
            os.remove(os.path.join(args.outdir, junk))
        except OSError:
            pass
    # diff.png 保留到 act.png 写完就删 —— 它是底图，已经合并进结果了。
    try:
        os.remove(diff_path)
    except OSError:
        pass

    doc = {
        "image": {"width": W, "height": H, "path": os.path.abspath(args.image)},
        "warnings": warnings,
        "blocks": blocks,
        "texts": texts,
        "uis": uis,
        "counts": {"blocks": len(blocks), "text": len(texts), "ui": len(uis)},
        "act_png": act_png,
    }
    act_json = os.path.join(args.outdir, "act.json")
    with open(act_json, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    print("IMAGE %dx%d" % (W, H))
    print("BLOCKS %d  TEXT %d  UI %d" % (len(blocks), len(texts), len(uis)))
    for w in warnings:
        print("WARN %s" % w)
    print("PNG %s" % act_png)
    print("JSON %s" % act_json)


if __name__ == "__main__":
    main()
