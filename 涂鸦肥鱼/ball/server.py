# -*- coding: utf-8 -*-
"""
whale-pet 独立实验 —— Python 端

ATTRIBUTION / 来源声明
----------------------
本文件源自其他开源项目，链路为：

    Yifffan/dsh-plugin-whale-pet 0.2.9   （原始项目）
            -> fork
    rongzi5/dsh-whale-pet                （本项目直接取用的版本）
            -> 修改
    DSH 涂鸦肥鱼 / dsh-doodle-fat-fish   （本项目）

  * 代码：MIT License，版权归原作者 Yifffan 与 rongzi5
  * 美术资源（whale.ico、dist/）：CC BY 4.0，需署名，未改动原图

完整声明见插件根目录的 THIRD-PARTY-NOTICES.md。

    python exp/server.py

做两件事：
  ① 提供静态页面（exp/dist/），浏览器打开就能看到鲸鱼
  ② 提供 /api/state —— 鲸鱼的「会话状态」由这里给

状态格式（照抄 DSH 的形状，见 src/client/runtime/session-observer.ts）：
    {
      "current": "s1",                      # 当前会话 id
      "sessions": { "s1": {...} },
      "conversation": {
        "running": true,                    # agent 在跑吗
        "nodes": [                          # 对话节点
          {"kind": "tool-call", "seq": 1, "time": ..., "call": {"name": "web_search"}},
          {"kind": "tool-result", "seq": 2, "time": ..., "isError": false,
           "resultView": {"exitCode": 0}}
        ],
        "lastAgentError": null
      },
      "pendingInteractions": {}             # {"s1": {"kind": "approval"}}
    }

⚠ 这个文件目前是「假数据发生器」——
   用 --demo 参数会周期性切换状态，方便肉眼观察鲸鱼的反应。
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")

# ══════════════════════════════════════════════════════════════════
#  聊天代理（/api/whale-pet/chat）
# ══════════════════════════════════════════════════════════════════
#
#  API Key 只在服务端：页面只调同源的 /api/whale-pet/chat，
#  Key 从环境变量取，浏览器里永远看不到。
API_BASE = os.environ.get("DSH_WHALE_API_BASE", "https://api.deepseek.com")
DEFAULT_MODEL = os.environ.get("DSH_WHALE_API_MODEL", "deepseek-chat")

MODEL_CATALOG = {
    "providers": [
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "models": [
                {"id": "deepseek-chat", "name": "DeepSeek Chat", "efforts": []},
                {
                    "id": "deepseek-reasoner",
                    "name": "DeepSeek Reasoner",
                    "efforts": [{"id": "low", "name": "低"},
                                {"id": "high", "name": "高"}],
                    "defaultEffort": "low",
                },
            ],
        },
    ],
    "default": {"provider": "deepseek", "model": DEFAULT_MODEL},
}


def _api_key():
    for name in ("DSH_WHALE_API_KEY", "DEEPSEEK_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# ══════════════════════════════════════════════════════════════════
#  状态
# ══════════════════════════════════════════════════════════════════

class State:
    """当前会话状态（会被 /api/state 读走）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "current": "s1",
            "sessions": {"s1": {"id": "s1"}},
            "conversation": {"running": False, "nodes": [], "lastAgentError": None},
            "pendingInteractions": {},
        }
        self._seq = 0

    def get(self):
        with self._lock:
            return json.loads(json.dumps(self._data))   # 深拷贝

    # ---------- 给外部（将来接 DSH）用的接口 ----------

    def set_running(self, running):
        with self._lock:
            self._data["conversation"]["running"] = bool(running)

    def add_tool(self, name, exit_code=None):
        """记一次工具调用（exit_code=None 表示「正在跑」）。"""
        with self._lock:
            self._seq += 1
            node = {
                "kind": "tool-call",
                "seq": self._seq,
                "time": time.time() * 1000,
                "call": {"name": name},
            }
            self._data["conversation"]["nodes"].append(node)
            if exit_code is not None:
                self._seq += 1
                self._data["conversation"]["nodes"].append({
                    "kind": "tool-result",
                    "seq": self._seq,
                    "time": time.time() * 1000,
                    "isError": exit_code != 0,
                    "resultView": {"exitCode": exit_code},
                })
            # 只留最近 200 条
            self._data["conversation"]["nodes"] = \
                self._data["conversation"]["nodes"][-200:]

    def set_pending(self, kind):
        with self._lock:
            if kind:
                self._data["pendingInteractions"] = {"s1": {"kind": kind}}
            else:
                self._data["pendingInteractions"] = {}

    def clear(self):
        with self._lock:
            self._data["conversation"]["nodes"] = []
            self._seq = 0


