# -*- coding: utf-8 -*-
"""Intuition Mark renderer -- draws marks onto an image from a JSON spec.

Python replacement for mark.ps1. Same spec, same stdout contract (one line of
JSON), so the Node side does not need to know which one ran.

    python mark.py --spec <file>       or       python mark.py --spec -

Spec:
  {
    "source": "C:\\in.png",             optional; omit for a blank canvas
    "view": {"width":1062,"height":627}, optional; the size the caller measured
                                        on, when that is a scaled-down copy of
                                        the source. Coordinates are multiplied
                                        by source/view per axis.
    "width": 1920, "height": 1080,      used only when there is no source
    "background": "#0b0e17",            used only for a blank canvas
    "output": "C:\\out.png",
    "color": "#00e0ff",                 optional default stroke colour
    "grid": {"step": 75},               optional coordinate grid
    "shapes": [ ... ]
  }

Shapes that fail to parse are skipped rather than aborting the frame: one bad
coordinate must not throw away the whole annotation.

Everything is Pillow + numpy, both already inside this plugin folder.
"""

import argparse
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont, ImageColor

# --- colour ------------------------------------------------------------------


def make_color(text, alpha=255):
    """Parse '#rgb' / '#rrggbb' / '#rrggbbaa' / 'rgb(...)' / 'rgba(...)'.

    CSS order for 8-digit hex: RRGGBBAA. Reading it as AARRGGBB would make
    '#00e0ff22' almost transparent, which looks like a missing fill rather than
    a wrong format.
    """
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
        rgb = ImageColor.getrgb(t)
        return (rgb[0], rgb[1], rgb[2], alpha)
    except Exception:
        return None


def pick_contrast(img, x, y, w, h):
    """Pick a stroke colour that stands out from the patch it will cover."""
    try:
        x0 = max(0, int(x))
        y0 = max(0, int(y))
        x1 = min(img.width, int(x + w))
        y1 = min(img.height, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return (0, 224, 255)
        crop = img.crop((x0, y0, x1, y1)).convert("RGB").resize((32, 32))
        # tobytes() rather than getdata(): Pillow 14 removes getdata on images.
        raw = crop.tobytes()
        if not raw:
            return (0, 224, 255)
        n = len(raw) // 3
        lum = 0.0
        for i in range(0, n * 3, 3):
            lum += 0.299 * raw[i] + 0.587 * raw[i + 1] + 0.114 * raw[i + 2]
        lum /= max(1, n)
        # Dark patch -> bright cyan; light patch -> deep cyan.
        return (0, 224, 255) if lum < 128 else (0, 96, 128)
    except Exception:
        return (0, 224, 255)


def _hex(c):
    return "#%02x%02x%02x" % (c[0], c[1], c[2])


# --- fonts -------------------------------------------------------------------


def load_font(size, bold=False):
    """A CJK-capable font, since labels are often Chinese."""
    size = max(6, int(round(size)))
    names = [
        ("msyhbd.ttc" if bold else "msyh.ttc"),
        ("msyhbd.ttf" if bold else "msyh.ttf"),
        ("simhei.ttf",),
        ("consola.ttf" if not bold else "consolab.ttf"),
    ]
    for group in names:
        for name in group:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def text_size(draw, text, font):
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]
    except Exception:
        return (len(text) * 8, 16)


# --- drawing -----------------------------------------------------------------


def draw_label(draw, shape, x, y, color):
    label = shape.get("label", "")
    if not label or not str(label).strip():
        return
    size = float(shape.get("labelSize", 13))
    font = load_font(size, bold=True)
    w, h = text_size(draw, str(label), font)
    pad = 4.0
    ty = y - h - 2
    if ty < 0:
        ty = y + 2
    draw.rectangle([x - pad, ty - pad, x + w + pad, ty + h + pad],
                   fill=(8, 10, 16, 150))
    draw.text((x, ty), str(label), font=font, fill=color)


def _rounded_rect_mask(size, radius):
    from PIL import Image as _I
    mask = _I.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                        radius=max(0, int(radius)), fill=255)
    return mask


def draw_ellipse_rotated(img, cx, cy, a, b, angle, stroke, width, fill,
                         center_dot):
    """Rotated ellipse: draw on a transparent layer, rotate, composite."""
    pad = max(a, b) * 2 + width + 8
    size = int(pad * 2)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ox = oy = size / 2
    box = [ox - a, oy - b, ox + a, oy + b]
    if fill:
        d.ellipse(box, fill=fill)
    if stroke:
        d.ellipse(box, outline=stroke, width=max(1, int(width)))
    if center_dot:
        d.ellipse([ox - 2, oy - 2, ox + 2, oy + 2], fill=stroke)
    layer = layer.rotate(-angle, resample=Image.BICUBIC, center=(ox, oy))
    img.alpha_composite(layer, (int(cx - ox), int(cy - oy)))


