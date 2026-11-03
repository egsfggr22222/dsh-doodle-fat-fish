# -*- coding: utf-8 -*-
"""Find a file by name through Everything's window-message IPC.

Python replacement for locate.ps1. Same command line, same JSON on stdout.

    python locate.py --find <marker-name>
    -> {"ok":true,"paths":["C:\\...\\marker"]}   or   {"ok":false,"reason":...}

Everything indexes every NTFS filename and answers WM_COPYDATA queries. This
asks it for one name and prints the full paths it knows, which is how the plugin
finds its own folder after that folder is renamed or moved: the marker file's
name is the identity, and its current path is whatever Everything says.

Protocol facts, each taken from voidtools' SDK or the maintained Rust port that
documents where the SDK is wrong:

  request   EVERYTHING_IPC_QUERY2 = 7 DWORDs (28 bytes) + UTF-16 search + NUL
  dwData    EVERYTHING_IPC_COPYDATA_QUERY2W (18)
  reply id  reply_copydata_message is an arbitrary caller-chosen id and comes
            back as the reply's dwData. The SDK comment calling it a fixed reply
            constant is wrong; matching on one never recognises a reply.
  reply     arrives by PostMessage, so the caller must pump messages
  list      EVERYTHING_IPC_LIST2 = 5 DWORDs (20 bytes)
  item      EVERYTHING_IPC_ITEM2 = flags(4) + data_offset(4), offset absolute
  string    u32 character count + UTF-16 + NUL

Everything is a third-party program OUTSIDE this plugin, so this module is
strictly optional: when it is not running the caller gets reason "not-running"
and the plugin keeps working without folder-relocation recovery.

stdlib + ctypes only.
"""

import argparse
import ctypes
import json
import struct
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_COPYDATA = 0x004A
PM_REMOVE = 0x0001
COPYDATA_QUERY2W = 18

REQUEST_FILE_NAME = 0x00000001
REQUEST_PATH = 0x00000002
REQUEST_FULL_PATH = 0x00000004
SORT_NAME_ASCENDING = 1

IPC_WNDCLASS = "EVERYTHING_TASKBAR_NOTIFICATION"


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [
        ("dwData", ctypes.c_ulonglong),
        ("cbData", wintypes.DWORD),
        ("lpData", ctypes.c_void_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def find_ipc_window():
    """The Everything notification window, or 0 when it is not running."""
    hwnd = user32.FindWindowW(IPC_WNDCLASS, None)
    if hwnd:
        return int(hwnd)
    # Some builds register a different class; fall back to a title match.
    hwnd = user32.FindWindowW(None, "Everything")
    return int(hwnd) if hwnd else 0


def parse_list(raw):
    """EVERYTHING_IPC_LIST2 -> list of full paths."""
    paths = []
    if len(raw) < 20:
        return paths
    numitems = struct.unpack_from("<I", raw, 4)[0]
    count = min(int(numitems), 256)
    for index in range(count):
        base = 20 + index * 8
        if base + 8 > len(raw):
            break
        cursor = struct.unpack_from("<i", raw, base + 4)[0]
        if cursor < 0 or cursor >= len(raw):
            continue
        # Items are laid out as filename, path, full path in ascending flag
        # order; take the first string that starts with a drive letter.
        for _ in range(3):
            if cursor + 4 > len(raw):
                break
            chars = struct.unpack_from("<I", raw, cursor)[0]
            start = cursor + 4
            end = start + chars * 2
            if end > len(raw) or chars > 4096:
                break
            try:
                text = raw[start:end].decode("utf-16-le", "replace")
            except Exception:
                break
            if len(text) > 2 and text[1] == ":":
                paths.append(text)
                break
            cursor = end + 2
    return paths


def query(name, max_results=64, timeout_ms=5000):
    """Ask Everything for one file name. Returns (paths, reason)."""
    ipc = find_ipc_window()
    if ipc == 0:
        return [], "not-running"

    replies = []
    hinst = kernel32.GetModuleHandleW(None)
    class_name = "DshLocate%d" % (int(time.time() * 1000) % 1000000)

    def on_message(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_COPYDATA:
                cds = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
                if cds.lpData and cds.cbData:
                    raw = ctypes.string_at(cds.lpData, cds.cbData)
                    replies.append(raw)
                    return 1
        except Exception:
            pass
        return user32.DefWindowProcW(wintypes.HWND(hwnd), msg, wparam, lparam)

    wndproc = WNDPROC(on_message)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = class_name
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        return [], "register-class-failed"

    hwnd = user32.CreateWindowExW(0, class_name, "dsh-locate", 0,
                                  0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        user32.UnregisterClassW(class_name, hinst)
        return [], "create-window-failed"

    try:
        reply_id = int(time.time() * 1000) % 0x7FFFFFFF
        header = struct.pack("<IIIIIII",
                             int(hwnd) & 0xFFFFFFFF,
                             reply_id,
                             0,
                             0,
                             max_results,
                             REQUEST_FULL_PATH,
                             SORT_NAME_ASCENDING)
        text = name.encode("utf-16-le") + b"\x00\x00"
        payload = header + text

        buf = ctypes.create_string_buffer(payload, len(payload))
        cds = COPYDATASTRUCT()
        cds.dwData = COPYDATA_QUERY2W
        cds.cbData = len(payload)
        cds.lpData = ctypes.cast(buf, ctypes.c_void_p)

        user32.SendMessageW(wintypes.HWND(ipc), WM_COPYDATA,
                            wintypes.WPARAM(int(hwnd)), ctypes.byref(cds))

        # Everything posts the reply, so pump until it lands.
        msg = MSG()
        waited = 0
        while not replies and waited < timeout_ms:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.002)
                waited += 2
    finally:
        user32.DestroyWindow(wintypes.HWND(hwnd))
        user32.UnregisterClassW(class_name, hinst)

    if not replies:
        return [], "no-reply"
    return parse_list(replies[0]), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--find", required=True)
    ap.add_argument("--max-results", type=int, default=64)
    ap.add_argument("--timeout-ms", type=int, default=5000)
    args = ap.parse_args()

    try:
        paths, reason = query(args.find, args.max_results, args.timeout_ms)
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "reason": "exception",
                                     "detail": str(e)}, ensure_ascii=False) + "\n")
        return 1

    if reason:
        sys.stdout.write(json.dumps({"ok": False, "reason": reason, "detail": ""},
                                    ensure_ascii=False) + "\n")
        return 1
    if not paths:
        sys.stdout.write(json.dumps({"ok": False, "reason": "marker-not-found",
                                     "detail": "", "paths": []},
                                    ensure_ascii=False) + "\n")
        return 0
    sys.stdout.write(json.dumps({"ok": True, "reason": "", "paths": paths},
                                ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
