# -*- coding: utf-8 -*-
"""Whale's Pencil -- the board, as a tkinter overlay.

Python replacement for pencil.ps1. Same command line, same state files, same
file protocols, so the Host side does not need to know which one ran:

    python board.py --scene s.json --users u.json --alive alive.txt \
                    --window window.txt --commands c.json \
                    --clear-signal clear.txt --labels labels.json \
                    --status status.json --stop-signal stop.txt --log board.log

Why tkinter and not the WPF original: the board used to be a PowerShell process,
i.e. it ran on an interpreter outside this plugin folder. Everything the plugin
executes must live inside its folder, and ocr_env/ already ships a Python with a
working _tkinter. Verified before writing this: -transparentcolor hides the
background while shapes stay visible, WS_EX_LAYERED/TOOLWINDOW/NOACTIVATE apply,
and SetWindowPos puts it on top.

Three hard-won rules, all discovered the expensive way:

  * DPI awareness must be declared BEFORE tk.Tk(). A DPI-unaware process is shown
    a 1536x864 desktop on a 1920x1080 panel at 125%, and a "full screen" window
    built from those numbers covers 80% of the screen.
  * The window must be -transparentcolor'd to a colour nothing else uses. The
    canvas is filled with exactly that colour, so the board is invisible until
    something is drawn. A near-black tint would make the whole screen unusable.
  * Click-through is done with SetWindowRgn, never with an extended style. The
    transparent layer is switched on once at startup and never toggled; only the
    window's hit region changes. Toggling WS_EX_TRANSPARENT instead would discard
    the layered surface on every mode switch, which repaints the whole window
    (a full-screen flash) and can drop the colour key (a black screen).
"""

import argparse
import json
import os
import re
import sys
import struct
import time
import tkinter as tk
from ctypes import wintypes

import ctypes
import subprocess
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
GDI32 = ctypes.WinDLL("gdi32", use_last_error=True)

# The region calls: CreateRectRgn/CombineRgn live in GDI32, GetWindowRgn and
# SetWindowRgn in USER32 -- the region is made by GDI but applied by the window
# manager. Handles are unsigned 32-bit values, so HRGN gets c_void_p, and a
# signed read of a high-bit handle (the clean_handle trap, see its comment)
# would point somewhere that is not a region at all.
GDI32.CreateRectRgn.restype = ctypes.c_void_p
GDI32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_int]
GDI32.CombineRgn.restype = ctypes.c_int
user32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
user32.GetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRgn.restype = ctypes.c_int

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
# The ring window's colour key. A key colour is how a plain Tk toplevel gets a
# transparent background: pixels of exactly this colour are not drawn. It must not
# appear in the ring itself, or that part of the circle vanishes -- the ring is
# drawn black or white, so a near-black blue key is safe.
RING_CHROMA = "#010203"
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
# Visibility without handing the geometry back to Tk. SW_SHOWNOACTIVATE shows a
# window without stealing focus; SW_HIDE hides it.
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
# Skips SetWindowPos' internal BitBlt of the old window contents. For a layered
# window whose pixels come entirely from UpdateLayeredWindow that copy is pure
# waste, and it runs on every move -- i.e. on every mouse event.
SWP_NOCOPYBITS = 0x0100
SWP_NOZORDER = 0x0004
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

# The exact colour that becomes invisible. Anything painted this colour vanishes,
# so it must be a colour no mark would ever use.
CHROMA = "#010203"

# Background alpha of the board surface, 0..255. Three is about 1%: the desktop
# shows through and the user cannot see the layer, but the pixels are not fully
# transparent, so Windows still routes the mouse here. Measured: at alpha 3 the
# window is clickable; a fully transparent (alpha 0) background would let clicks
# pass through instead.
BG_ALPHA = 3

# How often the state files are polled. 120ms is the WPF board's cadence: fast
# enough that a mark appears while the agent is still talking, slow enough that
# four stat calls per tick cost nothing.
POLL_MS = 120
# How long the heartbeat may be silent before the board concludes DSH is gone.
#
# WARNING: this was 6000 ms and it was too eager. The heartbeat is a Node
# `setInterval`, so anything that blocks the plugin's event loop stops it -- a
# synchronous session-log decompression in the summon path blocked it for about a
# second, and on a busy machine that is enough to cross a six-second line. The
# board then exited and took the whale with it, so a hitch in the Host showed up
# as a vanishing whale.
#
# This window is a backstop, not the primary defence: the plugin no longer blocks
# on those reads, and it restarts a board that dies unexpectedly. Fifteen seconds
# still notices a real DSH exit quickly while not mistaking a pause for one.
ALIVE_STALE_MS = 15000

MIN_WIDTH = 1
# 300, not 30. A 30-pixel ceiling cannot cover a region in one pass, which is what
# a marker is for; 300 can. The wheel steps one pixel at a time while the pen is
# thin and further per notch once it is thick, so the whole range stays reachable.
MAX_WIDTH = 300
# The user's ink: two colours, CapsLock flips between them. Black by default
# (readable on light wallpapers), white for dark ones. The user asked for this
# explicitly: the user draws in black or white, colour belongs to the agent.
#
# These are the two RGBA values the pen can produce; the colour each stroke was
# drawn with is fixed in the ink layer's pixels the moment it lands, so flipping
# the pen never touches work already on the board.
INK_BLACK = (0, 0, 0, 255)
INK_WHITE = (255, 255, 255, 255)
# 8, not 4. The ring's radius is floored at 4 px, so a 4-wide pen and an 8-wide pen
# drew the same 10 px cursor -- the first two wheel notches appeared to do nothing.
# Starting at 8 makes the initial ring 18 px, big enough to read as a circle and to
# show its size changing on the very first notch.
PEN_WIDTH = 8
# The eraser follows the pen: a fat pen needs a fat eraser, a hairline pen needs a
# fine one. ERASER_SCALE is how many pen-widths across the eraser is, and
# ERASER_MIN keeps it usable when the pen is at its thinnest.
ERASER_SCALE = 2.5
ERASER_MIN = 6

# --- the user's text boxes ------------------------------------------------------
#
# A text box is NOT ink. It is a real window with a real text control inside it,
# so the characters stay characters: selectable, copyable, and readable back out
# long after they were placed.
#
# WARNING: an earlier version placed a tk.Entry INSIDE the board window. The
# board's visible pixels come from UpdateLayeredWindow, which paints over
# everything Tk draws in that window -- the Entry existed, accepted keystrokes,
# and was invisible. A separate toplevel is a separate HWND, so Windows
# composites it and the board's bitmap cannot cover it.
TEXT_SIZE = 18
# A text box takes the pen's colour when it is created (text_hex()) and keeps it;
# flipping the pen later does not recolour boxes already on the board.
TEXT_BG = "#181820"             # the editor's backing while it is being typed into
# The other half of the pair: black text needs a light backing, white text a dark
# one. CapsLock flips both at once while the user is typing, so the box always
# reads -- white-on-white or black-on-black would be a blank rectangle.
TEXT_BG_LIGHT = "#f2f2f5"
TEXT_BORDER = "#8a8a96"         # neutral frame; the text itself carries the colour
# Once committed, the box disappears entirely: only the characters remain on
# screen. The container survives -- the window is still there, still selectable,
# still readable back out -- but it stops LOOKING like a container, because the
# user asked for text, not for a framed label sitting on their desktop.
TEXT_COMMIT_BG = "#010203"      # the board's chroma: fully transparent on screen
TEXT_COMMIT_BORDER = TEXT_COMMIT_BG
# Padding is asymmetric on purpose: a text box is a line of text in a border, so
# the horizontal room keeps the caret off the edge while the vertical room stays
# tight enough that a one-line box reads as one line, not as a small panel.
TEXT_PAD_X = 6
TEXT_PAD_Y = 2
TEXT_MIN_W = 26                 # an empty box is a thin strip, not a big frame
TEXT_MAX_W = 760                # past this it wraps instead of growing wider
TEXT_FONT = ("Microsoft YaHei UI", TEXT_SIZE)
# The drag hot zone: a ring of this thickness around a committed box catches the
# mouse. The ring is nearly invisible (alpha 0.03) but clickable, so hovering the
# edge shows the move cursor and dragging from there moves the box. The middle is
# carved out of the ring's hit region, so selecting the text underneath still
# works -- the ring is only the edges.
TEXT_HOT = 8
TEXT_GRIP_ALPHA = 0.03
TEXT_GRIP_BG = "#202028"        # near-invisible backing for the hot-zone ring

# --- the whale -----------------------------------------------------------------
#
# The whale (ball/deskpet.py) used to be started by pencil.ps1's Start-Ball. When
# the board moved to Python that code went with it, and the whale simply stopped
# appearing -- the plugin looked broken but nothing had "failed": nothing was
# starting it any more. It is started from here now, which is also simpler than
# before (Python launching Python, no PowerShell in between).
#
# deskpet.py is a single-instance program: it takes a byte-range lock on
# ball/whale.lock and exits quietly when another whale already holds it. The
# awkward case is a whale that takes the lock and then hangs inside WebView2 page
# load -- it never writes ball-status.json, so "is a whale alive?" cannot see it,
# yet it keeps the lock and blocks every later attempt. Clearing those before each
# spawn is what stops the board falling back to nothing at all.
#
# WARNING: these three constants are read by watch_whale/start_whale every tick.
# They were deleted once by accident while removing an unrelated block above
# them, and the board then raised NameError on every tick -- the whale vanished
# and nothing in the log said why. Keep them next to the code that uses them.

BALL_START_GRACE_S = 20.0     # a whale that has not reported by now is stuck
BALL_TRIES_MAX = 3
BALL_RETRY_COOLDOWN_S = 60.0


def become_aware():
    """Declare DPI awareness. MUST run before tk.Tk()."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen():
    g = user32.GetSystemMetrics
    g.argtypes = [ctypes.c_int]
    g.restype = ctypes.c_int
    x, y = g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN)
    w, h = g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        x = y = 0
        w, h = g(0), g(1)
    return x, y, w, h


# --- the "did I just black out the screen" guard ------------------------------
#
# A full-screen window that fails to be transparent is not a cosmetic bug: it
# covers the entire desktop in a solid colour and the user cannot see or click
# anything. That happened once while this board was being built, and it is not
# allowed to happen again.
#
# Before the board trusts itself on screen it takes one pixel from a corner it
# never draws on and compares it with the window's own background colour. If they
# match, the window is opaque and it destroys itself immediately.

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


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
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


# --- hardware cursor ------------------------------------------------------------
#
# A brush preview must be a CURSOR, not something painted into the board.
#
# WARNING: the first attempt drew the ring as a shape on the board's canvas and
# moved it every 120 ms. Every move edited the shape list, which repainted the
# whole 1920x1080 RGBA surface and pushed it through UpdateLayeredWindow -- four
# full-screen composites per pointer step. That is why it stuttered: the cost was
# the entire screen, for a circle.
#
# Every drawing program does the opposite. A brush outline is a system cursor:
# Windows owns it, moves it in the compositor, and the application does no drawing
# at all while the pointer travels. The app only rebuilds the bitmap when the
# brush SIZE or COLOUR changes -- a handful of times per session, not per frame.
#
# So the ring is built as an icon (CreateIconIndirect from two monochrome bitmaps)
# and handed to the window class with SetCursor. Moving it costs nothing.
#
# Size limit: a cursor is capped at 256x256 by Windows, so a pen wider than that
# falls back to the largest ring that fits. The stroke itself is still full width;
# only the preview stops growing.

CURSOR_MAX = 256
IDC_ARROW = 32512
GCLP_HCURSOR = -12


def build_ring_image(diameter, ring_rgb, fill_rgb, ring_width=2):
    """A PIL RGBA image of a ring (or a filled disc), ready for a layered window.

    The alpha channel is the whole point: the middle stays fully transparent so the
    desktop shows through, which a cursor bitmap cannot do at these sizes.
    """
    from PIL import Image, ImageDraw
    size = max(8, int(diameter))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = 1
    box = [inset, inset, size - 1 - inset, size - 1 - inset]
    if fill_rgb is not None:
        draw.ellipse(box, fill=(fill_rgb[0], fill_rgb[1], fill_rgb[2], 150))
    if ring_rgb is not None:
        draw.ellipse(box, outline=(ring_rgb[0], ring_rgb[1], ring_rgb[2], 255),
                     width=max(1, int(ring_width)))
    return img


def _declare_cursor_apis():
    """Give every cursor call a 64-bit signature.

    WARNING: this is the whole reason the ring never appeared, and it cost hours.
    ctypes defaults to a C `int` return and integer arguments, i.e. 32 bits. On
    64-bit Windows an HCURSOR is a 64-bit handle, so:

      * CreateIconIndirect returned a truncated handle -- measured: a NEGATIVE
        int (-1037694511). Every later call then used a value belonging to no
        cursor.
      * SetCursor received the same truncated value, failed, and returned the
        previous handle without raising.

    Both failures are silent, and a test that only checks "did the call raise"
    passes. Handles must be c_void_p on both sides.
    """
    try:
        user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
        user32.CreateIconIndirect.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.SetCursor.argtypes = [ctypes.c_void_p]
        user32.SetCursor.restype = ctypes.c_void_p
        user32.GetCursor.argtypes = []
        user32.GetCursor.restype = ctypes.c_void_p
        user32.DestroyCursor.argtypes = [ctypes.c_void_p]
        user32.DestroyCursor.restype = wintypes.BOOL
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                            ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                             ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
    except Exception:
        pass


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HANDLE),
        ("hbmColor", wintypes.HANDLE),
    ]


# Declared after ICONINFO exists: the argtypes reference the struct type.
_declare_cursor_apis()


def _make_cursor_bitmaps(diameter, ring_rgb, fill_rgb, ring_width=2):
    """Build the two monochrome bitmaps a cursor is made of.

    Returns (mask, colour) as HBITMAPs, or (None, None).

    A cursor is drawn with an AND mask and an XOR (colour) bitmap:
      * mask bit 1  -> the screen shows through (transparent)
      * mask bit 0  -> the colour bitmap decides (black stays black)
      * mask bit 0 and colour bit 1 -> the pixel is inverted, which is how the
        white halo is produced without knowing the wallpaper underneath.
    """
    size = max(8, min(int(diameter), CURSOR_MAX))
    centre = size / 2.0
    outer = centre - 1.0
    inner = max(0.0, outer - ring_width)

    # Pixel rows are written top-down here and flipped by the negative height in
    # BITMAPINFOHEADER, which is the convention CreateDIBSection expects.
    mask_rows = []
    colour_rows = []
    for py in range(size):
        mask_row = bytearray((size + 7) // 8)
        colour_row = bytearray((size + 7) // 8)
        for px in range(size):
            dx = px + 0.5 - centre
            dy = py + 0.5 - centre
            dist = (dx * dx + dy * dy) ** 0.5
            bit = 0x80 >> (px % 8)
            byte = px // 8
            if fill_rgb is not None and dist <= inner:
                # Filled disc (the Alt/eraser preview): opaque colour.
                mask_row[byte] &= ~bit
                if fill_rgb == (0, 0, 0):
                    colour_row[byte] &= ~bit     # black
                else:
                    colour_row[byte] |= bit      # white
            elif dist <= outer:
                # The ring itself: white on top of an inverted halo, so it reads
                # on any background.
                mask_row[byte] &= ~bit
                colour_row[byte] |= bit
            else:
                # Outside: transparent.
                mask_row[byte] |= bit
                colour_row[byte] &= ~bit
        mask_rows.append(bytes(mask_row))
        colour_rows.append(bytes(colour_row))

    def make_bitmap(rows):
        hdc = user32.GetDC(None)
        try:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size     # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 1
            bmi.bmiHeader.biCompression = 0    # BI_RGB
            bits = ctypes.c_void_p()
            bmp = GDI32.CreateDIBSection(
                hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp or not bits:
                return None
            stride = (size + 7) // 8
            for i, row in enumerate(rows):
                ctypes.memmove(bits.value + i * stride, row, stride)
            return bmp
        finally:
            user32.ReleaseDC(None, hdc)

    try:
        mask = make_bitmap(mask_rows)
        colour = make_bitmap(colour_rows)
        if not mask or not colour:
            return None, None
        return mask, colour
    except Exception:
        return None, None


def write_ring_cur(path, radius, white_pen, erasing):
    """Write a .cur file showing the pen as a ring, and return its Tk spec.

    THIS is the right way to give Tk a sized cursor, and it took a while to find:

      * Tk on Windows loads cursor files with `-cursor @<file.cur>` -- and the path
        MUST use forward slashes. `@C:\\Windows\\Cursors\\aero_arrow.cur` fails with
        "bad cursor spec", while `@C:/Windows/Cursors/aero_arrow.cur` loads. That
        one detail is why an earlier attempt concluded Tk could not do this at all.
      * Because Tk owns the cursor, there is no contest: no SetCursor in a poll, no
        WM_SETCURSOR hook, no flicker. Tk re-applies its own cursor on every
        WM_SETCURSOR and that is exactly what we want it to apply.

    A .cur is a tiny container: a 6-byte header, one 16-byte directory entry, then
    a BITMAPINFOHEADER whose height is DOUBLED (the XOR image plus the AND mask),
    the 32bpp BGRA pixels, and the 1bpp mask. Alpha in the XOR image is what makes
    the middle transparent.
    """
    size = max(8, min(int(round(radius * 2)) + 2, CURSOR_MAX))
    centre = size / 2.0
    # A 2px ring: thick enough to see at 8px, thin enough not to look like a blob
    # at 256px.
    ring_inner = max(0.0, centre - 2.0)
    ring_outer = centre - 0.5
    fill = getattr(write_ring_cur, "_never", None)  # placeholder, unused

    xor = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            dx, dy = x + 0.5 - centre, y + 0.5 - centre
            dist = (dx * dx + dy * dy) ** 0.5
            i = (y * size + x) * 4
            if erasing and dist <= ring_outer:
                # The eraser footprint: a solid disc, so the preview shows exactly
                # what a Shift-drag will remove.
                xor[i:i + 4] = b"\x00\x00\x00\xc0"
            elif ring_inner <= dist <= ring_outer:
                if white_pen:
                    xor[i:i + 4] = b"\xff\xff\xff\xff"   # BGRA white
                else:
                    xor[i:i + 4] = b"\x00\x00\x00\xff"   # BGRA black
            else:
                xor[i:i + 4] = b"\x00\x00\x00\x00"       # transparent

    and_row = ((size + 31) // 32) * 4
    and_mask = bytearray(and_row * size)
    for y in range(size):
        for x in range(size):
            dx, dy = x + 0.5 - centre, y + 0.5 - centre
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= ring_outer:
                continue                                  # opaque: bit stays 0
            and_mask[y * and_row + x // 8] |= 0x80 >> (x % 8)

    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    image = header + bytes(xor) + bytes(and_mask)
    hotspot = size // 2
    entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                        hotspot, hotspot, len(image), 22)
    try:
        with open(path, "wb") as f:
            f.write(struct.pack("<HHH", 0, 2, 1) + entry + image)
    except Exception:
        return ""
    # WARNING: forward slashes. Tk's cursor parser rejects backslashes.
    return "@" + path.replace("\\", "/")


def build_ring_cursor(radius, white_pen, erasing):
    """Create an HCURSOR showing the pen as a ring of the given radius.

    Returns an HCURSOR handle, or None if the cursor could not be built (the
    caller then falls back to an ordinary arrow rather than leaving the user with
    no pointer at all).

    Kept for completeness, but the board uses write_ring_cur() instead: handing the
    cursor to Tk avoids the whole set-and-be-overwritten problem.
    """
    diameter = int(round(radius * 2)) + 2
    if erasing:
        ring_rgb, fill_rgb = (0, 0, 0), (0, 0, 0)
    else:
        ring_rgb, fill_rgb = ((255, 255, 255), None) if white_pen else ((0, 0, 0), None)
    mask, colour = _make_cursor_bitmaps(diameter, ring_rgb, fill_rgb)
    if mask is None:
        return None
    try:
        info = ICONINFO()
        info.fIcon = False                    # a cursor, not an icon
        info.xHotspot = max(1, diameter // 2)  # the point the ring is centred on
        info.yHotspot = max(1, diameter // 2)
        info.hbmMask = mask
        info.hbmColor = colour
        cursor = user32.CreateIconIndirect(ctypes.byref(info))
        return cursor or None
    except Exception:
        return None
    finally:
        # The icon owns its bitmaps once created; the local handles can go.
        try:
            GDI32.DeleteObject(mask)
            GDI32.DeleteObject(colour)
        except Exception:
            pass


def set_cursor_handle(hwnd, cursor):
    """Apply an HCURSOR to a window, or restore the arrow when cursor is None."""
    try:
        if cursor:
            user32.SetClassLongPtrW(hwnd, -12, cursor)   # GCLP_HCURSOR
        else:
            arrow = user32.LoadCursorW(None, IDC_ARROW)
            user32.SetClassLongPtrW(hwnd, -12, arrow)
        return True
    except Exception:
        return False


def force_cursor(cursor):
    """Put a cursor under the pointer right now.

    WARNING: this is NOT redundant with SetClassLongPtrW. Tk handles
    WM_SETCURSOR itself and re-applies whatever the widget's `cursor` option says,
    which undid the class cursor every time the pointer crossed the window --
    measured: the handle set on the class never became the visible pointer, and
    because the widget was set to "none" the result was a smooth but INVISIBLE
    pointer. That is exactly what the user reported.

    SetCursor is the last word, but only until the next WM_SETCURSOR, so it is
    re-asserted on every poll. The call costs microseconds and allocates nothing,
    which is far cheaper than fighting Tk for the class cursor.
    """
    try:
        if cursor:
            # WARNING: argtypes/restype are mandatory here, not tidiness. Without
            # them ctypes passes the handle as a 32-bit int, the high half of a
            # 64-bit HCURSOR is truncated, and SetCursor is handed a value that
            # belongs to no cursor -- it then fails silently and the pointer keeps
            # whatever it had. That is why the ring never appeared while every
            # test that compared handles "passed": the calls were failing, not
            # being overridden.
            user32.SetCursor.argtypes = [ctypes.c_void_p]
            user32.SetCursor.restype = ctypes.c_void_p
            user32.SetCursor(cursor)
            return True
        arrow = user32.LoadCursorW(None, IDC_ARROW)
        if arrow:
            user32.SetCursor.argtypes = [ctypes.c_void_p]
            user32.SetCursor.restype = ctypes.c_void_p
            user32.SetCursor(arrow)
        return True
    except Exception:
        return False


def read_screen_pixel(x, y):
    """One pixel from the live desktop, as (r, g, b), or None."""
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return None
    hdc_mem = GDI32.CreateCompatibleDC(hdc_screen)
    bmp = GDI32.CreateCompatibleBitmap(hdc_screen, 1, 1)
    old = GDI32.SelectObject(hdc_mem, bmp)
    try:
        if not GDI32.BitBlt(hdc_mem, 0, 0, 1, 1, hdc_screen, x, y, SRCCOPY):
            return None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = 1
        bi.bmiHeader.biHeight = -1
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(4)
        if GDI32.GetDIBits(hdc_mem, bmp, 0, 1, buf, ctypes.byref(bi),
                           DIB_RGB_COLORS) != 1:
            return None
        b, g, r = buf.raw[0], buf.raw[1], buf.raw[2]
        return (r, g, b)
    except Exception:
        return None
    finally:
        GDI32.SelectObject(hdc_mem, old)
        GDI32.DeleteObject(bmp)
        GDI32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def chroma_rgb():
    """CHROMA as (r, g, b)."""
    h = CHROMA.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def get_ex_style(hwnd):
    """Read GWL_EXSTYLE as an UNSIGNED 32-bit value.

    WARNING: GetWindowLongW returns a signed c_long. On a style word with the
    high bit set (WS_EX_NOACTIVATE is 0x08000000, so plenty of combinations do)
    Python receives a negative number, and every later bit test is then applied
    to a sign-extended value -- 0xffffffffa9ed0e4a instead of 0xa9ed0e4a. All
    flags still "read" as set because of the padding, which is why a broken
    toggle still looked plausible. Mask with 0xFFFFFFFF and everything is exact.
    """
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    h = clean_handle(hwnd)
    return user32.GetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE) & 0xFFFFFFFF


def set_ex_style(hwnd, style):
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowLongW.restype = ctypes.c_long
    h = clean_handle(hwnd)
    return user32.SetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE,
                                 style & 0xFFFFFFFF)


def set_topmost(hwnd):
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    h = clean_handle(hwnd)
    user32.SetWindowPos(wintypes.HWND(h), wintypes.HWND(HWND_TOPMOST),
                        0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def clean_handle(hwnd):
    """Normalise an HWND to the value Win32 expects.

    WARNING: ctypes hands back handles as SIGNED ints when the high bit is set,
    so a real handle 0xb9b700de arrives as 0xffffffffb9b700de. Masking to 64
    bits does not help -- the garbage is already inside 64 bits -- and every
    call then addresses a window that does not exist, which surfaces as
    SetLayeredWindowAttributes returning FALSE with GetLastError() == 0 (no
    error at all, because nothing is technically wrong with the call).

    Windows handles are 32-bit values widened to pointer size, so take the low
    32 bits and widen it as an unsigned value.
    """
    return int(hwnd) & 0xFFFFFFFF


def top_level_of(hwnd):
    """Walk the parent chain to the real top-level window.

    WARNING: tkinter's winfo_id() returns a CHILD window. WS_EX_LAYERED set on a
    child does nothing the compositor acts on, so every transparency attempt
    aimed at it silently failed -- including LWA_ALPHA(0), which must make a
    window invisible. The handle that actually works is the root of the chain.
    """
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    cur = clean_handle(hwnd)
    for _ in range(8):
        parent = user32.GetParent(wintypes.HWND(cur))
        p = clean_handle(parent) if parent else 0
        if p == 0:
            return cur
        cur = p
    return cur


def _make_read_only(text):
    """Make a Tk Text widget read-only while keeping selection working.

    WARNING: state="disabled" is the obvious way and it is wrong here. A disabled
    Text ignores mouse drags, so the box could not be selected from at all -- the
    user reported exactly that ("I cannot Shift-select what you wrote"), and it was
    measured: the widget held the right characters, the window was interactive, and
    dragging selected nothing.

    state="normal" plus a key filter gives both properties: the characters can be
    selected and copied, and typing changes nothing. Delete/Backspace and any
    printable key are swallowed; the arrow keys, Home/End and Ctrl+C/Ctrl+A are
    left alone because they are what selecting and copying need.
    """
    try:
        text.configure(state="normal")

        def block(event):
            # Allow the navigation and copy chords; block everything that edits.
            if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                                "Prior", "Next", "Shift_L", "Shift_R",
                                "Control_L", "Control_R", "Caps_Lock"):
                return None
            if event.state & 0x4 and event.keysym.lower() in ("c", "a", "insert"):
                return None
            return "break"

        text.bind("<Key>", block)
        # A paste or a middle-click paste would also edit; drop those too.
        text.bind("<<Paste>>", lambda _e: "break")
        text.bind("<Button-2>", lambda _e: "break")
    except Exception:
        pass


def _bgr(hex_colour):
    """'#rrggbb' -> COLORREF (0x00bbggrr, which Win32 expects)."""
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_colorkey(hwnd, chroma):
    """Make every pixel of `chroma` fully transparent.

    Measured against the live desktop: with the correct handle this leaves a
    region diff of ~5 (the ring only) while "layered only" leaves ~33 (the whole
    window painted over the screen).

    Returns (ok, detail) where detail explains a failure -- a bare False was
    useless while chasing this because GetLastError stays 0 for a handle that
    does not name a layered window yet.
    """
    h = clean_handle(hwnd)
    try:
        user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
        user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        ok = user32.SetLayeredWindowAttributes(
            wintypes.HWND(h), wintypes.DWORD(_bgr(chroma)), 0, LWA_COLORKEY)
        if ok:
            return True, "ok"
        err = ctypes.get_last_error()
        is_win = user32.IsWindow(wintypes.HWND(h))
        style = get_ex_style(h)
        return False, ("SetLayeredWindowAttributes=0 err=%d hwnd=0x%08x "
                       "isWindow=%s exstyle=0x%08x layered=%s"
                       % (err, h, bool(is_win), style,
                          bool(style & WS_EX_LAYERED)))
    except Exception as e:
        return False, "exception %s" % e


class WINDOW_RECT(ctypes.Structure):
    """GetWindowRect's RECT."""
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