def normalize_shape(shape):
    """Turn the caller's flexible shape forms into the internal ones.

    The tool documents several spellings for the same thing; accepting all of
    them keeps the spec readable when hand-written:
      kind: box  + points:[[x,y],...]      -> x/y/w/h bounding box
      kind: box  + x,y,w,h | x1,y1,x2,y2   -> x/y/w/h
      kind: dot                            -> circle
      kind: circle + diameter/circumference-> r
      kind: ellipse + foci + circumference -> cx/cy/a/b/angle
    """
    t = str(shape.get("type", shape.get("kind", ""))).lower()
    if t in ("box", "rectangle"):
        t = "rect"
    elif t == "dot":
        t = "circle"

    if t == "rect":
        pts = shape.get("points")
        if isinstance(pts, list) and pts:
            xs = [float(p[0]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
            ys = [float(p[1]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
            if xs and ys:
                shape["x"] = min(xs)
                shape["y"] = min(ys)
                shape["w"] = max(xs) - min(xs)
                shape["h"] = max(ys) - min(ys)
        if ("w" not in shape or "h" not in shape) and "x2" in shape and "y2" in shape:
            x1 = float(shape.get("x1", 0) or 0)
            y1 = float(shape.get("y1", 0) or 0)
            x2 = float(shape.get("x2", 0) or 0)
            y2 = float(shape.get("y2", 0) or 0)
            shape["x"], shape["y"] = min(x1, x2), min(y1, y2)
            shape["w"], shape["h"] = abs(x2 - x1), abs(y2 - y1)

    elif t == "circle":
        if shape.get("r") is None:
            if shape.get("diameter") is not None:
                shape["r"] = float(shape["diameter"]) / 2.0
            elif shape.get("circumference") is not None:
                shape["r"] = float(shape["circumference"]) / (2 * math.pi)
        if shape.get("cx") is None:
            c = shape.get("center")
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                shape["cx"], shape["cy"] = float(c[0]), float(c[1])

    elif t == "ellipse":
        foci = shape.get("foci")
        circ = shape.get("circumference")
        if (isinstance(foci, list) and len(foci) == 2 and circ
                and shape.get("a") is None):
            # Two foci + perimeter -> centre, semi-axes, rotation. The geometry
            # that needs trigonometry lives here, in one place.
            f1 = (float(foci[0][0]), float(foci[0][1]))
            f2 = (float(foci[1][0]), float(foci[1][1]))
            cx = (f1[0] + f2[0]) / 2.0
            cy = (f1[1] + f2[1]) / 2.0
            d = math.hypot(f2[0] - f1[0], f2[1] - f1[1])
            c = d / 2.0
            # Ramanujan's approximation for a from perimeter and c.
            p = float(circ)
            a = p / (2 * math.pi)
            for _ in range(24):
                b2 = max(1e-9, a * a - c * c)
                b = math.sqrt(b2)
                h = ((a - b) ** 2) / ((a + b) ** 2) if (a + b) else 0
                approx = math.pi * (a + b) * (1 + 3 * h / (10 + math.sqrt(4 - 3 * h)))
                a += (p - approx) / (2 * math.pi)
            b = math.sqrt(max(1e-9, a * a - c * c))
            shape["cx"], shape["cy"] = cx, cy
            shape["a"], shape["b"] = a, b
            shape["angle"] = math.degrees(math.atan2(f2[1] - f1[1], f2[0] - f1[0]))
        if shape.get("a") is None and shape.get("rx") is not None:
            shape["a"] = float(shape["rx"])
        if shape.get("b") is None and shape.get("ry") is not None:
            shape["b"] = float(shape["ry"])

    shape["type"] = t
    return shape


def scale_shape(shape, sx, sy, sm):
    t = str(shape.get("type", shape.get("kind", ""))).lower()
    if t in ("box", "rectangle", "rect"):
        for name, f in (("x", sx), ("y", sy), ("x1", sx), ("y1", sy),
                        ("x2", sx), ("y2", sy), ("w", sx), ("h", sy)):
            if name in shape and shape[name] is not None:
                shape[name] = float(shape[name]) * f
        for name in ("radius",):
            if name in shape and shape[name] is not None:
                shape[name] = float(shape[name]) * sm
    elif t in ("circle", "dot"):
        for name, f in (("cx", sx), ("cy", sy), ("r", sm)):
            if name in shape and shape[name] is not None:
                shape[name] = float(shape[name]) * f
    elif t == "ellipse":
        for name, f in (("cx", sx), ("cy", sy), ("a", sm), ("b", sm)):
            if name in shape and shape[name] is not None:
                shape[name] = float(shape[name]) * f
    elif t == "text":
        for name, f in (("x", sx), ("y", sy), ("size", sm)):
            if name in shape and shape[name] is not None:
                shape[name] = float(shape[name]) * f
    for name in ("strokeWidth", "labelSize"):
        if name in shape and shape[name] is not None:
            shape[name] = float(shape[name]) * sm


# --- grid --------------------------------------------------------------------


def draw_grid(img, draw, step):
    step = float(step)
    if step <= 0:
        return
    gw, gh = img.size
    line = (0, 224, 255, 90)
    for gx in range(0, int(gw) + 1, int(step)):
        draw.line([(gx, 0), (gx, gh)], fill=line, width=1)
    for gy in range(0, int(gh) + 1, int(step)):
        draw.line([(0, gy), (gw, gy)], fill=line, width=1)
    size = max(8, min(11, step * 0.075))
    font = load_font(size)
    label_color = (255, 214, 0, 235)
    plate = (8, 10, 16, 120)
    for gy in range(0, int(gh) + 1, int(step)):
        for gx in range(0, int(gw) + 1, int(step)):
            t = "%d,%d" % (gx, gy)
            tw, th = text_size(draw, t, font)
            px = gx + 1
            py = gy + 1
            if px > gw - tw - 2:
                px = gx - tw - 1
            if py > gh - th - 2:
                py = gy - th - 1
            draw.rectangle([px, py, px + tw, py + th], fill=plate)
            draw.text((px, py), t, font=font, fill=label_color)


# --- main --------------------------------------------------------------------


def render(doc):
    source = str(doc.get("source", "") or "")
    if source:
        if not os.path.isfile(source):
            raise RuntimeError("no such file: %s" % source)
        img = Image.open(source).convert("RGBA")
    else:
        w = int(doc.get("width", 0) or 0)
        h = int(doc.get("height", 0) or 0)
        if w <= 0 or h <= 0:
            raise RuntimeError("blank canvas needs width and height")
        bg = make_color(doc.get("background", "#0b0e17"), 255) or (11, 14, 23, 255)
        img = Image.new("RGBA", (w, h), bg)

    # Rescale from a smaller view: the caller may only have seen a downscaled
    # copy, so its numbers live in that smaller space.
    view = doc.get("view")
    scale_x = scale_y = 1.0
    if isinstance(view, dict):
        vw = float(view.get("width", 0) or 0)
        vh = float(view.get("height", 0) or 0)
        if vw > 0 and vh > 0:
            scale_x = img.width / vw
            scale_y = img.height / vh
    rescaled = abs(scale_x - 1.0) > 1e-9 or abs(scale_y - 1.0) > 1e-9
    shapes = doc.get("shapes") or []
    shapes = [normalize_shape(s) for s in shapes if isinstance(s, dict)]
    if rescaled:
        sm = math.sqrt(scale_x * scale_y)
        for shape in shapes:
            scale_shape(shape, scale_x, scale_y, sm)

    draw = ImageDraw.Draw(img)
    default_color = make_color(doc.get("color", ""), 255)
    drawn = 0
    skipped = []

    grid = doc.get("grid")
    if isinstance(grid, dict) and float(grid.get("step", 0) or 0) > 0:
        draw_grid(img, draw, float(grid["step"]))

    for shape in shapes:
        if not isinstance(shape, dict):
            continue
        t = str(shape.get("type", shape.get("kind", ""))).lower()
        if t in ("box", "rectangle"):
            t = "rect"
        if t == "dot":
            t = "circle"
        try:
            stroke = make_color(shape.get("stroke", ""), 255)
            if stroke is None:
                # Auto contrast: dark patch gets bright cyan, light gets deep.
                bx = float(shape.get("x", 0) or 0)
                by = float(shape.get("y", 0) or 0)
                bw = float(shape.get("w", 0) or 0)
                bh = float(shape.get("h", 0) or 0)
                if bw <= 0 or bh <= 0:
                    ccx = float(shape.get("cx", 0) or 0)
                    ccy = float(shape.get("cy", 0) or 0)
                    rr = float(shape.get("r", 0) or 0)
                    if rr > 0:
                        bx, by, bw, bh = ccx - rr, ccy - rr, rr * 2, rr * 2
                if bw > 0 and bh > 0:
                    stroke = pick_contrast(img, bx, by, bw, bh) + (255,)
                else:
                    stroke = default_color or (0, 224, 255, 255)
            fill = make_color(shape.get("fill", ""), 46)
            width = float(shape.get("strokeWidth", 3) or 3)
            width = max(1.0, width)
            dash = str(shape.get("dash", "") or "")

            if t == "rect":
                x = float(shape.get("x", 0) or 0)
                y = float(shape.get("y", 0) or 0)
                w = float(shape.get("w", 0) or 0)
                h = float(shape.get("h", 0) or 0)
                if w < 0:
                    x, w = x + w, -w
                if h < 0:
                    y, h = y + h, -h
                if w <= 0 or h <= 0:
                    raise ValueError("rect needs a positive w and h")
                radius = float(shape.get("radius", 0) or 0)
                box = [x, y, x + w, y + h]
                if radius > 0:
                    if fill:
                        draw.rounded_rectangle(box, radius=radius, fill=fill)
                    draw.rounded_rectangle(box, radius=radius, outline=stroke,
                                           width=max(1, int(width)))
                else:
                    if fill:
                        draw.rectangle(box, fill=fill)
                    draw.rectangle(box, outline=stroke, width=max(1, int(width)))
                draw_label(draw, shape, x, y, stroke)
                drawn += 1

            elif t == "circle":
                cx = float(shape.get("cx", 0) or 0)
                cy = float(shape.get("cy", 0) or 0)
                r = float(shape.get("r", 0) or 0)
                if r <= 0:
                    raise ValueError("circle needs a positive r")
                box = [cx - r, cy - r, cx + r, cy + r]
                if fill:
                    draw.ellipse(box, fill=fill)
                draw.ellipse(box, outline=stroke, width=max(1, int(width)))
                # A centre dot makes the sent coordinate visible, not implied.
                draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=stroke)
                draw_label(draw, shape, cx - r, cy - r, stroke)
                drawn += 1

            elif t == "ellipse":
                cx = float(shape.get("cx", 0) or 0)
                cy = float(shape.get("cy", 0) or 0)
                a = float(shape.get("a", 0) or 0)
                b = float(shape.get("b", 0) or 0)
                angle = float(shape.get("angle", 0) or 0)
                if a <= 0 or b <= 0:
                    raise ValueError("ellipse needs positive semi-axes a and b")
                draw_ellipse_rotated(img, cx, cy, a, b, angle, stroke, width,
                                     fill, True)
                draw_label(draw, shape, cx - a, cy - b, stroke)
                drawn += 1

            elif t == "text":
                tx = float(shape.get("x", 0) or 0)
                ty = float(shape.get("y", 0) or 0)
                text = str(shape.get("text", "") or "")
                if not text.strip():
                    raise ValueError("text needs text")
                size = float(shape.get("size", 16) or 16)
                font = load_font(size)
                draw.text((tx, ty), text, font=font, fill=stroke)
                drawn += 1

            else:
                raise ValueError("unknown shape type: %s" % t)

        except Exception as e:
            skipped.append("%s: %s" % (t, e))

    if drawn == 0 and not (isinstance(grid, dict)
                           and float(grid.get("step", 0) or 0) > 0):
        raise RuntimeError("mark: nothing drawn - " + "; ".join(skipped))

    output = str(doc.get("output", "") or "")
    if not output:
        output = os.path.join(os.environ.get("TEMP", os.getcwd()),
                              "mark-%d.png" % int(__import__("time").time() * 1000))
    out_dir = os.path.dirname(output)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    img.convert("RGB").save(output, "PNG")
    result = {
        "ok": True,
        "path": output,
        "width": img.width,
        "height": img.height,
        "drawn": drawn,
        "skipped": skipped,
    }
    if rescaled:
        result["view"] = {"width": view.get("width"), "height": view.get("height")}
        result["scale"] = {"x": scale_x, "y": scale_y}
    return result


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
            sys.stderr.write("mark: empty spec\n")
            return 2
        doc = json.loads(raw)
    except Exception as e:
        sys.stderr.write("mark: unreadable spec: %s\n" % e)
        return 2

    try:
        result = render(doc)
    except Exception as e:
        sys.stderr.write("%s\n" % e)
        return 3
    # The caller reads the last stdout line as JSON.
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
