# -*- coding: utf-8 -*-
"""The island -- a small pill that reports what the agent is doing.

A standalone process, on purpose. It shares nothing with the board: the board is
a full-screen click-through overlay whose pixels come from UpdateLayeredWindow,
and everything in this file would be either invisible inside it or unable to
receive a click. The reference implementation reached the same conclusion and
made the island its own window; this goes one step further and makes it its own
program, so a fault in either cannot take the other down.

    python island.py --status status.json --stop-signal stop.txt \
                     --labels labels.json --log island.log

What it shows
-------------
  * a rounded, semi-transparent pill at the top centre of the screen;
  * a dot that breathes while work is happening;
  * what the agent is doing -- "调用 pencil" / "读取 find" / "思考中";
  * how long it has been going;
  * a pause button that asks the Host to stop the running turn.

When it first appears it grows a little and shows the conversation's name, then
settles into the compact readout. That first beat is the only moment the pill is
worth reading as a title; after that the user wants the status, not the name.

Exit
----
The Host stops it by deleting the heartbeat or writing the stop signal; a stale
status file means the agent finished, and the pill leaves on its own.
"""

import argparse
import json
import os
import sys
import time
import tkinter as tk
from ctypes import wintypes

import ctypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

# The colour that becomes invisible. Nothing is ever painted this colour.
CHROMA = "#010203"

# The pill's look. A dark, slightly translucent body with a hairline edge, so it
# reads as a system readout over any wallpaper.
PILL_BG = "#12121a"
PILL_EDGE = "#2a2a38"
TEXT_FG = "#ecf0f5"
TEXT_DIM = "#96a0ac"
DOT_THINK = "#788494"
DOT_READ = "#00e5ff"
DOT_TOOL = "#7cffb2"
STOP_RED = "#e2464e"
STOP_RED_HOVER = "#ff5a63"

POLL_MS = 120
# The status file is stale when the Host stops writing it: the agent is gone and
# the pill must not sit there claiming work that is not happening.
STALE_MS = 15000
# The intro beat: how long the expanded card stays up, and how long the shrink
# animation takes. Long enough to read a conversation title, short enough not to
# get in the way of the work it is reporting.
INTRO_MS = 2500
SHRINK_MS = 320


def become_aware():
    """Declare DPI awareness BEFORE tk.Tk(), or the screen is measured scaled."""
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
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def clean_handle(hwnd):
    """Win32 handles are 32-bit values widened to pointer size."""
    return int(hwnd) & 0xFFFFFFFF


def top_level_of(hwnd):
    """Walk to the real top-level window: winfo_id() is a CHILD handle."""
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


def set_topmost(hwnd):
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos(wintypes.HWND(clean_handle(hwnd)),
                        wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def make_tool_window(hwnd):
    """Keep it out of the taskbar and out of the way of focus."""
    try:
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                          ctypes.c_uint]
        user32.SetWindowLongW.restype = ctypes.c_long
        h = clean_handle(hwnd)
        style = user32.GetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE) & 0xFFFFFFFF
        user32.SetWindowLongW(wintypes.HWND(h), GWL_EXSTYLE,
                              style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception:
        pass


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception:
        return False


def mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def blend(hex_colour, level):
    """Fade a colour towards the pill body: the dot dims, it does not go black."""
    try:
        h = hex_colour.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        bg = PILL_BG.lstrip("#")
        br, bgc, bb = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
        k = max(0.0, min(1.0, float(level)))
        return "#%02x%02x%02x" % (int(br + (r - br) * k),
                                  int(bgc + (g - bgc) * k),
                                  int(bb + (b - bb) * k))
    except Exception:
        return hex_colour


def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """A rounded rectangle as a smoothed polygon.

    Tk has no rounded rectangle primitive. A smoothed polygon is the honest way
    to get one on a plain canvas: every point is exact, the curve is Tk's own
    spline, and it costs one item.
    """
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kwargs)