def _note_global(message):
    """Write a diagnostic line before a Board exists.

    push_rgba runs during startup, sometimes before the Board object is built, so
    it cannot use Board.note. This appends to the same trace file the rest of the
    board uses, which is what makes a failed present visible in board.log instead
    of looking like a silent refusal to run.
    """
    try:
        path = os.environ.get("DSH_BOARD_LOG", "")
        if not path:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
    except Exception:
        pass


def push_rgba_region(hwnd, x, y, w, h, ptr, full_w, full_h):
    """Present a window's pixels, per-pixel alpha, straight from a BGRA pointer.

    `ptr` must be the whole window's pixels, top-down BGRA, and must stay valid
    for the duration of the call. Nothing is copied on the Python side.

    WARNING: this is the per-frame hot path while the user draws. Measured on
    1920x1080, the PIL route (image.tobytes -> SetDIBits -> UpdateLayeredWindow)
    cost 9.43 ms per frame, of which about 7 ms was PIL copying 8 MB; handing
    numpy's address to SetDIBits costs 2.46 ms for the same result. That is the
    difference between a stroke that keeps up with the pointer and one that lags.

    UpdateLayeredWindow has no partial-update mode, so the whole surface goes to
    the compositor every time. The saving comes from not rebuilding the pixels,
    which is where the real cost was.
    """
    screen = user32.GetDC(None)
    if not screen:
        return False
    mem = GDI32.CreateCompatibleDC(screen)
    if not mem:
        user32.ReleaseDC(None, screen)
        return False
    bmp = GDI32.CreateCompatibleBitmap(screen, full_w, full_h)
    if not bmp:
        GDI32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        return False
    old = GDI32.SelectObject(mem, bmp)
    try:
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = full_w
        bi.bmiHeader.biHeight = -full_h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        GDI32.SetDIBits.argtypes = [wintypes.HDC, wintypes.HANDLE,
                                    wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT]
        GDI32.SetDIBits.restype = ctypes.c_int
        if GDI32.SetDIBits(mem, bmp, 0, full_h, ctypes.c_void_p(ptr),
                           ctypes.byref(bi), 0) != full_h:
            return False

        bf = BLENDFUNCTION()
        bf.BlendOp = 0
        bf.BlendFlags = 0
        bf.SourceConstantAlpha = 255
        bf.AlphaFormat = 1
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
            ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
            ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        pt_src = wintypes.POINT(0, 0)
        pt_dst = wintypes.POINT(x, y)
        size = wintypes.SIZE(full_w, full_h)
        return bool(user32.UpdateLayeredWindow(
            wintypes.HWND(clean_handle(hwnd)), screen,
            ctypes.byref(pt_dst), ctypes.byref(size), mem,
            ctypes.byref(pt_src), 0, ctypes.byref(bf), 0x00000002))
    finally:
        GDI32.SelectObject(mem, old)
        GDI32.DeleteObject(bmp)
        GDI32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def push_rgba(hwnd, x, y, w, h, rgba):
    """Present an RGBA buffer as the window's pixels, per-pixel alpha.

    WARNING: this is the only transparency mechanism that keeps the board
    clickable, and the difference is not subtle:

      * LWA_COLORKEY (colour key) makes the WHOLE window transparent to hit
        testing. Measured: a click anywhere went to the window underneath, even
        over a pixel painted solid red. Draw mode could therefore never receive
        the mouse.
      * LWA_ALPHA (whole-window alpha) is clickable, but it fades the strokes
        along with the background, so the ink is nearly invisible.
      * UpdateLayeredWindow with AC_SRC_ALPHA (per-pixel alpha) does both: the
        background pixels are transparent enough to show the desktop while the
        drawn pixels stay fully opaque and normal-coloured, AND the window still
        receives clicks. Measured: background read back as the exact desktop
        colour, the ring read back as pure (255,0,0), and WindowFromPoint
        returned the board at both points.

    The old WPF board got the same three properties from AllowsTransparency, which
    is per-pixel alpha underneath; this is its Win32 equivalent.
    """
    screen = user32.GetDC(None)
    if not screen:
        return False
    mem = GDI32.CreateCompatibleDC(screen)
    if not mem:
        user32.ReleaseDC(None, screen)
        return False
    bmp = GDI32.CreateCompatibleBitmap(screen, w, h)
    if not bmp:
        GDI32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        return False
    old = GDI32.SelectObject(mem, bmp)
    try:
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # top-down
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        src = ctypes.create_string_buffer(rgba, len(rgba))
        GDI32.SetDIBits.argtypes = [wintypes.HDC, wintypes.HANDLE,
                                    wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT]
        GDI32.SetDIBits.restype = ctypes.c_int
        rows = GDI32.SetDIBits(mem, bmp, 0, h, src, ctypes.byref(bi), 0)
        if rows != h:
            _note_global("push_rgba: SetDIBits rows=%s of %s" % (rows, h))
            return False

        bf = BLENDFUNCTION()
        bf.BlendOp = 0            # AC_SRC_OVER
        bf.BlendFlags = 0
        bf.SourceConstantAlpha = 255
        bf.AlphaFormat = 1        # AC_SRC_ALPHA
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
            ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
            ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        pt_src = wintypes.POINT(0, 0)
        pt_dst = wintypes.POINT(x, y)
        size = wintypes.SIZE(w, h)
        ok = user32.UpdateLayeredWindow(
            wintypes.HWND(clean_handle(hwnd)), screen,
            ctypes.byref(pt_dst), ctypes.byref(size), mem,
            ctypes.byref(pt_src), 0, ctypes.byref(bf), 0x00000002)
        if not ok:
            err = ctypes.get_last_error()
            # ERROR_INVALID_PARAMETER with a correct size usually means the
            # window is not in a state UpdateLayeredWindow accepts: it must be
            # layered AND must not have been painted normally. Showing it again
            # with SWP_SHOWWINDOW (no activation) puts it in that state.
            if err == 87:
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.SetWindowPos(wintypes.HWND(clean_handle(hwnd)),
                                    wintypes.HWND(-1), x, y, w, h,
                                    0x0040)  # SWP_SHOWWINDOW
                ok = user32.UpdateLayeredWindow(
                    wintypes.HWND(clean_handle(hwnd)), screen,
                    ctypes.byref(pt_dst), ctypes.byref(size), mem,
                    ctypes.byref(pt_src), 0, ctypes.byref(bf), 0x00000002)
                if ok:
                    _note_global("push_rgba: succeeded after re-show")
                else:
                    _note_global("push_rgba: retry failed err=%s"
                                 % ctypes.get_last_error())
        return bool(ok)
    finally:
        GDI32.SelectObject(mem, old)
        GDI32.DeleteObject(bmp)
        GDI32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)



class _Batch:
    """Context manager that turns many shape edits into one repaint."""

    def __init__(self, canvas):
        self._canvas = canvas
        self._was = False

    def __enter__(self):
        self._was = self._canvas.defer
        self._canvas.defer = True
        return self._canvas

    def __exit__(self, exc_type, exc, tb):
        self._canvas.defer = self._was
        if not exc_type:
            self._canvas._board.repaint()
        return False


class PilCanvas:
    """A canvas-shaped facade over a PIL surface.

    The board's drawing code was written against Tk's canvas API, and that API is
    the right shape for the job: create a shape, keep its id, delete it later.
    What Tk cannot do is give a window per-pixel alpha, and without that the board
    is either opaque (covering the screen) or click-through (unusable for
    drawing). So the shapes live in a PIL image and this class keeps the same
    method names, which means every existing draw call works unchanged.

    WARNING: this is not a general Tk canvas. It implements exactly the calls the
    board makes: create_rectangle, create_oval, create_line, create_text, delete,
    coords, find_all. Anything else raises AttributeError on purpose -- a silent
    no-op would make a future drawing call disappear without a trace.
    """

    def __init__(self, board):
        self._board = board
        self._next_id = 1
        self._shapes = {}       # id -> (kind, args, kwargs)
        # Shape edits are collected and painted once, instead of repainting the
        # whole board per call. See repaint().
        self.defer = False

    # -- creation ---------------------------------------------------------

    def create_rectangle(self, *args, **kwargs):
        return self._add("rect", args, kwargs)

    def create_oval(self, *args, **kwargs):
        return self._add("oval", args, kwargs)

    def create_line(self, *args, **kwargs):
        return self._add("line", args, kwargs)

    def create_text(self, *args, **kwargs):
        return self._add("text", args, kwargs)

    def _add(self, kind, args, kwargs):
        i = self._next_id
        self._next_id += 1
        self._shapes[i] = (kind, args, dict(kwargs))
        if not self.defer:
            self._board.repaint()
        return i

    # -- mutation ---------------------------------------------------------

    def coords(self, item, *args):
        entry = self._shapes.get(item)
        if entry is None:
            return
        kind, _old, kwargs = entry
        self._shapes[item] = (kind, args, kwargs)
        if not self.defer:
            self._board.repaint()

    def delete(self, item):
        if item in self._shapes:
            del self._shapes[item]
            if not self.defer:
                self._board.repaint()

    def itemconfigure(self, item, **kwargs):
        """Change one live shape's options in place.

        WARNING: this did not exist, and its absence was silent. Tk's Canvas has
        itemconfigure and this stand-in did not, so every call that tried to
        recolour a shape raised AttributeError inside a bare `except: pass` -- the
        pen ring stayed whatever colour it was created with, no matter what
        CapsLock or Shift said. Adding the method is what makes the ring follow
        the pen.
        """
        entry = self._shapes.get(item)
        if entry is None:
            return
        kind, args, options = entry
        merged = dict(options)
        merged.update(kwargs)
        self._shapes[item] = (kind, args, merged)
        if not self.defer:
            self._board.repaint()

    def itemcget(self, item, option):
        """Read one option back, so callers (and tests) can see the real value."""
        entry = self._shapes.get(item)
        if entry is None:
            return ""
        return entry[2].get(option, "")

    def batch(self):
        """Collect shape edits and repaint once at the end.

        WARNING: without this, rebuilding the ink layer repainted the whole screen
        once per shape. Drawing N strokes meant N full-screen composites, and the
        user sees that as the board flashing/reloading while they draw.
        """
        return _Batch(self)

    def find_all(self):
        return list(self._shapes.keys())

    def clear_shapes(self):
        """Forget every shape without repainting.

        Only the agent's marks live here now; the user's ink is pixels. Dropping
        the list is enough because the caller repaints once afterwards.
        """
        self._shapes = {}

    def configure(self, **_kwargs):
        """Accepted and ignored: there is no real widget to configure."""
        return None

    # -- painting ---------------------------------------------------------

    def paint(self, image, draw):
        """Draw every live shape into the given PIL image."""
        from PIL import ImageColor

        def colour(value, default=None):
            """Parse a CSS colour into something PIL can paint.

            WARNING: ImageColor.getrgb rejects `rgba(...)` outright -- it raises
            ValueError, and every caller here fell back to its default. The agent
            sends rgba for translucent fills, so those shapes were painted in the
            fallback colour no matter what was asked for. Measured: a fill of
            "rgba(0,229,255,0.10)" came out (0,0,0), i.e. black.

            The alpha is flattened onto black here, which is what the board's own
            background is at 1% opacity: a translucent fill should read as a dim
            version of its colour, not as an opaque one.
            """
            if not value or value in ("", "none"):
                return default
            text = str(value).strip()
            m = re.match(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
                         r"\s*(?:,\s*([\d.]+)\s*)?\)$", text, re.I)
            if m:
                r, g, b = (max(0, min(255, int(round(float(m.group(i))))))
                           for i in (1, 2, 3))
                a = 1.0 if m.group(4) is None else max(0.0, min(1.0, float(m.group(4))))
                if a >= 1.0:
                    return (r, g, b)
                return (int(r * a), int(g * a), int(b * a))
            try:
                return ImageColor.getrgb(text)
            except Exception:
                return default

        for kind, args, kwargs in self._shapes.values():
            try:
                if kind == "rect":
                    fill = colour(kwargs.get("fill"))
                    outline = colour(kwargs.get("outline"))
                    draw.rectangle(args[:4], fill=fill,
                                   outline=outline,
                                   width=int(kwargs.get("width") or 1))
                elif kind == "oval":
                    fill = colour(kwargs.get("fill"))
                    outline = colour(kwargs.get("outline"))
                    draw.ellipse(args[:4], fill=fill, outline=outline,
                                 width=int(kwargs.get("width") or 1))
                elif kind == "line":
                    pts = list(args)
                    if len(pts) >= 4:
                        coords = [(pts[i], pts[i + 1])
                                  for i in range(0, len(pts) - 1, 2)]
                        draw.line(coords,
                                  fill=colour(kwargs.get("fill"),
                                              (255, 255, 255)),
                                  width=int(kwargs.get("width") or 1),
                                  joint="curve")
                elif kind == "text":
                    text = kwargs.get("text") or (args[2] if len(args) > 2 else "")
                    if not text:
                        continue
                    xy = (args[0], args[1])
                    anchor = kwargs.get("anchor") or "la"
                    pil_anchor = {"nw": "la", "n": "ma", "ne": "ra",
                                  "w": "lm", "center": "mm",
                                  "e": "rm", "sw": "ld", "s": "md",
                                  "se": "rd"}.get(anchor, "la")
                    size = 16
                    font_spec = kwargs.get("font")
                    if isinstance(font_spec, (tuple, list)) and len(font_spec) >= 2:
                        try:
                            size = abs(int(font_spec[1]))
                        except Exception:
                            size = 16
                    try:
                        from PIL import ImageFont
                        font = ImageFont.truetype("msyh.ttc", max(6, size))
                    except Exception:
                        font = None
                    draw.text(xy, str(text),
                              fill=colour(kwargs.get("fill"),
                                          (255, 255, 255)),
                              anchor=pil_anchor, font=font)
            except Exception:
                # One bad shape must not stop the rest of the frame.
                continue


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


def read_json(path):
    raw = read_text(path)
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


# --- whale process helpers -----------------------------------------------------


def _process_alive(pid):
    """True only when the process exists AND has not exited.

    WARNING: OpenProcess succeeding does not mean the process is alive. On
    Windows a handle can still be opened for a process that has terminated, so a
    plain OpenProcess check reported a long-dead whale (pid 25516) as alive --
    the board then believed a whale was running, never started one, and the
    whale was simply missing. GetExitCodeProcess is what distinguishes the two:
    STILL_ACTIVE (259) means running, anything else means it has exited.
    """
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED = 0x1000
    STILL_ACTIVE = 259
    h = None
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    except Exception:
        return False
    finally:
        if h:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass


def _kill_pid(pid):
    try:
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        PROCESS_TERMINATE = 0x0001
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        kernel32.TerminateProcess(h, 1)
        kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _all_python_pids():
    """Every python.exe / pythonw.exe pid, without starting any process."""
    pids = []
    try:
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                      wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap and int(snap) != -1:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() in ("python.exe",
                                                       "pythonw.exe"):
                    pids.append(int(entry.th32ProcessID))
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
            kernel32.CloseHandle(snap)
    except Exception:
        pass
    return pids


def find_whale_pids():
    """Every live deskpet.py process.

    The lock file cannot be used to find them: deskpet.py locks it with msvcrt,
    a kernel byte-range lock, and the file cannot be opened at all while that is
    held -- not even for reading. Matching the command line works and does not
    depend on the whale having written anything.

    WARNING: do NOT shell out to wmic or tasklist for this. Both are console
    programs, and running one flashes a black console window titled
    "C:\WINDOWS\System32\Wbem\wmic.exe" on screen -- which is exactly the
    "command line window" the user saw at every plugin start. Everything here
    goes through ctypes, so no process is ever spawned to answer the question.
    """
    pids = []
    try:
        # Toolhelp32 gives the process list; QueryFullProcessImageNameW and the
        # PEB command line are not needed -- the process name plus the module
        # name is enough to recognise deskpet.py, and the name check below is
        # backed by the state-file pid when it is available.
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                      wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        INVALID = ctypes.c_void_p(-1).value
        if snap and int(snap) != int(INVALID):
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = (entry.szExeFile or "").lower()
                if name in ("python.exe", "pythonw.exe"):
                    cmd = _command_line_of(entry.th32ProcessID)
                    if "deskpet.py" in cmd:
                        pids.append(int(entry.th32ProcessID))
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
            kernel32.CloseHandle(snap)
    except Exception:
        pass
    return pids


def _command_line_of(pid):
    """A process's command line, read from its PEB. '' when unavailable.

    Reading the PEB needs PROCESS_VM_READ and works for same-bitness processes of
    the same user, which covers everything this plugin starts. Failures return ''
    rather than raising: an unreadable process is simply not a whale candidate.
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    h = None
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT),
                        ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [("Reserved1", ctypes.c_void_p),
                        ("PebBaseAddress", ctypes.c_void_p),
                        ("Reserved2", ctypes.c_void_p * 2),
                        ("UniqueProcessId", ctypes.c_void_p),
                        ("Reserved3", ctypes.c_void_p)]

        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                 False, int(pid))
        if not h:
            return ""
        pbi = PROCESS_BASIC_INFORMATION()
        ret = wintypes.ULONG()
        if ntdll.NtQueryInformationProcess(
                h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi),
                ctypes.byref(ret)) != 0:
            return ""
        if not pbi.PebBaseAddress:
            return ""

        kernel32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf,
                                              size, ctypes.byref(got)):
                return None
            return buf.raw[:got.value]

        # PEB -> ProcessParameters (offset 0x20 on 64-bit, 0x10 on 32-bit).
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        params_off = 0x20 if ptr_size == 8 else 0x10
        raw = read(pbi.PebBaseAddress + params_off, ptr_size)
        if not raw:
            return ""
        params = int.from_bytes(raw, "little")
        if not params:
            return ""
        # RTL_USER_PROCESS_PARAMETERS -> CommandLine UNICODE_STRING (0x70 / 0x40).
        cmd_off = 0x70 if ptr_size == 8 else 0x40
        raw = read(params + cmd_off, ctypes.sizeof(UNICODE_STRING))
        if not raw:
            return ""
        us = UNICODE_STRING.from_buffer_copy(raw)
        if not us.Buffer or us.Length <= 0 or us.Length > 32767:
            return ""
        raw = read(us.Buffer, us.Length)
        if not raw:
            return ""
        return raw.decode("utf-16-le", "replace")
    except Exception:
        return ""
    finally:
        if h:
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass


def whale_status(state_dir):
    """The whale's self-report: dict with at least status/pid, or None."""
    path = os.path.join(state_dir, "ball-status.json")
    doc = read_json(path)
    return doc if isinstance(doc, dict) else None


