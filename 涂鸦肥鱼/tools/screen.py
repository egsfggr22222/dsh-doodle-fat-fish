# -*- coding: utf-8 -*-
"""Report the desktop in physical pixels, or capture it, in one place.

Python replacement for screen.ps1. Same command line, same stdout, so callers
do not need to know which one ran.

    python screen.py --mode size                 -> "<w>x<h>@<x>,<y>"
    python screen.py --mode capture --out a.png  -> "<w>x<h>" (verified on disk)

A DPI-unaware process is shown a scaled-down desktop: on a 125% display with a
1920x1080 panel it reports 1536x864, and every coordinate derived from that is
wrong by the scale factor. Declaring DPI awareness first makes the numbers true,
and it asks the OS rather than assuming a resolution.

Capture uses BitBlt rather than GDI+ CopyFromScreen: the latter is known to come
back one or two pixels short at the right and bottom edges, which makes a "full
screen" screenshot silently not match the screen it claims to show. Every
capture is verified against the reported size and retried once.

Everything here is stdlib + ctypes, so it runs on any of the interpreters
already inside this plugin folder.
"""

import argparse
import os
import sys
import time

import ctypes
from ctypes import wintypes

# --- DPI awareness -----------------------------------------------------------
#
# Per-monitor-v2 is the modern mode; fall back to the system-wide call on builds
# that do not know it. Either way the process must be aware before any size is
# read, otherwise the values come back scaled.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _declare_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# --- virtual screen ----------------------------------------------------------


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def virtual_screen():
    """(x, y, w, h) of the whole desktop, in physical pixels."""
    get = user32.GetSystemMetrics
    get.argtypes = [ctypes.c_int]
    get.restype = ctypes.c_int
    x = get(SM_XVIRTUALSCREEN)
    y = get(SM_YVIRTUALSCREEN)
    w = get(SM_CXVIRTUALSCREEN)
    h = get(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        # GetSystemMetrics answers 0 for the virtual screen on a session with no
        # display. Fall back to the primary monitor so callers get a number
        # rather than a zero-sized rectangle.
        w = get(0)   # SM_CXSCREEN
        h = get(1)   # SM_CYSCREEN
        x = 0
        y = 0
    return x, y, w, h


# --- BitBlt capture ----------------------------------------------------------

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab(x, y, w, h):
    """Copy a screen rectangle into packed RGB bytes (top-down rows).

    Returns (rgb_bytes, w, h) or raises. The pixel buffer is read with
    GetDIBits into a bottom-up DIB and then flipped, which avoids relying on any
    imaging library for the transfer itself.
    """
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
        bi.bmiHeader.biHeight = -h          # negative = top-down DIB
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32        # BGRA, always available
        bi.bmiHeader.biCompression = BI_RGB
        size = w * h * 4
        buf = ctypes.create_string_buffer(size)
        copied = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi),
                                 DIB_RGB_COLORS)
        if copied != h:
            raise OSError("GetDIBits copied %d of %d rows" % (copied, h))
        return buf.raw, w, h
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def save_png(path, bgra, w, h):
    """Write BGRA bytes to a PNG through Pillow when present, else PIL-free."""
    try:
        from PIL import Image
    except Exception:
        raise RuntimeError("Pillow is required to save PNG")
    img = Image.frombuffer("RGBA", (w, h), bgra, "raw", "BGRA", 0, 1)
    img = img.convert("RGB")
    img.save(path, "PNG")


def do_capture(out_path):
    x, y, w, h = virtual_screen()
    last = None
    for attempt in (1, 2):
        try:
            bgra, gw, gh = grab(x, y, w, h)
            if gw != w or gh != h:
                raise OSError("grab returned %dx%d, expected %dx%d" % (gw, gh, w, h))
            save_png(out_path, bgra, w, h)
            # Confirm the file on disk matches, not just the in-memory copy.
            from PIL import Image
            with Image.open(out_path) as check:
                cw, ch = check.size
            if cw != w or ch != h:
                raise OSError("saved %dx%d, expected %dx%d" % (cw, ch, w, h))
            return w, h
        except Exception as e:
            last = e
            if attempt == 1:
                time.sleep(0.15)
    raise RuntimeError("capture failed after retry: %s" % last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["size", "capture"], default="size")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    _declare_dpi_aware()

    if args.mode == "size":
        x, y, w, h = virtual_screen()
        sys.stdout.write("%dx%d@%d,%d\n" % (w, h, x, y))
        return 0

    if not args.out:
        sys.stderr.write("capture needs --out\n")
        return 2
    try:
        w, h = do_capture(args.out)
    except Exception as e:
        sys.stderr.write("%s\n" % e)
        return 3
    sys.stdout.write("%dx%d\n" % (w, h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