class Island:
    def __init__(self, args):
        self.args = args
        self.log_path = args.log or ""
        self.labels = self._load_labels()
        self.screen = (0, 0, 0, 0)
        self.status_mtime = None
        self.visible = False
        self.expanded = False
        self.phase = ""
        self.tool = ""
        self.since = 0.0
        self.stop_pending = False
        self.intro_job = None
        self.conversation = ""

    # -- logging ----------------------------------------------------------

    def note(self, message):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
        except Exception:
            pass

    def _load_labels(self):
        defaults = {
            "islandThinking": "思考中",
            "islandTool": "调用",
            "islandRead": "读取",
            "islandStop": "暂停",
        }
        doc = read_json(self.args.labels) if self.args.labels else None
        if isinstance(doc, dict):
            for k, v in doc.items():
                if isinstance(v, str):
                    defaults[k] = v
        return defaults

    # -- window -----------------------------------------------------------

    def build(self):
        x, y, w, h = virtual_screen()
        self.screen = (x, y, w, h)

        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=CHROMA)
        try:
            root.attributes("-transparentcolor", CHROMA)
        except Exception as e:
            self.note("transparentcolor failed: %s" % e)
        try:
            # Slightly translucent, so the pill reads as an overlay rather than a
            # solid slab sitting on the wallpaper.
            root.attributes("-alpha", 0.93)
        except Exception as e:
            self.note("alpha failed: %s" % e)
        root.attributes("-topmost", True)

        canvas = tk.Canvas(root, bg=CHROMA, highlightthickness=0, bd=0,
                           width=self.compact_width(), height=self.compact_height())
        canvas.pack(fill="both", expand=True)

        self.root = root
        self.canvas = canvas
        root.update_idletasks()
        root.update()
        self.hwnd = top_level_of(int(root.winfo_id()))
        make_tool_window(self.hwnd)
        set_topmost(self.hwnd)
        self.note("island up %dx%d at %d,%d" % (w, h, x, y))

        # Start hidden: the pill appears when work starts, and a readout that is
        # always there stops being information.
        root.withdraw()

    def compact_height(self):
        return 40

    def compact_width(self):
        return 260

    def expanded_height(self):
        return 72

    def expanded_width(self):
        return 400

    # -- drawing ----------------------------------------------------------

    def _draw(self, width, height, expanded):
        """Repaint the pill. Called on every state change, not every tick."""
        c = self.canvas
        c.delete("all")
        c.configure(width=width, height=height)
        # Body: rounded, dark, with a hairline edge so it separates from any
        # wallpaper without needing a shadow.
        round_rect(c, 1, 1, width - 1, height - 1, height / 2.0,
                   fill=PILL_BG, outline=PILL_EDGE)

        pad = 16
        dot_r = 9
        dot_y = height / 2.0
        # The dot breathes; its fill is updated separately each tick so the
        # motion costs a colour change, not a full redraw.
        self.dot = c.create_oval(pad, dot_y - dot_r / 2.0,
                                 pad + dot_r, dot_y + dot_r / 2.0,
                                 fill=DOT_THINK, outline="")

        text_x = pad + dot_r + 10
        if expanded:
            # The intro beat: two lines, STATUS on top and the conversation TITLE
            # below. The status is what the user is waiting on and what keeps
            # changing, so it takes the larger line; the title says which chat
            # this is, and is only ever shown during this opening beat.
            #
            # WARNING: the title is the CONVERSATION's name ("查看 dsh伴随者
            # 文件夹内容"), never the status text. Substituting the status here made
            # the pill read "思考中 / 思考中" -- a title that says nothing.
            status = self._status_text()
            c.create_text(text_x, dot_y - 15, anchor="w",
                          text=status, fill=TEXT_FG,
                          font=("Microsoft YaHei UI", 12, "bold"))
            if self.conversation != "":
                c.create_text(text_x, dot_y + 14, anchor="w",
                              text=self.conversation, fill=TEXT_DIM,
                              font=("Microsoft YaHei UI", 9))
            self.text = None
        else:
            self.text = c.create_text(text_x, dot_y, anchor="w",
                                      text=self._status_text(), fill=TEXT_FG,
                                      font=("Microsoft YaHei UI", 10))

        # Elapsed clock, right-aligned before the button.
        self.elapsed = c.create_text(width - pad - 34, dot_y, anchor="e",
                                     text="", fill=TEXT_DIM,
                                     font=("Segoe UI", 9))

        # The pause button: a rounded red square, matching the pill's own
        # corners rather than sitting in it as a hard-edged block. Two bars
        # inside would read as "pause"; one shape reads as "stop", which is what
        # it does.
        bx = width - pad - 18
        self.stop = round_rect(c, bx, dot_y - 9, bx + 18, dot_y + 9, 6,
                               fill=STOP_RED, outline="")
        c.tag_bind(self.stop, "<ButtonPress-1>",
                   lambda _e: self.request_stop())
        c.tag_bind(self.stop, "<Enter>",
                   lambda _e: c.itemconfigure(self.stop, fill=STOP_RED_HOVER))
        c.tag_bind(self.stop, "<Leave>",
                   lambda _e: c.itemconfigure(self.stop, fill=STOP_RED))
        c.configure(cursor="hand2")
        c.tag_bind(self.stop, "<Enter>",
                   lambda _e: c.configure(cursor="hand2"))

    def _status_text(self):
        if self.phase == "read" and self.tool != "":
            return "%s %s" % (self.labels["islandRead"], self.tool)
        if self.phase == "tool" and self.tool != "":
            return "%s %s" % (self.labels["islandTool"], self.tool)
        return self.labels["islandThinking"]

    def _dot_colour(self):
        if self.phase == "read" and self.tool != "":
            return DOT_READ
        if self.phase == "tool" and self.tool != "":
            return DOT_TOOL
        return DOT_THINK

    def _place(self, width, height):
        x, y, sw, sh = self.screen
        self.root.geometry("%dx%d+%d+%d" % (width, height,
                                            x + (sw - width) // 2, y + 10))

    def show(self):
        if self.visible:
            return
        try:
            self.root.deiconify()
            # The intro only makes sense when there is a conversation name to
            # show: growing the pill to reveal nothing but the same status it
            # already had is motion without information. With no name, it opens
            # at its compact size and stays there.
            self.expanded = self.conversation != ""
            w = self.expanded_width() if self.expanded else self.compact_width()
            h = self.expanded_height() if self.expanded else self.compact_height()
            self._draw(w, h, self.expanded)
            self._place(w, h)
            self.root.update_idletasks()
            set_topmost(self.hwnd)
            self.visible = True
            self.note("island shown (%s)" % ("intro" if self.expanded else "compact"))
            if not self.expanded:
                return
            # The intro beat: grow, show the conversation name, then settle into
            # the compact readout. One timer, cancelled if the work ends first.
            if self.intro_job is not None:
                try:
                    self.root.after_cancel(self.intro_job)
                except Exception:
                    pass
            self.intro_job = self.root.after(INTRO_MS, self.collapse)
        except Exception as e:
            self.note("island show failed: %s" % e)

    def collapse(self):
        """Shrink from the intro card into the compact readout.

        Animated rather than snapped: the pill is telling the user "here is the
        conversation, now here is the work", and a jump makes it look like two
        unrelated things. The width and height ease together over SHRINK_MS, and
        the final frame draws the compact content.
        """
        self.intro_job = None
        if not self.visible:
            return
        try:
            from_w, from_h = self.expanded_width(), self.expanded_height()
            to_w, to_h = self.compact_width(), self.compact_height()
            steps = max(1, SHRINK_MS // 16)
            self.expanded = False

            def frame(i):
                if not self.visible:
                    return
                k = i / float(steps)
                # Ease out: fast at first, settling at the end, which reads as
                # "putting itself away" rather than "sliding".
                e = 1.0 - (1.0 - k) * (1.0 - k)
                w = int(from_w + (to_w - from_w) * e)
                h = int(from_h + (to_h - from_h) * e)
                if i >= steps:
                    self._draw(to_w, to_h, False)
                    self._place(to_w, to_h)
                else:
                    self._draw(w, h, True if k < 0.5 else False)
                    self._place(w, h)
                self.root.update_idletasks()
                set_topmost(self.hwnd)
                if i < steps:
                    self.root.after(16, lambda: frame(i + 1))
                else:
                    self.note("island collapsed")

            frame(0)
        except Exception as e:
            self.note("island collapse failed: %s" % e)

    def hide(self):
        if not self.visible:
            return
        if self.intro_job is not None:
            try:
                self.root.after_cancel(self.intro_job)
            except Exception:
                pass
            self.intro_job = None
        try:
            self.root.withdraw()
        except Exception:
            pass
        self.visible = False
        self.expanded = False
        self.note("island hidden")

    # -- state ------------------------------------------------------------

    def update(self):
        """Read status.json; show, redraw or hide accordingly."""
        path = self.args.status or ""
        if path == "":
            return
        stamp = mtime(path)
        if stamp is None:
            if self.visible:
                self.hide()
            return
        doc = read_json(path)
        if not isinstance(doc, dict):
            return
        if doc.get("busy") is not True:
            if self.visible:
                self.hide()
            return
        # A file nobody has touched for a while means the Host is gone; a pill
        # that keeps claiming work is worse than no pill.
        if time.time() - stamp > STALE_MS / 1000.0:
            if self.visible:
                self.hide()
            return

        phase = str(doc.get("phase") or "")
        tool = str(doc.get("tool") or "")
        try:
            since = float(doc.get("since") or 0)
        except Exception:
            since = 0.0
        conversation = str(doc.get("conversation") or doc.get("title") or "")
        changed = (phase != self.phase or tool != self.tool
                   or conversation != self.conversation)
        gained_name = conversation != "" and self.conversation == ""
        self.phase, self.tool, self.since = phase, tool, since
        self.conversation = conversation

        if not self.visible:
            self.show()
            # show() draws the intro with whatever state was already read, so a
            # redraw is only needed when the state moves while it is on screen.
            return
        # The conversation name arrives asynchronously from the Host (it is read
        # from the session), so it can land after the pill is already up. That is
        # the moment to run the intro: the name is the only reason to grow.
        if gained_name and not self.expanded:
            self.run_intro()
            return
        # WARNING: no mtime gate here. The status file is rewritten on every
        # event, and mtime resolution on Windows is coarse enough that two
        # different states can share one timestamp -- measured: the pill kept
        # showing "思考中" while status.json said "tool / pwsh". The parsed
        # fields are the truth; redraw whenever they change.
        if changed:
            if self.expanded:
                self._draw(self.expanded_width(), self.expanded_height(), True)
            else:
                self._draw(self.compact_width(), self.compact_height(), False)

    def run_intro(self):
        """Grow, show the conversation name, then settle back to the readout."""
        try:
            if self.intro_job is not None:
                try:
                    self.root.after_cancel(self.intro_job)
                except Exception:
                    pass
                self.intro_job = None
            self.expanded = True
            w, h = self.expanded_width(), self.expanded_height()
            self._draw(w, h, True)
            self._place(w, h)
            self.root.update_idletasks()
            set_topmost(self.hwnd)
            self.note("island intro (name arrived)")
            self.intro_job = self.root.after(INTRO_MS, self.collapse)
        except Exception as e:
            self.note("island intro failed: %s" % e)

    def tick(self):
        try:
            self.update()
            if self.visible:
                self._animate()
        except Exception as e:
            self.note("tick: %s" % e)
        try:
            self.root.after(POLL_MS, self.tick)
        except Exception:
            pass

    def _animate(self):
        """Move the clock and breathe the dot: motion says 'still going'."""
        try:
            if self.since > 0:
                total = int((time.time() * 1000.0 - self.since) // 1000)
                if total >= 0:
                    mins, secs = divmod(total, 60)
                    label = ("%dm%02ds" % (mins, secs)) if mins >= 1 else ("%ds" % secs)
                    if getattr(self, "elapsed", None) is not None:
                        self.canvas.itemconfigure(self.elapsed, text=label)
        except Exception:
            pass
        try:
            level = 0.35 + 0.65 * (abs(((time.time() * 10.0) % 20.0) - 10.0) / 10.0)
            if getattr(self, "dot", None) is not None:
                self.canvas.itemconfigure(self.dot,
                                          fill=blend(self._dot_colour(), level))
        except Exception:
            pass

    # -- the pause button -------------------------------------------------

    def request_stop(self):
        """Ask the Host to stop the running turn.

        Written to the same stop signal the window has always watched, so there
        is one cancel path rather than two. A second press while a request is in
        flight is ignored: the button sits under the pointer, and two cancels
        must not queue.
        """
        if self.stop_pending:
            return
        self.stop_pending = True
        path = self.args.stop_signal or ""
        if path != "":
            write_text(path, str(int(time.time() * 1000)))
            self.note("stop requested")
        try:
            self.root.after(1200, self._clear_stop_pending)
        except Exception:
            self.stop_pending = False

    def _clear_stop_pending(self):
        self.stop_pending = False

    def run(self):
        self.build()
        self.root.after(POLL_MS, self.tick)
        try:
            self.root.mainloop()
        finally:
            self.note("island down")


def _claim_single_instance(status_path):
    """Take a one-island lock atomically. False means another island owns it.

    WARNING: without this, every Host restart left the previous island alive and
    started another. Measured: 18 island processes were running at once, each with
    its own always-on-top window at the same spot, so the screen had a stack of
    overlapping pills and the BOARD could not be brought to the front because
    topmost windows were piled above it.

    O_CREAT|O_EXCL is the reliable primitive: the kernel decides which process
    creates the file, so two starts in the same millisecond cannot both win. A
    stale lock (its pid is gone) is cleared and retried once.
    """
    lock_path = os.path.join(os.path.dirname(os.path.abspath(status_path)),
                             "island.lock")
    for _attempt in (1, 2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            holder = 0
            try:
                with open(lock_path, encoding="utf-8-sig") as f:
                    holder = int(f.read().strip() or 0)
            except Exception:
                holder = 0
            if holder > 0 and _pid_alive(holder):
                return ""
            try:
                os.remove(lock_path)
            except Exception:
                return ""
        except Exception:
            # Cannot create the lock at all: refusing to run would be worse than
            # running without the guard.
            return ""
    return ""


def _pid_alive(pid):
    """Whether a pid is a live process.

    OpenProcess succeeding is not enough on Windows: a handle can be opened for a
    terminated process. The exit code is what settles it.
    """
    try:
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259        # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--stop-signal", required=True)
    ap.add_argument("--labels", default="")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    # One island per install. See _claim_single_instance for why.
    lock_path = _claim_single_instance(args.status)
    if lock_path == "":
        sys.stderr.write("[island] another island is running, exiting\n")
        return 0

    # WARNING: clear a stale stop signal BEFORE the window starts. The stop button
    # writes stop.txt and nothing ever removes it -- the Host reads it, but a Host
    # restart (or a plugin reload) leaves the file behind. On the next start the
    # signal was already there, so the island was treated as "the user asked me to
    # stop" and never appeared: measured, the pill vanished at 22:33 and was still
    # gone two hours later while status.json kept being written.
    #
    # A stop request is a message to the Host, not a persistent setting, so a new
    # island owns a clean channel.
    try:
        if args.stop_signal and os.path.isfile(args.stop_signal):
            os.remove(args.stop_signal)
            sys.stderr.write("[island] cleared a stale stop signal\n")
    except Exception:
        pass

    become_aware()
    island = Island(args)
    try:
        island.run()
    except Exception as e:
        island.note("fatal: %s" % e)
        return 1
    finally:
        # Give the lock back so the next start is not blocked by our own corpse.
        if lock_path:
            try:
                with open(lock_path, encoding="utf-8-sig") as f:
                    if int(f.read().strip() or 0) == os.getpid():
                        os.remove(lock_path)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