def whale_pid(state_dir):
    doc = whale_status(state_dir) or {}
    try:
        return int(doc.get("pid") or 0)
    except Exception:
        return 0


def whale_status_name(state_dir):
    doc = whale_status(state_dir) or {}
    return str(doc.get("status") or "")


def _digest(text):
    """A short stable hash of a scene document.

    The Host rewrites scene.json on every save, so mtime changes even when the
    marks did not. Hashing the text tells the two cases apart and avoids a full
    repaint (and its flicker) for a no-op write.
    """
    import hashlib
    return hashlib.blake2b(text.encode("utf-8", "replace"),
                           digest_size=12).hexdigest()


# Only the fields that actually change what is painted. Keys such as "id" are
# ignored on purpose: the id is the tracking key, not part of the appearance, so
# renaming a layer must not force a redraw.
_APPEARANCE_KEYS = (
    "type", "x", "y", "w", "h", "x1", "y1", "x2", "y2", "cx", "cy", "r",
    "rx", "ry", "radius", "points", "stroke", "fill", "width", "dash", "text",
    "size", "align", "weight", "head", "alpha", "src", "data",
)


def _fingerprint(item):
    """A comparable signature of one layer's painted appearance.

    Two renders with the same fingerprint must produce identical pixels, which is
    what lets render_scene skip them entirely instead of deleting and recreating
    every mark on the board.
    """
    import hashlib
    parts = []
    for key in _APPEARANCE_KEYS:
        if key in item:
            parts.append("%s=%r" % (key, item[key]))
    return hashlib.blake2b("|".join(parts).encode("utf-8", "replace"),
                           digest_size=10).hexdigest()



# --- low-level mouse hook -------------------------------------------------------
#
# WARNING: this exists because polling cannot be made smooth. The board's main loop
# is a Tk `after(120)` tick, so the pointer was SAMPLED 8 times a second -- the
# preview visibly stepped from one position to the next. Two things were measured
# before choosing this design:
#
#   * the whole tick costs 0.207 ms, so the work was never the problem: 8.3 Hz is
#     simply how often it looked;
#   * Tk's after(16) actually fires at ~21.8 ms with 16..31 ms jitter, because
#     Windows' default timer resolution is 15.6 ms. Asking for 60 Hz there does not
#     give 60 Hz, it gives 46 Hz and a wobble.
#
# WH_MOUSE_LL is event-driven instead of sampled: the OS calls back on every real
# pointer move. The hook lives in its own thread with its own message pump, and the
# callback does exactly one thing -- a SetWindowPos on an already-composited layered
# window (0.27 ms measured). Nothing Tk, nothing that can block: a slow low-level
# hook gets silently unhooked by Windows, which would look like "the preview froze".
#
# WARNING: the callback must never raise. An exception inside a ctypes callback is
# printed and the return value is garbage, which corrupts the hook chain.

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
VK_CAPITAL = 0x14
LLKEYBPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]
WM_MOUSEMOVE = 0x0200
LLMOUSEPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                 wintypes.WPARAM, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


def _declare_hook_apis():
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LLMOUSEPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t


class CapsLockDoubleTap:
    """Calls `on_double` when CapsLock is pressed twice quickly.

    WARNING: the pen colour used to follow the CapsLock LOCK state, read with
    GetAsyncKeyState. That cannot work as a toggle: the lock bit is only set while
    the key is latched, so the colour flipped to white on the press and back to
    black as soon as the state was re-read -- "I have to HOLD CapsLock to keep it
    white" is exactly that.

    A double tap is an EVENT, not a state, so it needs a hook. This one watches
    only VK_CAPITAL and reports two presses within DOUBLE_TAP_MS. It never
    swallows the key: CapsLock keeps doing its normal job for the rest of Windows.
    """

    DOUBLE_TAP_MS = 420

    def __init__(self, on_double):
        self._on_double = on_double
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._hook = None
        self._proc = None
        self._last = 0.0
        self.taps = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="caps-hook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self._hook is not None

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    def _run(self):
        def callback(n_code, w_param, l_param):
            # Never raise here: an exception in a ctypes callback corrupts the
            # hook chain and the return value becomes garbage.
            try:
                if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    info = ctypes.cast(l_param,
                                       ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if info.vkCode == VK_CAPITAL:
                        now = time.time()
                        self.taps += 1
                        if now - self._last <= self.DOUBLE_TAP_MS / 1000.0:
                            self._last = 0.0
                            self._on_double()
                        else:
                            self._last = now
            except Exception:
                pass
            try:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            except Exception:
                return 0

        self._proc = LLKEYBPROC(callback)
        try:
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                                  None, 0)
        except Exception:
            self._hook = None
        self._ready.set()
        if not self._hook:
            return
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.002)
        finally:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None


class MouseFollower:
    """Drives a callback from real pointer motion, on its own thread.

    `on_move(x, y)` is called for every WM_MOUSEMOVE with screen coordinates. It
    must return fast and must not touch Tk: it runs on the hook thread, and a slow
    low-level hook is unhooked by Windows.
    """

    def __init__(self, on_move):
        self._on_move = on_move
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._hook = None
        self._proc = None
        self.moves = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="mouse-hook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self._hook is not None

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    def _run(self):
        try:
            _declare_hook_apis()
        except Exception:
            self._ready.set()
            return

        def callback(n_code, w_param, l_param):
            # WARNING: never raise from here. An exception in a ctypes callback
            # corrupts the hook chain and the return value becomes garbage.
            try:
                if n_code >= 0 and w_param == WM_MOUSEMOVE:
                    info = ctypes.cast(l_param,
                                       ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    self.moves += 1
                    self._on_move(info.pt.x, info.pt.y)
            except Exception:
                pass
            try:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            except Exception:
                return 0

        self._proc = LLMOUSEPROC(callback)
        try:
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        except Exception:
            self._hook = None
        self._ready.set()
        if not self._hook:
            return
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                # PeekMessage + a short sleep rather than a blocking GetMessage:
                # the thread must be able to notice _stop without a posted message.
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.002)
        finally:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None


