# -*- coding: utf-8 -*-
"""
deskpet -- whale desk pet (pywebview).

    python exp/deskpet.py

ATTRIBUTION
-----------
This file is derived from other open-source work. The chain is:

    Yifffan/dsh-plugin-whale-pet 0.2.9   (original project)
            -> fork
    rongzi5/dsh-whale-pet                (the version taken here)
            -> modified
    DSH 涂鸦肥鱼 / dsh-doodle-fat-fish   (this project)

  * Code: MIT License. Copyright the original authors, Yifffan and rongzi5.
  * Art assets (whale.ico, dist/): CC BY 4.0. Attribution required; no
    modifications were made to the artwork itself.

Changes made here: the state channel reads this plugin's status.json, the
single-instance guard uses an O_CREAT|O_EXCL lock file, the state directory
arrives through --state-path-file (a Chinese path on the command line turned
into mojibake through the ANSI code page), and nothing outside the plugin
folder is referenced.

See THIRD-PARTY-NOTICES.md in the plugin root for the full statement.

Layout
------
  * main window "whale-pet"  : frameless + transparent + on_top,
                               loads ?mode=main, only the whale itself.
  * one window per popup     : menu / chat / memory / name / size,
                               each built from its own recipe (PANE_SPECS).
  * tray icon                : click toggles show/hide (no keyboard hotkey).

Every popup window follows the same startup as the whale window:
spawn off-screen, then hide()+show() to make transparency take effect,
then get placed next to the anchor when the component opens.

Why not one big panel window: each component has a very different size
(menu 156x200, memory 288x292), and a single window would either cover the
whale or waste transparent area.

Dependencies: pywebview, pythonnet and their native libraries are bundled in
    ball/runtime/, inside this plugin. Nothing outside the plugin folder is used.
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(ROOT)

# How patient the whale is with the Host's heartbeat.
#
# The heartbeat is written by the plugin every second with a Node setInterval, so
# it stops whenever the plugin's event loop is blocked -- a synchronous read of
# 33 MB of session logs blocked it for a second, and on a slower machine that
# crossed the old five-second line. The whale then animated itself out of
# existence while DSH was open, which is the one thing it must never do.
#
# A pause in the Host is not "DSH closed". Twenty seconds of silence plus three
# consecutive misses (~22 s total) leaves a real exit noticed quickly enough while
# making a hitch invisible.
HEARTBEAT_TOLERANCE_S = 20.0
MISS_LIMIT = 3


def _redirect_log():
    """把 stdout/stderr 落到球的状态目录里。

    自己写，不靠 PowerShell 的重定向：那条路试过两次都不成 ——
    .NET 事件回调里 $script: 不指向脚本作用域（日志 0 字节），
    而拼一个 .cmd 启动器会被引号拆行（启动器本身是坏的）。

    日志是排查的唯一线索：没有它，棋盘那边只能看到
    「ball: start timeout」，不知道为什么。
    """
    path = None
    argv = list(sys.argv)
    for i, arg in enumerate(argv):
        if arg == "--state-path-file" and i + 1 < len(argv):
            try:
                with open(argv[i + 1], encoding="utf-8-sig") as f:
                    state_dir = f.read().strip()
                path = os.path.join(state_dir, "ball", "ball.log")
            except Exception:
                path = None
    if path is None:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
    except Exception:
        pass

# pywebview lives INSIDE this plugin (ball/runtime/). Nothing outside the folder
# is consulted: an earlier version also added a legacy path on the Desktop, and
# that path won the import race -- the whale then failed with "pythonnet cannot
# be loaded" because the Desktop copy's dependencies do not match the bundle's.
LIB_PYWEBVIEW = os.path.join(HERE, "runtime")
if os.path.isdir(LIB_PYWEBVIEW):
    if LIB_PYWEBVIEW in sys.path:
        sys.path.remove(LIB_PYWEBVIEW)
    sys.path.insert(0, LIB_PYWEBVIEW)

# pythonnet must be told which runtime to use BEFORE `import clr` can work, and
# the choice is not free:
#
#   coreclr (.NET 8)  has no GUI framework in the bundled redistributable
#   netfx   (.NET 4)   is where System.Windows.Forms lives
#
# pywebview's Windows backend is WinForms, so netfx is the one that works here.
# Even then, pythonnet does not expose Framework assemblies by name until they
# are referenced explicitly -- without the AddReference calls below, `import
# System.Windows.Forms` fails with ModuleNotFoundError and pywebview reports
# "pythonnet cannot be loaded", which is misleading: pythonnet loaded fine.
try:
    from pythonnet import load as _pn_load
    _pn_load("netfx")
    import clr as _clr
    _clr.AddReference("System.Windows.Forms")
    _clr.AddReference("System.Drawing")
except Exception as _e:  # pragma: no cover - reported through the log
    sys.stderr.write("[deskpet] pythonnet setup failed: %s\n" % _e)


MENU_EXTRA_JS = r"""
(function () {
  // 桌面球比 DSH 面板多了三件事：开始绘制 / 唤起 agent / 一键清除。
  // 原版菜单不认识画板，所以把三项插到最前面，用原版的按钮样式，
  // 点击走同一条 pywebview.api 通道。
  //
  // ⚠ 不要用 window.__xxx 做「只跑一次」的闸。之前那样写，第一次调用
  //   发生在菜单还没显示时（元素不存在），闸却已经关上了，后面每次
  //   调用都直接 return —— 表现为 nodes 永远是 0。
  //   改成幂等的：每次调用都重新找元素，找到且没插过才插。

  var api = window.pywebview && window.pywebview.api;
  if (!api || !api.ball_menu_action) return 0;

  // ⚠ 不按 class 名找：那是 CSS Modules 的产物（v2b488c_menu），
  //   换个构建就变。页面上那层 <div role="menu"> 才是稳定的锚点。
  var menu = document.querySelector('[role="menu"]');
  if (!menu) {
    var all = document.querySelectorAll('[class*="menu"]');
    for (var i = 0; i < all.length; i++) {
      if (all[i].querySelector('button')) { menu = all[i]; break; }
    }
  }
  if (!menu) return 0;
  if (menu.querySelector('[data-ball-extra]')) return menu.querySelectorAll('[data-ball-extra]').length;

  var sample = menu.querySelector('button');
  var cls = sample ? sample.className : '';
  var first = menu.firstChild;
  var items = [['ink', '开始绘制'], ['call', '唤起 agent'], ['clear', '一键清除']];

  items.forEach(function (pair) {
    var b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('data-ball-extra', pair[0]);
    b.className = cls;
    b.textContent = pair[1];
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      var a = window.pywebview && window.pywebview.api;
      if (a && a.ball_menu_action) a.ball_menu_action(pair[0]);
    });
    menu.insertBefore(b, first);
  });

  var sep = document.createElement('div');
  sep.setAttribute('data-ball-extra', 'sep');
  sep.style.cssText = 'height:1px;margin:5px 6px;background:rgba(35,49,94,.14)';
  menu.insertBefore(sep, first);

  // 这三项在桌面球上用不了，直接不显示（只删 UI，功能代码原样留着）：
  //
  //   聊天   —— 要一条同源的 LLM 代理，球的窗口是独立的，拿不到 DSH 的
  //             会话与凭证
  //   记忆   —— 记忆存在 DSH 的会话里，球这边看不到
  //   命名   —— 名字属于 DSH 面板里那只鲸鱼，桌面球没有那个面板
  //
  // 留着只会点了出错或没反应，所以整项隐藏。
  var HIDE = ['聊天', '记得什么', '命名'];
  var buttons = menu.querySelectorAll('button');
  for (var k = 0; k < buttons.length; k++) {
    var b2 = buttons[k];
    if (b2.hasAttribute('data-ball-extra')) continue;
    var label = b2.textContent || '';
    for (var hi = 0; hi < HIDE.length; hi++) {
      if (label.indexOf(HIDE[hi]) >= 0) {
        b2.setAttribute('data-ball-extra', 'hidden-item');
        b2.style.display = 'none';
        break;
      }
    }
  }

  // 这里不再量尺寸、不再调 fit_menu。
  //
  // 菜单尺寸是写死的（PANE_SPECS["menu"] = 203x277，就是下面这 9 项加
  // padding 的实测值）。窗口在创建时就用这个尺寸、按 _pane_rect 算好的
  // 位置一次显示到位 —— 不会先出现在右边再被挪到左边。
  // 如果以后增减菜单项，改 PANE_SPECS 里那个 w/h 即可。

  return menu.querySelectorAll('[data-ball-extra]').length;
})();
"""


def _inject_when_ready(win, seconds=4.0):
    """在接下来几秒里反复试注入，直到页面真的出现了那几个按钮。

    evaluate_js 在 React 还没挂载时也会成功返回 —— 但页面上什么都没有，
    所以它返回的成功没有意义。这里改用「注入后再数一遍节点」来判定。
    """
    end = time.time() + seconds
    last_error = None
    counts = []
    while time.time() < end:
        try:
            win.evaluate_js(MENU_EXTRA_JS)
            count = win.evaluate_js(
                "document.querySelectorAll('[data-ball-extra]').length")
            counts.append(count)
            if isinstance(count, (int, float)) and count > 0:
                sys.stderr.write("[ball] menu extras injected, nodes=%s\n" % count)
                return
        except Exception as e:
            last_error = e
        time.sleep(0.25)
    sys.stderr.write("[ball] menu extras not injected (nodes=%s err=%s)\n"
                     % (counts[-1] if counts else "?", last_error))


def inject_menu_extra(win):
    """给原版菜单窗插进画板那三项（页面自己不会画它们）。"""
    if not BALL.get("state_dir"):
        return
    threading.Thread(target=_inject_when_ready, args=(win, 6.0), daemon=True).start()


def write_status(status, error=""):
    """告诉棋盘「球起来了」。

    pencil.ps1 靠这个文件判断要不要退回那个丑丑的 WPF 圆点。
    原版没有这份通知（它是唯一的门，不需要自证），但在这里球是棋盘的
    子进程，棋盘得知道它到底起没起来。
    """
    if not BALL.get("state_dir"):
        return
    payload = {"status": status, "error": error, "pid": os.getpid(),
               "at": int(time.time() * 1000)}
    try:
        path = os.path.join(BALL["state_dir"], "ball", "ball-status.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        sys.stderr.write("[ball] status write failed: %s\n" % e)


def board_intent(word):
    """把一个菜单意图交给棋盘：写 tray.txt，pencil.ps1 会读走。"""
    base = BALL.get("state_dir") or ""
    if not base:
        return
    try:
        with open(os.path.join(base, "tray.txt"), "w", encoding="utf-8") as f:
            f.write(str(word))
        sys.stderr.write("[ball] intent %s\n" % word)
    except Exception as e:
        sys.stderr.write("[ball] intent failed: %s\n" % e)


def board_watch():
    """心跳停了，鲸鱼才退出。

    ⚠ 判据只有一个：**alive.txt 还在不在刷新**。

      alive.txt 由 DSH 插件的心跳线程每秒写一次。它代表「插件进程还在跑」，
      而插件进程就是 DSH 进程本身 —— 所以：

        DSH 窗口显示着   → 插件在跑 → 心跳在写 → 鲸鱼活
        DSH 缩到托盘     → 插件**仍然在跑** → 心跳照写 → 鲸鱼活 ✓
        DSH 真的关掉     → 插件没了 → 心跳停 → 鲸鱼退出 ✓
        DSH 被强杀/崩溃  → 同上 → 鲸鱼退出 ✓

    ⚠ 为什么**不再看窗口**：
      用户把 DSH 缩到托盘时，Electron 会销毁主窗口，EnumWindows 里找不到。
      以前拿窗口当判据，一到托盘就判「DSH 死了」，鲸鱼演完动画退出；
      画板立刻把它拉起来，起来又判死 —— 这就是「不断开启又关闭」的死循环。

    ⚠ 为什么**不再看进程**：
      实测这台机器上 `tasklist`、`Get-CimInstance`、`Get-Process` 全部
      看不到 CodeBuddy.exe（明明有 9 个在跑，四种查法都返回 0）。
      基于进程名的判据在这里根本不可用。

    宽限：HEARTBEAT_TOLERANCE_S 老化 + MISS_LIMIT 次 miss（约 22 秒）。

    ⚠ 这个宽度是被一次真实故障定下来的：心跳是插件里的 setInterval，
      插件的事件循环一被同步操作卡住，心跳就断。召唤流程里同步解压
      33MB 会话日志卡了约 1 秒，慢一点的机器就越过了当时 5 秒的线，
      画板和鲸鱼一起判死 —— 用户看到的是「agent 一看图鲸鱼就没了」。
      主机卡一下不等于 DSH 关了，所以这里宁可迟钝。
    """
    base = BALL.get("state_dir") or ""
    if not base:
        return
    path = os.path.join(base, "alive.txt")
    boot = time.time()
    misses = 0
    while True:
        time.sleep(0.8)
        try:
            if not os.path.isfile(path):
                misses += 1
            else:
                age = time.time() - os.path.getmtime(path)
                misses = 0 if age < HEARTBEAT_TOLERANCE_S else misses + 1
        except Exception:
            misses += 1

        if time.time() - boot <= 8:
            continue
        # WARNING: this used to be `age < 4` and 2 misses, about five seconds. The
        # heartbeat is a Node setInterval, so a blocked event loop stops it -- the
        # Host's own synchronous session-log read blocked it for a second, and on
        # a slower machine that crossed the line. The whale then performed its
        # exit animation and vanished while DSH was still wide open, which is the
        # one thing it must never do.
        #
        # The whale leaves when DSH leaves. A pause in the Host is not that, so the
        # tolerance is generous on purpose; a genuine exit is still noticed.
        if misses >= MISS_LIMIT:
            sys.stderr.write("[ball] board heartbeat gone, exiting\n")
            _exit_with_spin("board heartbeat gone")


def _exit_with_spin(reason):
    """退出：**先把窗口藏起来**，动画照旧跑完，然后关进程。

    ⚠ 顺序是「先隐藏 → 再关闭」，不是「先演完动画 → 再隐藏」。
      以前是先演 1.25 秒的旋转缩小、演完才隐藏，用户必须盯着那一秒多
      才看到鲸鱼消失 —— 关 DSH 时这 1.25 秒是多余的等待。

      现在：窗口一藏，屏幕上立刻干净；动画仍在后台执行完（保持逻辑完整、
      不会留下半截状态），然后再结束进程。

    ⚠ 为什么还要执行动画而不是直接杀进程：
      直接杀会让 WebView2 的渲染状态停在半路，下次启动可能带着残留
      （缩小的 scale、歪着的角度）。让它跑完，页面状态是干净的。

    ⚠ 为什么用 TerminateProcess 而不是 os._exit：
      os._exit 在这条路径上会被 pywebview / WebView2 的收尾钩子拦住，
      进程和窗口都留着（实测）。TerminateProcess 不跑清理，直接结束。
    """
    sys.stderr.write("[ball] exit(%s)\n" % reason)
    try:
        sys.stderr.flush()
    except Exception:
        pass

    # 1. 先藏窗口 —— 屏幕上立刻干净，这是用户看到的那一步。
    try:
        hwnd = _find_hwnd(_MAIN_TITLE)
        if hwnd:
            _u32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass

    # 2. 动画照旧执行完（窗口已藏，用户看不见，但页面状态会走完）。
    try:
        whale_dive(leaving=True)
    except Exception as e:
        sys.stderr.write("[ball] spin failed: %s\n" % e)
    time.sleep(1.15)

    # 3. 关进程。
    # ⚠ 这里不再往 stderr 写：stderr 是管道，缓冲满了 write 会一直阻塞。
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        handle = kernel32.OpenProcess(0x0001, 0, os.getpid())  # PROCESS_TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 0)
    except Exception:
        pass
    os._exit(0)


def _wait_whale_ready(seconds):
    """等页面把鲸鱼画出来（window.__whale.whalePet 存在）。"""
    end = time.time() + seconds
    while time.time() < end:
        win = _WIN.get("ref")
        if win is not None:
            try:
                if win.evaluate_js(
                        "(function(){return !!(window.__whale && "
                        "window.__whale.whalePet);})()"):
                    return True
            except Exception:
                pass
        time.sleep(0.05)
    return False


def _wait_scale_report(seconds):
    """等页面报一次比例（set_scale），返回报上来的值；没等到就是 None。"""
    end = time.time() + seconds
    while time.time() < end:
        value = _scale_report["value"]
        if value is not None:
            return value
        time.sleep(0.05)
    return None


def _settled_pos(seconds, tolerance=24):
    """等页面报的位置**稳定**下来，返回落点。

    ⚠ 不能拿第一份报告：窗口刚在屏幕外建好时，页面报的是它自己推算的位置，
      第一份常常是错的（实测从 (906,152) 下一份就跳到 (1136,189)）。
      拿错的那份当落点，窗口挪过去后页面立刻纠正 —— 屏幕上就是一步瞬移。

    判定：连续两份报告的差距 <= tolerance 才算稳定。鲸鱼一直在游，所以
    tolerance 不能设成 0；60ms 一帧的游动幅度只有几像素，24 足够区分
    「正常游动」和「一次性纠正」。
    """
    end = time.time() + seconds
    last = None
    while time.time() < end:
        if _pos_report["seen"]:
            cur = (_pos_report["x"], _pos_report["y"])
            if last is not None:
                dx = abs(cur[0] - last[0])
                dy = abs(cur[1] - last[1])
                if dx <= tolerance and dy <= tolerance:
                    return cur
            last = cur
        time.sleep(0.06)
    if _pos_report["seen"]:
        return (_pos_report["x"], _pos_report["y"])
    return None


def _rebuild_main_offscreen(api, scale, animate=True):
    """按新比例重建鲸鱼窗口，然后从极小长出来。

    ⚠ 新窗口是**隐藏着、直接在落点上**创建的（见 _create_main_window），
      所以这里既不用挪窗口，也不会闪 —— 窗口出现之前鲸鱼已经被缩到极小。

    ⚠ 落点用重建前的位置：新窗口创建时就摆在那儿，页面加载后自己会
      从 localStorage 恢复它记的位置（通常一致）。即使差一点，也是在
      鲸鱼只有 scale=0.001 的时候差的，看不见。

    animate=False（改比例走这条）：尺寸已经对了，露出来即可，不演长大动画。
    """
    _offscreen["hold"] = True
    _intro["running"] = True
    try:
        _rebuild_main(api, scale)
        # 比例已经落到 _main_scale 上了，欠账清掉，别再被 _finish_intro 补一次。
        _rebuild["pending"] = None
        _wait_whale_ready(1.6)
        # 等页面加载完（on_loaded 只记一个标志，不 show 任何东西）。
        end = time.time() + 2.0
        while time.time() < end and not _loaded["done"]:
            time.sleep(0.05)
        # 等页面报一次位置，让 _main_pos 跟上新页面（不挪窗口，只是记账）。
        end = time.time() + 1.0
        while time.time() < end and not _pos_report["seen"]:
            time.sleep(0.05)
        target = (_main_pos["x"], _main_pos["y"])
        sys.stderr.write("[ball] rebuild target (%d,%d)\n" % target)
        _rebuild["placed"] = True

        # 缩到极小 → 挪到落点（藏着，看不见）→ show → （可选）长大。
        whale_shrink_now()
        target = _settled_pos(0.8) or (_main_pos["x"], _main_pos["y"])
        moved = _move_window(int(target[0]), int(target[1]))
        _main_pos["x"] = int(target[0])
        _main_pos["y"] = int(target[1])
        sys.stderr.write("[ball] rebuild move to (%d,%d) ok=%s\n"
                         % (target[0], target[1], moved))
        time.sleep(0.08)
        _show_main_window(True)
        if animate:
            whale_dive(leaving=False)
            _offscreen["hold"] = False
            time.sleep(1.15)
        else:
            time.sleep(0.15)
            _offscreen["hold"] = False
        whale_reset_clip()
    except Exception as e:
        sys.stderr.write("[ball] offscreen rebuild failed: %s\n" % e)
    finally:
        _finish_intro()


def _intro_spin():
    """开机出场：把已经在落点上、隐藏着的窗口露出来，并从极小长到正常大小。

    ⚠ 这个函数**一次都不挪窗口**。窗口创建时就在落点、且 hidden=True
      （见 _create_main_window），所以只要「缩小 → show → 长大」三步。

    ⚠ 以前为什么闪：
      窗口先在屏幕外出生，然后「缩到极小 → 启动动画 → 等 120ms → 挪回落点」。
      动画是缓出（ease = 1-(1-t)²，前段长得最快），120ms 时鲸鱼已经长到
      21.6% —— 挪窗口那一刻，屏幕上就是一头中等大小的鲸鱼凭空出现在中央。
      只要还要挪窗口，就总有一帧是「已经长大、位置还在旧处」。
      把窗口从一开始就放在落点，这个矛盾就不存在了。

    ⚠ 比例不一致要先重建：窗口按默认 1.0 建，页面报的常是 0.5。重建也走
      「隐藏着在落点建 → 缩到极小 → show」同一条路，所以同样不闪。
    """
    _offscreen["hold"] = True
    _intro["running"] = True
    # 页面可能在我们开场之前就报过比例、排了一个去抖重建 —— 取消它，
    # 交给下面这段统一处理，免得两条路同时重建。
    if _rebuild["timer"] is not None:
        try:
            _rebuild["timer"].cancel()
        except Exception:
            pass
        _rebuild["timer"] = None

    # 等页面把鲸鱼画上去（窗口是隐藏的，画在哪儿都看不见）。
    _wait_whale_ready(1.2)

    # 等页面报位置与比例。位置决定落点，比例决定要不要重建。
    _settled_pos(1.5)
    scale = _wait_scale_report(1.5)
    if scale is not None and abs(scale - _main_scale["value"]) >= 0.001:
        sys.stderr.write("[ball] intro: rebuild %.2f -> %.2f\n"
                         % (_main_scale["value"], scale))
        _rebuild_main_offscreen(_api["ref"], scale)
        return

    try:
        # 缩到极小（窗口还藏着，屏幕上什么都没有）。
        whale_shrink_now()
        # ⚠ 在**藏着**的时候把窗口挪到页面要的位置。
        #   创建窗口时页面还没加载，_main_pos 还是占位 (200,200)，只能先用它建。
        #   页面加载后会报出真实位置（从 localStorage 恢复的），这里补上那一次
        #   挪动 —— 此时窗口不可见，挪多远都看不见。这是整个流程里唯一一次
        #   位移，而且发生在 show 之前。
        target = _settled_pos(0.8) or (_main_pos["x"], _main_pos["y"])
        _move_window(int(target[0]), int(target[1]))
        _main_pos["x"] = int(target[0])
        _main_pos["y"] = int(target[1])
        # 等它真的画出来，否则 show 的第一帧可能还是正常大小 —— 又闪一下。
        time.sleep(0.08)
        _show_main_window(True)
        # 从这一点开始长大。
        whale_dive(leaving=False)
        _offscreen["hold"] = False
        _rebuild["placed"] = True
        time.sleep(1.15)
        whale_reset_clip()
    except Exception as e:
        sys.stderr.write("[ball] intro spin failed: %s\n" % e)
    finally:
        # 动画结束（成功或异常都要走到），放开重建封锁并补上被推迟的比例。
        _finish_intro()

PORT = 48900

# ----------------------------------------------------------------------
# 画板通道（本插件新增，原版没有）
# ----------------------------------------------------------------------
#
# 桌宠在 DSH 上是「画板的入口」：右键菜单多三项 —— 开始绘制 / 唤起 agent /
# 一键清除。它们不在这里实现，只是往棋盘的状态目录写一个词，由
# pencil.ps1 读走并执行（这正是原来那个 WPF 圆点用的 tray.txt 通道）。
#
# 同时看着棋盘的心跳：棋盘一死（DSH 被关、被强杀），桌宠跟着退，
# 否则屏幕上会留下一条没人管的鲸鱼。
BALL = {
    "state_dir": "",     # 棋盘状态目录（tray.txt / alive.txt 所在）
    "state_file": "",    # DSH 的 status.json
    "demo": False,
}

DIVE_JS = r"""
function __whaleDive(leaving) {
  // 进出绘制模式：让模型在三维空间里绕垂直轴自转。
  //
  // ⚠ 这是真的三维旋转，不是把 canvas 当平面压扁：
  //   鲸鱼是 three.js 画的，模型外面有一层 petPivot group。往
  //   petPivot.rotation 上加角度，透视会让身体自然产生前后遮挡与压扁 ——
  //   那是渲染出来的，不是 CSS 变形。之前用 canvas 的 rotateY 只是把一张
  //   平面图做透视变形，看着还是 2D。
  //
  // 通道：scene 里新增了 setSpin(yaw, pitch, scale)，它在动画器之后叠加到
  // petPivot 上。必须走这条通道 —— animator.update() 每帧都覆写
  // petPivot.rotation 与 scale，直接改会被抹掉。

  var whale = window.__whale && window.__whale.whalePet;
  if (!whale || typeof whale.setSpin !== 'function') return false;

  var start = performance.now();
  var dur = leaving ? 1150 : 1050;

  // 角度：消失 0 → +3.4 圈（缓入，越转越快）；出现 -2.6 圈 → 0（缓出，越转越慢）。
  var fromYaw = leaving ? 0 : -Math.PI * 2 * 2.6;
  var toYaw = leaving ? Math.PI * 2 * 3.4 : 0;
  // ⚠ 消失的终点必须是 0（用一个极小数避免除零），不能停在 0.02：
  //   0.02 的鲸鱼在 200px 的窗口里还有几个像素，用户看到的就是
  //   「关闭之后有一个很小很小的点在那儿」。
  var TINY = 0.001;
  var fromScale = leaving ? 1 : TINY;
  var toScale = leaving ? TINY : 1;
  // 顺带一点绕水平轴的俯仰，翻起来更像「在空间里转」而不是原地打转。
  var fromPitch = leaving ? 0 : -0.5;
  var toPitch = leaving ? 0.5 : 0;

  var ease = leaving
    ? function (t) { return t * t; }
    : function (t) { return 1 - (1 - t) * (1 - t); };

  function frame(now) {
    var t = Math.min(1, (now - start) / dur);
    var k = ease(t);
    whale.setSpin(
      fromYaw + (toYaw - fromYaw) * k,
      fromPitch + (toPitch - fromPitch) * k,
      fromScale + (toScale - fromScale) * k
    );
    if (t < 1) {
      requestAnimationFrame(frame);
    } else {
      whale.setSpin(toYaw, toPitch, toScale);
    }
  }
  requestAnimationFrame(frame);
  return true;
}
"""


def whale_dive(leaving):
    """让鲸鱼沉下去 / 浮上来。"""
    win = _WIN.get("ref")
    if win is None:
        return
    try:
        applied = win.evaluate_js(
            "(%s)(%s);" % (DIVE_JS.strip(), "true" if leaving else "false"))
        mode = "out" if leaving else "in"
        # 回读一次：确认页面里真的有 setSpin（老 bundle 没有这个方法，
        # 那样动画会静默不生效，只看到窗口直接消失）。
        has = win.evaluate_js(
            "(function(){var w=window.__whale && window.__whale.whalePet;"
            "return !!(w && typeof w.setSpin === 'function');})()")
        sys.stderr.write("[ball] dive %s applied=%s hasSetSpin=%s\n"
                         % (mode, applied, has))
    except Exception as e:
        sys.stderr.write("[ball] dive failed: %s\n" % e)


def whale_shrink_now():
    """把模型瞬间缩到极小（不演动画），用于「窗口露出之前」。

    ⚠ 为什么需要它：显示鲸鱼的顺序是「复位 → 露窗口 → 演放大动画」，
      但 whale_reset_clip() 复位到的是 scale 1（正常大小）。窗口一露出来，
      第一帧就是完整大小的鲸鱼，下一帧动画才把它缩到极小再放大 ——
      用户看到的就是「先整个大的闪一下，然后才开始转着长出来」。

      所以在露窗口之前，先把它按到极小；露出来的第一帧就已经是小点，
      接着动画从这个点长起来，中间不会有大的那一帧。
    """
    win = _WIN.get("ref")
    if win is None:
        return
    try:
        win.evaluate_js(
            "(function(){var w=window.__whale && window.__whale.whalePet;"
            "if(w && w.setSpin) w.setSpin(0,0,0.001);"
            "return true;})()")
    except Exception as e:
        sys.stderr.write("[ball] shrink failed: %s\n" % e)


def whale_reset_clip():
    """把三维翻转清干净，让模型回到正常姿态与大小。

    ⚠ 必须做，三个时刻都要：
      · 消失动画演完、窗口已藏 → 不清的话下次显示第一帧是缩小的
      · 出现动画演完 → 不清的话残留的角度会让鲸鱼歪着游
      · 出现**之前**要用 whale_shrink_now()，不是这个函数：
        这里复位到 scale 1（正常大小），窗口一露出来就会闪一下大的。

    复位走 setSpin(0, 0, 1)：那是真正驱动 petPivot 的通道，
    改 CSS 一点用都没有（旋转在 WebGL 里，不在 DOM 样式里）。
    """
    win = _WIN.get("ref")
    if win is None:
        return
    try:
        win.evaluate_js(
            "(function(){var w=window.__whale && window.__whale.whalePet;"
            "if(w && w.setSpin) w.setSpin(0,0,1);"
            "var p=document.querySelector('[data-whale-size]');"
            "if(p){p.style.clipPath='';p.style.marginTop='';}"
            "return true;})()")
    except Exception as e:
        sys.stderr.write("[ball] reset anim failed: %s\n" % e)


def _watch_commands():
    """盯着 ball-cmd.txt：棋盘进退绘制模式时写 `1`（露出来）/ `0`（藏起来）。

    ⚠ 这条通道原本没实现 —— deskpet.py 从来没读过 ball-cmd.txt，
      所以棋盘进绘制模式时鲸鱼根本不会隐藏（Hide-Ball 写了文件，没人读）。
      现在补上，顺便把「跃起入水 / 破水而出」接在这一刻。

    启动时先记下文件里的旧值，不清掉就会刚一上线就执行一条过期命令。
    """
    base = BALL.get("state_dir") or ""
    if not base:
        return
    path = os.path.join(base, "ball", "ball-cmd.txt")
    last = ""
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8-sig") as f:
                last = f.read().strip()
            sys.stderr.write("[ball] stale command ignored: %r\n" % last)
    except Exception:
        last = ""

    while True:
        time.sleep(0.25)
        try:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8-sig") as f:
                word = f.read().strip()
            if word == "" or word == last:
                continue
            last = word
            if word == "quit":
                # 棋盘要求退出。这里也要先演动画再走 —— 棋盘发 quit 的时机
                # 通常是「心跳停了」（DSH 关了），和 board_watch 判死是
                # 同一件事，只是它先发现。两条路都走 _exit_with_spin，
                # 否则用户看到的就是鲸鱼直接消失（或者留一个小点）。
                sys.stderr.write("[ball] command quit -> spin out\n")
                _exit_with_spin("board requested quit")
            want_visible = (word == "1")
            if want_visible == (not _hidden["on"]):
                continue
            # 先把戏演完再藏/露：两个动画都要窗口还看得见才有得看。
            # 时长与 DIVE_JS 里的 keyframes 对齐（1150ms 消失 / 1050ms 出现）。
            #
            # ⚠ 和托盘点图标共用 _toggle_lock：板子发命令和用户点图标
            #   可能是同一时刻，两条路同时演动画就会互相抢 rotate/scale。
            #   抢不到锁说明已经有一轮在跑，这条命令跳过（下一轮心跳还会发）。
            if not _toggle_lock.acquire(blocking=False):
                sys.stderr.write("[ball] command skipped (anim busy) want_visible=%s\n"
                                 % want_visible)
                continue
            try:
                if want_visible:
                    # 出现：窗口先显示在屏幕外，等动画转起来再挪回原位。
                    # ⚠ 为什么不在原位先 shrink 再 show：WebView2 画的第一帧
                    #   是正常大小的鲸鱼，那一帧发生在我们的 JS 能干预之前，
                    #   所以不管怎么提前 setSpin，屏幕上都会闪一下大的。
                    #   把那一帧放到屏幕外去画，用户就只看到从小长出来。
                    _hidden["on"] = False
                    target = _window_pos() or (200, 200)
                    show_whale_offscreen_then_slide_in(target)
                else:
                    # 消失：窗口还开着，让它越转越快、越缩越小，演完再藏。
                    whale_dive(leaving=True)
                    time.sleep(1.15)
                    _hidden["on"] = True
                    _show_main_window(False)
                    whale_reset_clip()
            finally:
                try:
                    _toggle_lock.release()
                except Exception:
                    pass
            sys.stderr.write("[ball] visible=%s\n" % want_visible)
        except Exception as e:
            sys.stderr.write("[ball] command watch failed: %s\n" % e)

# mode=main => the whale is pinned to the centre of its window and the
# window itself is moved to show movement.
URL = "http://127.0.0.1:%d/?mode=main" % PORT

# 鲸鱼窗口尺寸 = 基准 × 鲸鱼比例（缩放面板那个 x%）。
#
# ⚠ 不用 win.resize() 改大小：resize 底层是 SetWindowPos(..., SWP_SHOWWINDOW)，
#   会触发重绘、把透明弄丢（实测缩放时窗口一闪一闪、变不透明）。
#   改大小的做法是「销毁窗口 → 按新尺寸在屏幕外重建 → 走一遍启动流程
#   （hide/show 让透明生效）→ 移回原位」。
#   重建的只有鲸鱼这一扇窗，进程、托盘、组件窗口都不受影响。
WIN_BASE_W = 200
WIN_BASE_H = 350
WIN_MAX_SCALE = 1.5

SPAWN_X = -20000
SPAWN_Y = -20000

# 「屏幕外」用的固定坐标。
#
# ⚠ 不能用 screen_w + 200 算：_screen_size() 返回的是**逻辑像素**（这台机器
#   1536），而 SetWindowPos / GetWindowRect 用的是**物理像素**（1920）。
#   1536 + 200 = 1736，在物理坐标下仍在屏幕内 —— 所谓「画在屏幕外」的第一帧
#   其实画在屏幕上，那就是用户看到的闪一下、以及之后那 565px 的可见跳变。
#   用一个远超任何分辨率的固定值，两种坐标系下都真的在屏幕外。
OFFSCREEN_X = -20000
OFFSCREEN_Y = -20000

# ----------------------------------------------------------------------
# Win32 helpers (topmost / tool window)
# ----------------------------------------------------------------------

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_u32.FindWindowW.restype = wintypes.HWND
_u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongW.restype = ctypes.c_long
_u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_u32.SetWindowLongW.restype = ctypes.c_long
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                              ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.GetSystemMetrics.argtypes = [ctypes.c_int]
_u32.GetSystemMetrics.restype = ctypes.c_int

# 判断「DSH 窗口还在不在」用。board_watch 靠这个区分
# 「插件进程活着」和「用户还开着 DSH」—— 关窗口后 Electron 后台不退，
# 心跳照写，只有问窗口才知道真相。
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.IsWindowVisible.restype = wintypes.BOOL
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowTextW.restype = ctypes.c_int
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_u32.EnumWindows.restype = wintypes.BOOL
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowRect.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5

SM_CXSCREEN = 0
SM_CYSCREEN = 1

_MAIN_TITLE = "whale-pet"


def _start_taskbar_guard():
    """Continuously strip WS_EX_APPWINDOW from every window this process owns.

    WARNING: this is a watcher, not a one-shot fix, and that is deliberate.
    pywebview applies its default window style AFTER create_window returns and
    even after before_show, so any single attempt to set TOOLWINDOW loses the
    race -- measured: panes stayed visible and taskbar-eligible for 0-188ms no
    matter where the style was applied, which is the Python icon the user saw
    flash in the taskbar.

    A guard thread that sweeps every few milliseconds wins regardless of when
    pywebview touches the style, because it keeps re-applying the correction.
    It only ever changes APPWINDOW/TOOLWINDOW, so it cannot disturb anything the
    whale itself does with geometry or transparency.
    """
    import threading

    def sweep():
        user32 = _u32
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        pid = os.getpid()
        while True:
            try:
                def cb(hwnd, _lp):
                    try:
                        owner = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd,
                                                        ctypes.byref(owner))
                        if owner.value != pid:
                            return True
                        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                        if (ex & WS_EX_APPWINDOW) or not (ex & WS_EX_TOOLWINDOW):
                            user32.SetWindowLongW(
                                hwnd, GWL_EXSTYLE,
                                ((ex | WS_EX_TOOLWINDOW)
                                 & ~WS_EX_APPWINDOW) & 0xFFFFFFFF)
                    except Exception:
                        pass
                    return True

                user32.EnumWindows(_WNDENUMPROC(cb), 0)
            except Exception:
                pass
            time.sleep(0.004)

    t = threading.Thread(target=sweep, daemon=True)
    t.start()
    return t


def _hwnd_of(win):
    """从一个 pywebview 窗口对象拿 HWND。

    ⚠ 不要用 _find_hwnd(title) 去找刚创建的窗口：那是 FindWindowW(None, title)，
      靠**标题**匹配。before_show 触发时窗口对象内部已经把 handle 建好了，
      但标题可能还没应用到窗口上 —— 实测这时候 FindWindowW 返回 0，于是
      样式根本没改成，窗口以 APPWINDOW 显示出来，任务栏就多了一个按钮。

    pywebview 把 WinForms 窗体挂在 win.native 上，Handle 一定可用。
    """
    try:
        native = getattr(win, "native", None)
        if native is None:
            return 0
        return int(native.Handle.ToInt64())
    except Exception:
        return 0


def _style_by_win(win, add, remove=0):
    """按窗口对象改扩展样式（用 _hwnd_of，不靠标题匹配）。"""
    hwnd = _hwnd_of(win)
    if not hwnd:
        return False
    try:
        ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        want = (ex | add) & ~remove
        if want == ex:
            return True
        _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
        return True
    except Exception:
        return False


def _find_hwnd(title=_MAIN_TITLE):
    return _u32.FindWindowW(None, title)


def _grab_pane_style(win, name, timeout_ms=1200):
    """Put a freshly created pane out of the taskbar, before it is shown.

    WARNING: this must happen BEFORE pywebview shows the window. Its WinForms
    backend does Show()/Hide() for a hidden transparent window, and the taskbar
    button is created the instant Show() runs. before_show fires just before that
    and is not early enough -- measured: each pane was visible and non-TOOLWINDOW
    for 93-188ms, which is the Python icon the user saw flash in the taskbar.

    WARNING: win.native is None for a while after create_window returns, so
    _hwnd_of gives 0 and a short poll gives up. The window does exist during that
    time, however, and its TITLE is already set -- so this falls back to
    FindWindowW by title, which is the only handle available this early.
    """
    title = PANE_TITLES.get(name, "")
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        hwnd = _hwnd_of(win)
        if not hwnd and title:
            try:
                hwnd = int(_u32.FindWindowW(None, title) or 0)
            except Exception:
                hwnd = 0
        if hwnd:
            try:
                _style(hwnd, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW)
                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                SWP_HIDEWINDOW = 0x0080
                _u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                  SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                  | SWP_NOACTIVATE | SWP_HIDEWINDOW)
            except Exception:
                pass
            return True
        time.sleep(0.002)
    sys.stderr.write("[deskpet] pane %s no hwnd in %dms\n" % (name, timeout_ms))
    return False


def _hide_window(win):
    """Hide a window by handle, immediately and without pywebview's help.

    WARNING: being off-screen is not the same as being hidden. A window parked at
    (-20000,-20000) is still mapped: it still counts in the window manager, still
    flashes in the taskbar, and still shows up in Alt-Tab. SWP_HIDEWINDOW is what
    actually takes it out.
    """
    hwnd = _hwnd_of(win)
    if not hwnd:
        return False
    try:
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_HIDEWINDOW = 0x0080
        _u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                          | SWP_NOACTIVATE | SWP_HIDEWINDOW)
        return True
    except Exception:
        return False


def _style(hwnd, add, remove=0):
    ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    _u32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | add) & ~remove)


def _set_topmost(hwnd):
    """只调层级，不动样式。

    ⚠ 以前这里也调 _style()，而调用方 keep_top 每 1.5 秒跑一次 ——
      等于每 1.5 秒触发一次窗口重绘。设为置顶并不需要改扩展样式，
      TOOLWINDOW 已经在窗口第一次显示前设好了。
    """
    if not hwnd:
        return
    _u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _apply_no_activate(hwnd):
    """Topmost + tool window. Deliberately NOT WS_EX_NOACTIVATE: once a popup
    has focus, a non-activatable main window would never receive keystrokes.

    ⚠ 只在样式**真的需要改**的时候才调 SetWindowLong。
      SetWindowLong(GWL_EXSTYLE) 会触发一次整窗重绘，而以前这里是每次
      show 都无条件调一遍 —— 那一次重绘就是「显示时闪一下」的来源之一。
      现在 TOOLWINDOW 在窗口第一次显示之前就设好了（见 _create_main_window
      的 before_show），这里绝大多数情况下不需要再动样式。
    """
    if not hwnd:
        return
    try:
        ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        # 已经是工具窗、且没有 EX_APPWINDOW 残留 → 样式不用动，只调层级。
        need_style = bool(ex & WS_EX_APPWINDOW) or not (ex & WS_EX_TOOLWINDOW)
        if need_style:
            _u32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        _u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                          | (SWP_FRAMECHANGED if need_style else 0))
    except Exception:
        pass


def _activate_window(hwnd):
    """把焦点给一个窗口（组件窗打开时用，滑杆/输入框要收到键盘和滑轮）。"""
    if not hwnd:
        return
    try:
        _u32.SetForegroundWindow(hwnd)
        _u32.SetFocus(hwnd)
    except Exception:
        pass


def _ui(fn):
    """Run a WinForms call on the UI thread. move/resize/hide must not be
    called straight from a WebView2 JS callback thread."""
    try:
        import System
        w = _WIN["ref"]
        w.native.Invoke(System.Action(fn))
        return True
    except Exception:
        return False


def _show_main_window(visible):
    """显示 / 隐鲸鱼主窗口。

    ⚠ 走 Win32 ShowWindow，不用 pywebview 的 hide()/show()：
      pywebview 那边要等 shown 事件、还会 Activate 抢焦点，隐藏后再显示
      经常只剩个空窗口（WebView2 不重绘）。
    ⚠ 同时要把页面里的 hidden 状态清掉：那条状态会把鲸鱼整个
      display:none —— 窗口显示了，画面上仍然没有鲸鱼（实测就是这个问题）。
    """
    hwnd = _find_hwnd(_MAIN_TITLE)
    if not hwnd:
        return False

    def _apply():
        _u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)
        if visible:
            _apply_no_activate(hwnd)
        return True

    ok = _ui(_apply)
    if not ok:
        ok = _apply()

    # ⚠ evaluate_js 必须在 UI 线程**之外**调：它会等脚本执行结果，而脚本
    #   要靠 UI 线程的消息循环才能跑 —— 在 Invoke 里面调会互相等死
    #   （实测：第二次点托盘整个卡住，鲸鱼状态停在半路）。
    if visible:
        try:
            _WIN["ref"].evaluate_js(
                "window.__whale && window.__whale.whalePet"
                " && window.__whale.whalePet.setHidden(false)")
        except Exception as e:
            sys.stderr.write("[deskpet] clear hidden failed: %s\n" % e)
    return bool(ok)


def _window_pos():
    """当前鲸鱼窗口的左上角（屏幕 CSS 像素）。"""
    hwnd = _find_hwnd(_MAIN_TITLE)
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top)


def _move_window(x, y):
    """只挪位置，不动大小、不激活。"""
    hwnd = _find_hwnd(_MAIN_TITLE)
    if not hwnd:
        return False
    try:
        _u32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                          SWP_NOSIZE | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def show_whale_offscreen_then_slide_in(target, dive_ms=1050, pad_ms=120):
    """把已经在落点上、隐藏着的窗口露出来，并从极小长到正常大小。

    ⚠ 这个函数现在**一次都不挪窗口**。
      名字里的 offscreen / slide_in 是历史遗留（早期确实要挪），现在窗口
      创建时就在落点上、且是 hidden 的（见 _create_main_window），所以
      只需要「缩小 → show → 长大」。

    ⚠ 为什么必须不挪：
      以前是「屏幕外 show → 缩到极小 → 启动动画 → 等 120ms → 挪回落点」。
      但动画是缓出（ease = 1-(1-t)²，前段长得最快），120ms 时 t=0.114、
      ease=0.215 —— 挪窗口那一刻鲸鱼已经长到 21.6%，是一头中等大小的
      鲸鱼凭空出现在屏幕中央，用户看到的就是「一闪而过」。
      只要还要挪窗口，就一定有一帧是「大小已经长起来、位置还在旧处」。
      把窗口从一开始就放在落点，这个问题就不存在了。

    流程：
      1. 接管位置（hold），防止页面在缩放期间推窗口
      2. 按到极小 —— 此时窗口还藏着，屏幕上什么都没有
      3. 等一帧，让 WebView 真的把「极小」渲染出来
      4. show —— 屏幕上出现一个几乎看不见的点
      5. 启动放大动画，从那个点长大
      6. 演完复位，交还位置控制
    """
    if target is None:
        return False

    _offscreen["hold"] = True
    _intro["running"] = True
    try:
        # 1. 缩到极小（窗口还藏着，看不见任何东西）。
        whale_shrink_now()
        # 2. 等它真的画出来：不等的话 show 的第一帧可能还是正常大小 ——
        #    那又变成闪一下。两帧足够了（60Hz 下 33ms）。
        time.sleep(0.08)
        # 3. 露出来：此刻鲸鱼是 scale=0.001，屏幕上是一个点。
        _main_pos["x"] = int(target[0])
        _main_pos["y"] = int(target[1])
        _show_main_window(True)
        # 4. 从这一点开始长大。
        whale_dive(leaving=False)
        # 位置控制可以交还了：窗口已在落点，页面接着推也只是正常游动。
        _offscreen["hold"] = False
        # 5. 等动画演完，再复位（角度/缩放归零）。
        time.sleep(max(0.0, dive_ms / 1000.0) + 0.1)
        whale_reset_clip()
    finally:
        _finish_intro()
    return True

def _finish_intro():
    """出场动画收尾：放开重建封锁，并把动画期间被推迟的比例补上。

    ⚠ 必须**一定**被调用（成功和异常两条路都要）。忘了调的话，
      _intro["running"] 永远是 True，之后所有比例变更都会被无限推迟。
    """
    _intro["running"] = False
    _offscreen["hold"] = False
    pending = _rebuild["pending"]
    if pending is None or abs(pending - _main_scale["value"]) < 0.001:
        return
    _rebuild["pending"] = None
    try:
        inst = _api["ref"]
        if inst is not None:
            # ⚠ 走桌面外那条：直接 _rebuild_main 会让新窗口在原地露一头
            #   正常大小的鲸鱼 —— 那正是「动画演完闪一下」。
            _rebuild_main_offscreen(inst, pending, animate=False)
    except Exception as e:
        sys.stderr.write("[ball] deferred rebuild failed: %s\n" % e)


def _js_string(text):
    """把 Python 字符串变成合法的 JS 字面量（用 json 转义，含引号）。"""
    import json
    return json.dumps(str(text), ensure_ascii=False)


def _run_in_main(js):
    """在鲸鱼窗口的页面里执行一段 JS。

    ⚠ 不要在 UI 线程（_ui 的 Invoke 里）调这个：evaluate_js 要等脚本结果，
      而脚本得靠 UI 线程消息循环才能跑，会互相等死。这里都由页面线程调用。
    """
    win = _WIN["ref"]
    if win is None:
        return False
    try:
        win.evaluate_js("(function(){%s; return 1})()" % js)
        return True
    except Exception as e:
        sys.stderr.write("[deskpet] run_in_main failed: %s\n" % e)
        return False


def _screen_size():
    """Screen size in CSS pixels (same unit as screenX / screenY)."""
    try:
        import webview as _wv
        scr = _wv.screens[0]
        w = int(scr.width)
        h = int(scr.height)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return (int(_u32.GetSystemMetrics(SM_CXSCREEN)),
                int(_u32.GetSystemMetrics(SM_CYSCREEN)))
    except Exception:
        return 1280, 800


# ----------------------------------------------------------------------
# Tray icon (Shell_NotifyIconW)
# ----------------------------------------------------------------------

WM_USER = 0x400
WM_CONTEXTMENU = 0x007B
NIN_SELECT = 0x400
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_SHOWTIP = 0x80
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
MF_STRING = 0
MF_SEPARATOR = 0x800
TPM_RETURNCMD = 0x100
TPM_RIGHTBUTTON = 0x2

ID_TRAY_TOGGLE = 1
# 「一键清除」：把画板上的东西全清掉。
# 和球右键菜单那一项走同一条通道（board_intent("clear") → tray.txt →
# pencil.ps1 读走执行），所以两处行为完全一致。
ID_TRAY_CLEAR = 3

TRAY_CALLBACK = WM_USER + 20


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", wintypes.BYTE * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(NOTIFYICONDATA)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
_u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                            wintypes.UINT, ctypes.c_int, ctypes.c_int,
                            wintypes.UINT]
_u32.LoadImageW.restype = wintypes.HANDLE
_u32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
_u32.LoadIconW.restype = wintypes.HANDLE
_u32.CreatePopupMenu.restype = wintypes.HMENU
_u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                             wintypes.UINT, wintypes.LPWSTR]
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowRect.restype = wintypes.BOOL
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.SetForegroundWindow.restype = wintypes.BOOL
_u32.SetFocus.argtypes = [wintypes.HWND]
_u32.SetFocus.restype = wintypes.HWND
_u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.HWND, ctypes.c_void_p]
_u32.TrackPopupMenu.restype = ctypes.c_int
_u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM]
_u32.DestroyMenu.argtypes = [wintypes.HMENU]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32b = _u32

_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_u32.RegisterClassW.argtypes = [ctypes.c_void_p]
_u32.RegisterClassW.restype = wintypes.ATOM
_u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                 wintypes.LPCWSTR, wintypes.DWORD,
                                 ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int,
                                 wintypes.HWND, wintypes.HMENU,
                                 wintypes.HINSTANCE, ctypes.c_void_p]
_u32.CreateWindowExW.restype = wintypes.HWND
_u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
_u32.DefWindowProcW.restype = ctypes.c_ssize_t
_u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                             wintypes.UINT, wintypes.UINT]
_u32.GetMessageW.restype = ctypes.c_int
_u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_u32.DispatchMessageW.restype = ctypes.c_ssize_t
_u32.PostQuitMessage.argtypes = [ctypes.c_int]
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

WS_EX_TOOLWINDOW_ = 0x00000080
WS_POPUP_ = 0x80000000

_TRAY_CLASS = "WhalePetTrayWindow"

_TRAY = {
    "hwnd": None,
    "added": False,
    "on_toggle": None,
    "thread": None,
    "proc": None,
}


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,
                              wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


def _tray_proc(hwnd, msg, wparam, lparam):
    """托盘窗口的消息处理：左键切换显示，右键弹菜单。"""
    try:
        if msg == TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONUP, NIN_SELECT & 0xFFFF):
                t = _TRAY["on_toggle"]
                if t is not None:
                    t.toggle_hidden()
                return 0
            if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                _tray_menu(hwnd)
                return 0
        if msg == 0x0010:      # WM_CLOSE
            return 0
        if msg == 0x0002:      # WM_DESTROY
            _u32.PostQuitMessage(0)
            return 0
    except Exception as exc:
        sys.stderr.write("[deskpet] tray proc failed: %s\n" % exc)
    return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)


_TRAY["proc"] = _WNDPROC(_tray_proc)


def _tray_icon():
    """托盘图标：优先用 whale.ico（exp/make-icon.py 生成），否则退回系统图标。"""
    for folder in (ROOT, HERE, PROJECT_ROOT):
        for name in ("whale.ico", "whale-pet.ico"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                for size in (0, 32, 16):
                    handle = _u32.LoadImageW(None, path, IMAGE_ICON,
                                             size, size, LR_LOADFROMFILE)
                    if handle:
                        return handle
    return _u32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION


def _tray_add(hwnd):
    if _TRAY["added"] or not hwnd:
        return False
    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
    nid.uCallbackMessage = TRAY_CALLBACK
    nid.hIcon = _tray_icon()
    nid.szTip = "鲸鲸（点一下显示 / 隐藏）"
    ok = _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    if ok:
        nid.uVersion = 4
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        _TRAY["added"] = True
    else:
        sys.stderr.write("[deskpet] tray icon add failed\n")
    return bool(ok)


def _tray_remove():
    if not _TRAY["added"]:
        return
    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = _TRAY["hwnd"]
    nid.uID = 1
    _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    _TRAY["added"] = False


def _tray_menu(hwnd):
    # 两项：显示 / 隐藏、一键清除。
    # 没有「退出」：它和「隐藏」只差一步，误点就把整条鲸鱼和棋盘的联系断了
    # （棋盘还活着，球没了）。要退出走棋盘那条路，或者直接关 DSH。
    menu = _u32.CreatePopupMenu()
    _u32.AppendMenuW(menu, MF_STRING, ID_TRAY_TOGGLE, "显示 / 隐藏鲸鱼")
    _u32.AppendMenuW(menu, MF_STRING, ID_TRAY_CLEAR, "一键清除画板")
    pos = wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(pos))
    _u32.SetForegroundWindow(hwnd)
    cmd = 0
    try:
        cmd = _u32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                  pos.x, pos.y, 0, hwnd, None)
    finally:
        # ⚠ 这里必须传 0 而不是 None：LPARAM 是整数类型，None 会让 ctypes 抛
        #   "argument 4: TypeError: wrong type"，异常一路冒到 _tray_proc，
        #   后面的菜单命令就整段跳过了（实测：点 show 没反应）。
        _u32.PostMessageW(hwnd, 0, 0, 0)
        _u32.DestroyMenu(menu)

    if cmd == ID_TRAY_TOGGLE:
        t = _TRAY["on_toggle"]
        if t is not None:
            t.toggle_hidden()
    elif cmd == ID_TRAY_CLEAR:
        # 和球右键菜单的「一键清除」同一条通道，不在这里直接动画板：
        # 画板的状态归 pencil.ps1 管，球只负责转达意图。
        board_intent("clear")


def build_tray(on_toggle):
    """建一个 Win32 隐藏窗口收托盘点击，再挂 Shell_NotifyIconW。

    ⚠ 不用 .NET 的 NotifyIcon：它的 MouseClick 只能靠真鼠标点才触发，
      没法用 PostMessage 验证（实测点了没反应也查不出来）。
      Win32 这条路点击就是一条消息，测试时直接 PostMessage 就能验。

    窗口：0×0、弹出式、不进任务栏，自己跑一个消息循环（独立线程）。
    消息：WM_USER+20 的 lParam 低字 = 鼠标事件。
    """
    _TRAY["on_toggle"] = on_toggle
    if _TRAY.get("thread") is not None:
        return True

    def run():
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(_TRAY["proc"], ctypes.c_void_p)
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = _tray_icon()
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = _TRAY_CLASS

            atom = _u32.RegisterClassW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != 1410:  # 1410 = already exists
                sys.stderr.write("[deskpet] RegisterClass failed: %d\n"
                                 % ctypes.get_last_error())
                return

            hwnd = _u32.CreateWindowExW(
                WS_EX_TOOLWINDOW_, _TRAY_CLASS, "whale-pet-tray",
                WS_POPUP_, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                sys.stderr.write("[deskpet] tray window failed: %d\n"
                                 % ctypes.get_last_error())
                return

            _TRAY["hwnd"] = hwnd
            if not _tray_add(hwnd):
                sys.stderr.write("[deskpet] tray icon add failed\n")
                return
            sys.stderr.write("[deskpet] tray icon ready (hwnd=%d)\n" % hwnd)
            sys.stderr.flush()

            msg = wintypes.MSG()
            while True:
                ret = _u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    sys.stderr.write("[deskpet] tray loop exit (ret=%d)\n" % ret)
                    sys.stderr.flush()
                    break
                _u32.TranslateMessage(ctypes.byref(msg))
                _u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            sys.stderr.write("[deskpet] tray loop failed: %s\n" % exc)

    t = threading.Thread(target=run, daemon=True)
    _TRAY["thread"] = t
    t.start()
    return True


# ----------------------------------------------------------------------
# Popup window recipes
# ----------------------------------------------------------------------

#   activate: True  = 打开时拿走焦点（输入框、滑杆要用键盘/滑轮）
#             False = 不抢焦点（菜单点了就走，不宜夺走别的窗口焦点）
PANE_SPECS = {
    # 菜单：尺寸直接写成**内容真实需要的值**，不是先给个最小值再改。
    #
    # ⚠ 为什么是 203x221 这种奇怪的数字：这是页面里量出来的实际值。
    #   原来 9 项是 203x277；隐藏「和鲸鲸聊天」「鲸鲸记得什么」「命名」
    #   三项后剩 6 项（开始绘制 / 唤起 agent / 一键清除 / 调整大小 /
    #   开关角落吸附 / 回到角落 / 隐藏 其实是 7 项，只是前三项和后面
    #   之间有一条分隔线）。实测 193 会把最后一项切掉，221 正好。
    #   一次给对，窗口从头到尾只出现一次、位置不再变。
    #
    #   如果以后菜单项增减，改这里的 w/h 即可。
    "menu": {"w": 203, "h": 221, "gap": 4, "activate": False},
    # chat 不在这里：桌面球没有同源的 LLM 代理（窗口是独立的，拿不到 DSH
    # 的会话与凭证），聊天在 DSH 窗口里进行。菜单里那一项也被注入脚本隐藏了。
    "memory": {"w": 288, "h": 292, "gap": 18, "activate": True},
    "name": {"w": 288, "h": 116, "gap": 18, "activate": True},
    "size": {"w": 288, "h": 136, "gap": 18, "activate": True},
}

PANE_TITLES = {
    "menu": "whale-pet-menu",
    "memory": "whale-pet-memory",
    "name": "whale-pet-name",
    "size": "whale-pet-size",
}

_PANES = {name: None for name in PANE_SPECS}
_opened = {name: False for name in PANE_SPECS}
# 最近一次打开组件时的锚点（屏幕 CSS 像素）
_anchor = {"x": 0.0, "y": 0.0}
_WIN = {"ref": None}
# on: 现在是否隐藏
_hidden = {"on": False}
# 「显示 / 隐藏」动画是否正在跑。
#
# ⚠ 用锁而不是布尔标志：bool 判断和赋值之间有时间缝，连点的两次点击可能
#   同时通过判断。acquire(blocking=False) 是原子的，抢不到就说明已经有一轮
#   在跑，这次点击直接丢弃。
# ⚠ 动画大约 1.15 秒，期间点击全部无效 —— 这是用户要求的行为：
#   演完才能触发下一次，避免多个动画线程同时改 rotate/scale 导致卡顿。
_toggle_lock = threading.Lock()
# 鲸鱼比例（面板那个 x%），窗口尺寸 = WIN_BASE × 它
_main_scale = {"value": 1.0}


def _read_persisted_scale():
    """从 WebView2 的 localStorage 里读出页面存的比例。

    ⚠ 为什么值得读：窗口尺寸 = 基准 × 比例，而比例存在页面里。以前是先用
      默认 1.0 建窗口，等页面把它存的比例报上来（常见 0.5），发现不一致再
      「销毁 + 重建」一次。那一次重建是任务栏按钮闪出来的根源 ——
      pywebview 只对**第一个**窗口触发 before_show，重建出来的第二个窗口
      没有钩子可用，它带着 WS_EX_APPWINDOW 显示了一下（实测 94ms），
      任务栏就在这 94ms 里加了按钮。

      开局就把比例读对，就不用重建，第二个窗口根本不存在。

    ⚠ 读不到就返回 None，调用方继续用默认 1.0，行为和以前一样（会重建一次）。
      这段解析只做「尽力而为」：leveldb 是追加写的日志文件，最新值在最后，
      所以取最后一个匹配。
    """
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return None
    root = os.path.join(appdata, "pywebview", "EBWebView", "Default",
                        "Local Storage", "leveldb")
    if not os.path.isdir(root):
        return None
    try:
        import glob
        import re
        last = None
        for path in glob.glob(os.path.join(root, "*")):
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except Exception:
                continue
            if b"whale-pet" not in raw:
                continue
            # leveldb 里字符串是 UTF-16LE 存的
            text = raw.decode("utf-16-le", errors="ignore")
            for m in re.finditer(r'"size":\s*([0-9.]+)', text):
                try:
                    last = float(m.group(1))
                except Exception:
                    pass
        if last is None:
            return None
        if not (0.5 <= last <= WIN_MAX_SCALE):
            return None
        return last
    except Exception:
        return None
# 鲸鱼窗口最后停在哪（重建后要回到这儿）
_main_pos = {"x": 200, "y": 200}
# 出场动画期间暂时接管窗口位置：True 时丢掉页面发来的 move_window_to。
#
# ⚠ 为什么需要：页面每帧都在推窗口（它算好游动位置再调 api.move_window_to）。
#   我们刚把窗口挪到屏幕外准备演放大动画，下一帧就被页面拉回桌面内 ——
#   屏幕上就是「闪一下」。必须在出场这段时间里禁止页面改位置。
_offscreen = {"hold": False}
# 出场动画是否正在播。
#
# ⚠ 和 _offscreen["hold"] 是两件事：
#   _offscreen["hold"]   窗口位置由 Python 接管（动画前 120ms）
#   _intro["running"]    整个动画期间禁止重建窗口（1050ms）
# 启动时页面会先报一个默认比例，触发去抖重建；如果那 0.35 秒正好落在
# 动画里，窗口会被销毁重建，新窗口从正常大小开始 —— 用户看到的就是
# 「转到一半突然变大」。所以整个动画期间都要拦着重
_intro = {"running": False}
# 当前 Api 实例（推迟的重建要用它）。
_api = {"ref": None}
# 页面报上来的第一份比例。出场动画要等它 —— 窗口是按默认 1.0 建的，
# 页面存的是别的值（常见 0.5）就必须重建，重建得赶在动画之前。
_scale_report = {"value": None}
# 页面报过位置没有。_main_pos 的初值是占位 (200, 200)，不等页面报一次
# 就开演，鲸鱼会从屏幕左上角长出来。
_pos_report = {"seen": False, "x": 0, "y": 0, "at": 0.0}
# on_loaded 里那次 hide()/show()（让透明生效）跑完了没有。
# 那一下会按创建时的 SPAWN 重新显示窗口，重建流程必须等它结束再摆位置，
# 否则刚摆好就被它顶掉 —— 屏幕上就是「演完动画然后瞬移」。
_loaded = {"done": False}
# 重建去抖：滑杆拖动会连发很多次，等它停手再重建
_rebuild = {"timer": None, "pending": None, "placed": False}


def _main_size(scale=None):
    """窗口尺寸 = 基准 × 比例（取整）。"""
    s = _main_scale["value"] if scale is None else scale
    return (int(round(WIN_BASE_W * s)), int(round(WIN_BASE_H * s)))


def _pane_rect(name, ax, ay, w, h, screen_w, screen_h):
    """算出组件窗该摆在哪儿。

    规则：**按鼠标点击点落在左半屏还是右半屏决定展开方向。**

      · 点在左半屏 → 菜单出现在点的右侧（菜单左上角贴住点击点）
      · 点在右半屏 → 菜单出现在点的左侧（菜单右上角贴住点击点）

    这样菜单永远朝着屏幕里侧展开，不会跑出屏幕，也不会盖住点击的位置。
    纵向同理：点在上半屏往下展开，点在下半屏往上展开。

    以前是按「哪边剩余空间够」来算，结果是鲸鱼靠近中间时方向不固定、
    菜单还会压住鲸鱼本身。按点击点的半屏判断，方向可预期，用户点了
    就知道菜单会往哪边开。
    """
    spec = PANE_SPECS[name]
    gap = spec["gap"]
    margin = 8
    # 上下留白：菜单栏/任务栏那种系统条，别让窗口贴上去。
    top_reserve = 40
    bottom_reserve = 48

    # --- 横向：按点击点所在的半屏决定往哪边展开 ---
    if ax <= screen_w / 2:
        # 左半屏 → 往右展开，菜单左边贴住点击点
        x = ax + gap
        # 极少数情况（点离中线很近、菜单又很宽）右边仍可能不够，
        # 这时才翻到左边。
        if x + w > screen_w - margin:
            x = ax - gap - w
    else:
        # 右半屏 → 往左展开，菜单右边贴住点击点
        x = ax - gap - w
        if x < margin:
            x = ax + gap
    x = max(margin, min(x, screen_w - w - margin))

    # --- 纵向：同样按点击点的半屏决定 ---
    if ay <= screen_h / 2:
        # 上半屏 → 往下展开，菜单上边贴住点击点
        y = ay + gap
        if y + h > screen_h - bottom_reserve:
            y = ay - gap - h
    else:
        # 下半屏 → 往上展开，菜单下边贴住点击点
        y = ay - gap - h
        if y < top_reserve:
            y = ay + gap
    y = max(top_reserve, min(y, screen_h - h - bottom_reserve))

    return int(x), int(y)


VK_LBUTTON = 0x01
_watch = {"thread": None, "ignore_until": 0.0}
# 每个组件窗口当前的形状（去重：尺寸没变就不 resize）
_shapes = {}


def _request_measure(name):
    """让组件窗口自己量一遍尺寸并回报（place）。

    ⚠ 在 UI 线程之外调：evaluate_js 要等脚本结果，脚本又要靠 UI 线程跑。
    """
    win = _PANES.get(name)
    if win is None:
        return
    try:
        win.evaluate_js("window.__whaleMeasure && window.__whaleMeasure()")
    except Exception as e:
        print("[pane:%s] measure failed: %s" % (name, e))


def _pane_hwnds():
    """当前所有**开着**的组件窗口句柄（用于判断点在不在里面）。"""
    found = []
    for name in PANE_SPECS:
        if not _opened.get(name):
            continue
        hwnd = _find_hwnd(PANE_TITLES[name])
        if hwnd:
            found.append(hwnd)
    return found


def _point_inside(hwnds, x, y):
    rect = wintypes.RECT()
    for hwnd in hwnds:
        if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        # 还停在屏幕外（-10000）说明还没摆出来，不能算「在里面」
        if rect.left <= -9000:
            continue
        if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
            return True
    return False


def _outside_watch_loop():
    """轮询鼠标左键：按下时若不在任何组件窗口里 → 关掉所有组件窗口。

    ⚠ 为什么不用窗口失焦：主窗口是 focus=False 建的，组件窗口
      SetForegroundWindow 又常被系统拒绝，blur 基本不会来 ——
      表现就是「点外面关不掉」（实测）。
    """
    pressed = False
    while True:
        time.sleep(0.08)
        hwnds = _pane_hwnds()
        if not hwnds:
            return
        state = _u32.GetAsyncKeyState(VK_LBUTTON) & 0x8000
        if state:
            if pressed:
                continue
            pressed = True
            # 刚弹出来的那一小会儿不算「点外面」：那一下就是打开它的点击。
            if time.time() < _watch["ignore_until"]:
                continue
            pt = wintypes.POINT()
            _u32.GetCursorPos(ctypes.byref(pt))
            if not _point_inside(hwnds, pt.x, pt.y):
                print("[panes] click outside -> close all")
                sys.stdout.flush()
                _panes.close_all()
                return
        else:
            pressed = False


def _start_outside_watch():
    thread = _watch.get("thread")
    if thread is not None and thread.is_alive():
        return
    thread = threading.Thread(target=_outside_watch_loop, daemon=True)
    _watch["thread"] = thread
    thread.start()


def set_hidden_animated(hidden):
    """显示 / 隐藏鲸鱼：先演旋转缩放，再真的切换窗口。

    ⚠ 以前是直接 _show_main_window()：窗口「啪」一下没了 / 冒出来。
      现在和进退绘制模式、DSH 启停用同一套动画。

    顺序不能颠倒：
      隐藏 → 窗口还开着，先演「越转越小」，演完再藏
      显示 → 先清掉上一轮残留、把窗口露出来，再演「从小转到大」

    ⚠ 异步执行：函数里要 sleep 一秒多等动画演完，而两个调用方
      （托盘消息回调、pywebview 的 JS 桥）都不该被卡住 ——
      在它们身上 sleep 会让托盘菜单僵住、页面也跟着没响应。
      所以起一个线程去演，调用方立刻返回。

    ⚠ 动画期间**拒收**新的点击（_toggle_busy 锁）。
      以前只判断 `want_hidden == _hidden["on"]`，而 _hidden["on"] 要等动画
      演完（1.15 秒后）才更新 —— 这期间连点几下，每一次都能通过判断，
      于是同时起了好几个线程，每个都在抢着做 rotate/scale 和 Hide/Show。
      表现就是鲸鱼卡顿、动画打架。

      加了锁之后：动画一开始就上锁，演完（含异常）才解锁。这期间的点击
      直接丢弃，用户连点也不会让鲸鱼卡住。想要下一次动画，等这一轮演完
      再点 —— 这正是「执行动画时点击无效，演完才能触发下一次」。
    """
    want_hidden = bool(hidden)
    if want_hidden == _hidden["on"]:
        return
    # 已经有一轮动画在跑 → 这一次点击无效，直接丢掉。
    if not _toggle_lock.acquire(blocking=False):
        sys.stderr.write("[ball] toggle ignored (anim busy) want_hidden=%s\n"
                         % want_hidden)
        return

    def _run():
        try:
            if want_hidden:
                # 藏：窗口还看得见，先演完再藏。
                whale_dive(leaving=True)
                time.sleep(1.15)
                _hidden["on"] = True
                _show_main_window(False)
                whale_reset_clip()
            else:
                # 显：窗口先显示在屏幕外，动画转起来再挪回原位。
                # ⚠ 不在原位先 shrink 再 show —— WebView2 的第一帧是正常
                #   大小，那一帧在我们的 JS 之前就画好了，挡不住（实测两次）。
                #   必须在挪回来之前记下目标位置，挪出去之后就查不到了。
                target = _window_pos() or (200, 200)
                _hidden["on"] = False
                show_whale_offscreen_then_slide_in(target)
        except Exception as e:
            sys.stderr.write("[ball] set_hidden anim failed: %s\n" % e)
        finally:
            # ⚠ 一定要在 finally 里解锁：中途抛异常也得放开，否则之后
            #   所有点击都会被当成「动画还在跑」永久丢弃。
            try:
                _toggle_lock.release()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


class PaneApi:
    def __init__(self, name):
        self.name = name

    def ball_menu_action(self, kind):
        """菜单里那三项画板功能。

        ⚠ 必须放在 PaneApi 上，不能只放在 Api 上：菜单窗的 js_api 是
          PaneApi，注入的按钮调的也是这个对象。放错类的结果是
          `window.pywebview.api` 里根本没有这个方法，注入脚本一开头
          就 return 了 —— 表现是菜单里永远看不到那三项（实测 nodes=0）。
        """
        try:
            word = str(kind)
            print("[pane:%s] ball_menu_action(%s)" % (self.name, word))
            sys.stdout.flush()
            if word in ("ink", "call", "clear"):
                board_intent(word)
            _panes.close("menu")
        except Exception as e:
            print("[pane:%s] ball_menu_action failed: %s" % (self.name, e))

    def set_hidden(self, hidden):
        """菜单里点「隐藏」→ 演完动画再藏窗口。"""
        try:
            _panes.close(str(self.name))
            set_hidden_animated(bool(hidden))
        except Exception as e:
            print("[pane:%s] set_hidden failed: %s" % (self.name, e))

    def open_sibling(self, kind):
        """组件窗口里再点开另一个组件（菜单 → 聊天/记忆/命名/大小）。

        ⚠ 锚点用**菜单打开时**那个鼠标点：Python 在 Panes.open 里记下了它，
          组件窗口自己不知道鼠标位置（它只是一个跟随的窗）。
        """
        try:
            name = str(kind)
            print("[pane:%s] open_sibling(%s)" % (self.name, name))
            sys.stdout.flush()
            if name not in PANE_SPECS:
                return
            _panes.open(name, _anchor["x"], _anchor["y"])
        except Exception as e:
            print("[pane:%s] open_sibling failed: %s" % (self.name, e))
            sys.stdout.flush()

    def run(self, command):
        """在鲸鱼窗口里执行一个动作（吸附开关 / 回角落 / 说话）。

        command:
            'snap-now'   → 立刻滑到最近的角落
            'toggle-snap'→ 翻转角落吸附开关
        """
        try:
            if command == 'snap-now':
                _run_in_main("window.__whale.whalePet.snapToCornerNow()")
            elif command == 'toggle-snap':
                _run_in_main(
                    "window.__whale.whalePet.setSnapToCorner("
                    "!window.__whale.whalePet.getSnapshot().snapToCorner)")
        except Exception as e:
            print("[pane:%s] run(%s) failed: %s" % (self.name, command, e))

    def say(self, text, ttl_ms=6000):
        """让鲸鱼窗口冒一句话（聊天回复的气泡画在鲸鱼身上，不在这里）。"""
        try:
            clipped = str(text)[:600]
            _run_in_main("window.__whale.whalePet.showBubble(%s, %d)"
                         % (_js_string(clipped), int(ttl_ms)))
        except Exception as e:
            print("[pane:%s] say failed: %s" % (self.name, e))

    def place(self, width, height):
        """组件窗口回报自己量到的真实尺寸。

        ⚠ 只有这个组件**已经被打开**时才处理：组件窗口一建好就会量一次，
          不拦住的话启动瞬间五个窗口会自己冒出来（实测：启动闪一下）。
        """
        try:
            if not _opened.get(self.name):
                return
            _panes.resize_to(self.name, float(width), float(height))
        except Exception as e:
            print("[pane:%s] place failed: %s" % (self.name, e))

    def close(self):
        _panes.close(self.name)


class Panes:
    def open(self, name, ax, ay, w=None, h=None):
        # 记下这个锚点：之后再从组件窗口里开别的组件，位置就按它算。
        _anchor["x"] = float(ax)
        _anchor["y"] = float(ay)
        # 菜单每次打开都补一遍画板三项：注入要等 React 把这个窗渲染出来，
        # 开机时那一次多半太早（实测 nodes=0）。
        if name == "menu":
            win0 = _PANES.get("menu")
            if win0 is not None:
                threading.Thread(target=_inject_when_ready,
                                 args=(win0, 4.0), daemon=True).start()
        w = float(w) if w else float(PANE_SPECS[name]["w"])
        h = float(h) if h else float(PANE_SPECS[name]["h"])
        w = max(w, float(PANE_SPECS[name]["w"]))
        h = max(h, float(PANE_SPECS[name]["h"]))
        win = _PANES.get(name)
        if win is None:
            return

        def _apply():
            try:
                win.resize(int(round(w)), int(round(h)))
            except Exception as e:
                print("[pane:%s] resize failed: %s" % (name, e))
            screen_w, screen_h = _screen_size()
            x, y = _pane_rect(name, ax, ay, int(round(w)), int(round(h)),
                              screen_w, screen_h)
            try:
                win.move(x, y)
                win.on_top = True
                win.show()
                # ⚠ 每次显示后都重新贴 TOOLWINDOW：pywebview 建窗时给了
                #   APPWINDOW，show() 之后任务栏就会多一个「python」按钮。
                #   在这里改一次最可靠（窗口已经有了真实 hwnd）。
                try:
                    hwnd_pane = _find_hwnd(PANE_TITLES[name])
                    if hwnd_pane:
                        # ⚠ 只在样式真的需要改时才动 SetWindowLong ——
                        #   它每次都触发整窗重绘。样式已经在 before_show
                        #   时设好了，这里通常什么都不做。
                        ex = _u32.GetWindowLongW(hwnd_pane, GWL_EXSTYLE) & 0xFFFFFFFF
                        if (ex & WS_EX_APPWINDOW) or not (ex & WS_EX_TOOLWINDOW):
                            _style(hwnd_pane, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW)
                except Exception:
                    pass
                # ⚠ 拖滑杆时窗口一失焦就会关，所以有输入控件的组件必须
                #   主动拿走焦点（主窗口是 focus=False 建的，不会抢回去）。
                if PANE_SPECS[name].get("activate", True):
                    _activate_window(_find_hwnd(PANE_TITLES[name]))
            except Exception as e:
                print("[pane:%s] place failed: %s" % (name, e))

        sys.stdout.write("[pane:%s] open %dx%d at (%d,%d)\n"
                         % (name, int(round(w)), int(round(h)), ax, ay))
        sys.stdout.flush()
        if not _ui(_apply):
            try:
                _apply()
            except Exception as e:
                print("[pane:%s] open failed: %s" % (name, e))
        # ⚠ 顺序很重要：先真的把窗口摆出来，再记成「已打开」——
        #   否则外部点击检测会看到它在屏幕外的初始位置，把刚弹出来的窗关掉。
        _opened[name] = True
        _watch["ignore_until"] = time.time() + 0.5
        _start_outside_watch()
        # 菜单的尺寸是写死的（PANE_SPECS），摆位一次算对，
        # 不再「先显示再 resize 再挪位」—— 那会让用户看到窗口跳一下。
        if name != "menu":
            # 其它组件（记忆/命名/大小）内容长度会变，仍需要量一次。
            _request_measure(name)

    def resize_to(self, name, w, h):
        """组件窗口报来真实尺寸 → 按它调窗口大小（位置保持不动）。"""
        win = _PANES.get(name)
        if win is None:
            return
        spec = PANE_SPECS[name]
        w = max(float(w), float(spec["w"]))
        h = max(float(h), float(spec["h"]))
        w = min(w, 900.0)
        h = min(h, 800.0)
        if _shapes.get(name) == (int(round(w)), int(round(h))):
            return
        _shapes[name] = (int(round(w)), int(round(h)))

        def _apply():
            try:
                win.resize(int(round(w)), int(round(h)))
            except Exception as e:
                print("[pane:%s] resize_to failed: %s" % (name, e))

        if not _ui(_apply):
            try:
                _apply()
            except Exception:
                pass

    def close(self, name):
        win = _PANES.get(name)
        if win is None:
            return
        _opened[name] = False
        _ui(lambda: win.hide())

    def close_all(self):
        for name in list(_PANES):
            self.close(name)


_panes = Panes()


def _place_main():
    """把鲸鱼窗口摆到它该在的位置（重建之后、启动第一次报告之后）。"""
    win = _WIN["ref"]
    if win is None or _hidden["on"]:
        return
    # ⚠ 出场/重建期间不许摆位置：新窗口刚在桌面外藏好、还没演动画，
    #   这里一 move 就把它怼回桌面上（move 内部是 SetWindowPos 带
    #   SWP_SHOWWINDOW），屏幕上直接闪一头正常大小的鲸鱼。
    if _offscreen["hold"] or _intro["running"]:
        return
    try:
        win.move(_main_pos["x"], _main_pos["y"])
        _apply_no_activate(_find_hwnd(_MAIN_TITLE))
        _rebuild["placed"] = True
    except Exception as e:
        sys.stderr.write("[deskpet] place main failed: %s\n" % e)


def _flush_rebuild(api):
    """去抖到点：按待定的比例重建鲸鱼窗口。"""
    _rebuild["timer"] = None
    target = _rebuild["pending"]
    _rebuild["pending"] = None
    if target is None:
        return
    if abs(target - _main_scale["value"]) < 0.001:
        _place_main()
        return
    if _hidden["on"]:
        _main_scale["value"] = target
        return
    # ⚠ 改比例也走「桌面外重建」：直接 _rebuild_main 会让新窗口在原地
    #   怼上一头正常大小的鲸鱼（第一帧拦不住），滑一下滑杆就是闪一下。
    _rebuild_main_offscreen(api, target, animate=False)


def _rebuild_main(api, scale):
    """销毁鲸鱼窗口，按新尺寸在屏幕外重建，走一遍启动流程。

    ⚠ 只重建鲸鱼这一扇窗：进程不退出、托盘线程不动、组件窗口不动。
    """
    old = _WIN["ref"]
    _main_scale["value"] = scale
    _rebuild["placed"] = False
    _loaded["done"] = False
    if old is not None:
        try:
            old.hide()
        except Exception:
            pass
        try:
            old.destroy()
        except Exception as e:
            sys.stderr.write("[deskpet] destroy main failed: %s\n" % e)
    _WIN["ref"] = _create_main_window(api)
    width, height = _main_size()
    sys.stderr.write("[deskpet] main window rebuilt %dx%d\n" % (width, height))
    sys.stderr.flush()


class Api:
    def __init__(self):
        self.keep_topmost = True

    def move_window_to(self, x, y):
        # ⚠ 隐藏状态下必须丢掉这些位置更新：pywebview 的 move() 内部是
        #   SetWindowPos(..., SWP_SHOWWINDOW)，每帧调一次就会把藏起来的窗口
        #   重新显示出来（实测：日志 hidden=True，鲸鱼却还在）。
        if _hidden["on"]:
            return
        try:
            px = int(round(float(x)))
            py = int(round(float(y)))
            # 一律记账：hold 期间不挪窗，但要把页面**最新**想要的落点记下来，
            # 露脸那一刻用它（见 _settled_pos）。
            _main_pos["x"] = px
            _main_pos["y"] = py
            _pos_report["seen"] = True
            _pos_report["at"] = time.time()
            _pos_report["x"] = px
            _pos_report["y"] = py
            # ⚠ 出场/重建期间不挪窗：页面每帧都在推窗口，我们刚把窗口挪到
            #   屏幕外，下一帧就被它拉回桌面内 —— 那一帧就是「闪一下」。
            if _offscreen["hold"]:
                return
            w = _WIN["ref"]
            if w is not None:
                w.move(px, py)
        except Exception as e:
            print("[api] move_window_to failed:", e)

    def open_pane(self, kind, ax, ay):
        try:
            name = str(kind)
            if name in PANE_SPECS:
                _panes.open(name, float(ax), float(ay))
        except Exception as e:
            print("[api] open_pane failed:", e)

    def ball_menu_action(self, kind):
        """菜单里那三项画板功能（原版菜单没有，插进去的）。

        ink / call / clear 原样交给棋盘，不在这里做任何事 —— 画板是
        pencil.ps1 的地盘。
        """
        try:
            word = str(kind)
            if word in ("ink", "call", "clear"):
                board_intent(word)
            _panes.close("menu")
        except Exception as e:
            print("[api] ball_menu_action failed:", e)

    def close_pane(self, kind):
        try:
            _panes.close(str(kind))
        except Exception as e:
            print("[api] close_pane failed:", e)

    def set_scale(self, percent):
        """页面报来鲸鱼比例（面板那个 x%）。

        比例变了 → 去抖后按新尺寸重建鲸鱼窗口；比例没变 → 如果窗口还没
        摆到位置上，就摆过去（启动时第一份报告走这条路）。

        ⚠ 不调 win.resize()：那会把透明弄丢。改大小 = 销毁 + 按新尺寸重建。
        """
        try:
            pct = float(percent)
        except Exception:
            return
        if not (pct > 0):
            return
        pct = max(0.5, min(WIN_MAX_SCALE, pct))
        # 记下页面报的比例：出场动画要等这一份，才能决定要不要先重建。
        if _scale_report["value"] is None:
            _scale_report["value"] = pct
        if abs(pct - _main_scale["value"]) < 0.001:
            _place_main()
            return
        # ⚠ 出场动画期间**不许重建窗口**（用的是 _intro，不是 _offscreen：
        #   位置控制 120ms 后就交还了，但动画要跑满 1050ms）。
        #   启动时页面会先报一个默认比例（常见 0.5），和 1.0 不同，于是
        #   0.35 秒后去 _flush_rebuild → 销毁窗口重建。那一刻出场动画才演到
        #   三分之一，新窗口从正常大小开始，看起来就是「转到一半突然变大」。
        if _intro["running"]:
            _rebuild["pending"] = pct
            sys.stderr.write("[ball] scale %.2f deferred (intro anim running)\n" % pct)
            return
        _rebuild["pending"] = pct
        if _rebuild["timer"] is not None:
            return
        timer = threading.Timer(0.35, _flush_rebuild, args=(self,))
        timer.daemon = True
        _rebuild["timer"] = timer
        timer.start()

    def set_hidden(self, hidden):
        """托盘「显示 / 隐藏鲸鱼」：走和球菜单同一套动画。"""
        try:
            win = _WIN["ref"]
            if win is None:
                return
            _panes.close_all()
            set_hidden_animated(bool(hidden))
            sys.stderr.write("[deskpet] hidden=%s\n" % bool(hidden))
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write("[api] set_hidden failed: %s\n" % e)
            sys.stderr.flush()

    def toggle_hidden(self):
        self.set_hidden(not _hidden["on"])

    def quit(self):
        try:
            _tray_remove()
        except Exception:
            pass
        try:
            _panes.close_all()
        except Exception:
            pass
        try:
            w = _WIN["ref"]
            if w is not None:
                w.destroy()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Local server
# ----------------------------------------------------------------------

def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


class Server:
    def __init__(self, port=PORT):
        self.port = port
        self.proc = None

    def start(self):
        srv_py = os.path.join(HERE, "server.py")
        args = [sys.executable, "-u", srv_py, "--port", str(self.port)]
        # --demo 是「拿假数据周期切换状态」的演示模式。插件里不能开：
        # 鲸鱼要跟着 DSH 真实在干什么，不是跟着一个演的循环。
        if BALL["demo"]:
            args += ["--demo", "--interval", "6"]
        if BALL["state_file"]:
            args += ["--state-file", BALL["state_file"]]
        # ⚠ 必须带 CREATE_NO_WINDOW，否则任务栏会闪一下「python」。
        #
        #   python.exe 是**控制台子系统**程序，Windows 为它分配控制台时会
        #   连带创建一个控制台窗口。即使这里把 stdout/stderr 都重定向到
        #   DEVNULL，那个控制台窗口照样会被创建、并在任务栏上占一个按钮
        #   （随后因为进程无窗口而消失）——用户看到的就是「开机时任务栏
        #   闪一下 python」。
        #
        #   CREATE_NO_WINDOW 让子进程不创建控制台，任务栏从头到尾不会有它。
        #   pencil.ps1 启动鲸鱼时用的 CreateNoWindow=True 是同一件事。
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(
            args,
            cwd=HERE, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags)
        t0 = time.time()
        while time.time() - t0 < 20:
            if _port_open(self.port):
                return True
            time.sleep(0.3)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def _create_main_window(api):
    """建鲸鱼窗口：直接在落点上创建，创建后保持隐藏，由出场流程决定何时露。

    ⚠ 这是「一次都不位移」的关键。以前的做法是「屏幕外出生 → show →
    挪回落点」，两次位移里必然有一次被用户看见：
      · 挪窗口如果发生在动画开始后 120ms，鲸鱼已经长到 21.6%（缓出前段
        长得最快），那就是一头中等大小的鲸鱼凭空出现在屏幕中央 —— 闪。
      · 挪窗口如果发生在动画开始前，那一帧还画着正常大小的鲸鱼 —— 也闪。
    窗口出生就在落点，就根本不需要挪。

    ⚠ **不能传 hidden=True**：pywebview 对它的实现是
        browser.Opacity = 0; browser.Show(); browser.Hide(); browser.Opacity = 1
      先 Show 再 Hide。Show 那一瞬间窗口就出现在屏幕上（Opacity=0 只让内容
      透明，窗口框体照样占位），实测在 (200,200) 露了约 0.5 秒 —— 那正是
      用户看到的那一闪。

      只传 transparent=True 走另一个分支：Show() 之后立刻 Hide()，中间没有
      可见帧；而且这个分支本来就是 pywebview 为了让透明生效而设计的。
      创建完再由 _intro_spin 用 Win32 ShowWindow 决定何时显示。
    """
    import webview

    width, height = _main_size()
    win = webview.create_window(
        title=_MAIN_TITLE,
        url=URL,
        width=width,
        height=height,
        frameless=True,
        transparent=True,
        on_top=True,
        easy_drag=False,
        resizable=False,
        focus=False,
        # ⚠ 出生点用屏幕外，不用 _main_pos：pywebview 在 create 里会
        #   Show() 一次（让透明生效的那个 hack），那一帧会真的画在屏幕上。
        #   建在 (200,200) 就露在屏幕左上角 —— 实测 trace 里那一帧就是它。
        #   建在屏幕外，同一帧画在看不见的地方，然后由出场流程挪到落点
        #   （那时窗口已经藏起来了，挪动不可见）。
        x=OFFSCREEN_X, y=OFFSCREEN_Y,
        js_api=api,
    )

    def on_loaded():
        # ⚠ 这里**什么都不做**：不 show、不 hide、不 move。
        #
        #   以前这里有 win.hide() + win.show() + sleep(0.5)，是早期为了让
        #   WebView2 的透明生效而加的。但 show() 会把窗口按 pywebview 自己
        #   记的坐标显示出来（实测 (1696,160)，屏幕内），于是又得 hide、
        #   又得挪回落点 —— 那几次动作就是闪的来源。
        #
        #   现在窗口从创建起就在落点，创建时 pywebview 自己做了
        #   Show/Hide 让透明生效，这里没有任何需要做的事。
        sys.stderr.write("[deskpet] main page loaded (stays hidden)\n")
        sys.stderr.flush()
        _loaded["done"] = True

    try:
        # ⚠ 在**显示之前**打上 TOOLWINDOW —— 这是任务栏不出现按钮的关键。
        #
        #   pywebview 建出来的窗口默认是 WS_EX_APPWINDOW，Show() 一被调用，
        #   任务栏立刻加一个「python」按钮；之后再用 SetWindowLong 去掉，
        #   那个按钮已经画出来了，于是用户看到「任务栏闪一下」。
        #
        #   before_show 是 pywebview 在 browser.Show() **之前**触发的唯一钩子
        #   （winforms.py 的 create_window 里：events.before_show.set() 就在
        #   Show 之前）。在这里改扩展样式，Show 那一刻窗口已经是工具窗，
        #   任务栏从头到尾不会出现按钮。
        #
        #   参考 C:\Users\computer\Desktop\把文件列成MD清单.py 的做法：它是
        #   先 setWindowFlags(Qt.Tool | ...) 再 show，所以从来不闪。原理相同
        #   —— 窗口第一次可见之前就必须是「工具窗」。
        def before_show_style():
            try:
                # ⚠ 用窗口对象的 native.Handle，不用 FindWindowW(标题)。
                #   before_show 触发时窗口对象已有 handle，但标题可能还没应用
                #   到窗口上 —— 按标题找会返回 0，样式改不成，窗口就以
                #   APPWINDOW 显示出来，任务栏多一个按钮。
                hwnd = _hwnd_of(win)
                ex_before = (_u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                             if hwnd else -1)
                ok = _style_by_win(win, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW)
                ex_after = (_u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                            if hwnd else -1)
                sys.stderr.write(
                    "[deskpet] before_show hwnd=%s ok=%s ex 0x%X -> 0x%X\n"
                    % (hwnd, ok, ex_before, ex_after))
                sys.stderr.flush()
            except Exception as e:
                sys.stderr.write("[deskpet] before_show style failed: %s\n" % e)

        win.events.before_show += before_show_style
    except Exception as e:
        sys.stderr.write("[deskpet] bind before_show failed: %s\n" % e)

    try:
        win.events.loaded += on_loaded
    except Exception as e:
        sys.stderr.write("[deskpet] bind loaded failed: %s\n" % e)

    # ⚠ before_show 只有**第一次**建窗时会触发。重建（_rebuild_main）时
    #   pywebview 不会再调 before_show.set()，所以第二个窗口的样式没人设，
    #   它就以 WS_EX_APPWINDOW 显示出来 —— 任务栏上多一个按钮，随后又被
    #   后面的 _apply_no_activate 去掉，用户看到的就是「任务栏闪一下」。
    #   实测 trace：重建后的窗口有 vis=True TOOL=False APP=True 的 92ms 窗口。
    #
    #   所以这里补一条：窗口对象一拿到 hwnd 就立刻改成工具窗，不等任何事件。
    #   这是重建路径唯一的保障。
    threading.Thread(target=_style_when_ready, args=(win,), daemon=True).start()
    return win


def _style_when_ready(win, seconds=3.0):
    """盯着窗口对象，一拿到 hwnd 就把它改成工具窗（不进任务栏）。

    ⚠ 为什么用轮询而不是事件：before_show 只在第一次建窗时触发（见上），
      而窗口的 hwnd 是 WinForms 窗体构造完才有的，create_window 返回时
      还不一定有。每 5ms 试一次，拿到就立刻设，尽量赶在任何 Show 之前。
    """
    end = time.time() + seconds
    while time.time() < end:
        hwnd = _hwnd_of(win)
        if hwnd:
            try:
                ex = _u32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                if (ex & WS_EX_APPWINDOW) or not (ex & WS_EX_TOOLWINDOW):
                    _u32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                        (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                    sys.stderr.write(
                        "[deskpet] style-watch hwnd=%s ex 0x%X -> TOOLWINDOW\n"
                        % (hwnd, ex))
                    sys.stderr.flush()
                    return
            except Exception:
                pass
        time.sleep(0.005)


def acquire_instance_lock(where):
    """单实例锁：同一个状态目录只允许一条鲸鱼。

    ⚠ 为什么需要：画板（pencil.ps1）会被反复停/起（DSH 缩托盘、插件重载），
      每次起来都想拉一条鲸鱼。没有锁的话就是两条、三条叠在屏幕上
      （实测见过三个）。有了锁，第二条一看锁被占就直接退出，用户始终只看到一条。

    ⚠ 用文件锁而不是「查 pid 再判断」：pid 会被复用，而且查进程要额外调
      tasklist（那玩意儿在 GBK 环境下还不可靠）。文件锁由内核管，
      持有进程一死锁就自动释放，没有陈旧判断的问题。
    """
    if not where:
        return None
    try:
        os.makedirs(os.path.dirname(where), exist_ok=True)
    except Exception:
        pass
    try:
        handle = open(where, "a+")
    except Exception as e:
        sys.stderr.write("[deskpet] lock open failed: %s\n" % e)
        return None
    # ⚠ 文件必须是**非空**的：msvcrt.locking 锁的是「从当前偏移开始的 N 个
    #   字节」，空文件上锁会失败（实测锁文件 0 字节、两条鲸鱼同时活着）。
    #   先写一个字节占位，再上锁。
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("0")
            handle.flush()
        handle.seek(0)
    except Exception:
        pass
    try:
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception as e:
        # 占不到锁说明已经有鲸鱼在跑，安静退出。
        sys.stderr.write("[deskpet] lock busy (%s), another whale is running\n" % e)
        try:
            handle.close()
        except Exception:
            pass
        return None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except Exception:
        pass
    sys.stderr.write("[deskpet] lock acquired %s pid=%d\n" % (where, os.getpid()))
    return handle


def main():
    _redirect_log()

    # Start the taskbar guard before any window exists, so the very first style
    # pywebview applies is already corrected. Nothing outside this process is
    # touched: it only strips APPWINDOW from windows this process owns.
    _start_taskbar_guard()

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="", help="棋盘状态目录（tray.txt / alive.txt 所在）")
    ap.add_argument("--state-path-file", default="",
                    help="状态目录写在哪个文件里（优先于 --state-dir）")
    ap.add_argument("--state-file", default="", help="DSH 写的 status.json")
    ap.add_argument("--demo", action="store_true", help="演示模式：拿假数据周期切换状态")
    args = ap.parse_args()

    # 插件目录是中文名，命令行按 ANSI 传参会把路径打乱。所以 pencil.ps1 把
    # 状态目录写进一个 UTF-8 小文件，用 --state-path-file 指过来，这里读它。
    state_dir = ""
    if args.state_path_file:
        try:
            with open(args.state_path_file, encoding="utf-8-sig") as f:
                state_dir = f.read().strip()
        except Exception as e:
            sys.stderr.write("[deskpet] cannot read state path: %s\n" % e)
    if not state_dir:
        state_dir = args.state_dir
    if not state_dir and args.state_file:
        state_dir = os.path.dirname(os.path.abspath(args.state_file))

    BALL["state_dir"] = os.path.abspath(state_dir) if state_dir else ""
    if args.state_file:
        BALL["state_file"] = os.path.abspath(args.state_file)
    elif BALL["state_dir"]:
        # status.json 就在棋盘状态目录里（pencil.ps1 的 -Status 指向它）。
        # 走文件推导而不是命令行：插件目录是中文名。
        guess = os.path.join(BALL["state_dir"], "status.json")
        BALL["state_file"] = guess if os.path.isfile(guess) else ""
    else:
        BALL["state_file"] = ""
    BALL["demo"] = bool(args.demo)
    sys.stderr.write("[deskpet] state_dir=%s status=%s\n"
                     % (BALL["state_dir"], BALL["state_file"]))

    # 单实例：同一个状态目录只允许一条鲸鱼。
    # 占不到锁说明已经有一条在跑，这里直接安静退出 —— 不是错误。
    lock_path = os.path.join(BALL["state_dir"], "ball", "whale.lock") if BALL["state_dir"] else ""
    lock = acquire_instance_lock(lock_path)
    if lock is None and lock_path != "":
        sys.stderr.write("[deskpet] another whale holds the lock, exiting\n")
        return 0

    try:
        import webview
    except Exception as e:
        sys.stderr.write("[deskpet] cannot import pywebview: %s\n" % e)
        return 1

    srv = Server()
    if _port_open(PORT):
        sys.stderr.write("[deskpet] server already running, reuse it\n")
    elif srv.start():
        sys.stderr.write("[deskpet] server ready %s\n" % URL)
    else:
        sys.stderr.write("[deskpet] server failed to start\n")
        return 1

    api = Api()
    # 存一份给推迟的重建用（出场动画演完后才执行）。
    _api["ref"] = api

    # ⚠ 建窗口之前先把页面存的鲸鱼比例读出来，直接按最终尺寸建。
    #
    #   不这样做的话流程是：按默认 1.0 建 → 页面报上来 0.5 → 尺寸不一致 →
    #   销毁重建一次。而重建出来的第二个窗口拿不到 before_show 钩子
    #   （pywebview 只对第一个窗口触发），它会带着 WS_EX_APPWINDOW 显示
    #   ~94ms —— 任务栏就在这 94ms 里多出一个 python 按钮。
    #   开局就按 0.5 建，就不用重建，那个窗口根本不会出现。
    persisted = _read_persisted_scale()
    if persisted is not None:
        _main_scale["value"] = persisted
        sys.stderr.write("[deskpet] persisted scale %.2f (no rebuild needed)\n"
                         % persisted)
    _WIN["ref"] = _create_main_window(api)
    width, height = _main_size()
    sys.stderr.write("[deskpet] main window %dx%d\n" % (width, height))

    for name, spec in PANE_SPECS.items():
        win = webview.create_window(
            title=PANE_TITLES[name],
            url="%s&pane=%s" % (URL, name),
            width=spec["w"],
            height=spec["h"],
            frameless=True,
            transparent=True,
            on_top=True,
            easy_drag=False,
            resizable=False,
            # WARNING: hidden=True, not just off-screen coordinates. A window
            # parked at (-20000,-20000) is still mapped: it still counts in the
            # window manager, still flashes in the taskbar, and still appears in
            # Alt-Tab. That is why "move it outside the desktop" does not stop
            # the flash. hidden=True keeps the pane out of the window manager
            # from creation until the user opens it.
            hidden=True,
            x=SPAWN_X, y=SPAWN_Y,
            js_api=PaneApi(name),
        )
        _PANES[name] = win
        # Put the pane out of the taskbar before pywebview can show it.
        #
        # WARNING: doing this in before_show is too late. pywebview's WinForms
        # backend calls browser.Show()/Hide() for a hidden transparent window,
        # and the taskbar button is added the moment Show() runs. before_show
        # fires just before that -- close, but the flash was measured at 93-109ms
        # of a visible, non-TOOLWINDOW window per pane, which is exactly the
        # Python icon the user saw appear in the taskbar.
        #
        # The handle does not exist until the window is created, but it appears
        # within a few milliseconds. Polling for it right here, in a tight loop,
        # wins the race against the Show() that follows.
        _grab_pane_style(win, name)

        def _pane_before_show(_win=win, _name=name):
            try:
                _style_by_win(_win, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW)
                _hide_window(_win)
            except Exception as e:
                sys.stderr.write("[deskpet] pane %s before_show failed: %s\n"
                                 % (_name, e))

        try:
            win.events.before_show += _pane_before_show
        except Exception as e:
            sys.stderr.write("[deskpet] bind pane before_show failed: %s\n" % e)
        sys.stderr.write("[deskpet] pane %s %dx%d\n"
                         % (name, spec["w"], spec["h"]))

    def bind_pane(name, win):
        def on_pane_loaded():
            # WARNING: do NOT hide()/show()/sleep(0.5)/hide() here.
            #
            # That dance was added so WebView2 would apply transparency, but it
            # makes every pane newly VISIBLE for half a second at startup: four
            # panes spawning at (-20000,-20000) and being shown one after the
            # other. Measured lifetimes 752-891ms each, which is exactly the
            # small window the user saw flashing when the plugin started.
            #
            # A pane is created off-screen and must stay HIDDEN until the user
            # opens it. Being off-screen is not the same as being hidden: an
            # off-screen window is still mapped and still participates in the
            # taskbar / Alt-Tab, which is why "it is outside the desktop" did not
            # solve the flash either. Hide is the only thing that takes it out of
            # the window manager entirely.
            try:
                win.hide()
            except Exception as e:
                sys.stderr.write("[deskpet] pane %s hide failed: %s\n"
                                 % (name, e))
            if name == "menu":
                inject_menu_extra(win)
            sys.stderr.write("[deskpet] pane %s ready\n" % name)

        win.events.loaded += on_pane_loaded

    for name, win in _PANES.items():
        try:
            bind_pane(name, win)
        except Exception as e:
            sys.stderr.write("[deskpet] bind %s failed: %s\n" % (name, e))

    def keep_top():
        while True:
            time.sleep(1.5)
            if not api.keep_topmost:
                continue
            try:
                hwnd = _find_hwnd(_MAIN_TITLE)
                if hwnd and not _hidden["on"]:
                    _set_topmost(hwnd)
                for name, pane in _PANES.items():
                    if pane is None or _opened.get(name, False) is False:
                        continue
                    _set_topmost(_find_hwnd(PANE_TITLES[name]))
            except Exception:
                pass

    threading.Thread(target=keep_top, daemon=True).start()

    def after_start():
        try:
            _WIN["ref"].events.shown.wait(8)
        except Exception:
            pass
        _apply_no_activate(_find_hwnd(_MAIN_TITLE))
        # 告诉棋盘：球真的起来了，别退回 WPF 圆点。
        write_status("running")
        # 出场动画：从很小转到正常大小。
        # 和退出时那一下（越转越小）对称 —— 用户看到的是一头一尾
        # 都有动作，而不是「凭空出现 / 凭空消失」。
        # 放在这里而不是页面里：页面加载完就画了，那时窗口还没显示，
        # 动画等于白演。
        threading.Thread(target=_intro_spin, daemon=True).start()
        try:
            build_tray(api)
        except Exception as e:
            sys.stderr.write("[deskpet] tray init failed: %s\n" % e)
        # 棋盘心跳没了就跟着退（DSH 被关掉时棋盘是被强杀的，不会给我们发信号）。
        threading.Thread(target=board_watch, daemon=True).start()
        # 棋盘进退绘制模式时藏/露鲸鱼，并演一下入水/出水。
        threading.Thread(target=_watch_commands, daemon=True).start()

    try:
        webview.start(func=after_start, gui="edgechromium",
                      private_mode=False)
    finally:
        write_status("stopped")
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
