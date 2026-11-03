# -*- coding: utf-8 -*-
"""Titanium Fisheye -- capture engine.

Python replacement for capture.ps1. Same modes, same JSON on stdout.

    python capture.py --mode screen  --out a.png
    python capture.py --mode window  --out a.png --hwnd 12345
    python capture.py --mode desktop --out a.png
    python capture.py --mode list
    python capture.py --mode info

Targets, each using the mechanism that actually works for it:

  screen   BitBlt from the desktop DC. Sees exactly what a person sees: the
           topmost pixels, so a covering window hides whatever is beneath it.
  window   PrintWindow with PW_RENDERFULLCONTENT. The window draws itself into
           memory, so a covered window still yields its full content without
           being raised or focus-stolen.
  desktop  PrintWindow on the shell window. Wallpaper and icons come back in
           full even when application windows cover the screen, because the
           desktop paints itself rather than being read from it.

Accuracy rules, because a screenshot that is silently wrong is worse than none:
  - DPI awareness is declared before any size is read, so every number is in
    physical pixels and matches what the user sees.
  - Window rects come from DwmGetWindowAttribute, which excludes the invisible
    resize border GetWindowRect includes.
  - Every capture is measured after the fact and rejected if its pixel size does
    not equal what was requested.
  - A capture that comes back blank is reported as blank, never handed over as
    a valid image.

stdlib + ctypes + Pillow, all already inside this plugin folder.
"""

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes

from PIL import Image

# --- win32 -------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
PW_RENDERFULLCONTENT = 2
DIB_RGB_COLORS = 0
BI_RGB = 0
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GW_OWNER = 4


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def become_aware():
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen():
    get = user32.GetSystemMetrics
    get.argtypes = [ctypes.c_int]
    get.restype = ctypes.c_int
    x, y = get(SM_XVIRTUALSCREEN), get(SM_YVIRTUALSCREEN)
    w, h = get(SM_CXVIRTUALSCREEN), get(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        x = y = 0
        w, h = get(0), get(1)
    return x, y, w, h


def window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def try_bound(hwnd):
    """On-screen bounds excluding the invisible resize border."""
    r = RECT()
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(r), ctypes.sizeof(RECT))
        if hr == 0:
            return r
    except Exception:
        pass
    if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return r
    return None


ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_windows():
    """Top-level windows a person could point at."""
    out = []
    fg = user32.GetForegroundWindow()

    def cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = window_text(hwnd)
            if not title:
                return True
            if user32.GetWindow(hwnd, GW_OWNER):
                return True
            r = try_bound(hwnd)
            if r is None:
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0:
                return True
            out.append({
                "hwnd": int(hwnd),
                "title": title,
                "className": class_name(hwnd),
                "x": r.left, "y": r.top, "w": w, "h": h,
                "minimized": bool(user32.IsIconic(hwnd)),
                "foreground": int(hwnd) == int(fg),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(ENUM_PROC(cb), 0)
    return out


def desktop_host():
    """The shell window that paints wallpaper and icons."""
    try:
        shell = user32.GetShellWindow()
        if shell:
            return shell
    except Exception:
        pass
    progman = user32.FindWindowW("Progman", None)
    if progman:
        return progman
    worker = 0
    while True:
        worker = user32.FindWindowExW(0, worker, "WorkerW", None)
        if not worker:
            break
        if user32.FindWindowExW(worker, 0, "SHELLDLL_DefView", None):
            return worker
    return 0


def _dib_to_image(buf, w, h):
    """BGRA buffer (top-down) -> RGBA PIL image."""
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)


def grab_screen(x, y, w, h):
    """Copy the visible screen with BitBlt."""
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC failed")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    if not hdc_mem:
        user32.ReleaseDC(None, hdc_screen)
        raise OSError("CreateCompatibleDC failed")
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    if not bmp:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        raise OSError("CreateCompatibleBitmap failed")
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y,
                            SRCCOPY | CAPTUREBLT):
            raise OSError("BitBlt failed")
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        size = w * h * 4
        buf = ctypes.create_string_buffer(size)
        copied = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi),
                                 DIB_RGB_COLORS)
        if copied != h:
            raise OSError("GetDIBits copied %d of %d rows" % (copied, h))
        return _dib_to_image(buf.raw, w, h)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def render_window(hwnd, w, h):
    """Ask a window to paint itself into a bitmap of the given size."""
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC failed")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        ok = user32.PrintWindow(wintypes.HWND(hwnd), hdc_mem, PW_RENDERFULLCONTENT)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        copied = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi),
                                 DIB_RGB_COLORS)
        if copied != h:
            raise OSError("GetDIBits copied %d of %d rows" % (copied, h))
        return _dib_to_image(buf.raw, w, h), bool(ok)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def ink_ratio(img):
    """Share of sampled pixels that are not near-black.

    A failed PrintWindow returns an empty bitmap; this catches it so a black
    frame is never reported as a successful capture.
    """
    small = img.convert("RGB").resize((96, 96))
    raw = small.tobytes()
    n = len(raw) // 3
    lit = 0
    for i in range(0, n * 3, 3):
        if raw[i] > 14 or raw[i + 1] > 14 or raw[i + 2] > 14:
            lit += 1
    return round(lit / max(1, n), 4)


