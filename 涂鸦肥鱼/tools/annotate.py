# -*- coding: utf-8 -*-
"""Composite pencil marks onto an image and write a new PNG.

Python replacement for annotate.ps1. Same spec, same stdout (the output path).

    python annotate.py --spec <file>      or      python annotate.py --spec -

Spec: {"source":"C:\\in.png","output":"C:\\out.png","unit":"screen|window",
       "items":[ ...same primitives as the board... ]}

For an image, "screen" means picture pixels: the annotator draws at the same
coordinates the board would use, so a mark composed for the screen lands on the
same place in the picture when the picture was a screenshot of it.

Items supported: rect, ellipse, path, arrow, text, image (data: URL).
Pillow only -- it already ships inside this plugin.
"""

import argparse
import base64
import io
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --- colour ------------------------------------------------------------------


def make_color(text, alpha=255):
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    try:
        if t.lower().startswith("rgba(") or t.lower().startswith("rgb("):
            inner = t[t.index("(") + 1: t.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            r = int(float(parts[0]))
            g = int(float(parts[1]))
            b = int(float(parts[2]))
            a = int(float(parts[3]) * 255) if len(parts) >= 4 else alpha
            return (max(0, min(255, r)), max(0, min(255, g)),
                    max(0, min(255, b)), max(0, min(255, a)))
        if t.startswith("#"):
            h = t[1:]
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) == 6:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
            if len(h) == 8:
                return (int(h[0:2], 16), int(h[2:4], 16),
                        int(h[4:6], 16), int(h[6:8], 16))
        named = {
            "cyan": (0, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
            "white": (255, 255, 255), "black": (0, 0, 0), "yellow": (255, 255, 0),
            "blue": (0, 0, 255), "magenta": (255, 0, 255), "orange": (255, 165, 0),
            "gray": (128, 128, 128), "grey": (128, 128, 128),
        }
        if t.lower() in named:
            c = named[t.lower()]
            return (c[0], c[1], c[2], alpha)
    except Exception:
        return None
    return None


def load_font(size, bold=False):
    size = max(6, int(round(size)))
    names = ["msyhbd.ttc" if bold else "msyh.ttc",
             "msyhbd.ttf" if bold else "msyh.ttf",
             "simhei.ttf", "consolab.ttf" if bold else "consola.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()

    try:
        if args.spec == "-":
            raw = sys.stdin.read()
        else:
            with open(args.spec, "r", encoding="utf-8") as f:
                raw = f.read()
        if not raw.strip():
            sys.stderr.write("annotate: empty spec\n")
            return 2
        doc = json.loads(raw)
    except Exception as e:
        sys.stderr.write("annotate: unreadable spec: %s\n" % e)
        return 2

    source = str(doc.get("source", "") or "")
    if not source or not os.path.isfile(source):
        sys.stderr.write("annotate: no such file: %s\n" % source)
        return 2

    output = str(doc.get("output", "") or "")
    if not output:
        d = os.path.dirname(source)
        n = os.path.splitext(os.path.basename(source))[0]
        output = os.path.join(d, n + "-pencil.png")

    img = Image.open(source).convert("RGBA")
    screen_unit = str(doc.get("unit", "screen")) != "window"
    scale_x = img.width / 100.0
    scale_y = img.height / 100.0

    def sx(v):
        return float(v) if screen_unit else float(v) * scale_x

    def sy(v):
        return float(v) if screen_unit else float(v) * scale_y

    draw = ImageDraw.Draw(img)

    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", ""))
        try:
            if t == "rect":
                x, y = sx(item.get("x", 0)), sy(item.get("y", 0))
                w, h = sx(item.get("w", 0)), sy(item.get("h", 0))
                if w <= 0 or h <= 0:
                    continue
                fill = str(item.get("fill", "") or "")
                if fill and fill != "none":
                    fc = make_color(fill, 64)
                    if fc:
                        draw.rectangle([x, y, x + w, y + h], fill=fc)
                stroke = str(item.get("stroke", "") or "")
                if stroke and stroke != "none":
                    sc = make_color(stroke, 255) or (0, 255, 255, 255)
                    draw.rectangle([x, y, x + w, y + h], outline=sc,
                                   width=max(1, int(float(item.get("width", 2) or 2))))

            elif t == "ellipse":
                x, y = sx(item.get("x", 0)), sy(item.get("y", 0))
                w, h = sx(item.get("w", 0)), sy(item.get("h", 0))
                if w <= 0 or h <= 0:
                    continue
                fill = str(item.get("fill", "") or "")
                if fill and fill != "none":
                    fc = make_color(fill, 64)
                    if fc:
                        draw.ellipse([x, y, x + w, y + h], fill=fc)
                stroke = str(item.get("stroke", "") or "")
                if stroke and stroke != "none":
                    sc = make_color(stroke, 255) or (0, 255, 255, 255)
                    draw.ellipse([x, y, x + w, y + h], outline=sc,
                                 width=max(1, int(float(item.get("width", 2) or 2))))

            elif t == "path":
                pts = item.get("points") or []
                if len(pts) < 2:
                    continue
                sc = make_color(item.get("stroke", ""), 255) or (0, 255, 255, 255)
                coords = [(sx(p[0]), sy(p[1])) for p in pts
                          if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(coords) >= 2:
                    draw.line(coords, fill=sc,
                              width=max(1, int(float(item.get("width", 2) or 2))),
                              joint="curve")

            elif t == "arrow":
                x1, y1 = sx(item.get("x1", 0)), sy(item.get("y1", 0))
                x2, y2 = sx(item.get("x2", 0)), sy(item.get("y2", 0))
                sc = make_color(item.get("stroke", ""), 255) or (0, 255, 255, 255)
                wdt = max(1, int(float(item.get("width", 2) or 2)))
                draw.line([(x1, y1), (x2, y2)], fill=sc, width=wdt)
                head = float(item.get("head", 18) or 18)
                ang = math.atan2(y2 - y1, x2 - x1)
                for spread in (0.45, -0.45):
                    draw.line([(x2, y2),
                               (x2 - head * math.cos(ang + spread),
                                y2 - head * math.sin(ang + spread))],
                              fill=sc, width=wdt)

            elif t == "text":
                text = str(item.get("text", "") or "")
                if not text:
                    continue
                size = float(item.get("size", 18) or 18)
                bold = str(item.get("weight", "")) == "bold"
                font = load_font(size, bold)
                col = make_color(item.get("color", ""), 255) or (255, 255, 255, 255)
                x, y = sx(item.get("x", 0)), sy(item.get("y", 0))
                align = str(item.get("align", "") or "")
                anchor = {"center": "ma", "right": "ra"}.get(align, "la")
                draw.text((x, y), text, font=font, fill=col, anchor=anchor)

            elif t == "image":
                src2 = str(item.get("src", "") or "")
                comma = src2.find(",")
                if comma < 0:
                    continue
                data = base64.b64decode(src2[comma + 1:])
                sub = Image.open(io.BytesIO(data)).convert("RGBA")
                x, y = sx(item.get("x", 0)), sy(item.get("y", 0))
                w = sx(item["w"]) if item.get("w") else sub.width
                h = sy(item["h"]) if item.get("h") else sub.height
                sub = sub.resize((max(1, int(w)), max(1, int(h))))
                img.alpha_composite(sub, (int(x), int(y)))
                draw = ImageDraw.Draw(img)
        except Exception:
            # One bad item must not throw away the whole annotation.
            continue

    out_dir = os.path.dirname(output)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    img.convert("RGB").save(output, "PNG")
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