STATE = State()

# DSH 的会话状态文件（pencil.ps1 的 -Status 指向它）。
STATE_FILE = {"path": ""}


def live_state():
    """会话状态：有 DSH 的 status.json 就用它，没有就用内存里那份。

    DSH 一直在写 {busy, phase, tool}。翻译成页面观察者要的形状即可 ——
    不需要为桌宠另开一条通道，也不需要它自己去问 agent。
    """
    path = STATE_FILE["path"]
    if not path:
        return STATE.get()
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception:
        return STATE.get()
    busy = doc.get("busy") is True
    tool = str(doc.get("tool") or "")
    nodes = []
    if busy:
        nodes.append({"kind": "tool-call", "seq": 1, "call": {"name": tool or "thinking"}})
    return {
        "current": "ball",
        "sessions": {"ball": {"id": "ball"}},
        "conversation": {"running": busy, "nodes": nodes, "lastAgentError": None},
        "pendingInteractions": {},
    }


# ══════════════════════════════════════════════════════════════════
#  演示：周期性切换状态，方便肉眼观察
# ══════════════════════════════════════════════════════════════════

DEMO_STEPS = [
    ("idle",        lambda: (STATE.set_running(False), STATE.clear())),
    ("working",     lambda: (STATE.set_running(True),
                             STATE.add_tool("web_search", 0))),
    ("working2",    lambda: STATE.add_tool("edit", 0)),
    ("running",     lambda: STATE.add_tool("pwsh")),
    ("error",       lambda: STATE.add_tool("pwsh", 1)),
    ("awaiting",    lambda: STATE.set_pending("approval")),
    ("back",        lambda: (STATE.set_pending(None), STATE.clear())),
]


def demo_loop(interval):
    i = 0
    while True:
        try:
            name, fn = DEMO_STEPS[i % len(DEMO_STEPS)]
            fn()
            sys.stderr.write("[demo] %s\n" % name)
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write("[demo] 出错: %s\n" % e)
        finally:
            i += 1
            time.sleep(interval)


# ══════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIST, **kw)

    def log_message(self, fmt, *args):
        # 压掉静态资源刷屏，只留错误
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            sys.stderr.write("[http] " + (fmt % args) + "\n")

    def end_headers(self):
        # ⚠ 静态资源一律 no-store：改完 app.js 重新打开就拿到新的，
        #   WebView2 不会把旧页面缓存住（实测改完不刷新还是旧界面）。
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._json(200, live_state())
            return
        if self.path.startswith("/api/whale-pet/models"):
            self._json(200, MODEL_CATALOG)
            return
        if self.path.startswith("/api/whale-pet/health"):
            self._json(200, {"ok": True, "configured": _api_key() != ""})
            return
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/whale-pet/chat"):
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._json(400, {"error": "messages required"})
            return

        key = _api_key()
        if key == "":
            self._json(503, {"error": "没配置 API Key（设 DEEPSEEK_API_KEY）"})
            return

        model = payload.get("model") or DEFAULT_MODEL
        req = urllib.request.Request(
            API_BASE.rstrip("/") + "/chat/completions",
            data=json.dumps({"model": model, "messages": messages,
                             "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            self._json(200, {"content": content})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            self._json(502, {"error": "上游 %s: %s" % (e.code, detail)})
        except Exception as e:
            self._json(502, {"error": "上游不可达: %s" % e})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=48900)
    ap.add_argument("--demo", action="store_true",
                    help="周期切换状态，方便观察")
    ap.add_argument("--interval", type=float, default=6.0)
    ap.add_argument("--state-file", default="",
                    help="DSH 的 status.json；给了就按它报状态，不再用内存里那份")
    args = ap.parse_args()

    if args.state_file:
        STATE_FILE["path"] = os.path.abspath(args.state_file)

    if not os.path.isdir(DIST):
        sys.stderr.write("找不到 %s —— 先跑：\n"
                         "    node exp/build-standalone.mjs\n" % DIST)
        return 1

    if args.demo:
        threading.Thread(target=demo_loop, args=(args.interval,),
                         daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    sys.stderr.write("=" * 56 + "\n")
    sys.stderr.write("  whale-pet 独立实验\n")
    sys.stderr.write("  打开: http://127.0.0.1:%d/\n" % args.port)
    sys.stderr.write("  状态: http://127.0.0.1:%d/api/state\n" % args.port)
    if args.demo:
        sys.stderr.write("  演示模式: 每 %.0f 秒切换一次\n" % args.interval)
    sys.stderr.write("=" * 56 + "\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n退出。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