def save_checked(img, path, expect_w, expect_h):
    """Save, then confirm the file itself has the right size."""
    if img.width != expect_w or img.height != expect_h:
        return {"ok": False, "reason": "size-mismatch",
                "detail": "captured %dx%d, expected %dx%d"
                          % (img.width, img.height, expect_w, expect_h)}
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    img.convert("RGB").save(path, "PNG")
    with Image.open(path) as check:
        if check.width != expect_w or check.height != expect_h:
            return {"ok": False, "reason": "file-mismatch",
                    "detail": "file is %dx%d, expected %dx%d"
                              % (check.width, check.height, expect_w, expect_h)}
    return None


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def fail(reason, detail):
    emit({"ok": False, "reason": reason, "detail": detail})
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="screen",
                    choices=["screen", "window", "desktop", "list", "info"])
    ap.add_argument("--out", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--hwnd", type=int, default=0)
    args = ap.parse_args()

    become_aware()
    sx, sy, sw, sh = virtual_screen()

    if args.mode == "info":
        emit({"ok": True,
              "screen": {"w": sw, "h": sh, "x": sx, "y": sy},
              "desktopHost": int(desktop_host()),
              "monitorCount": 1})
        return 0

    if args.mode == "list":
        emit({"ok": True, "screen": {"w": sw, "h": sh},
              "windows": list_windows()})
        return 0

    if args.mode == "screen":
        if not args.out:
            return fail("missing-out", "screen capture needs --out")
        try:
            img = grab_screen(sx, sy, sw, sh)
            ink = ink_ratio(img)
            bad = save_checked(img, args.out, sw, sh)
            if bad:
                emit(bad)
                return 1
            emit({"ok": True, "kind": "screen", "path": args.out,
                  "width": sw, "height": sh,
                  "screen": {"w": sw, "h": sh}, "ink": ink,
                  "note": "the visible screen: windows covering the desktop "
                          "appear as the user sees them"})
            return 0
        except Exception as e:
            return fail("capture-failed", str(e))

    if args.mode == "desktop":
        if not args.out:
            return fail("missing-out", "desktop capture needs --out")
        host = desktop_host()
        if not host:
            return fail("no-desktop-window", "the shell window was not found")
        try:
            img, _ok = render_window(host, sw, sh)
            ink = ink_ratio(img)
            # A blank or partly painted desktop means the shell declined to
            # render; say so instead of shipping a black frame.
            if ink < 0.02:
                emit({"ok": False, "reason": "blank-capture",
                      "detail": "desktop rendered with only %.1f%% ink; the "
                                "shell may not support PrintWindow" % (ink * 100)})
                return 1
            bad = save_checked(img, args.out, sw, sh)
            if bad:
                emit(bad)
                return 1
            emit({"ok": True, "kind": "desktop", "path": args.out,
                  "width": sw, "height": sh, "screen": {"w": sw, "h": sh},
                  "ink": ink,
                  "note": "the desktop painted by itself: wallpaper and icons "
                          "are visible even where windows cover them"})
            return 0
        except Exception as e:
            return fail("capture-failed", str(e))

    # window
    if not args.out:
        return fail("missing-out", "window capture needs --out")
    all_windows = list_windows()
    target = None
    if args.hwnd:
        for w in all_windows:
            if w["hwnd"] == args.hwnd:
                target = w
                break
        if target is None and user32.IsWindow(wintypes.HWND(args.hwnd)):
            r = try_bound(args.hwnd)
            if r is not None:
                target = {
                    "hwnd": args.hwnd, "title": window_text(args.hwnd),
                    "className": class_name(args.hwnd),
                    "x": r.left, "y": r.top,
                    "w": r.right - r.left, "h": r.bottom - r.top,
                    "minimized": bool(user32.IsIconic(wintypes.HWND(args.hwnd))),
                    "foreground": False,
                }
    elif args.title:
        for w in all_windows:
            if w["title"] == args.title:
                target = w
                break
        if target is None:
            loose = [w for w in all_windows if args.title in w["title"]]
            if len(loose) == 1:
                target = loose[0]
            elif len(loose) > 1:
                names = " | ".join(w["title"] for w in loose)
                return fail("ambiguous-title",
                            "%d windows match; pass hwnd instead: %s"
                            % (len(loose), names))
    else:
        return fail("missing-target", "window capture needs --title or --hwnd")

    if target is None:
        return fail("window-not-found", "no matching window")
    if target["minimized"]:
        return fail("minimized",
                    "this window is minimized; call list and pick a visible one")
    if target["w"] <= 0 or target["h"] <= 0:
        return fail("empty-bounds",
                    "window reports %dx%d" % (target["w"], target["h"]))

    try:
        img, _ok = render_window(target["hwnd"], target["w"], target["h"])
        ink = ink_ratio(img)
        if ink < 0.01:
            emit({"ok": False, "reason": "blank-capture",
                  "detail": "window rendered with only %.1f%% ink; it may "
                            "refuse PrintWindow" % (ink * 100),
                  "hwnd": target["hwnd"], "title": target["title"]})
            return 1
        bad = save_checked(img, args.out, target["w"], target["h"])
        if bad:
            emit(bad)
            return 1
        emit({"ok": True, "kind": "window", "path": args.out,
              "width": target["w"], "height": target["h"],
              "screen": {"w": sw, "h": sh},
              "hwnd": target["hwnd"], "title": target["title"],
              "className": target["className"],
              "bounds": {"x": target["x"], "y": target["y"],
                         "w": target["w"], "h": target["h"]},
              "ink": ink,
              "note": "the window painted itself: full content even where "
                      "other windows cover it"})
        return 0
    except Exception as e:
        return fail("capture-failed", str(e))


if __name__ == "__main__":
    sys.exit(main())