class Board:
    def __init__(self, args, plugin_dir=None, python_exe=None):
        self.args = args
        self.log_path = args.log or ""
        self.tick_count = 0
        self.mode = "idle"                  # idle | ink
        self.hidden = []                    # scene ids the user erased
        self.last_scene_mtime = None
        self.last_scene_digest = None
        self.last_command_rev = -1
        self.last_clear_tick = -1
        self.hwnd = 0
        self.through = None                 # current hit-region state
        self.scene_items = {}               # id -> {ids, fingerprint}
        self.labels = self._load_labels()
        self.before_pixel = None            # desktop corner before the window
        self.aborted = False
        self.screen = (0, 0, 0, 0)
        # The whale is the board's only door: with no whale there is no way to
        # switch the pencil on. It is started from here and watched here.
        self.ball_proc = None
        self.ball_tries = 0
        self.ball_spawn_pid = 0
        self.ball_spawn_at = 0.0
        self.ball_last_check = 0.0
        self.ball_give_up_at = 0.0
        # The user's own ink. Strokes are the source of truth: they are what
        # user.json holds and what the ink layer is redrawn from.
        self.strokes = []
        self.ink_items = []
        self.active_points = []
        self.active_item = None
        self.pen_width = PEN_WIDTH
        self.stroke_seq = 0
        self.user_rev = 0
        # Text the user typed on the board. A text is NOT ink: it lives in its own
        # window, so the characters stay characters -- selectable, copyable, and
        # readable back out long after they were placed.
        self.texts = []
        self.text_seq = 0
        self.text_editor = None             # the box being typed into, if any
        self.text_wins = {}                 # id -> the committed box's window
        self.menu = None
        self.menu_at = (0.0, 0.0)
        # Eraser state: set on Alt+left press, cleared on release. While it is
        # set, mouse motion paints background instead of ink.
        self.erasing = False
        # Set while an Alt-drag is selecting text; see on_text_select.
        self._alt_select = None
        self.erase_last = None
        # The user's ink colour: False = black (default), True = white. CapsLock
        # in draw mode flips it.
        self.ink_white = False
        # A text box being dragged: set on press over a box, cleared on release.
        # While it is set, mouse motion moves the box instead of drawing.
        self.text_drag = None
        # Drag grips: id -> the ring window around each committed box.
        self.grips = {}
        # Pen-width tooltip: its own small always-on-top window beside the pointer.
        self.badge_win = None
        self.badge_label = None
        self.badge_job = None
        # NOTE: the island is NOT here. It is a separate program (tools/island.py)
        # because it has nothing to do with drawing: the board is a full-screen
        # click-through overlay, and a readout with a button needs a window that
        # can receive a click. Keeping them apart means a fault in either cannot
        # take the other down.
        # Everything is painted into this RGBA surface, then handed to the
        # window as-is. Tk draws nothing: its canvas exists only to receive
        # mouse events, because a Tk widget cannot have per-pixel alpha.
        self.surface = None
        self.draw = None
        # The plugin folder is the parent of tools/. The state directory is the
        # parent of the file the Host passed as --window (that is the board's
        # state folder), which is also where the whale keeps its ball/ subdir.
        self.plugin_dir = plugin_dir or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.python_exe = python_exe or sys.executable
        self.board_state_dir = os.path.dirname(
            os.path.abspath(args.window)) if args.window else self.plugin_dir

    # -- logging -------------------------------------------------------------

    def note(self, message):
        """Append a timestamped line to the trace, if one was given.

        The board used to be completely silent when -Log was missing, which made
        a healthy board look hung. Keep the same convention (no --log, no
        tracing) but never raise: tracing is optional.
        """
        if not self.log_path:
            return
        try:
            line = "%s %s\n" % (time.strftime("%H:%M:%S"), message)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _load_labels(self):
        """Localised text arrives as JSON, never as command-line arguments.

        A command line is the wrong channel for non-ASCII text on Windows, and
        the plugin folder itself has a Chinese name.
        """
        defaults = {
            "textBox": "文本框",
            "call": "呼唤 agent",
            "clear": "一键清除",
            "exitDraw": "退出作画",
            "editText": "编辑文字",
            "deleteText": "删除",
        }
        doc = read_json(self.args.labels) if self.args.labels else None
        if isinstance(doc, dict):
            for k, v in doc.items():
                if isinstance(v, str):
                    defaults[k] = v
        return defaults

    # -- window --------------------------------------------------------------

    def build(self):
        x, y, w, h = virtual_screen()
        self.screen = (x, y, w, h)

        # Sample the desktop BEFORE the window exists, from a corner the board
        # never draws on. Compared after the window is up: if that corner now
        # reads as our background colour, the window is opaque and is covering
        # the whole screen.
        self.before_pixel = read_screen_pixel(x + 3, y + 3)

        # WARNING: tk.Tk() maps the window immediately, so it is visible for the
        # few frames it takes to apply the layered style and the colour key --
        # a small black rectangle flashing on screen at every start. The window
        # is therefore created fully transparent (-alpha 0) and only made opaque
        # AFTER the colour key is in place.
        #
        # WARNING: withdraw() is the wrong tool here. A withdrawn window has no
        # usable handle for the Win32 calls below, and deiconify() re-maps it,
        # which brings the flash straight back. -alpha 0 keeps a real, mapped
        # window that simply cannot be seen yet.
        root = tk.Tk()
        # WARNING: do NOT call root.attributes("-alpha", ...) anywhere.
        #
        # Tk implements -alpha with SetLayeredWindowAttributes, and Windows will
        # not accept UpdateLayeredWindow (error 87, invalid parameter) once that
        # has been called on the window: the two layered modes are mutually
        # exclusive per window. Measured: with -alpha 0.0 at creation, every
        # present() failed with 87; without it the same code succeeded.
        root.overrideredirect(True)         # no title bar, no border
        root.configure(bg=CHROMA)

        # The Tk canvas receives mouse events but is never drawn to: the pixels
        # come from the RGBA surface below, because a Tk widget cannot have
        # per-pixel alpha and without that the board cannot be both transparent
        # and clickable.
        hit_layer = tk.Canvas(root, width=w, height=h, bg=CHROMA,
                              highlightthickness=0, bd=0)
        hit_layer.pack(fill="both", expand=True)
        root.update_idletasks()
        root.update()

        import numpy as np
        from PIL import Image, ImageDraw
        # The board's pixels live in ONE persistent buffer for the whole session.
        #
        # WARNING: this is a drawing surface, not a scene graph. A stroke is
        # painted into these pixels and stays there; nothing records its shape and
        # nothing replays it. Erasing clears the pixels under the pointer back to
        # background. That is how a paint program works, and it is what makes
        # drawing cheap: a segment costs one short line into the buffer instead of
        # a rebuild of every mark on the board.
        #
        # It is a numpy array rather than a PIL image because its address goes
        # straight to SetDIBits. Measured on 1920x1080: the PIL path (tobytes ->
        # SetDIBits -> UpdateLayeredWindow) cost 9.43 ms per frame, of which ~7 ms
        # was PIL copying 8 MB; the numpy pointer path costs 2.46 ms.
        self.pixels = np.zeros((h, w, 4), dtype=np.uint8)
        self.pixels[:, :, 3] = BG_ALPHA
        # WARNING: PIL does not write through to this array once it draws, so
        # sync() copies the image back after every drawing operation. Presenting
        # the image directly would mean a full tobytes copy every frame, which is
        # the cost this change removes.
        self.surface = Image.fromarray(self.pixels, "RGBA")
        self.draw = ImageDraw.Draw(self.surface)
        # The user's ink lives in its OWN layer, exactly like a paint program's
        # ink layer. Every stroke is painted into this image once, in the colour
        # the pen had at that moment, and it is never recomputed -- so a colour
        # change (CapsLock) affects only what is drawn NEXT, and a repaint pastes
        # this layer back verbatim. That is why no per-stroke colour record is
        # needed: the pixels ARE the record.
        self.ink_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self.ink_draw = ImageDraw.Draw(self.ink_layer)
        canvas = PilCanvas(self)

        self.root = root
        self.canvas = canvas
        self.hit_layer = hit_layer
        # Bind the mouse before the window is shown: entering draw mode must be
        # usable immediately, and a board that cannot draw is not a board.
        self.bind_mouse()

        # WARNING: the handle matters more than the style. Tk's winfo_id() is a
        # CHILD window -- GetParent on it returns the real top-level -- and
        # WS_EX_LAYERED applied to a child changes nothing the compositor acts
        # on. Every earlier transparency attempt failed for exactly this reason:
        # even LWA_ALPHA(0), which must make a window invisible, had no visible
        # effect because it was aimed at the wrong handle. Walk up to the root.
        inner = int(root.winfo_id())
        self.hwnd = top_level_of(inner)
        self.note("hwnd inner=0x%08x top=0x%08x" % (inner, self.hwnd))

        # WARNING: the window is positioned but NOT shown yet.
        #
        # A layered window that is shown before UpdateLayeredWindow has content
        # displays whatever Tk painted -- solid black across the whole screen,
        # which is what made the transparency check read (0,0,0) at the corner
        # while other parts of the screen were fine. Showing it only after the
        # first present() means the very first pixels the user can see are the
        # correct ones.
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos(wintypes.HWND(self.hwnd), wintypes.HWND(0),
                            x, y, w, h,
                            SWP_NOACTIVATE | SWP_NOZORDER)
        root.update()
        got = WINDOW_RECT()
        user32.GetWindowRect(wintypes.HWND(self.hwnd), ctypes.byref(got))
        self.note("geometry %dx%d at %d,%d (want %dx%d at %d,%d)"
                  % (got.right - got.left, got.bottom - got.top,
                     got.left, got.top, w, h, x, y))

        # A LAYERED window with per-pixel alpha, presented by UpdateLayeredWindow.
        #
        # WARNING: the transparency mechanism is what decides whether draw mode
        # can work at all. Measured on this machine:
        #   colour key  -> background invisible, but the WHOLE window is
        #                  click-through (a click over a solid red pixel still
        #                  reached the window underneath), so the board never got
        #                  the mouse;
        #   LWA_ALPHA   -> clickable, but it fades the strokes along with the
        #                  background, so the ink is nearly invisible;
        #   per-pixel   -> background shows the desktop, strokes stay fully
        #                  opaque and normal-coloured, AND clicks land on the
        #                  board. This is what the old WPF board got from
        #                  AllowsTransparency.
        style = get_ex_style(self.hwnd)
        set_ex_style(self.hwnd,
                     style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        root.update()
        self.note("exstyle 0x%08x -> 0x%08x (layered=%s)"
                  % (style, get_ex_style(self.hwnd),
                     bool(get_ex_style(self.hwnd) & WS_EX_LAYERED)))

        # The Tk canvas is not drawn to at all; the pixels come from an RGBA
        # buffer built with PIL. Tk only provides the window and the event loop.
        self.tk_canvas_off()
        transparent_ok = self.present()

        set_topmost(self.hwnd)
        # Show it only now, with content already in place. Showing earlier made
        # the first frames solid black across the screen.
        user32.SetWindowPos(wintypes.HWND(self.hwnd), wintypes.HWND(-1),
                            x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        root.update()
        self.apply_through(True)

        # WARNING: never call root.attributes("-alpha", ...) after this point.
        # Tk implements -alpha with SetLayeredWindowAttributes, which switches the
        # window out of per-pixel-alpha mode and clears the style bits this board
        # depends on -- measured: after one -alpha call the window reported
        # layered=False, toolwindow=False, and the whole transparency setup was
        # gone while the board kept running as an opaque rectangle.
        write_text(self.args.window, str(os.getpid()))
        self.note("board up %dx%d at %d,%d" % (w, h, x, y))
        if not transparent_ok:
            self.note("refusing to run without transparency")
            self.abort()

    def tk_canvas_off(self):
        """Hide the Tk canvas: the window's pixels come from the RGBA buffer."""
        try:
            self.canvas.place_forget()
        except Exception:
            pass

    def present(self):
        """Show the board's pixels on screen. True when the window accepted them.

        Hands the numpy buffer's address straight to the compositor: no PIL
        tobytes, no intermediate copy. Measured on 1920x1080 this is 2.46 ms per
        frame versus 9.43 ms for the old PIL path, and it runs on every mouse move
        while drawing.

        WARNING: the buffer presented here is self.pixels, not self.surface. PIL
        does not write through to a numpy array that an image was made from, so
        sync() copies the image's bytes into the array after every drawing
        operation. Presenting self.surface directly would mean a full tobytes copy
        each frame, which is exactly the cost being removed.
        """
        try:
            ptr = self.pixels.ctypes.data
            ok = push_rgba_region(self.hwnd, self.screen[0], self.screen[1],
                                  self.screen[2], self.screen[3], ptr,
                                  self.screen[2], self.screen[3])
            if not ok:
                self.note("present rejected err=%s" % ctypes.get_last_error())
            return ok
        except Exception as e:
            self.note("present failed: %s" % e)
            return False

    def sync(self):
        """Copy the PIL image's pixels into the numpy buffer, then show them.

        WARNING: this copy is required, not optional. Image.fromarray shares memory
        only until PIL decides to write; measured, after drawing a stroke the image
        held the ink while the array still held the empty background, so present()
        would have shown a blank board.

        The copy is a single memcpy of the screen, and it happens once per drawn
        segment -- not once per shape on the board.
        """
        try:
            import numpy as np
            self.pixels[:] = np.asarray(self.surface)
        except Exception as e:
            self.note("sync failed: %s" % e)
            return
        self.present()

    def abort(self):
        """Tear the window down at once and leave nothing behind."""
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            os.remove(self.args.window)
        except Exception:
            pass
        self.aborted = True

    def verify_transparent(self):
        """Report the board's transparency; never abort on it.

        WARNING: this used to abort the board, and it was wrong every time.
        Measured with four sample points before and after the board came up, the
        desktop was byte-identical at all four -- the board is genuinely
        transparent -- yet the check read (0,0,0) and killed a perfectly good
        board. The sample is taken through GDI while a layered window is being
        re-presented, so it can observe the surface mid-update rather than what
        the user sees.

        It now only records what it saw. A real opacity failure shows up as the
        user's screen going dark, which is not something a self-check can be
        trusted to catch anyway -- and a wrong abort is worse than no check.
        """
        if self.aborted:
            return
        bx, by = self.screen[0] + 3, self.screen[1] + 3
        px = read_screen_pixel(bx, by)
        self.note("transparency sample %s (baseline %s)"
                  % (px, self.before_pixel))

    def apply_through(self, want):
        """Let the mouse through (idle) or catch it (draw mode).

        With per-pixel alpha there is no colour key to preserve, so the honest
        mechanism is available again: WS_EX_TRANSPARENT makes the window
        click-through, and clearing it lets the board receive the mouse. The old
        WPF board did exactly this (it toggled TRANSPARENT and NOACTIVATE by
        mode), and the reason it was avoided here earlier no longer applies --
        that reason was the colour key, which a style change would discard.

        WARNING: WS_EX_NOACTIVATE must also follow the mode. A window that cannot
        be activated never receives the mouse, so leaving NOACTIVATE set in draw
        mode makes draw mode dead even though everything else looks correct.

        The surface is re-presented after the style change: SetWindowLong makes
        Windows forget the layered content, and without this the window shows
        nothing until the next repaint.
        """
        if self.through == want:
            return
        h = clean_handle(self.hwnd)
        style = get_ex_style(h)
        if want:
            style = style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            style = style & ~WS_EX_TRANSPARENT & ~WS_EX_NOACTIVATE
        set_ex_style(h, style)
        self.through = want

        # Re-present and re-raise. Both are needed after a style change: the
        # layered content is gone, and the board must be above the app window to
        # receive the click.
        self.sync()
        set_topmost(h)
        if not want:
            try:
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                user32.SetForegroundWindow.restype = wintypes.BOOL
                got = bool(user32.SetForegroundWindow(wintypes.HWND(h)))
                self.note("foreground = %s" % got)
            except Exception as e:
                self.note("foreground failed: %s" % e)
        self.note("click-through = %s (transparent=%s noactivate=%s)"
                  % (want, bool(get_ex_style(h) & WS_EX_TRANSPARENT),
                     bool(get_ex_style(h) & WS_EX_NOACTIVATE)))

    # -- the whale -----------------------------------------------------------

    def ball_state_dir(self):
        """Where the whale keeps its own files (log, lock, status)."""
        return os.path.join(self.board_state_dir, "ball")

    def clear_stale_whales(self):
        """Kill whales that hold the lock but never reported being up.

        A whale that hangs inside WebView2 page load takes whale.lock, never
        writes ball-status.json, and therefore blocks every later attempt with
        "another whale holds the lock". It also cannot be found through the lock
        file (msvcrt holds a kernel byte-range lock, so the file cannot even be
        opened), which is why the process list is used instead.
        """
        pids = find_whale_pids()
        if not pids:
            return
        good = 0
        if whale_status_name(self.ball_state_dir()) == "running":
            good = whale_pid(self.ball_state_dir())
        killed = 0
        for pid in pids:
            if pid == good:
                continue
            # Our own newborn gets the grace period: it has not had time to
            # write its status yet.
            if pid == self.ball_spawn_pid:
                if time.time() - self.ball_spawn_at < BALL_START_GRACE_S:
                    continue
            self.note("ball: clearing stale whale pid %d" % pid)
            if _kill_pid(pid):
                killed += 1
        if killed:
            time.sleep(0.4)

    def start_whale(self):
        """Start the whale if it is not already up. True when one is running."""
        self.clear_stale_whales()

        existing = whale_pid(self.ball_state_dir())
        if existing > 0 and _process_alive(existing):
            self.ball_spawn_pid = existing
            return True

        # A spawn of our own may still be inside its grace period. Without this
        # the board would start a new whale every few seconds, and every one of
        # them would be refused by the previous whale's lock while the first one
        # was still loading -- the board then never sees "running" at all.
        if self.ball_spawn_pid > 0 and _process_alive(self.ball_spawn_pid):
            if time.time() - self.ball_spawn_at < BALL_START_GRACE_S:
                self.note("ball: previous spawn still starting")
                return False

        if self.ball_tries >= BALL_TRIES_MAX:
            return False

        script = os.path.join(self.plugin_dir, "ball", "deskpet.py")
        if not os.path.isfile(script):
            self.note("ball: script missing %s" % script)
            self.ball_tries = BALL_TRIES_MAX
            return False

        ball_dir = self.ball_state_dir()
        try:
            os.makedirs(ball_dir, exist_ok=True)
        except Exception:
            pass

        # The state directory travels in a UTF-8 file rather than on the command
        # line: this plugin's folder has a Chinese name, and a Windows command
        # line hands non-ASCII through the ANSI code page, which turned
        # "dsh伴随者" into mojibake on the Python side and made every later path
        # wrong.
        state_file = os.path.join(ball_dir, "state-path.txt")
        write_text(state_file, self.board_state_dir)

        # A stale status from the previous run would fool the readiness check.
        try:
            os.remove(os.path.join(ball_dir, "ball-status.json"))
        except Exception:
            pass

        self.ball_tries += 1
        if self.ball_tries >= BALL_TRIES_MAX:
            self.ball_give_up_at = time.time()
        self.note("ball: starting attempt %d" % self.ball_tries)

        # WARNING: the whale must be started with the WINDOWLESS interpreter.
        #
        # python.exe is a console-subsystem program: Windows allocates a console
        # for it, and that console window is the black "command line" rectangle
        # that appears near the top right at every plugin start. CREATE_NO_WINDOW
        # does not reliably prevent it -- the console belongs to the subsystem,
        # not to our flags.
        #
        # pythonw.exe is the same interpreter linked for the GUI subsystem, so no
        # console exists at all. sys.executable is whatever started the board,
        # normally pythonw.exe but python.exe when the board is run from a shell,
        # so the choice is made explicitly here instead of inherited.
        # WARNING: the whale needs pythonnet (it draws with Windows Forms), and
        # pythonnet lives in the plugin's ocr_env, NOT in the bundled runtime.
        #
        # self.python_exe is sys.executable of the BOARD, which is the wrong
        # answer: the board is launched through ocr_env\Scripts\pythonw.exe, but
        # that is a launcher that re-execs runtime\python\pythonw.exe, so the board
        # reports the RUNTIME as its own interpreter. Measured: the whale then died
        # with "pythonnet setup failed: No module named 'pythonnet'" and its window
        # stayed parked off-screen at -20000,-20000, which is why the whale never
        # appeared while the board looked fine.
        exe = self.python_exe
        env_python = os.path.join(self.plugin_dir, "ocr_env", "Scripts",
                                  "python.exe")
        if os.path.isfile(env_python):
            exe = env_python
        if exe and os.path.basename(exe).lower() == "python.exe":
            pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(pythonw):
                exe = pythonw
        try:
            self.ball_proc = subprocess.Popen(
                [exe, script, "--state-path-file", state_file],
                cwd=self.plugin_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.note("ball: spawn failed: %s" % e)
            return False

        self.ball_spawn_pid = self.ball_proc.pid
        self.ball_spawn_at = time.time()

        # Wait for the whale to report running. WebView2 cold start is a second
        # or two; past the grace period it is stuck, and a stuck whale holding
        # the lock is worse than none, so it gets killed.
        deadline = time.time() + 12.0
        while time.time() < deadline:
            time.sleep(0.3)
            state = whale_status_name(ball_dir)
            if state == "running":
                self.ball_tries = 0
                live = whale_pid(ball_dir)
                if live > 0:
                    self.ball_spawn_pid = live
                self.note("ball: running")
                return True
            if state == "failed":
                self.note("ball: reported failed")
                return False
            if self.ball_proc.poll() is not None:
                # The whale exited on its own: usually because another one holds
                # the lock, which is not an error to report twice.
                if whale_pid(ball_dir) <= 0:
                    self.note("ball: launcher exited, no ball pid")
                    break

        stuck = self.ball_spawn_pid
        if stuck > 0 and _process_alive(stuck):
            self.note("ball: start timeout, killing stuck pid %d" % stuck)
            _kill_pid(stuck)
            time.sleep(0.3)
        self.ball_spawn_pid = 0
        self.note("ball: start timeout")
        return False

    def watch_whale(self):
        """Keep exactly one whale alive, retrying after a failure window."""
        now = time.time()
        healthy = False
        live_pid = whale_pid(self.ball_state_dir())
        if live_pid > 0 and _process_alive(live_pid):
            # WARNING: a stale ball-status.json outlives its whale, and Windows
            # reuses pids. Measured: the file named pid 16156 as "running" long
            # after that process was gone -- and if an unrelated program had
            # inherited the number, this check would call the whale healthy
            # forever and it would never be restarted.
            #
            # The guard is one-directional on purpose: only a NON-EMPTY python
            # list that excludes the pid proves the pid is not ours. An empty list
            # means the scan failed, which must not be read as "the whale is
            # gone" or the board would restart it in a loop.
            pythons = _all_python_pids()
            if pythons and live_pid not in pythons:
                self.note("ball: pid %d is not a python process, stale record"
                          % live_pid)
                live_pid = 0
            else:
                healthy = True

        if not healthy and self.ball_spawn_pid > 0 and _process_alive(self.ball_spawn_pid):
            if now - self.ball_spawn_at < BALL_START_GRACE_S:
                return          # still starting, not dead
            self.note("ball: stuck past grace (%d), killing"
                      % self.ball_spawn_pid)
            _kill_pid(self.ball_spawn_pid)
            self.ball_spawn_pid = 0

        if healthy:
            return
        if now - self.ball_last_check < 6.0:
            return
        self.ball_last_check = now
        if self.ball_tries >= BALL_TRIES_MAX:
            # Cool down instead of giving up for good: one WebView2 hiccup must
            # not write the whale off for the life of the session.
            if now - self.ball_give_up_at >= BALL_RETRY_COOLDOWN_S:
                self.note("ball: cooldown over, retrying")
                self.ball_tries = 0
            return
        self.note("ball: gone, restarting")
        self.start_whale()

    # -- scene ---------------------------------------------------------------

    def set_mode(self, mode):
        if mode == self.mode:
            return
        # Leaving draw mode must not leave the width badge baked into the drawing.
        self.hide_width_badge()
        self.mode = mode
        # Text boxes are windows, so they follow the pencil: visible in both modes
        # (the summon screenshot must contain them) but only catching the mouse
        # while drawing.
        if mode == "idle":
            self.close_text_editor()
            self.text_drag = None
        self.set_text_windows_active(mode == "ink")
        self.apply_through(mode == "idle")
        self.apply_cursor()
        # Whale: dive out when the board takes the mouse, surface when it gives
        # it back. The whale animates the transition itself (spin + shrink, then
        # spin + grow), so the board only sends the command.
        self.command_whale("0" if mode == "ink" else "1")
        # Take the keyboard while drawing.
        #
        # WARNING: a Tk key binding only fires when the window owns the keyboard
        # focus, and this window is created with WS_EX_NOACTIVATE so it never
        # takes focus on its own. Without this, Esc did nothing at all -- the
        # binding was registered and never reached. The old WPF board called
        # Activate() and Keyboard.Focus() when entering draw mode for the same
        # reason.
        if mode == "ink":
            self.take_keyboard()
        self.note("mode = %s" % mode)

    def take_keyboard(self):
        """Give the board the keyboard so Esc and the other keys arrive."""
        try:
            h = clean_handle(self.hwnd)
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow(wintypes.HWND(h))
            user32.SetFocus.argtypes = [wintypes.HWND]
            user32.SetFocus(wintypes.HWND(h))
        except Exception as e:
            self.note("take_keyboard win32: %s" % e)
        # Tk's own focus model: focus_force ignores the window manager, which is
        # what is needed for an overrideredirect window that cannot be activated
        # through the normal route.
        for widget in (getattr(self, "hit_layer", None), self.root):
            if widget is None:
                continue
            try:
                widget.focus_force()
            except Exception:
                pass
        self.note("keyboard taken")

    def raise_board(self):
        """Put the board back in front of everything, including the app.

        WARNING: being WS_EX_TOPMOST is not enough on its own. The DSH window is
        a normal (non-topmost) window, but SetForegroundWindow from user clicks
        brings it forward in the z-order, and the board is then behind it even
        though the board is topmost -- measured: with the board in draw mode, a
        click at the screen centre reached the DSH window
        (Chrome_WidgetWin_1), not the board, so no stroke was ever drawn.

        Re-asserting the topmost position pushes the board back above it without
        touching the extended style (which would discard the layered surface).

        WARNING: raising the board also buries the text boxes. Both are topmost,
        and among topmost windows the most recently raised wins -- measured: the
        text box sat above the board until the board was raised, and then the words
        disappeared under the board's bitmap. So every text box is re-raised right
        after. A text box is content ON the board; it must never be behind it.
        """
        try:
            set_topmost(self.hwnd)
            self._raise_text_windows()
            self.note("board raised")
        except Exception as e:
            self.note("raise failed: %s" % e)

    def _raise_text_windows(self):
        """Keep every text box above the board, in its own stacking order."""
        for entry in getattr(self, "texts", []) or []:
            win = getattr(self, "text_wins", {}).get(entry.get("id"))
            if win is None:
                continue
            try:
                hwnd = clean_handle(top_level_of(int(win.winfo_id())))
                set_topmost(hwnd)
            except Exception:
                continue

    def command_whale(self, word):
        """Tell the whale to hide ("0") or show ("1").

        This is the channel the whale watches: ball/ball-cmd.txt inside its own
        state folder. Writing it is enough -- the whale polls it and runs its
        dive animation.
        """
        try:
            path = os.path.join(self.ball_state_dir(), "ball-cmd.txt")
            write_text(path, word)
            self.note("ball cmd %s" % word)
        except Exception as e:
            self.note("ball cmd failed: %s" % e)

    def apply_cursor(self):
        """Show or hide the pen ring for the current mode.

        WARNING: read the comment block above _ensure_ring_window before changing
        this. The ring is NOT a cursor any more, and cannot be: Windows clamps the
        pointer to the system cursor size (32x32 here), so every ring -- 10 px or
        256 px -- rendered at 32 px and only its stroke changed. The user reported
        exactly that: "the ring does not change size, the stroke gets thinner as
        the pixels get bigger".

        So the ring is its own click-through window that follows the pointer. Moving
        it costs one SetWindowPos (0.27 ms measured); nothing is repainted.
        """
        try:
            if self.mode != "ink":
                self._stop_mouse_follow()
                self._apply_arrow_cursor()
                return
            # A plain crosshair would be drawn ON TOP of the ring window, so the
            # board's own cursor is hidden while the ring is up.
            for widget in (self.root, getattr(self, "hit_layer", None)):
                if widget is None:
                    continue
                try:
                    widget.configure(cursor="none")
                except Exception:
                    pass
            self._install_ring_cursor()
            self._start_mouse_follow()
        except Exception as e:
            self.note("cursor failed: %s" % e)

    def _apply_arrow_cursor(self):
        """Idle mode: hide the ring window and give the pointer back."""
        self._hide_ring_window()
        for widget in (self.root, getattr(self, "hit_layer", None)):
            if widget is None:
                continue
            try:
                widget.configure(cursor="arrow")
            except Exception:
                pass

    def _pen_radius(self):
        """The ring's radius in screen pixels: half the pen, never below 6."""
        return max(6.0, float(self.pen_width) / 2.0)

    def _ring_signature(self):
        """What the ring looks like: rebuild only when this changes."""
        return (round(self._pen_radius(), 1),
                bool(getattr(self, "ink_white", False)),
                bool(getattr(self, "erasing", False)))

    # -- the pen ring, drawn in its own window ---------------------------------
    #
    # WARNING: read this before trying a cursor again. Every cursor-based attempt
    # failed for one reason, and it is not fixable:
    #
    #   Windows clamps the pointer to the SYSTEM cursor size -- 32x32 on this
    #   machine, read with GetSystemMetrics(SM_CXCURSOR). Any larger bitmap is
    #   scaled down to 32x32, so a 10 px ring and a 256 px ring both render at
    #   32 px and only their stroke thickness differs. That is exactly what the
    #   user saw: "the ring does not change size, the stroke gets thinner as the
    #   pixels get bigger".
    #
    # Drawing programs solve this by not using a cursor at all: they show a
    # separate always-on-top window that follows the pointer. That is what this
    # does, and it is the only way to get an honest size.
    #
    # Cost: SetWindowPos measured at 0.27 ms per move (against 2.15 ms through Tk's
    # geometry), so following the pointer at 60 Hz uses ~1.6% of one core.

    RING_MS = 16          # ~60 Hz follow rate

    # -- the pen preview, as a pure Win32 layered window -----------------------
    #
    # WARNING: read this before trying Tk drawing or a cursor again. Both were
    # tried and both fail for structural reasons:
    #
    #   * A Tk cursor is clamped to the SYSTEM cursor size -- GetSystemMetrics
    #     (SM_CXCURSOR) is 32 here -- so every ring from 10 px to 256 px rendered
    #     at 32 px and only its stroke changed. Not fixable in application code.
    #
    #   * -transparentcolor and UpdateLayeredWindow are two MUTUALLY EXCLUSIVE
    #     transparency models on the same window. Tk installs a colour key for
    #     -transparentcolor; after that, UpdateLayeredWindow fails silently, and
    #     Tk's PhotoImage does not produce a per-pixel alpha surface anyway. The
    #     window reported IsWindowVisible=True while the compositor drew nothing --
    #     measured: GetWindowRect and IsWindowVisible both looked correct and the
    #     screen stayed empty.
    #
    # So this window has NO Tk content at all: no Label, no Canvas, no
    # -transparentcolor, no -alpha. Its HWND is a bare shell and every pixel comes
    # from push_rgba(), the same per-pixel-alpha path the board itself uses. One
    # call, one atomic surface, no compositing conflicts.

    def _ensure_ring_window(self):
        """Create the preview window: a bare layered shell, no Tk children."""
        if getattr(self, "_ring_win", None) is not None:
            return True
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            # WARNING: do NOT call withdraw(). Tk then owns the window's mapped
            # state and ShowWindow is ignored -- measured: ShowWindow returned 0,
            # winfo_ismapped stayed 0, state stayed "withdrawn", and
            # UpdateLayeredWindow drew into an unmapped window. The window is
            # created mapped and parked off-screen instead, so Tk never has a
            # visibility decision to override.
            win.geometry("4x4+-4000+-4000")
            win.update_idletasks()
            # No -transparentcolor, no -alpha: either installs the colour-key /
            # whole-window model and makes UpdateLayeredWindow fail silently, which
            # is exactly the invisible-preview bug.
            self._ring_win = win
            self._ring_hwnd = clean_handle(top_level_of(int(win.winfo_id())))
            style = get_ex_style(self._ring_hwnd)
            set_ex_style(self._ring_hwnd, style | WS_EX_LAYERED
                         | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                         | WS_EX_TOOLWINDOW)
            # WARNING: being WS_EX_LAYERED is not the same as being topmost. The
            # preview sat BEHIND the board's bitmap because nothing ever put it in
            # the topmost band -- SetWindowPos was called with hWndInsertAfter=0,
            # which means "leave the z-order alone". It only surfaced for a moment
            # when CapsLock rebuilt the surface, because that path calls
            # SetWindowPos again and Tk had lifted the window in between.
            set_topmost(self._ring_hwnd)
            self.note("ring window created")
            return True
        except Exception as e:
            self.note("ring window failed: %s" % e)
            return False

    def _ring_appearance(self):
        """(diameter, colour, alpha) for the disc right now.

        The disc IS the pen: same diameter as the stroke, black for a black pen,
        white for a white one (CapsLock), half-transparent while erasing so it
        reads as "this removes" rather than "this paints".
        """
        diameter = max(4, int(round(self.pen_width)))
        if getattr(self, "erasing", False):
            return diameter, (0, 0, 0), 120
        if getattr(self, "ink_white", False):
            return diameter, (255, 255, 255), 255
        return diameter, (0, 0, 0), 255

    def _render_ring(self, diameter, colour, alpha):
        """A fresh RGBA buffer holding one disc, transparent everywhere else."""
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        # The disc is inset by one pixel so its edge is not clipped by the window
        # border, and drawn with PIL's own antialiasing off: a hard edge reads more
        # honestly as "this is the brush footprint" than a soft one.
        ImageDraw.Draw(img).ellipse(
            [0, 0, diameter - 1, diameter - 1],
            fill=(colour[0], colour[1], colour[2], alpha))
        return img.tobytes()

    def _install_ring_cursor(self):
        """Draw the disc at the pen's real size and put it under the pointer."""
        if not self._ensure_ring_window():
            return
        diameter, colour, alpha = self._ring_appearance()
        sig = (diameter, colour, alpha)
        if getattr(self, "_ring_sig", None) != sig:
            self._ring_sig = sig
            self._ring_size = diameter
            self._ring_visible = True
            try:
                px, py = self.root.winfo_pointerxy()
            except Exception:
                px, py = self.screen[0] + self.screen[2] // 2, self.screen[1] + self.screen[3] // 2
            x = px - diameter // 2
            y = py - diameter // 2
            self._ring_pos = (x, y)
            try:
                # WARNING: order matters. A window Tk withdrew is not mapped, and
                # UpdateLayeredWindow on an unmapped window leaves it unmapped --
                # measured: GetWindowRect and the size were both right while
                # IsWindowVisible stayed False and the screen showed nothing.
                # ShowWindow first, then size/position, then push the surface.
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.SetWindowPos(clean_handle(self._ring_hwnd), HWND_TOPMOST,
                                    x, y, diameter, diameter, SWP_NOACTIVATE)
                # One atomic surface push. No Tk involvement, so no colour key can
                # swallow the circle and nothing can be re-asserted behind our back.
                rgba = self._render_ring(diameter, colour, alpha)
                ok = push_rgba(self._ring_hwnd, x, y, diameter, diameter, rgba)
                if not ok:
                    self.note("ring push failed for %dpx" % diameter)
                else:
                    self.note("ring %dpx %s alpha=%d" %
                              (diameter, "white" if colour[0] else "black", alpha))
            except Exception as e:
                self.note("ring draw failed: %s" % e)
            return
        self._move_ring_window()

    def _hide_ring_window(self):
        if getattr(self, "_ring_win", None) is None:
            return
        try:
            if getattr(self, "_ring_visible", False):
                # Park it off-screen: Tk owns the mapped state, so SW_HIDE would be
                # ignored and the disc would stay on screen.
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.SetWindowPos(clean_handle(self._ring_hwnd), 0,
                                    -4000, -4000, 4, 4, SWP_NOACTIVATE | SWP_NOZORDER)
                self._ring_visible = False
                self._ring_sig = None
        except Exception:
            pass

    def _move_ring_to(self, px, py):
        """Centre the disc on a screen point. Called from the hook thread.

        WARNING: this runs on the mouse-hook thread, so it does nothing except one
        SetWindowPos on an already-composited layered window (0.27 ms measured). No
        Tk calls, no drawing, no locks: a slow low-level hook is unhooked by
        Windows and the preview would simply freeze.
        """
        if not getattr(self, "_ring_visible", False):
            return
        hwnd = getattr(self, "_ring_hwnd", 0)
        if not hwnd:
            return
        size = getattr(self, "_ring_size", 32)
        x = px - size // 2
        y = py - size // 2
        if getattr(self, "_ring_pos", None) == (x, y):
            return
        self._ring_pos = (x, y)
        try:
            # HWND_TOPMOST, not 0: the preview must stay in the topmost band or
            # the board (also topmost, and full-screen) covers it.
            user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, size, size,
                                SWP_NOACTIVATE | SWP_NOCOPYBITS)
        except Exception:
            pass

    def _move_ring_window(self):
        """Place the disc under the pointer once, using the current position.

        Used when the disc is first shown or resized; steady-state motion is driven
        by the hook, not by this.
        """
        if not getattr(self, "_ring_visible", False):
            return
        try:
            px, py = self.root.winfo_pointerxy()
        except Exception:
            return
        self._move_ring_to(px, py)

    def _start_mouse_follow(self):
        """Install the low-level mouse hook that drives the disc."""
        if getattr(self, "_follower", None) is not None:
            return
        try:
            follower = MouseFollower(self._move_ring_to)
            if follower.start():
                self._follower = follower
                self.note("mouse hook installed")
            else:
                self.note("mouse hook unavailable, falling back to polling")
        except Exception as e:
            self.note("mouse hook failed: %s" % e)

    def _stop_mouse_follow(self):
        follower = getattr(self, "_follower", None)
        if follower is None:
            return
        try:
            follower.stop()
        except Exception:
            pass
        self._follower = None
        self.note("mouse hook removed")

    def _tick_pen_ring(self):
        """Rebuild the disc when the pen changes; motion is hook-driven.

        WARNING: nothing here follows the pointer any more. Motion comes from
        MouseFollower, which is event-driven and runs on its own thread -- the Tk
        tick only runs at ~8 Hz and made the preview step visibly. This function
        exists for the one case the hook cannot cover: the pen's size or colour
        changed, so the surface must be re-pushed.
        """
        if self.mode != "ink":
            return
        if getattr(self, "_ring_sig", None) != self._ring_appearance():
            self._install_ring_cursor()


    # -- the user's ink ------------------------------------------------------

    def bind_mouse(self):
        """Wire the mouse so draw mode can actually draw.

        WARNING: without this the board is a display only. Draw mode said it was
        on (the cursor left click-through), but no button was ever bound, so
        pressing the mouse did nothing -- the whole point of draw mode.

        Events are bound to the Tk canvas that sits over the window. That canvas
        is never painted; it exists to turn clicks into Tk events, because the
        visible pixels come from the RGBA surface and a PIL image cannot receive
        input.
        """
        target = getattr(self, "hit_layer", None) or self.canvas
        target.bind("<ButtonPress-1>", self.on_press)
        target.bind("<B1-Motion>", self.on_drag)
        target.bind("<ButtonRelease-1>", self.on_release)
        # WARNING: the two modifier gestures are deliberately on different keys:
        #
        #   Shift + left  -> erase   (the established gesture; do not move it)
        #   Alt   + left  -> select text
        #
        # They were briefly swapped while chasing a selection bug, which is the
        # opposite of what the user asked for. Shift stays the eraser; Alt is the
        # one that selects, so a Shift-drag over a word erases (as it always did)
        # and an Alt-drag picks the words up.
        #
        # The plain left bindings are registered first so the more specific
        # modifier patterns can be matched before them.
        target.bind("<Shift-ButtonPress-1>", self.on_erase)
        target.bind("<Alt-ButtonPress-1>", self.on_text_select)
        target.bind("<ButtonPress-3>", self.open_board_menu)
        target.bind("<MouseWheel>", self.on_wheel)
        target.bind("<Button-4>", lambda e: self.on_wheel(
            type("E", (), {"delta": 120})()))
        target.bind("<Button-5>", lambda e: self.on_wheel(
            type("E", (), {"delta": -120})()))
        # CapsLock flips the user's ink between black and white. KeyRelease, not
        # Press: CapsLock also toggles the OS-level lock state, and binding the
        # release keeps the flip from racing the system's own handling.
        try:
            self.root.bind_all("<KeyRelease-Caps_Lock>", self.on_caps_lock)
        except Exception:
            pass
        # Esc must work no matter which widget holds focus, so it is bound on the
        # whole application rather than on one widget. A plain root.bind only
        # fires when the root itself has focus, and the board is a frameless
        # window that can end up with focus on the hit layer or the text entry.
        try:
            self.root.bind_all("<Escape>", self.on_escape)
        except Exception:
            try:
                self.root.bind("<Escape>", self.on_escape)
            except Exception:
                pass
        self.build_board_menu()

    def _caps_lock_on(self):
        """Whether CapsLock is currently latched, read from the OS.

        WARNING: the key event is not usable here. The board is a frameless,
        click-through window that usually does NOT hold focus -- the user is
        drawing over other applications -- so `<KeyRelease-Caps_Lock>` bound with
        bind_all almost never fires. That is why pressing CapsLock appeared to do
        nothing. Reading the lock state asks the question the event stood in for:
        is CapsLock on right now?
        """
        try:
            # WARNING: GetKeyState is thread-local -- it reflects the key state as
            # this thread's message queue last saw it, and a board that does not
            # hold keyboard focus never sees the key at all. GetAsyncKeyState reads
            # the real global state instead.
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            # Bit 0 is the toggle (latched).
            return bool(user32.GetAsyncKeyState(0x14) & 0x0001)
        except Exception:
            return bool(getattr(self, "ink_white", False))

    def _sync_ink_colour(self):
        """Start or stop the CapsLock double-tap watcher.

        WARNING: this no longer reads the CapsLock LOCK state. That state is only
        true while the key is latched, so the pen flipped to white on the press and
        straight back to black when the state was re-read -- which is why the user
        had to HOLD CapsLock to keep a white pen. The colour is now toggled by a
        double tap instead; see CapsLockDoubleTap.
        """
        if self.mode == "ink":
            self._start_caps_watch()
        else:
            self._stop_caps_watch()

    def _start_caps_watch(self):
        if getattr(self, "_caps_watch", None) is not None:
            return
        try:
            watch = CapsLockDoubleTap(self._toggle_ink_colour)
            if watch.start():
                self._caps_watch = watch
                self.note("caps double-tap watch started")
            else:
                self.note("caps watch unavailable")
        except Exception as e:
            self.note("caps watch failed: %s" % e)

    def _stop_caps_watch(self):
        watch = getattr(self, "_caps_watch", None)
        if watch is None:
            return
        try:
            watch.stop()
        except Exception:
            pass
        self._caps_watch = None

    def _alt_down(self):
        """Whether Alt is held right now, read from the OS."""
        try:
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            return bool(user32.GetAsyncKeyState(0x12) & 0x8000)
        except Exception:
            return False

    def _sync_alt_hittable(self):
        """While Alt is held, let the text boxes take the mouse.

        WARNING: forwarding the press from the board is NOT enough, and that is
        what made Alt-drag fall through to the desktop. A click-through text box
        makes Windows look further down, and the board's pixels under the words are
        transparent (1% background), so the click skipped the board too and landed
        on whatever window was behind it.

        The fix is to stop being click-through BEFORE the press: the boxes become
        hittable the moment Alt goes down, so the click lands on the words and Tk
        selects normally. Release Alt and they go back to click-through.
        """
        want_hittable = self._alt_down()
        state = getattr(self, "_alt_hittable", None)
        if state == want_hittable:
            return
        self._alt_hittable = want_hittable
        for entry in getattr(self, "texts", []) or []:
            win = self.text_wins.get(entry.get("id"))
            if win is None:
                continue
            try:
                self._set_text_window_through(win, not want_hittable)
            except Exception:
                continue
        self.note("alt %s -> text boxes %s"
                  % ("down" if want_hittable else "up",
                     "hittable" if want_hittable else "click-through"))

    def _toggle_ink_colour(self):
        """Flip the pen between black and white. Runs on the hook thread."""
        if self.mode != "ink":
            return
        self.ink_white = not bool(getattr(self, "ink_white", False))
        self.note("ink colour %s (double tap)"
                  % ("white" if self.ink_white else "black"))
        # The ring and any open editor must follow, but both touch Tk, which is not
        # safe from this thread -- so the work is queued onto the Tk loop.
        try:
            self.root.after(0, self._after_colour_change)
        except Exception:
            pass

    def _after_colour_change(self):
        try:
            self._restyle_open_editor()
        except Exception:
            pass
        try:
            self._tick_pen_ring()
        except Exception:
            pass

    def on_caps_lock(self, _event=None):
        """Legacy single-press hook. Kept inert: the colour is a double tap now."""
        return "break"

    def _unused_caps_lock(self, _event=None):
        """CapsLock in draw mode: change the colour of the NEXT stroke.

        It changes the pen, not the board. Ink already on the board is pixels and
        stays exactly as it was -- the user asked for "this is the colour I draw
        with now", not for a repaint that recolours their earlier work.

        While a text box is being typed into, the box follows immediately: black
        text on white, or white text on black. The user asked for that switch
        without having to leave the box first, and it costs one repaint of a
        window that is already open.

        Only while drawing: in idle mode the board does not own the keyboard, and
        a CapsLock press there belongs to whatever app the user is working in.
        """
        if self.mode != "ink":
            return
        # Take the OS state instead of flipping a copy: the key press has already
        # toggled it, and a local flag would drift the moment anything else
        # changed the lock.
        self.ink_white = self._caps_lock_on()
        self.note("ink colour %s" % ("white" if self.ink_white else "black"))
        # The open editor flips with the pen: the box is a container whose text
        # and background are a pair, so a white-on-black box must become
        # black-on-white, not white-on-white.
        self._restyle_open_editor()
        self._tick_pen_ring()
        return "break"

    def _restyle_open_editor(self):
        """Repaint the box being typed into with the current pen colour."""
        editor = getattr(self, "text_editor", None)
        if editor is None:
            return
        try:
            ink = self.text_hex()
            bg = TEXT_BG if ink == "#ffffff" else TEXT_BG_LIGHT
            widget = editor.get("text")
            if widget is not None:
                widget.configure(bg=bg, fg=ink, insertbackground=ink,
                                 selectbackground="#3a3a48",
                                 selectforeground="#ffffff")
            win = editor.get("win")
            if win is not None:
                win.configure(bg=bg)
                for child in win.winfo_children():
                    try:
                        child.configure(bg=bg)
                    except Exception:
                        pass
        except Exception as e:
            self.note("editor restyle failed: %s" % e)

    def build_board_menu(self):
        """The right-click menu, with the same entries as the WPF board."""
        try:
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label=self.labels.get("textBox", "Text box"),
                          command=lambda: self.open_text_editor(*self.menu_at))
            m.add_command(label=self.labels.get("call", "Call agent"),
                          command=self.request_summon)
            m.add_command(label=self.labels.get("clear", "Clear"),
                          command=self.clear_all)
            m.add_separator()
            m.add_command(label=self.labels.get("exitDraw", "Stop drawing"),
                          command=lambda: self.set_mode("idle"))
            self.menu = m
        except Exception as e:
            self.note("board menu failed: %s" % e)

    def on_press(self, event):
        """Start a stroke, or commit an open text box.

        A click anywhere on the board is "outside the box", so it ends the edit.
        The click that commits does NOT also start a stroke: the user was aiming at
        the box's surroundings, not at drawing there, and a stray dot appearing
        under their pointer every time they finish typing would be wrong.
        """
        if self.mode != "ink":
            return
        x, y = float(event.x), float(event.y)
        if self.text_editor is not None:
            self.commit_text_editor()
            return
        # WARNING: a press on a committed box no longer starts a drag. Boxes are
        # placed, not moved -- the user asked for that explicitly. The press is
        # swallowed instead of drawing, because a dot appearing under the pointer
        # every time someone clicks near their text would be wrong.
        #
        # Reading the text back is an idle-mode gesture now: leave draw mode and
        # hold Shift (see _tick_text_readable). While drawing, a press on a box is
        # simply swallowed so it cannot paint a stroke through the words.
        hit = self.text_at(x, y)
        if hit is not None:
            return
        self.active_points = [(x, y)]
        self.active_item = None
        self._paint_segment(x, y, x, y, self.pen_width)

    def on_text_select(self, event):
        """Alt+left on a text box: hand the drag to the words so they select.

        WARNING: making the box interactive is NOT enough. The board owns the
        mouse in draw mode, so an Alt-drag arrives here, not at the text window --
        and simply setting the box hittable changed nothing because this press was
        already consumed. The event has to be FORWARDED to the widget under the
        pointer, which is what turns Alt+drag into an ordinary text selection.

        Shift+left stays the eraser; this is the only gesture that reads text.
        """
        x, y = float(event.x), float(event.y)
        # Commit any open editor first: two live editors would disagree with the
        # record.
        if self.text_editor is not None:
            self.commit_text_editor()
        entry = self.text_at(x, y)
        if entry is None:
            return
        win = self.text_wins.get(entry["id"])
        if win is None:
            return
        widget = entry.get("_widget")
        if widget is None:
            # The user's own boxes drop _widget on commit; find it again.
            for child in win.winfo_children():
                if child.winfo_class() == "Text":
                    widget = child
                    break
                for grand in child.winfo_children():
                    if grand.winfo_class() == "Text":
                        widget = grand
                        break
                if widget is not None:
                    break
        try:
            # Make it hittable and focused, then let Tk's own Text bindings do the
            # selection from this press onwards.
            self._set_text_window_through(win, False)
            widget.configure(cursor="xterm")
            widget.focus_force()
            # Translate the board's coordinates into the widget's own space.
            wx = int(x - entry["x"])
            wy = int(y - entry["y"])
            widget.event_generate("<ButtonPress-1>", x=wx, y=wy)
            # Tk continues the drag itself: B1-Motion goes to the widget that
            # received the press, so the selection follows the pointer.
            self._alt_select = {"widget": widget, "entry": entry,
                                "origin": (entry["x"], entry["y"])}
            self.note("text select started at %d,%d" % (wx, wy))
        except Exception as e:
            self.note("text select failed: %s" % e)

    def on_drag(self, event):
        """Extend the stroke, keep erasing, or keep extending a text selection.

        WARNING: the eraser has to keep working while the button is held. Binding
        Shift+left only on press meant one click erased one disc, so covering an
        area meant clicking repeatedly -- not what an eraser is. Tk delivers
        Shift+drag as a normal <B1-Motion>, so the eraser state is tracked here
        and the drag paints background instead of ink.
        """
        # An Alt-drag that started on a text box keeps selecting: forward the
        # motion to the widget so Tk extends its own selection.
        selecting = getattr(self, "_alt_select", None)
        if selecting is not None:
            try:
                widget = selecting["widget"]
                entry = selecting["entry"]
                widget.event_generate("<B1-Motion>",
                                      x=int(float(event.x) - entry["x"]),
                                      y=int(float(event.y) - entry["y"]))
            except Exception:
                pass
            return
        if self.mode != "ink":
            return
        x, y = float(event.x), float(event.y)
        if self.erasing:
            # Sweep: fill every disc between the last point and this one, so a
            # fast drag leaves a continuous erased band instead of a dotted line.
            lx, ly = self.erase_last or (x, y)
            self._erase_path(lx, ly, x, y)
            # Text boxes fall to the same sweep, not just to the initial press.
            self._erase_text_at(x, y)
            self.erase_last = (x, y)
            return
        if not self.active_points:
            return
        lx, ly = self.active_points[-1]
        # Ignore jitter smaller than a couple of pixels: it would add thousands of
        # points to a stroke that looks identical, and the points are published.
        if (x - lx) ** 2 + (y - ly) ** 2 < 4:
            return
        self.active_points.append((x, y))
        self._paint_segment(lx, ly, x, y, self.pen_width)

    def ink_rgba(self):
        """The pen colour right now: black or white, chosen by CapsLock."""
        return INK_WHITE if getattr(self, "ink_white", False) else INK_BLACK

    def _paint_segment(self, x1, y1, x2, y2, width):
        """Paint one segment into the pixels and show it.

        This is the entire drawing path. The segment becomes pixels and stays
        there; nothing records it and nothing replays it. A round cap is drawn at
        the end so a fast drag does not look dotted.
        """
        if self.surface is None:
            return
        try:
            w = max(1, int(width))
            ink = self.ink_rgba()
            # Paint into the ink layer (the permanent record) and into the
            # visible surface. The layer is what survives a repaint.
            self.ink_draw.line([x1, y1, x2, y2], fill=ink, width=w)
            r = w / 2.0
            self.ink_draw.ellipse([x2 - r, y2 - r, x2 + r, y2 + r], fill=ink)
            self.draw.line([x1, y1, x2, y2], fill=ink, width=w)
            self.draw.ellipse([x2 - r, y2 - r, x2 + r, y2 + r], fill=ink)
            self.sync()
        except Exception as e:
            self.note("paint_segment failed: %s" % e)

    def on_release(self, event):
        """Finish the stroke: the ink is already on the board.

        Nothing is redrawn here. The points are recorded only so the agent can
        read what was drawn through user.json; they are never used to paint again.
        """
        selecting = getattr(self, "_alt_select", None)
        if selecting is not None:
            try:
                widget = selecting["widget"]
                entry = selecting["entry"]
                widget.event_generate("<ButtonRelease-1>",
                                      x=int(float(event.x) - entry["x"]),
                                      y=int(float(event.y) - entry["y"]))
                got = widget.tag_ranges("sel")
                if got:
                    self.note("text selected: %d chars"
                              % len(widget.get("sel.first", "sel.last")))
            except Exception:
                pass
            self._alt_select = None
            return
        if self.erasing:
            # The sweep filled its discs without pushing; show the result now.
            self.erasing = False
            self.erase_last = None
            self.sync()
            return
        if self.mode != "ink":
            self.active_points = []
            return
        if self.text_drag is not None:
            # The drop ends the move: one save, not one per mouse move.
            entry = self.text_drag["entry"]
            self.text_drag = None
            self.save_user()
            self.note("text box %d moved to %d,%d"
                      % (entry.get("seq"), entry["x"], entry["y"]))
            return
        points = self.active_points
        self.active_points = []
        self.active_item = None
        if not points:
            return
        self.stroke_seq += 1
        self.strokes.append({
            "id": "u%d-%d" % (self.stroke_seq, int(time.time() * 1000)),
            "width": self.pen_width,
            "points": [[p[0], p[1]] for p in points],
        })
        self.save_user()
        self.note("stroke %d points" % len(points))


    def on_erase(self, event):
        """Alt+left erases: clear the pixels under the pointer.

        WARNING: erasing is a pixel operation, not a lookup. Earlier versions
        tried to work out WHICH recorded stroke the click belonged to and drop it
        from a list -- first by vertex distance, then by segment distance -- and
        every miss left ink visibly on screen, which is why the eraser kept
        "not working". Clearing pixels cannot miss: whatever colour is under the
        pointer is overwritten with the board's background, so the eraser works
        even on ink it has no record of.

        This is how a paint program works: the canvas is a bitmap, drawing writes
        pixels, erasing writes background. No shape is stored, so nothing can get
        out of sync with what the user sees.
        """
        if self.mode != "ink":
            return
        # Enter erase mode and stay in it until the button comes up, so holding
        # Alt+left and dragging sweeps the eraser instead of erasing once.
        self.erasing = True
        self.erase_last = (float(event.x), float(event.y))
        self.active_points = []
        self.erase_pixels(float(event.x), float(event.y), self.eraser_radius())
        # A text box is erasable too. It is a window rather than pixels, so the
        # pixel sweep cannot reach it; erasing over one removes the box and its
        # words. Without this, text was the one thing on the board the eraser
        # could not touch, which the user reported as "the eraser stops at text".
        self._erase_text_at(float(event.x), float(event.y))
        # The stroke record exists only so the agent can read what was drawn. Once
        # pixels are gone there is no honest way to say which strokes survived, so
        # the record is dropped and the agent reads the screenshot instead.
        if self.strokes:
            self.strokes = []
            self.save_user()
        self.note("erasing from %d,%d" % (event.x, event.y))

    def _erase_path(self, x1, y1, x2, y2):
        """Erase along a segment so a fast sweep leaves a continuous band.

        Erasing only at the sampled pointer positions would leave gaps whenever
        the pointer moves further than the eraser's diameter between two events --
        which is normal during a quick sweep.
        """
        dx = x2 - x1
        dy = y2 - y1
        dist = (dx * dx + dy * dy) ** 0.5
        radius = self.eraser_radius()
        # Step at half the eraser radius so consecutive discs overlap.
        step = max(1.0, radius / 2.0)
        steps = max(1, int(dist / step))
        for i in range(1, steps + 1):
            t = i / float(steps)
            self.erase_pixels(x1 + dx * t, y1 + dy * t, radius)
        # One push per mouse move, not per disc.
        self.sync()

    def eraser_radius(self):
        """The eraser's radius, in the same units the ring is drawn in.

        WARNING: this used to be `max(6, pen_width * 2.5) / 2`, which is 2.5x the
        pen's half-width -- the erased hole was far bigger than the circle the
        user was aiming with, so erasing felt like it grabbed at random. The ring
        IS the cursor now, so the eraser uses exactly its radius: what you see
        under the pointer is what gets removed.
        """
        return self._pen_radius()

    def erase_pixels(self, x, y, radius):
        """Fill a disc with the board's background colour.

        The screen is not pushed here: a sweep calls this many times per mouse
        move, and one push after the whole path is both faster and smoother than
        one per disc.
        """
        if self.surface is None:
            return
        try:
            r = max(2.0, float(radius))
            # Erase from BOTH: the visible surface and the ink layer. Missing the
            # layer would make the erased ink come back on the next repaint (the
            # layer is what repaint pastes down), so the eraser would look broken
            # the moment the agent drew anything.
            box = [x - r, y - r, x + r, y + r]
            self.draw.ellipse(box, fill=(0, 0, 0, BG_ALPHA))
            ink = getattr(self, "ink_draw", None)
            if ink is not None:
                ink.ellipse(box, fill=(0, 0, 0, 0))
        except Exception as e:
            self.note("erase_pixels failed: %s" % e)

    def render_ink(self):
        """Rebuild the board's pixels from the record, once.

        The ink is pixels, so anything that repaints the surface has to put the
        strokes back: they are not shapes waiting to be redrawn by the canvas, and
        without this the user's drawing disappeared the moment the agent added a
        mark of its own.

        Texts are deliberately NOT painted here. They are their own windows (see
        _make_text_window), so they are not pixels and never enter this buffer --
        that is what keeps them selectable and extractable.
        """
        self.repaint()

    def load_user(self):
        """Read back the user's strokes from user.json.

        Done once at startup: the file is the board's own output, so a restart
        inherits what was already drawn instead of starting blank.
        """
        doc = read_json(self.args.users) if self.args.users else None
        if not isinstance(doc, dict):
            return
        strokes = []
        for s in doc.get("strokes") or []:
            if not isinstance(s, dict):
                continue
            pts = s.get("points") or []
            if not isinstance(pts, list) or len(pts) < 2:
                continue
            strokes.append({
                "id": str(s.get("id") or ""),
                "width": int(s.get("width") or self.pen_width),
                "points": pts,
            })
        self.strokes = strokes
        texts = []
        for t in doc.get("texts") or []:
            if not isinstance(t, dict):
                continue
            label = str(t.get("text") or "").strip()
            if not label:
                continue
            texts.append({"id": str(t.get("id") or ""),
                          "text": label,
                          "x": float(t.get("x") or 0),
                          "y": float(t.get("y") or 0),
                          "size": int(t.get("size") or TEXT_SIZE),
                          "w": int(t.get("w") or 0),
                          "h": int(t.get("h") or 0)})
        self.texts = texts
        # A text is a window, so restoring it means building the window again.
        # Numbers keep counting from where the saved boxes ended, otherwise a
        # restart would renumber the existing boxes and the agent would be told
        # the wrong thing about which box is which.
        self.text_seq = 0
        for t in self.texts:
            try:
                n = int(str(t.get("id") or "").lstrip("t").split("-")[0])
                self.text_seq = max(self.text_seq, n)
            except Exception:
                pass
        try:
            self.user_rev = int(doc.get("revision") or 0)
        except Exception:
            self.user_rev = 0
        hidden = doc.get("hidden")
        if isinstance(hidden, list):
            self.hidden = [str(h) for h in hidden]
        if strokes:
            self.note("loaded %d stroke(s)" % len(strokes))

    def save_user(self):
        """Publish the user's strokes and texts so the agent can see them."""
        try:
            payload = {
                "revision": self.user_rev,
                "strokes": [{"id": s.get("id"), "width": s.get("width"),
                             "color": self.text_hex(), "points": s.get("points")}
                            for s in self.strokes],
                "texts": [{"id": t.get("id"), "text": t.get("text"),
                           "x": t.get("x"), "y": t.get("y"),
                           "size": t.get("size"),
                           "w": t.get("w"), "h": t.get("h")}
                          for t in self.texts],
                "hidden": list(self.hidden),
            }
            self.user_rev += 1
            payload["revision"] = self.user_rev
            write_text(self.args.users, json.dumps(payload))
        except Exception as e:
            self.note("save-user failed: %s" % e)

    def clear_all(self):
        """Wipe everything on the board and tell the Host it happened.

        This is what every "one-click clear" reaches: the whale's menu entry, the
        tray entry and the board's own menu. The old WPF Clear-All did the same
        four things, and all four are needed here:

          * drop the user's strokes
          * drop the agent's marks (and the erase bookkeeping with them)
          * publish the empty state to user.json
          * touch the clear signal, which is how the Host learns the user cleared
            the board and stops re-drawing what was just removed

        WARNING: clearing only the scene is not a clear. That was the state of
        this board -- the agent's marks went, the user's ink stayed, and the Host
        kept its own copy of the scene -- so "one-click clear" appeared to do
        nothing at all.

        WARNING: clearing is a pixel operation. Filling the buffer with the board's
        background removes the user's ink and the agent's marks at once, with
        nothing to iterate over and nothing to rebuild. The earlier version walked
        the shape lists and deleted them one by one, which meant a clear could
        leave behind anything the lists had lost track of.
        """
        try:
            if self.surface is not None:
                self.draw.rectangle(
                    [0, 0, self.screen[2], self.screen[3]],
                    fill=(0, 0, 0, BG_ALPHA))
            # The ink layer is the permanent record repaint pastes back, so it has
            # to be cleared too or "clear" would come back on the next repaint.
            ink = getattr(self, "ink_draw", None)
            if ink is not None:
                ink.rectangle([0, 0, self.screen[2], self.screen[3]],
                              fill=(0, 0, 0, 0))
        except Exception as e:
            self.note("clear fill failed: %s" % e)
        # The text boxes are windows, not pixels: the fill above cannot remove
        # them, so they are destroyed explicitly or "clear" would leave the words
        # floating over an empty board.
        self.clear_text_windows()
        self.strokes = []
        self.ink_items = []
        self.active_points = []
        self.active_item = None
        self.texts = []
        self.hidden = []
        self.scene_items = {}
        self.canvas.clear_shapes()
        self.sync()
        self.save_user()
        try:
            write_text(self.args.clear_signal,
                       str(int(time.time() * 1000)))
        except Exception:
            pass
        self.note("cleared by user")

    def open_board_menu(self, event):
        """Right-click menu inside the board.

        Same four entries as the WPF board: text box, call the agent, clear
        everything, and leave draw mode. Each one posts the intent through the
        same channel the whale uses, so there is one code path per action.

        Right-click ON a text box is different: the menu is about that box, so it
        offers edit and delete instead of the board-wide actions.
        """
        if self.mode != "ink":
            return
        # Opening the menu abandons whatever gesture was in progress.
        self.active_points = []
        self.active_item = None
        self.menu_at = (float(event.x), float(event.y))
        hit = self.text_at(float(event.x), float(event.y))
        if hit is not None:
            self.open_text_menu(hit, int(event.x_root), int(event.y_root))
            return
        try:
            self.menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            try:
                self.menu.grab_release()
            except Exception:
                pass

    def open_text_menu(self, entry, x_root, y_root):
        """The menu for one text box: edit it, or delete it.

        Built fresh on every right-click so the labels stay localised and the
        closure captures THIS box, not whichever box was last hit.
        """
        try:
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label=self.labels.get("editText", "Edit text"),
                          command=lambda: self.edit_text_box(entry))
            m.add_command(label=self.labels.get("deleteText", "Delete"),
                          command=lambda: self.delete_text_box(entry))
            m.tk_popup(x_root, y_root)
        finally:
            try:
                self.menu.grab_release()
            except Exception:
                pass

    # -- the user's text boxes ------------------------------------------------
    #
    # A text box is a container, not ink. Two things follow from that and both are
    # deliberate:
    #
    #   * it is a real toplevel window with a real Text widget in it, so the
    #     characters stay characters -- selectable, copyable, and readable back out
    #     at any time;
    #   * it is NOT painted into the board's pixel buffer, so erasing ink cannot
    #     cut a hole through it and a repaint cannot wipe it.
    #
    # WARNING: an earlier version placed a tk.Entry INSIDE the board window. The
    # board's visible pixels come from UpdateLayeredWindow, which paints over
    # everything Tk draws in that window -- the Entry existed and accepted keys and
    # was completely invisible. A separate toplevel is a separate HWND, so Windows
    # composites it and the board's bitmap cannot cover it.

    def open_text_editor(self, x, y):
        """Drop an empty text box at the point the user right-clicked.

        The box is editable while it is open: Enter inserts a newline (so text can
        be multi-line), Esc throws it away, and clicking anywhere outside commits
        it. Committing does not paint anything -- the box simply stops being an
        editor and stays a container.
        """
        self.close_text_editor()
        try:
            win, text = self._make_text_window(x, y, "")
            if win is None:
                return
            self.text_editor = {"win": win, "text": text,
                                "x": float(x), "y": float(y)}
            text.focus_set()
            self._focus_text_window(win)
            # Enter is a newline here, not a commit: a box holds as many lines as
            # the user wants. Only a click outside (or Esc to discard) ends it.
            text.bind("<Escape>", lambda _e: (self.close_text_editor(), "break")[1])
            # The box must reshape itself as the user types. Binding the text
            # widget's own modify event covers every way text can change --
            # typing, pasting, deleting, undo -- instead of one key at a time.
            text.bind("<<Modified>>", lambda _e: self._on_editor_changed())
            text.edit_modified(False)
            self._fit_text_window(win, text)
            self.note("text editor opened at %d,%d" % (x, y))
        except Exception as e:
            self.note("text editor failed: %s" % e)

    def _on_editor_changed(self):
        """Reshape the open editor to fit what has just been typed."""
        editor = self.text_editor
        if editor is None:
            return
        try:
            editor["text"].edit_modified(False)
            self._fit_text_window(editor["win"], editor["text"])
        except Exception as e:
            self.note("text reshape failed: %s" % e)

    def commit_text_editor(self):
        """Freeze the open editor into a numbered text box.

        The box keeps its window; only the editing stops. Nothing is re-painted
        into the board's pixels -- the text was never pixels to begin with.
        """
        editor = self.text_editor
        if editor is None:
            return
        try:
            body = editor["text"].get("1.0", "end-1c").strip()
        except Exception:
            body = ""
        x = editor["x"]
        y = editor["y"]
        win = editor["win"]
        existing = editor.get("entry")
        self.text_editor = None
        if not body:
            # An empty box is not content: drop the window instead of leaving a
            # blank frame sitting on the user's screen. For a box that was edited
            # down to nothing, that means the box is gone entirely -- deleting by
            # emptying is still deleting, and it is the fastest way to do it.
            try:
                win.destroy()
            except Exception:
                pass
            if existing is not None:
                try:
                    self.texts.remove(existing)
                except Exception:
                    pass
                try:
                    self.text_wins.pop(existing["id"], None)
                except Exception:
                    pass
                self.save_user()
            self.note("text editor closed empty")
            return
        if existing is not None:
            # This commit comes from editing an existing box: same box, same id,
            # same number -- only the words and the shape change.
            entry = existing
            entry["text"] = body
            entry["_widget"] = editor["text"]
        else:
            self.text_seq += 1
            entry = {
                "id": "t%d-%d" % (self.text_seq, int(time.time() * 1000)),
                "seq": self.text_seq,
                "text": body,
                "x": float(x),
                "y": float(y),
                "size": TEXT_SIZE,
                "w": 0,
                "h": 0,
                "_widget": editor["text"],
            }
            self.texts.append(entry)
        # The window the user typed into becomes the committed box, resized to fit
        # exactly what is in it. Rebuilding it would lose the selection and look
        # like a flicker for no reason.
        self._finish_text_window(win, entry, editable=False)
        entry.pop("_widget", None)
        # The grip was hidden while the box was being edited; bring it back, then
        # the mode decides whether it catches the mouse.
        grip = self.grips.get(entry["id"])
        if grip is not None:
            try:
                grip.deiconify()
            except Exception:
                pass
        # A new box must obey the mode it was created in, or a box committed in
        # draw mode would keep eating clicks after the user stops drawing.
        # WARNING: always click-through, whatever the mode. A box that eats
        # clicks swallows the pen's strokes in draw mode, and in idle mode the
        # board is meant to be out of the way. The ONLY thing that may make a box
        # interactive is idle+Shift (see _tick_text_readable), so a box behaves
        # the same whether the agent wrote it or the user did.
        self._set_text_window_through(win, False)
        self.save_user()
        self.note("text committed: box %d, %d chars"
                  % (entry["seq"], len(body)))

    def close_text_editor(self, keep_existing=False):
        """Abandon the open editor without committing it.

        Two very different cases share this entry point:

          * a FRESH box (typed, not yet committed): there is no record and no
            word on screen except inside this window, so discarding means
            destroying the window;
          * an EXISTING box being edited: the words live on in the record and
            were on screen before the edit began. Discarding must NOT destroy
            the window -- that would take the user's text with it, which is
            exactly the bug this guards against. The window just goes back to
            how it was before the edit: committed look, old text.
        """
        editor = self.text_editor
        if editor is None:
            return
        self.text_editor = None
        existing = editor.get("entry")
        if keep_existing and existing is not None:
            self._restore_text_window(editor, existing)
            return
        try:
            editor["win"].destroy()
        except Exception:
            pass

    def _restore_text_window(self, editor, entry):
        """Put an edited box back the way it was, unchanged.

        The text the user may have half-changed is reverted to the record, the
        committed (transparent) look comes back, and the window stays on screen.
        Esc means "leave it as it was", not "throw my text away".
        """
        win = editor["win"]
        text = editor["text"]
        try:
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", str(entry.get("text") or ""))
            text.configure(state="normal", bg=TEXT_COMMIT_BG)
            _make_read_only(text)
        except Exception as e:
            self.note("text revert failed: %s" % e)
        try:
            for child in win.winfo_children():
                child.configure(bg=TEXT_COMMIT_BG,
                                highlightbackground=TEXT_COMMIT_BORDER,
                                highlightcolor=TEXT_COMMIT_BORDER)
            win.configure(bg=TEXT_COMMIT_BG)
            h = top_level_of(int(win.winfo_id()))
            style = get_ex_style(h)
            if not style & WS_EX_LAYERED:
                set_ex_style(h, style | WS_EX_LAYERED)
            win.attributes("-transparentcolor", TEXT_COMMIT_BG)
        except Exception as e:
            self.note("text restyle failed: %s" % e)
        try:
            self._fit_text_window(win, text, entry)
        except Exception as e:
            self.note("text refit failed: %s" % e)
        self._set_text_window_through(win, False)
        self.note("text box %d edit cancelled" % entry.get("seq"))

    def edit_text_box(self, entry):
        """Re-open a committed box for editing: Ctrl+Shift+click, or the box menu.

        The box's own window becomes the editor again -- no rebuild, so the words
        do not flicker and the position is exactly where the box already is. The
        editing look comes back (backing, border, caret), the text becomes
        writable, and a click outside commits it like any new box.

        WARNING: the entry keeps its id and seq on purpose. Editing is not
        re-creating: the box the agent already saw stays the same box, and only
        its text and shape change.
        """
        if self.text_editor is not None:
            self.commit_text_editor()
        try:
            win = self.text_wins.get(entry["id"])
            text = None
            # The Text widget sits inside the border frame, not directly under the
            # toplevel, so the walk has to go one level deeper.
            for child in win.winfo_children():
                if child.winfo_class() == "Text":
                    text = child
                    break
                for grandchild in child.winfo_children():
                    if grandchild.winfo_class() == "Text":
                        text = grandchild
                        break
                if text is not None:
                    break
            if win is None or text is None:
                self.note("text edit failed: window gone")
                return
            # Back to the editing look: the committed style was the transparent
            # chroma; the editor needs its visible backing and border again.
            text.configure(state="normal", bg=TEXT_BG)
            for child in win.winfo_children():
                child.configure(bg=TEXT_BG,
                                highlightbackground=TEXT_BORDER,
                                highlightcolor=TEXT_BORDER)
            win.configure(bg=TEXT_BG)
            try:
                win.attributes("-transparentcolor", "")
            except Exception:
                pass
            self.text_editor = {"win": win, "text": text,
                                "x": float(entry["x"]), "y": float(entry["y"]),
                                "entry": entry}
            # The grip would fight the editor: its ring sits over the same edges
            # the user may want to sweep text selection across, and a drag from
            # the ring would move a box mid-edit. It comes back on commit.
            grip = self.grips.get(entry["id"])
            if grip is not None:
                try:
                    grip.withdraw()
                except Exception:
                    pass
            # Reshape live while the user edits, same contract as a fresh box.
            #
            # WARNING: Esc here must KEEP the box. This window IS the committed
            # box -- destroying it takes the user's text off the screen and out
            # of the record. Esc while editing an existing box means "cancel the
            # edit": the words go back to what they were, the window stays.
            text.bind("<Escape>", lambda _e: (self.close_text_editor(True), "break")[1])
            text.bind("<<Modified>>", lambda _e: self._on_editor_changed())
            text.edit_modified(False)
            text.focus_set()
            self._focus_text_window(win)
            # The caret goes where the user will start changing things: the end.
            text.mark_set("insert", "end-1c")
            self.note("text box %d opened for edit" % entry["seq"])
        except Exception as e:
            self.note("text edit failed: %s" % e)

    def delete_text_box(self, entry):
        """Remove one text box: its window, its record, and its saved state."""
        try:
            grip = self.grips.pop(entry["id"], None)
            if grip is not None:
                grip.destroy()
        except Exception as e:
            self.note("text delete grip failed: %s" % e)
        try:
            win = self.text_wins.pop(entry["id"], None)
            if win is not None:
                win.destroy()
        except Exception as e:
            self.note("text delete window failed: %s" % e)
        try:
            self.texts.remove(entry)
        except Exception:
            pass
        self.save_user()
        self.note("text box %d deleted" % entry.get("seq"))

    def _focus_text_window(self, win):
        """Give a text window the keyboard.

        WARNING: the board is created WS_EX_NOACTIVATE and sits topmost, so a plain
        focus_set() inside the new toplevel is not enough -- the toplevel must be
        activated and the board must step out of the way for the duration. Without
        this, typing went to the board (or nowhere) and the box looked dead.
        """
        try:
            win.deiconify()
            win.lift()
            win.attributes("-topmost", True)
            win.focus_force()
        except Exception as e:
            self.note("text focus failed: %s" % e)
        try:
            h = clean_handle(win.winfo_id())
            top = top_level_of(h)
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow(wintypes.HWND(top))
            user32.SetFocus.argtypes = [wintypes.HWND]
            user32.SetFocus(wintypes.HWND(top))
        except Exception as e:
            self.note("text focus win32: %s" % e)

    def text_hex(self):
        """The colour a NEW text box gets: the pen colour, as a hex string."""
        return "#ffffff" if getattr(self, "ink_white", False) else "#000000"

    def _make_text_window(self, x, y, body, width=0, height=0):
        """Create one text box window that hugs its text.

        The box is sized by its CONTENT, not by a fixed frame: an empty box is a
        thin strip a few characters wide, and it grows as characters arrive -- wider
        as a line gets longer, taller as lines are added. There is no scrolling
        area and no fixed canvas; the window is exactly as big as the text in it,
        which is what a text box is in any drawing program.

        WARNING: the earlier version gave every box a fixed 360x96 frame with a
        Text widget that scrolled inside it. That is a text AREA, not a text box:
        the user asked for the frame to follow the words.

        Returns (win, text_widget).
        """
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            # The backing pairs with the ink: white text on a dark box, black text
            # on a light one. A fixed dark backing made black-on-black.
            ink = self.text_hex()
            bg = TEXT_BG if ink == "#ffffff" else TEXT_BG_LIGHT
            win.configure(bg=bg)
            win.attributes("-topmost", True)
            frame = tk.Frame(win, bg=bg,
                             highlightbackground=TEXT_BORDER,
                             highlightcolor=TEXT_BORDER,
                             highlightthickness=1)
            frame.pack(fill="both", expand=True)
            text = tk.Text(
                frame, bg=bg, fg=ink,
                insertbackground=ink,
                selectbackground="#3a3a48", selectforeground="#ffffff",
                relief="flat", bd=0, wrap="char",
                font=TEXT_FONT, padx=TEXT_PAD_X, pady=TEXT_PAD_Y,
                width=1, height=1,
                # WARNING: no scrollbar and no fixed size. A box that scrolls is a
                # box whose frame no longer matches its words.
                highlightthickness=0, borderwidth=0,
            )
            text.pack(fill="both", expand=True)
            if body:
                text.insert("1.0", body)
            win.geometry("+%d+%d" % (int(x), int(y)))
            return win, text
        except Exception as e:
            self.note("text window failed: %s" % e)
            return None, None

    def _fit_text_window(self, win, text, entry=None):
        """Size the window to exactly the text inside it, and report the size.

        The measurement is the whole point of a text box: the widest line sets the
        width, the line count sets the height. Nothing is clamped to a minimum
        frame, so an empty box really is a thin strip and a one-word box really is
        one word wide.

        Wrapping: a line longer than TEXT_MAX_W wraps (the widget's wrap mode does
        that itself), so the width stops growing there and the height takes over --
        which is the same rule a drawing program follows.
        """
        import tkinter.font as tkfont
        font = tkfont.Font(font=text.cget("font"))
        body = text.get("1.0", "end-1c")

        # The widest line as typed, before wrapping.
        widest = 0
        for line in body.split("\n"):
            try:
                widest = max(widest, font.measure(line))
            except Exception:
                pass

        # Width: hug the text, clamped to the wrapping limit. One extra character
        # of room so the caret is never flush against the border.
        w = widest + TEXT_PAD_X * 2 + font.measure("0") // 2 + 4
        w = int(max(TEXT_MIN_W, min(TEXT_MAX_W, w)))

        # Height: ask the widget, do not compute it from the font. Tk knows its own
        # line spacing, padding and border; a hand-computed line height was off by
        # a third of a line and made an empty box look like a thick frame.
        lines = max(1, body.count("\n") + 1)
        try:
            # Wrapping means one typed line can occupy several display lines, so the
            # widget is made the final width first and then asked how many lines it
            # actually uses.
            win.geometry("%dx1" % w)
            win.update_idletasks()
            shown = int(text.count("1.0", "end-1c", "displaylines") or 0)
            if shown > 0:
                lines = shown
            text.configure(height=lines)
            win.update_idletasks()
            h = int(text.winfo_reqheight()) + 2
        except Exception:
            h = int(font.metrics("linespace")) * lines + TEXT_PAD_Y * 2 + 4

        win.geometry("%dx%d+%d+%d" % (w, h,
                                      int(entry["x"]) if entry else win.winfo_x(),
                                      int(entry["y"]) if entry else win.winfo_y()))
        if entry is not None:
            entry["w"] = w
            entry["h"] = h
            # The grip is a ring around the box: its size and place follow the
            # box, or an edited box's new shape would keep the old ring.
            grip = self.grips.get(entry["id"]) if entry.get("id") else None
            if grip is not None:
                try:
                    grip.geometry("%dx%d+%d+%d"
                                  % (w + TEXT_HOT * 2, h + TEXT_HOT * 2,
                                     int(entry["x"]) - TEXT_HOT,
                                     int(entry["y"]) - TEXT_HOT))
                    hRgn = GDI32.CreateRectRgn(0, 0, w + TEXT_HOT * 2,
                                               h + TEXT_HOT * 2)
                    inner = GDI32.CreateRectRgn(TEXT_HOT, TEXT_HOT,
                                                TEXT_HOT + w, TEXT_HOT + h)
                    GDI32.CombineRgn(hRgn, hRgn, inner, 4)  # RGN_DIFF
                    GDI32.DeleteObject(inner)
                    user32.SetWindowRgn(
                        wintypes.HWND(top_level_of(int(grip.winfo_id()))),
                        hRgn, True)
                except Exception as e:
                    self.note("grip refit failed: %s" % e)
        return w, h

    def _finish_text_window(self, win, entry, editable):
        """Turn the editor into a committed text, leaving only the words visible.

        Committing strips the container's look: the dark backing and the red
        border go away, and what stays on screen is exactly the red characters --
        the user asked for text, not for a framed label parked on their desktop.

        The window itself survives, because it IS the container: the text inside is
        still selectable and copyable, and still readable back out for the agent.
        It is transparent now, so it is invisible -- except for its words.
        """
        try:
            text = entry["_widget"]
        except Exception:
            text = None
        try:
            if text is not None:
                # WARNING: state="disabled" is NOT the way to make a committed box
                # read-only. A disabled Tk Text cannot be selected from at all --
                # measured: dragging over it selected nothing, so the user's
                # Shift+drag gesture did nothing and the words could not be
                # copied. The box must stay state="normal" and instead swallow the
                # editing keys, which leaves selection working.
                text.configure(state="normal", bg=TEXT_COMMIT_BG)
                _make_read_only(text)
        except Exception:
            pass
        try:
            # The frame draws the border; pointing it at the chroma colour removes
            # the visible box without touching the layout.
            for child in win.winfo_children():
                child.configure(bg=TEXT_COMMIT_BG,
                                highlightbackground=TEXT_COMMIT_BORDER,
                                highlightcolor=TEXT_COMMIT_BORDER)
            win.configure(bg=TEXT_COMMIT_BG)
            # WARNING: Tk's -transparentcolor silently does nothing when the
            # toplevel's parent is an overrideredirect window -- measured: a
            # toplevel under the board's frameless root shows one solid block of
            # the chroma colour, while the same toplevel under a withdrawn normal
            # root turns out transparent. Tk never puts WS_EX_LAYERED on the child
            # in that case, and the colour key cannot work without it. So the
            # layered style is put on the window's own HWND here, by hand.
            h = top_level_of(int(win.winfo_id()))
            style = get_ex_style(h)
            if not style & WS_EX_LAYERED:
                set_ex_style(h, style | WS_EX_LAYERED)
            win.attributes("-transparentcolor", TEXT_COMMIT_BG)
        except Exception as e:
            self.note("text restyle failed: %s" % e)
        # Fit the frame to the words.
        try:
            self._fit_text_window(win, text, entry)
        except Exception as e:
            self.note("text resize failed: %s" % e)
        self.text_wins[entry["id"]] = win
        # WARNING: no drag grip any more. A committed box used to carry a
        # near-invisible edge ring whose only job was to be draggable, and the user
        # asked for the box to stop moving: text placed on the board is meant to
        # stay where it was put. The grip also swallowed clicks at the edges, which
        # is now the eraser's job.
        # Esc while a committed box has focus must still leave draw mode, and a
        # click on the box must not be mistaken for a click on the board.
        try:
            win.bind("<Escape>", lambda _e: self.on_escape())
        except Exception:
            pass
        # WARNING: raise the box above the board. Both windows are topmost, and
        # among topmost windows the most recently raised wins -- measured: a box
        # created before the board's next raise ended up BEHIND the board's bitmap,
        # so its words vanished. Text is content on the board, never under it.
        try:
            set_topmost(clean_handle(top_level_of(int(win.winfo_id()))))
        except Exception:
            pass
        self.note("text box %d placed at %d,%d"
                  % (entry["seq"], entry["x"], entry["y"]))

    # -- the drag grip ---------------------------------------------------------
    #
    # A committed box is transparent except for its glyphs, and the mouse falls
    # THROUGH the transparent parts -- so a press "on the box" usually lands on
    # whatever is underneath, and the drag written against the board's input
    # layer never fires. The fix is a dedicated grip: a nearly invisible ring
    # around the box that catches the mouse at the edges, shows the move cursor,
    # and drags from there. The ring's hit region is the border band only -- the
    # centre is carved out, so selecting the words still reaches the Text widget.

    def _make_text_grip(self, entry, win):
        """Give one committed box its edge hot zone, replacing any old one."""
        try:
            old = getattr(entry, "get", lambda _k, _d=None: None)("grip")
            if old is not None:
                try:
                    old.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            w = int(entry.get("w") or 0)
            h = int(entry.get("h") or 0)
            if w <= 0 or h <= 0:
                return
            grip = tk.Toplevel(self.root)
            grip.overrideredirect(True)
            grip.configure(bg=TEXT_GRIP_BG)
            try:
                grip.attributes("-alpha", TEXT_GRIP_ALPHA)
            except Exception:
                pass
            grip.attributes("-topmost", True)
            # The window must exist on screen before its region is set: a mapped
            # HWND gets the region, a freshly created one can drop it on the
            # first geometry change (measured: SetWindowRgn returned 1 but the
            # handle reported no region when the window had not been mapped).
            grip.geometry("%dx%d+%d+%d" % (w + TEXT_HOT * 2, h + TEXT_HOT * 2,
                                           int(entry["x"]) - TEXT_HOT,
                                           int(entry["y"]) - TEXT_HOT))
            grip.update_idletasks()
            # The ring: the whole window's region minus the inner rectangle. Only
            # the band around the box catches the mouse; the centre is a hole, so
            # clicks there fall through to the Text widget underneath.
            hRgn = GDI32.CreateRectRgn(0, 0, w + TEXT_HOT * 2, h + TEXT_HOT * 2)
            inner = GDI32.CreateRectRgn(TEXT_HOT, TEXT_HOT,
                                        TEXT_HOT + w, TEXT_HOT + h)
            GDI32.CombineRgn(hRgn, hRgn, inner, 4)  # RGN_DIFF
            GDI32.DeleteObject(inner)
            # WARNING: SetWindowRgn lives in USER32, not GDI32 -- the region is
            # made by GDI but applied by the window manager. Calling it on GDI32
            # raises AttributeError and the grip silently failed to take shape.
            hwnd = top_level_of(int(grip.winfo_id()))
            ok = user32.SetWindowRgn(wintypes.HWND(hwnd), hRgn, True)
            if not ok:
                self.note("grip region rejected hwnd=0x%08x" % hwnd)
            # The grip belongs to the box: it moves when the box moves, and it
            # starts the drag through the same state the board's handlers use.
            grip.bind("<ButtonPress-1>", lambda e: self._grip_press(entry, e))
            grip.bind("<B1-Motion>", self._grip_drag)
            grip.bind("<ButtonRelease-1>", self._grip_release)
            grip.bind("<ButtonPress-3>", lambda e: self.open_text_menu(
                entry, int(e.x_root), int(e.y_root)))
            try:
                # The move cursor says this edge drags; the text underneath keeps
                # its own cursors (I-beam from the Text widget).
                grip.configure(cursor="fleur")
            except Exception:
                pass
            entry["grip"] = grip
            self.grips[entry["id"]] = grip
            self._set_grip_through(grip, self.mode != "ink")
        except Exception as e:
            self.note("text grip failed: %s" % e)

    def _grip_press(self, entry, event=None):
        if self.mode != "ink":
            return
        if self.text_editor is not None:
            self.commit_text_editor()
        x, y = self._pointer_at(event)
        # The offset keeps whatever part of the box the press landed on under the
        # pointer, so the box does not jump when the drag starts.
        self.text_drag = {"entry": entry,
                          "off_x": x - entry["x"],
                          "off_y": y - entry["y"]}

    def _grip_drag(self, event):
        if self.text_drag is None:
            return
        entry = self.text_drag["entry"]
        x, y = self._pointer_at(event)
        self._move_text_window(entry,
                               x - self.text_drag["off_x"],
                               y - self.text_drag["off_y"])

    def _grip_release(self, _event):
        if self.text_drag is None:
            return
        entry = self.text_drag["entry"]
        self.text_drag = None
        self.save_user()
        self.note("text box %d moved to %d,%d"
                  % (entry.get("seq"), entry["x"], entry["y"]))

    def _pointer_at(self, event):
        """The pointer's screen position, from the event when it has one.

        A Tk motion event carries x_root/y_root, which is the honest position of
        THIS gesture; falling back to GetCursorPos reads wherever the pointer is
        now, which is only right while a real drag is in flight.
        """
        if event is not None:
            try:
                return float(event.x_root), float(event.y_root)
            except Exception:
                pass
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return float(pt.x), float(pt.y)

    def _set_grip_through(self, grip, through):
        """Idle means the grip lets the mouse pass; drawing means it catches."""
        try:
            h = top_level_of(int(grip.winfo_id()))
            style = get_ex_style(h)
            if through:
                style = style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            else:
                style = style & ~WS_EX_TRANSPARENT & ~WS_EX_NOACTIVATE
            set_ex_style(h, style)
        except Exception as e:
            self.note("grip through failed: %s" % e)

    def restore_text_windows(self):
        """Rebuild the windows for texts loaded from user.json.

        A text is a window, so it does not survive as pixels: reopening the board
        has to create the window again or the user's words would silently vanish
        while the strokes came back.
        """
        for old in list(self.text_wins.values()):
            try:
                old.destroy()
            except Exception:
                pass
        self.text_wins = {}
        for entry in list(self.texts):
            try:
                win, text = self._make_text_window(
                    entry.get("x") or 0, entry.get("y") or 0,
                    str(entry.get("text") or ""))
                if win is None:
                    continue
                entry["_widget"] = text
                self._finish_text_window(win, entry, editable=False)
                entry.pop("_widget", None)
            except Exception as e:
                self.note("text restore failed: %s" % e)

    def text_at(self, x, y):
        """The committed box under a screen point, if any.

        Used so a press on a box is routed to the box rather than to the ink layer.
        """
        for entry in reversed(self.texts):
            try:
                w = int(entry.get("w") or 0)
                h = int(entry.get("h") or 0)
                if w <= 0 or h <= 0:
                    continue
                if (entry["x"] <= x <= entry["x"] + w
                        and entry["y"] <= y <= entry["y"] + h):
                    return entry
            except Exception:
                continue
        return None

    def _erase_text_at(self, x, y):
        """Remove any text box the eraser passes over.

        A box is a window, not pixels, so erase_pixels cannot reach it. This is
        the bridge: same gesture, same footprint, so the user does not have to
        remember that text needs a different key.
        """
        entry = self.text_at(x, y)
        if entry is None:
            return
        try:
            self.delete_text_box(entry)
            self.note("text box %d erased" % entry.get("seq", 0))
        except Exception as e:
            self.note("text erase failed: %s" % e)

    # -- reading a box back ----------------------------------------------------
    # A committed box is not a picture of text: the characters are still a real
    # text control, so they can be selected and copied. The catch is that while
    # drawing the board owns the mouse, and a press on a box would start a stroke
    # on top of it.
    #
    # So reading is an IDLE-mode gesture: leave draw mode, hold Shift, and the
    # boxes become selectable -- the pointer turns into an I-beam and dragging
    # selects words, which is the same muscle memory as any text field.
    #
    # WARNING: this used to be a 2.5 s hover in DRAW mode. The user rejected it:
    # waiting two and a half seconds to read a word is worse than a mode switch,
    # and the hover also made the pen feel unresponsive over text.
    #
    # Right-click still offers delete and edit, so a box is not stranded.

    def _shift_down(self):
        """Whether Shift is held right now, read from the OS.

        Same reason as CapsLock: the board usually does not hold keyboard focus,
        so a key event would never arrive.
        """
        try:
            # WARNING: GetAsyncKeyState, not GetKeyState. GetKeyState is
            # thread-local: a window that does not hold focus never receives the
            # key, so it kept reporting Shift as HELD and every text box was made
            # interactive on every tick -- measured: "text boxes readable" repeated
            # every few seconds with no key pressed, and the boxes could not be
            # made inert again.
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            # 0x10 is VK_SHIFT; the high bit means "currently down".
            return bool(user32.GetAsyncKeyState(0x10) & 0x8000)
        except Exception:
            return False

    def _tick_text_readable(self):
        """Keep every committed box selectable.

        WARNING: this used to require idle+Shift, and that was the wrong design.
        The user's own boxes were selectable without any modifier while the agent's
        were not, so the same kind of text behaved two different ways -- reported as
        "I can select mine but not yours". A committed box IS readable text; there
        is no reason to hide that behind a chord.

        So the boxes are simply left interactive, and this tick only repairs a box
        that drifted (a mode change or a restyle can clear the flag).
        """
        if getattr(self, "_read_active", None) == "always":
            return
        self._read_active = "always"
        for entry in getattr(self, "texts", []) or []:
            win = self.text_wins.get(entry.get("id"))
            if win is None:
                continue
            try:
                self._set_text_window_through(win, False)
                widget = entry.get("_widget")
                if widget is not None:
                    widget.configure(cursor="xterm")
            except Exception:
                continue

    def _begin_text_readable(self):
        """Make every committed box interactive so Shift+drag selects words."""
        try:
            self._read_active = True
            for entry in self.texts:
                win = self.text_wins.get(entry["id"])
                if win is None:
                    continue
                widget = entry.get("_widget")
                if widget is not None:
                    try:
                        widget.configure(cursor="xterm")
                    except Exception:
                        pass
                self._set_text_window_through(win, False)
            self.note("text boxes readable (shift, idle)")
        except Exception as e:
            self.note("text readable failed: %s" % e)

    def _end_text_readable(self):
        """Put the boxes back to sleep: inert and click-through again."""
        try:
            self._read_active = None
            for entry in self.texts:
                win = self.text_wins.get(entry["id"])
                if win is None:
                    continue
                self._set_text_window_through(win, True)
                widget = entry.get("_widget")
                if widget is not None:
                    try:
                        widget.configure(cursor="arrow")
                    except Exception:
                        pass
        except Exception:
            pass

    def _move_text_window(self, entry, x, y):
        """Place one text box window at a new point and update its record.

        The whole box moves -- window, record and everything -- so a move is just
        two numbers changing. The record is saved once, on release, not here: a
        drag generates hundreds of moves and the file would churn for nothing.
        """
        try:
            x = float(x)
            y = float(y)
            win = self.text_wins.get(entry["id"])
            if win is not None:
                win.geometry("+%d+%d" % (int(x), int(y)))
            grip = self.grips.get(entry["id"])
            if grip is not None:
                try:
                    grip.geometry("+%d+%d" % (int(x) - TEXT_HOT, int(y) - TEXT_HOT))
                except Exception:
                    pass
            entry["x"] = x
            entry["y"] = y
        except Exception as e:
            self.note("text move failed: %s" % e)

    def clear_text_windows(self):
        """Drop every committed text window, without touching the board's pixels."""
        for grip in list(self.grips.values()):
            try:
                grip.destroy()
            except Exception:
                pass
        self.grips = {}
        for win in list(self.text_wins.values()):
            try:
                win.destroy()
            except Exception:
                pass
        self.text_wins = {}
        self.close_text_editor()

    def set_text_windows_active(self, interactive):
        """Keep the boxes visible always; let the mouse through when idle.

        WARNING: hiding the boxes when the board went idle looked right and was
        wrong. The user summons the agent from idle, so the screenshot taken at
        that moment would have contained no text boxes -- the user's own words
        would be missing from the very picture they asked the agent to look at.

        So the boxes stay on screen in both modes, the way the ink does, and only
        their mouse behaviour follows the mode: idle means the user is working on
        their desktop, so a box must not swallow clicks meant for the window
        underneath; draw mode means the boxes are part of the board and their text
        must be selectable.
        """
        for win in list(self.text_wins.values()):
            self._set_text_window_through(win, not interactive)
        for grip in list(self.grips.values()):
            self._set_grip_through(grip, not interactive)
        editor = self.text_editor
        if editor is not None:
            self._set_text_window_through(editor["win"], not interactive)

    def _set_text_window_through(self, win, through):
        """Hide a text box's BACKING while leaving the words fully opaque.

        Three ways were tried and only this one is right:

          * WS_EX_TRANSPARENT -- makes the window invisible to hit-testing, so the
            box could not be clicked or selected from at all. Rejected.
          * window alpha 0.05 -- fades the TEXT too. The words became a ghost,
            which is not what "transparent background" means. Rejected.
          * a colour key on the backing colour -- the backing disappears, the
            glyphs stay at full opacity, and the window stays hittable. This is
            what a text box should look like: words on the desktop, no frame.

        `through` therefore only controls whether the backing is keyed out; the
        text is never made translucent.
        """
        try:
            h = clean_handle(top_level_of(int(win.winfo_id())))
            style = get_ex_style(h)
            # WARNING: the box MUST be click-through in draw mode, otherwise a
            # plain click lands on the text window and Tk starts selecting on its
            # own -- which is the "I can drag-select without holding Alt" report.
            # The board has to receive the press so it can choose between drawing,
            # erasing (Shift) and selecting (Alt), and Alt-select forwards the
            # event on to the widget.
            #
            # WS_EX_TRANSPARENT does make the window unhittable, which is exactly
            # what is wanted here: the board is the one that decides.
            if through:
                style = style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            else:
                style = style & ~WS_EX_TRANSPARENT & ~WS_EX_NOACTIVATE
            set_ex_style(h, style)
            if through:
                # WARNING: -transparentcolor needs WS_EX_LAYERED on the window's
                # own HWND, and Tk does not always put it there for an
                # overrideredirect toplevel. Without the bit the attribute is
                # silently ignored -- measured: the call returned normally, the
                # property read back as '', and 9743 pixels of the dark backing
                # were still painted over the desktop.
                if not style & WS_EX_LAYERED:
                    set_ex_style(h, style | WS_EX_LAYERED)
                # The backing becomes the chroma colour, and the key removes only
                # pixels of exactly that colour. Glyph pixels are a different
                # colour, so they survive at full opacity.
                try:
                    for child in win.winfo_children():
                        child.configure(bg=TEXT_COMMIT_BG)
                        for grand in child.winfo_children():
                            grand.configure(bg=TEXT_COMMIT_BG)
                except Exception:
                    pass
                try:
                    win.configure(bg=TEXT_COMMIT_BG)
                except Exception:
                    pass
                try:
                    win.attributes("-transparentcolor", TEXT_COMMIT_BG)
                except Exception:
                    pass
                try:
                    win.attributes("-alpha", 1.0)
                except Exception:
                    pass
            # WARNING: do NOT clear the colour key in the else branch. A committed
            # box always wants its backing invisible -- that is what "text on the
            # desktop" means -- and clearing it here undid the key that
            # _finish_text_window had just set, leaving 9743 pixels of dark backing
            # painted over the desktop.
        except Exception as e:
            self.note("text through failed: %s" % e)

    def on_wheel(self, event):
        """Wheel changes the pen width and shows the new width beside the pointer.

        The badge is not decoration: without it the wheel has no visible effect
        until the user draws a line, so there is no way to tell whether it worked
        or how thick the pen now is. The WPF board showed the same thing.
        """
        if self.mode != "ink":
            return
        step = 1 if getattr(event, "delta", 0) > 0 else -1
        self.pen_width = max(MIN_WIDTH,
                             min(MAX_WIDTH, self.pen_width + step * self._width_step()))
        # WARNING: rebuild the ring here, not only in the poll. The poll runs on the
        # board's 120 ms tick, and between a wheel notch and that tick the pointer
        # still shows the OLD ring -- which reads as "the wheel does nothing to the
        # cursor". Rebuilding is a small file write, and the wheel is a deliberate
        # action, so the cost is invisible.
        self._install_ring_cursor()
        try:
            self.show_width_badge(float(event.x), float(event.y))
        except Exception as e:
            self.note("badge failed: %s" % e)
        self.note("pen width %d" % self.pen_width)

    def _width_step(self):
        """How much one wheel notch moves the pen.

        WARNING: one pixel per notch at the thin end made the wheel feel broken.
        The ring's radius is floored at 4 px, so pen widths 1..8 all render the
        same 10 px cursor -- measured: eight notches produced an identical cursor
        file every time, and the user reported "the ring size does not change".
        The step therefore starts at 2, which moves the ring on the first notch,
        and grows from there so the whole 1..300 range stays about 25 notches away.
        """
        w = self.pen_width
        if w < 12:
            return 2
        if w < 40:
            return 4
        if w < 100:
            return 8
        if w < 180:
            return 16
        return 32

    def show_width_badge(self, x, y):
        """Float a small tooltip beside the pointer showing the pen width.

        This is its own always-on-top window, the way a system tooltip works: it
        sits above everything, ignores the mouse, and is not part of the board at
        all. It must NOT be painted into the board's pixels -- the number is not
        ink, and drawing it there would either bake it into the user's strokes or
        punch a hole in them when it cleared.

        It appears on the wheel and disappears shortly after the wheel stops, so it
        is visible exactly while the user is changing the width.
        """
        if self.surface is None:
            return
        try:
            label = "%d px" % self.pen_width
            win = getattr(self, "badge_win", None)
            if win is None or not self._badge_alive():
                win = self._make_badge_window()
                if win is None:
                    return
                self.badge_win = win
            # Update the text every time, so rolling the wheel visibly counts.
            self.badge_label.configure(text=label)
            # Measure, then place just below-right of the pointer.
            win.update_idletasks()
            bw = max(56, win.winfo_reqwidth())
            bh = max(26, win.winfo_reqheight())
            bx = int(x) + 20
            by = int(y) + 20
            sw, sh = self.screen[2], self.screen[3]
            if bx + bw > sw:
                bx = max(0, int(x) - bw - 20)
            if by + bh > sh:
                by = max(0, int(y) - bh - 20)
            win.geometry("%dx%d+%d+%d" % (bw, bh, bx, by))
            win.deiconify()
            win.lift()
            set_topmost(int(win.winfo_id()))
            if self.badge_job is not None:
                try:
                    self.root.after_cancel(self.badge_job)
                except Exception:
                    pass
            self.badge_job = self.root.after(900, self.hide_width_badge)
        except Exception as e:
            self.note("badge failed: %s" % e)

    def _badge_alive(self):
        try:
            return bool(self.badge_win.winfo_exists())
        except Exception:
            return False

    def _make_badge_window(self):
        """Create the tooltip window once, then reuse it.

        It is a Toplevel rather than a Tk widget inside the board because the board
        is a layered, click-through, colour-managed window; anything drawn inside it
        either becomes part of the ink or is invisible.
        """
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)          # no title bar, no border
            win.configure(bg="#141418")
            try:
                win.attributes("-alpha", 0.92)
            except Exception:
                pass
            win.attributes("-topmost", True)
            frame = tk.Frame(win, bg="#141418",
                             highlightbackground="#ff2020",
                             highlightthickness=1)
            frame.pack(fill="both", expand=True)
            self.badge_label = tk.Label(
                frame, text="%d px" % self.pen_width, bg="#141418",
                fg="#ffffff", font=("Microsoft YaHei UI", 11),
                padx=8, pady=3)
            self.badge_label.pack()
            # Never take focus or the mouse: the user is drawing, not clicking here.
            try:
                win.attributes("-disabled", True)
            except Exception:
                pass
            win.withdraw()
            return win
        except Exception as e:
            self.note("badge window failed: %s" % e)
            return None

    def hide_width_badge(self):
        """Hide the tooltip. The window is kept for the next roll."""
        if self.badge_job is not None:
            try:
                self.root.after_cancel(self.badge_job)
            except Exception:
                pass
            self.badge_job = None
        win = getattr(self, "badge_win", None)
        if win is None:
            return
        try:
            if self._badge_alive():
                win.withdraw()
        except Exception:
            pass

    def on_escape(self, _event=None):
        """Esc closes the text editor first, then leaves draw mode.

        It also abandons any stroke in progress: pressing Esc mid-drag should not
        leave a half-drawn line stuck to the canvas.

        Esc's meaning follows what is being edited: a fresh box is discarded
        (nothing was on screen before it), an existing box keeps its words and
        window (they were there before the edit began). Both routes land here --
        the widget-level binding and this bind_all fallback -- so the decision
        lives in close_text_editor and this path just passes it through.
        """
        if self.text_editor is not None:
            self.close_text_editor(keep_existing=True)
            return "break"
        if self.active_points:
            self.active_points = []
            self.active_item = None
            self.repaint()
        if self.mode != "idle":
            self.set_mode("idle")
        # "break" stops Tk from also passing the key on, which would otherwise
        # re-enter this handler through bind_all.
        return "break"

    def clear_scene(self, repaint=True):
        """Remove every agent mark from the canvas.

        `repaint` defaults to True so a direct call shows the result; render_scene
        passes False because it repaints itself once after the whole update, and
        repainting per shape would be both slower and visibly flickery.
        """
        for item in self.scene_items.values():
            for canvas_id in item.get("ids", ()):
                try:
                    self.canvas.delete(canvas_id)
                except Exception:
                    pass
        self.scene_items = {}
        if repaint:
            self.repaint()

    def render_scene(self, doc):
        """Bring the canvas in line with the agent's layers, item by item.

        Coordinates: unit == "screen" means physical desktop pixels, which is
        exactly the canvas coordinate space because the window is built at the
        real screen size. unit == "window" means percent of the board.

        This is an INCREMENTAL update, not a rebuild. Each layer is tracked by
        its id together with a fingerprint of its drawn properties:

          * unchanged fingerprint -> nothing is touched, the item stays exactly
            as it is on screen. Adding one mark to a board with thirty marks
            therefore creates one item and leaves thirty alone;
          * changed fingerprint  -> the old item is deleted and the new one
            drawn, so a mark that moves does not leave its previous copy behind;
          * id gone from the doc -> that item is deleted.

        WARNING: the previous version deleted everything and redrew. Two things
        went wrong with that. The obvious one was a visible blink: clearing
        leaves the canvas empty while the rebuild runs, and Tk repaints in that
        gap (measured: 20 empty frames out of 20 re-renders). The subtler one was
        cost: every update rebuilt the whole scene, so the more the agent drew,
        the longer each repaint took and the more it looked like the board was
        redrawing itself.
        """
        # After abort() the canvas is gone; any later tick must not touch it.
        if self.aborted:
            return
        if not isinstance(doc, dict) or doc.get("enabled") is not True:
            self.clear_scene(repaint=False)
            self.repaint()
            return
        screen_unit = str(doc.get("unit", "screen")) != "window"
        x0, y0, w, h = self.screen

        def mx(v):
            v = float(v)
            return v - x0 if screen_unit else v * w / 100.0

        def my(v):
            v = float(v)
            return v - y0 if screen_unit else v * h / 100.0

        wanted = {}
        for index, item in enumerate(doc.get("layers") or []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "") or "")
            if item_id == "":
                # An anonymous layer has no stable identity, so it cannot be
                # tracked incrementally; fall back to a positional key.
                item_id = "#%d" % index
            if item_id in self.hidden:
                continue
            wanted[item_id] = item

        # Remove items whose id is no longer in the document.
        for item_id in list(self.scene_items.keys()):
            if item_id not in wanted:
                self._drop(item_id)

        # Add or update the rest.
        for item_id, item in wanted.items():
            fingerprint = _fingerprint(item)
            existing = self.scene_items.get(item_id)
            if existing is not None and existing.get("fingerprint") == fingerprint:
                continue
            if existing is not None:
                self._drop(item_id)
            try:
                # Tell _spawn_agent_text which layer it is drawing, so a later
                # erase of that layer can find and destroy the window it made.
                self._spawning_agent_id = item_id
                ids = self._draw_item(item, mx, my)
            except Exception as e:
                self.note("scene item failed: %s" % e)
                continue
            finally:
                self._spawning_agent_id = ""
            self.scene_items[item_id] = {"ids": ids, "fingerprint": fingerprint}

        self.repaint()

    def _drop(self, item_id):
        """Delete one tracked layer and forget it.

        WARNING: a text layer is not just a canvas shape -- it owns a real Toplevel
        window that holds the words. Deleting the shape left the window on screen,
        which is why erasing an agent-written label reported success and changed
        nothing: measured, 1280 green pixels before and 1280 after. The window has
        to be destroyed too, and its record removed from self.texts so nothing tries
        to reuse it.
        """
        entry = self.scene_items.pop(item_id, None)
        if entry is None:
            return
        for canvas_id in entry.get("ids", ()):
            try:
                self.canvas.delete(canvas_id)
            except Exception:
                pass
        # Tear down any text box this layer created.
        for text_entry in list(getattr(self, "texts", []) or []):
            if text_entry.get("agent_id") != item_id:
                continue
            win = self.text_wins.pop(text_entry.get("id"), None)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
            try:
                self.texts.remove(text_entry)
            except Exception:
                pass
            self.note("agent text %s removed with its layer" % item_id)

    def repaint(self):
        """Rebuild the RGBA surface from the record and show it.

        Three things go into the pixels, in this order:

          * the board's background at BG_ALPHA, so the desktop shows through while
            the window still catches the mouse;
          * the user's strokes, replayed from self.strokes;
          * the agent's marks, from the canvas shape list.

        WARNING: the strokes MUST be replayed here. The ink is pixels, not a shape
        the canvas knows about, so a repaint that only cleared and drew the agent's
        shapes erased everything the user had drawn -- measured: adding one agent
        mark wiped the whole board. The stroke record exists for exactly this.
        """
        if self.surface is None or self.draw is None:
            return
        try:
            self.draw.rectangle([0, 0, self.surface.width, self.surface.height],
                                fill=(0, 0, 0, BG_ALPHA))
            # Paste the user's ink layer back verbatim. This is the whole point of
            # keeping the ink in its own image: the pixels already carry the colour
            # each stroke was drawn with, so a repaint reproduces the board instead
            # of recolouring it with whatever the pen is set to now.
            self.surface.alpha_composite(self.ink_layer)
            self.canvas.paint(self.surface, self.draw)
            self.sync()
        except Exception as e:
            self.note("repaint failed: %s" % e)

    def _draw_item(self, item, mx, my):
        t = str(item.get("type", ""))
        stroke = item.get("stroke") or ""
        fill = item.get("fill") or ""
        # "none" is a real value, not a colour: an outline-only mark is what an
        # omitted fill should have meant all along. Passing it through as a colour
        # name made PIL reject it and fall back, so the mark came out solid.
        if str(fill).strip().lower() in ("none", "transparent", "null"):
            fill = ""
        width = int(float(item.get("width", 4) or 4))
        ids = []

        if t == "rect":
            x, y = mx(item.get("x", 0)), my(item.get("y", 0))
            w, h = mx(item.get("w", 0)) - mx(0), my(item.get("h", 0)) - my(0)
            if w <= 0 or h <= 0:
                return ids
            if fill:
                ids.append(self.canvas.create_rectangle(
                    x, y, x + w, y + h, fill=fill, outline=""))
            if stroke:
                ids.append(self.canvas.create_rectangle(
                    x, y, x + w, y + h, outline=stroke, width=width))

        elif t == "ellipse":
            x, y = mx(item.get("x", 0)), my(item.get("y", 0))
            w, h = mx(item.get("w", 0)) - mx(0), my(item.get("h", 0)) - my(0)
            if w <= 0 or h <= 0:
                return ids
            if fill:
                ids.append(self.canvas.create_oval(
                    x, y, x + w, y + h, fill=fill, outline=""))
            if stroke:
                ids.append(self.canvas.create_oval(
                    x, y, x + w, y + h, outline=stroke, width=width))

        elif t == "path":
            pts = item.get("points") or []
            coords = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    coords.extend([mx(p[0]), my(p[1])])
            if len(coords) >= 4 and stroke:
                ids.append(self.canvas.create_line(
                    *coords, fill=stroke, width=width,
                    joinstyle="round", capstyle="round", smooth=False))

        elif t == "arrow":
            x1, y1 = mx(item.get("x1", 0)), my(item.get("y1", 0))
            x2, y2 = mx(item.get("x2", 0)), my(item.get("y2", 0))
            if stroke:
                ids.append(self.canvas.create_line(
                    x1, y1, x2, y2, fill=stroke, width=width, capstyle="round"))
                import math
                ang = math.atan2(y2 - y1, x2 - x1)
                head = float(item.get("head", 18) or 18)
                for spread in (0.45, -0.45):
                    ids.append(self.canvas.create_line(
                        x2, y2,
                        x2 - head * math.cos(ang + spread),
                        y2 - head * math.sin(ang + spread),
                        fill=stroke, width=width, capstyle="round"))

        elif t == "text":
            text = str(item.get("text", "") or "")
            if not text:
                return ids
            size = int(float(item.get("size", 18) or 18))
            align = str(item.get("align", "") or "")
            # WARNING: the agent sends the colour as `color` on text items (the
            # tool schema documents it that way), while strokes carry `stroke`.
            # Reading only `stroke` made every agent label fall back to white --
            # the whale drawing that came out all-white text was this.
            text_colour = stroke or item.get("color") or "#ffffff"
            # A label the agent writes is TEXT, not a picture of text. It goes
            # through the same box the user's own typing does, so the words stay
            # characters: selectable, copyable, and erasable with the same
            # gesture. Painting it into the bitmap made it ink -- pretty, but
            # impossible to read back, which is what the user reported.
            #
            # The anchor is translated to a top-left corner here because a window
            # is positioned by its corner, not by its baseline.
            ax = float(item.get("x", 0) or 0)
            ay = float(item.get("y", 0) or 0)
            if align in ("center", "right"):
                approx = self._measure_text(text, size)
                if align == "center":
                    ax -= approx[0] / 2.0
                else:
                    ax -= approx[0]
            ids.append(self._spawn_agent_text(ax, ay, text, size, text_colour))

        return ids

    def _measure_text(self, text, size):
        """Approximate the pixel size of a line, for anchor translation only.

        Exactness is not needed: the box measures itself properly once it exists.
        This only decides where its corner goes when the agent asked for centred
        or right-aligned text.
        """
        try:
            width = max(len(line) for line in text.split("\n"))
        except ValueError:
            width = len(text)
        return (width * size * 0.62, size * (text.count("\n") + 1) * 1.4)

    def _spawn_agent_text(self, x, y, body, size, colour):
        """Create a committed text box for a label the agent asked for.

        The box is created straight into the committed state -- no editor, no
        focus, no cursor -- because the agent is placing words, not inviting the
        user to type. Everything else matches a box the user made: it can be read
        back by hovering and removed with the eraser.
        """
        try:
            win, widget = self._make_text_window(x, y, body)
            if win is None:
                return None
            # Honour the requested size and colour rather than the pen's.
            try:
                widget.configure(font=("Microsoft YaHei UI", -size), fg=colour)
            except Exception:
                pass
            self.text_seq = getattr(self, "text_seq", 0) + 1
            entry = {"id": "t%d-%d" % (self.text_seq, int(time.time() * 1000)),
                     "seq": self.text_seq,
                     # Which scene layer created this box, so _drop can find it
                     # again when the agent erases that layer.
                     "agent_id": getattr(self, "_spawning_agent_id", ""),
                     "text": body, "x": float(x), "y": float(y),
                     "size": size, "w": 0, "h": 0,
                     # _finish_text_window reads the widget out of the entry, and
                     # without it the box could not be measured or restyled.
                     "_widget": widget}
            self.texts.append(entry)
            # WARNING: map the window BEFORE making it layered. _finish_text_window
            # sets WS_EX_LAYERED, and a window that has never been shown ends up
            # with the style but no composited surface: it exists, IsWindowVisible
            # is true, the Text widget holds the right characters and colour, and
            # the screen shows nothing at all.
            #
            # Measured: this is exactly why agent text was invisible while the
            # user's own typing worked -- the user path pumps the event loop while
            # typing, so its window was already mapped before commit.
            # update_idletasks() is NOT enough; the window needs a real loop pass.
            self.root.update()
            self._finish_text_window(win, entry, editable=False)
            # WARNING: obey the mode, exactly as the user's own boxes do. Without
            # this the agent's text was interactive from the moment it appeared --
            # selectable while drawing, with no Shift -- while a box the user typed
            # was click-through until idle+Shift. Two boxes on one board behaving
            # differently is the bug the user hit: "I could copy right after you
            # drew, then I could not after clicking elsewhere".
            # Click-through, so the BOARD receives every press and decides what a
            # drag means: draw, erase (Shift) or select (Alt). If the box were
            # hittable, Tk would start its own selection on a plain click -- which
            # is the "I can drag-select without holding Alt" report.
            self._set_text_window_through(win, True)
            self.root.update()
            self.note("agent text box %d placed" % entry["seq"])
            return self.canvas.create_rectangle(0, 0, 0, 0, outline="", fill="")
        except Exception as e:
            self.note("agent text failed: %s" % e)
            return None

    # -- timers --------------------------------------------------------------

    def poll_scene(self):
        """Re-render only when the scene DOCUMENT changed.

        WARNING: comparing file mtime is not enough. The Host rewrites
        scene.json on every save -- including saves where nothing about the marks
        changed -- so mtime moves while the content is identical. Re-rendering on
        mtime alone repaints the whole board for no reason, and every repaint is
        a chance for the user to see a flicker. Compare the parsed revision and a
        cheap digest of the layer list instead.
        """
        m = mtime(self.args.scene)
        if m is None or m == self.last_scene_mtime:
            return
        self.last_scene_mtime = m
        raw = read_text(self.args.scene)
        if raw is None:
            return
        digest = _digest(raw)
        if digest == self.last_scene_digest:
            return
        self.last_scene_digest = digest
        doc = read_json(self.args.scene)
        if doc is not None:
            self.render_scene(doc)
            self.note("scene rendered")

    def poll_commands(self):
        doc = read_json(self.args.commands)
        if doc is None:
            return
        try:
            rev = int(doc.get("rev", 0) or 0)
        except Exception:
            return
        if rev <= self.last_command_rev:
            return
        self.last_command_rev = rev

        mode = doc.get("mode")
        if mode in ("ink", "idle"):
            self.set_mode(mode)

        erase = doc.get("erase")
        if isinstance(erase, dict):
            for item_id in erase.get("ids") or []:
                sid = str(item_id)
                if sid and sid not in self.hidden:
                    self.hidden.append(sid)
            self.note("commands applied rev %d" % rev)

    def poll_tray(self):
        """Act on an intent the whale dropped in tray.txt.

        The whale's menu is the only door onto the board: "start drawing", "call
        agent" and "clear" are written into tray.txt beside the state files, and
        the board is supposed to read them and act. That whole channel was
        missing after the move to Python, so pressing "start drawing" wrote a file
        nobody read -- the board simply never entered draw mode.

        The file is consumed (deleted) when read: an intent left on disk would
        replay on every board restart.
        """
        path = os.path.join(self.board_state_dir, "tray.txt")
        raw = read_text(path)
        if raw is None:
            return
        try:
            os.remove(path)
        except Exception:
            pass
        intent = raw.strip()
        if not intent:
            return
        self.note("tray: intent %s" % intent)

        if intent == "ink":
            self.set_mode("ink")
        elif intent == "idle":
            self.set_mode("idle")
        elif intent == "clear":
            self.clear_all()
        elif intent == "text":
            # The text tool needs a position; without one it opens in the middle
            # of the screen so the request is never silently dropped.
            #
            # Draw mode first: a click outside commits the box, and clicks only
            # reach the board while it is catching the mouse. Opening the editor
            # from idle produced a box that could never be committed.
            self.set_mode("ink")
            cx = self.screen[0] + self.screen[2] // 2
            cy = self.screen[1] + self.screen[3] // 2
            self.open_text_editor(cx, cy)
        elif intent == "call":
            # The Host owns "summon the agent": it captures the screen and
            # prompts. The board only forwards the request.
            self.request_summon()

    def request_summon(self):
        """Ask the Host to capture the screen and prompt the agent.

        Written as the same summon.json the whale uses, so there is one summon
        path rather than two.
        """
        try:
            path = os.path.join(self.board_state_dir, "summon.json")
            payload = {"at": int(time.time() * 1000),
                       "note": "board asked for the agent"}
            write_text(path, json.dumps(payload))
            self.note("summon posted")
        except Exception as e:
            self.note("summon failed: %s" % e)

    def poll_clear(self):
        raw = read_text(self.args.clear_signal)
        if raw is None:
            return
        try:
            tick = int(float(raw.strip() or "0"))
        except Exception:
            return
        if tick <= self.last_clear_tick:
            return
        self.last_clear_tick = tick
        # The Host is telling the board the user cleared everything, so the ink
        # goes too -- not just the agent's marks. Otherwise the strokes the user
        # just cleared stay on screen because the Host's signal only ever reached
        # the scene layer.
        for item in list(self.ink_items):
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        # Text boxes are windows, so wiping the pixel buffer does not remove them.
        self.clear_text_windows()
        self.strokes = []
        self.ink_items = []
        self.texts = []
        self.active_points = []
        self.active_item = None
        self.clear_scene()
        self.note("cleared by user")

    def poll_alive(self):
        m = mtime(self.args.alive)
        if m is None:
            self.note("heartbeat gone")
            self.root.quit()
            return
        if (time.time() - m) * 1000 > ALIVE_STALE_MS:
            self.note("heartbeat stale")
            self.root.quit()

    def tick(self):
        self.tick_count += 1
        try:
            self.poll_alive()
            self.poll_scene()
            self.poll_commands()
            self.poll_tray()
            self.poll_clear()
            # The pen ring must follow the pointer even when it is over another
            # application's window, and Tk only reports motion inside its own
            # windows -- so it is polled rather than event-driven.
            self._sync_ink_colour()
            self._sync_alt_hittable()
            self._tick_pen_ring()
            # Idle mode is when text can be read: Shift selects words, so they can
            # be copied out. Polled for the same reason as the ring.
            self._tick_text_readable()
            # The island is its own program; the board does not drive it.
            # Every ~1s, cheap: the whale is the only door onto the board, so it
            # is watched rather than assumed.
            if self.tick_count % 8 == 0:
                self.watch_whale()
        except Exception as e:
            self.note("tick: %s" % e)
        try:
            self.root.after(POLL_MS, self.tick)
        except Exception:
            pass

    def run(self):
        self.build()
        # An aborted board must not re-render: build() calls abort() when the
        # window could not be made transparent, and touching the destroyed
        # canvas afterwards logs "invalid command name".
        if self.aborted:
            return
        # Render whatever is already on the board before the first tick, so a
        # restart does not blink the existing marks away.
        doc = read_json(self.args.scene)
        if doc is not None:
            self.render_scene(doc)
            self.last_scene_mtime = mtime(self.args.scene)
            raw = read_text(self.args.scene)
            if raw is not None:
                self.last_scene_digest = _digest(raw)
            self.note("scene rendered (initial)")
        # Bring back whatever the user had drawn before a restart, and set the
        # pointer for the current mode. Without the reload, restarting the board
        # silently threw away the user's ink.
        self.load_user()
        self.render_ink()
        # Texts are windows, so they do not come back with the pixels: they have to
        # be built again, or the user's words would vanish on every restart while
        # the strokes returned.
        self.restore_text_windows()
        self.apply_cursor()
        # Check for an opaque cover 700ms after mapping: the layered attributes
        # settle asynchronously, so an immediate check would race them. If the
        # check fails the board destroys itself rather than sitting on top of
        # the user's whole desktop.
        self.root.after(700, self.verify_transparent)
        # The whale is this board's only door -- with no whale there is no way to
        # switch the pencil on -- so it is started here, never left to chance.
        #
        # WARNING: it runs on a BACKGROUND thread, not on the Tk thread.
        # start_whale waits up to 12s for the whale to report running, and doing
        # that inside an after() callback freezes the whole UI loop for those 12
        # seconds -- including poll_alive, which then reads a heartbeat that
        # aged while it was blocked and decides the Host is gone. The board quit
        # immediately after a perfectly successful whale start.
        self.root.after(400, lambda: threading.Thread(
            target=self.start_whale, daemon=True).start())
        self.root.after(POLL_MS, self.tick)
        try:
            self.root.mainloop()
        finally:
            # WARNING: do NOT kill the whale here. The board's lifetime is tied
            # to the DSH window (the Host stops the board whenever DSH is
            # minimised to the tray), while the whale is supposed to survive that.
            # Killing it here produced the "whale keeps vanishing and coming
            # back" loop. The whale watches alive.txt and leaves on its own when
            # the Host really goes away.
            try:
                os.remove(self.args.window)
            except Exception:
                pass
            self.note("board down")


def _is_python_pid(pid):
    """Whether a pid is a python process, i.e. a plausible board launcher.

    The venv's Scripts/pythonw.exe is a launcher that spawns the real interpreter
    and exits, so the lock must be held by either of them. Anything else in the
    ancestry (explorer, the DSH app, a shell) outlives the board and must never
    keep the lock alive.
    """
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                      wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap:
            return False
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return False
            while True:
                if entry.th32ProcessID == pid:
                    name = (entry.szExeFile or "").lower()
                    return name.startswith("python")
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    return False
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return False


def _parent_pid(pid):
    """The parent of a process, or 0. Used to record a whole launcher chain."""
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap:
            return 0
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return 0
            while True:
                if entry.th32ProcessID == pid:
                    return int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    return 0
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return 0


def _read_holders(lock_path):
    """Every pid recorded in the lock file, as a list."""
    try:
        with open(lock_path, encoding="utf-8-sig") as f:
            raw = f.read().strip()
    except Exception:
        return []
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out


def claim_board_lock(window_path):
    """Take the one-board lock atomically. False means another board owns it.

    O_CREAT|O_EXCL is the reliable primitive: creating the file either succeeds or
    fails, and the kernel decides which, so two boards starting in the same
    millisecond cannot both win. A named mutex looked equivalent but was not --
    two boards started 400 ms apart both acquired it, and the pid file the Host
    reads is not written until the window is up, which left a window in which
    neither guard could see the other.

    A stale lock (its pid is gone) is cleared and retried once, so a board that
    crashed or was killed does not lock the user out of the pencil for the session.
    """
    lock_path = os.path.join(os.path.dirname(os.path.abspath(window_path)),
                             "board.lock")
    for _attempt in (1, 2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                # WARNING: write the whole ancestor chain, not just os.getpid().
                # This venv's Scripts/pythonw.exe is a launcher that spawns the real
                # interpreter and then EXITS, so the pid recorded here can be dead a
                # second later while a perfectly healthy board runs on as its child.
                # Measured: the second board read the first board's launcher pid,
                # found it dead, deleted the lock and claimed it -- two full-screen
                # overlays stacked. Any live pid in the chain means the lock is held.
                # WARNING: the chain must stop at the launcher, NOT walk up into
                # long-lived system processes. Recording the whole ancestry put
                # explorer.exe in the lock, and explorer lives for the entire
                # session -- so every later board read the lock, found a live pid
                # and refused to start. Measured: the board was stuck on "starting"
                # forever because pid 1988 (explorer.exe) was still alive while
                # every board pid in the chain was dead.
                #
                # Only pids that could plausibly BE a launcher of this board are
                # kept: the python interpreter itself and its immediate wrapper.
                chain = []
                pid = os.getpid()
                for _ in range(3):
                    if pid <= 0 or pid in chain:
                        break
                    if pid != os.getpid() and not _is_python_pid(pid):
                        break
                    chain.append(pid)
                    pid = _parent_pid(pid)
                os.write(fd, ",".join(str(x) for x in chain).encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            holders = _read_holders(lock_path)
            # WARNING: holder == 0 means the lock exists but its pid is not written
            # yet -- another board is in the middle of claiming it. Treating that as
            # a stale leftover and deleting it is what let two full-screen boards
            # run at once: measured, both logged claim=True within one second and
            # two 1536x864 overlays were stacked on screen. An unreadable lock is
            # treated as owned, never as free.
            if not holders:
                # Give the creator a full second, in steps: a lock that is still
                # empty after that really is a leftover.
                for _wait in range(10):
                    time.sleep(0.1)
                    holders = _read_holders(lock_path)
                    if holders:
                        break
                if not holders:
                    # Still empty after a short wait: the writer died between
                    # create and write. Now it really is a leftover.
                    try:
                        os.remove(lock_path)
                    except Exception:
                        return False
                    continue
            if any(_process_alive(pid) for pid in holders):
                return False
            # The holder is gone: this is a leftover, so clear it and try again.
            try:
                os.remove(lock_path)
            except Exception:
                return False
        except Exception:
            # Cannot create the lock at all (unwritable folder). Refusing to start
            # is worse than running without the guard, so let it through.
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--users", required=True)
    ap.add_argument("--alive", required=True)
    ap.add_argument("--window", required=True)
    ap.add_argument("--commands", required=True)
    ap.add_argument("--clear-signal", required=True)
    ap.add_argument("--labels", default="")
    ap.add_argument("--status", default="")
    ap.add_argument("--stop-signal", default="")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    # push_rgba can fail before a Board exists; give it the same trace file.
    try:
        os.environ["DSH_BOARD_LOG"] = args.log or ""
    except Exception:
        pass

    # One board per install, enforced by the process itself.
    #
    # WARNING: the Host's own guard is not enough. It decides whether a board is
    # already running by reading the pid file, which the board only writes once
    # its window is up -- so two starts inside that window both pass the check
    # and two full-screen overlays end up stacked. They fight over the topmost
    # slot (the user sees flickering) and both consume tray.txt, so pressing
    # "start drawing" can be swallowed by an instance the user cannot even see.
    #
    # WARNING: GetLastError must NOT be the decision. It is thread-local and ANY
    # intervening Win32 call clears it, including ones ctypes makes on its own
    # while marshalling arguments. Two boards started in the same instant and both
    # got past the old check, which is exactly what this guard exists to stop:
    # stacked full-screen overlays fighting for topmost, two tray icons, and a
    # "start drawing" click disappearing into an instance the user cannot see.
    #
    # Ownership is the reliable signal instead. WaitForSingleObject with a zero
    # timeout answers "is it mine" without consulting the last error at all, and
    # an abandoned mutex (the previous owner died) counts as acquired.
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x00000080
    mutex = kernel32.CreateMutexW(None, False, "Local\\dsh-companion-board")
    held = kernel32.WaitForSingleObject(mutex, 0) if mutex else WAIT_OBJECT_0

    # Belt and braces: a live pid in the window file means a board is up, whatever
    # the mutex says. This also covers a straggler from a build that used a
    # different mutex name.
    owner = 0
    try:
        with open(args.window, encoding="utf-8-sig") as f:
            owner = int(f.read().strip() or 0)
    except Exception:
        owner = 0
    if owner and owner != os.getpid() and _process_alive(owner):
        held = WAIT_ABANDONED + 1

    claimed = claim_board_lock(args.window)

    # Record the decision either way. "Two boards are running" and "the guard did
    # nothing" look identical from outside without this line, and the values below
    # are the only way to tell which happened.
    try:
        with open(args.log, "a", encoding="utf-8") as f:
            f.write("%s board: guard pid=%d mutex=%s wait=%s window-owner=%d "
                    "claim=%s\n"
                    % (time.strftime("%H:%M:%S"), os.getpid(), bool(mutex),
                       hex(held) if held else "0", owner, claimed))
    except Exception:
        pass

    # WARNING: the lock file is the authority, not the mutex. A mutex is released
    # by the kernel only when every handle to it closes, and a board killed with
    # Stop-Process or a crashed Tk can leave one behind for the life of the
    # session -- measured: a fresh board found `wait=0x102` (someone else holds
    # it) while the lock file was free and no board process existed, and then
    # refused to start at all. The file lock is created with O_EXCL and cleared
    # when its owner's pid is gone, so it reflects reality.
    #
    # The mutex stays as a secondary check: it still catches two boards racing
    # inside the same millisecond, before either has written the lock file.
    already = (not claimed)
    if already:
        # Another board is up. Record why, so a "nothing happens" report can be
        # traced to this rather than guessed at.
        try:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write("%s board: already running (window owner pid %s, lock %s), "
                        "exiting\n" % (time.strftime("%H:%M:%S"),
                                       owner or "none", claimed))
        except Exception:
            pass
        return 0

    # Before Tk(): otherwise the screen is measured in scaled pixels.
    become_aware()

    board = Board(args)
    try:
        board.run()
    except Exception as e:
        board.note("fatal: %s" % e)
        return 1
    finally:
        # Give the lock back on the way out. A crash is covered by the stale-pid
        # check, but a clean exit should not leave the file for the next start to
        # have to reason about.
        try:
            lock_path = os.path.join(os.path.dirname(os.path.abspath(args.window)),
                                     "board.lock")
            with open(lock_path, encoding="utf-8-sig") as f:
                if int(f.read().strip() or 0) == os.getpid():
                    os.remove(lock_path)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
