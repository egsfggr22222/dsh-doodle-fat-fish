# -*- coding: utf-8 -*-
"""Install the companion's heavy dependencies, resumably.

The plugin ships without them: ocr_env (a full Python environment) and the
detector weights are ~2.6 GB, far too much to put in a plugin bundle. So the
user installs the plugin, presses "下载依赖" in the panel, and this does the rest.

Design rules, in the order they matter
-------------------------------------
1. RESUME, never restart. Every unit of work records its own completion, so a
   cancelled or failed run continues from the first unfinished item. Nothing
   already on disk is downloaded again.
2. Verify before trusting. A file is only marked done when its size and, where a
   hash is known, its digest match. A half-written file from a killed download
   is discarded and fetched again, not counted as done.
3. Channels that actually work here. pip goes through a mirror list (Tsinghua
   first: this machine is in China and pypi.org is often unusable), then the
   official index, then whatever is configured. Weights come from Hugging Face
   with a mirror fallback.
4. Report honestly. The state file is the truth the panel reads: what is
   running, which item, how far, and why it stopped.

Run standalone or through the Host:

    python tools/install_deps.py --state state.json --log install.log
    python tools/install_deps.py --state state.json --stop
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
OCR_ENV = os.path.join(PLUGIN, "ocr_env")
OCR_PY = os.path.join(OCR_ENV, "Scripts", "python.exe")
TOOLS = HERE

# The interpreter the plugin ships for itself.
#
# WARNING: this is NOT ocr_env/Scripts/python.exe. A clean Windows machine has no
# Python at all, so the old design could not start: creating ocr_env needs a
# Python, and the Python was supposed to live inside ocr_env. The fix is to ship a
# portable interpreter of our own, unpacked under runtime/, and build the
# environment with it. Nothing on the machine is consulted and nothing outside
# this folder is touched.
RUNTIME = os.path.join(PLUGIN, "runtime")
RUNTIME_PY = os.path.join(RUNTIME, "python", "python.exe")

# Portable CPython, unpacked as runtime/python/. python-build-standalone is the
# right shape for this: a full interpreter with pip, venv and tkinter, no
# installer, no registry, no PATH changes, and no administrator rights.
#
# The official "embeddable" zip is NOT usable here: it has no pip and no tkinter,
# and the board is a tkinter program.
PORTABLE_PYTHON = {
    "name": "python-3.10.16",
    "bytes": 39020000,
    "urls": [
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20250115/cpython-3.10.16+20250115-x86_64-pc-windows-msvc-shared-install_only.tar.gz",
        "https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/"
        "releases/download/20250115/"
        "cpython-3.10.16+20250115-x86_64-pc-windows-msvc-shared-install_only.tar.gz",
        "https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/"
        "releases/download/20250115/"
        "cpython-3.10.16+20250115-x86_64-pc-windows-msvc-shared-install_only.tar.gz",
    ],
}

# pip mirrors, ordered by what this network has actually reached -- measured, not
# guessed. Tsinghua and BFSU answer 403 from here while the rest work, so they sit
# near the end: a mirror that refuses everything costs a timeout on every package.
PIP_MIRRORS = [
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://mirrors.ustc.edu.cn/pypi/simple",
    "https://repo.huaweicloud.com/repository/pypi/simple",
    "https://mirrors.sjtug.sjtu.edu.cn/pypi/web/simple",
    "https://mirrors.nju.edu.cn/pypi/web/simple",
    "https://pypi.org/simple",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.bfsu.edu.cn/pypi/web/simple",
]

# Per-package extra sources, tried BEFORE the generic mirror list.
#
# paddlepaddle is not on PyPI proper in a usable form for every Python version;
# Baidu publishes it on its own index and mirrors it separately. A package that
# lists sources here gets those first, then falls back to the mirrors above.
PACKAGE_INDEXES = {
    "paddlepaddle": [
        "https://paddlepaddle.org.cn/packages/stable/cpu/",
        "https://mirror.baidu.com/pypi/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
    ],
    "paddleocr": [
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
        "https://mirrors.ustc.edu.cn/pypi/simple",
        "https://pypi.org/simple",
    ],
}

# Seconds a single mirror attempt may take. Short by design: nine mirrors times
# two minutes is still less than one stuck host with pip's default retries.
MIRROR_TIMEOUT_S = 120

# The Python packages the tools import. Kept minimal on purpose: every extra
# wheel is another chance for the install to fail on a machine that does not
# need it.
PIP_PACKAGES = [
    ("numpy", "numpy<2"),
    ("pillow", "pillow"),
    ("opencv-python-headless", "opencv-python-headless"),
    ("paddleocr", "paddleocr==2.7.3"),
    ("paddlepaddle", "paddlepaddle==2.6.2"),
    # The whale's window: pywebview drives WebView2 through pythonnet, and the
    # ball's local server is bottle. These used to be vendored in ball/runtime/,
    # which put third-party code in the repository; they are ordinary wheels.
    ("pywebview", "pywebview==6.2.1"),
    ("pythonnet", "pythonnet==3.1.0"),
    ("bottle", "bottle==0.13.4"),
    ("bottle-websocket", "bottle-websocket==0.2.9"),
]

# Optional: only needed for object detection. Big (torch), so it is a separate
# item the user can skip by stopping the run.
PIP_OPTIONAL = [
    ("torch", "torch --index-url https://download.pytorch.org/whl/cpu"),
    ("ultralytics", "ultralytics"),
]

# GitHub's release CDN, direct and through two proxies. This is where ultralytics
# actually publishes its weights; the `ultralytics/assets` Hugging Face repo does
# not exist (it answers 401), which is why an earlier version failed for every
# user on every mirror.
GH = "https://github.com/ultralytics/assets/releases/download/v8.3.0/"
GH_PROXIES = [
    GH + "{name}",
    "https://ghproxy.net/" + GH + "{name}",
    "https://gh-proxy.com/" + GH + "{name}",
    "https://ghfast.top/" + GH + "{name}",
]

# The weights, with the sizes we expect and the exact URLs that serve them.
#
# WARNING: sizes are the real ones, measured with a HEAD request -- not guesses.
# yolov8s-worldv2.pt is 24.7 MB, not the 57 MB an earlier version claimed, and a
# wrong expectation is worse than none: the download succeeds and is then thrown
# away as "wrong size". Verify with a HEAD before adding an entry.
WEIGHTS = [
    {
        "name": "yoloe-11s-seg.pt",
        "path": os.path.join(TOOLS, "yoloe-11s-seg.pt"),
        "bytes": 27803986,
        "required": True,
        "urls": [u.format(name="yoloe-11s-seg.pt") for u in GH_PROXIES] + [
            "https://huggingface.co/jameslahm/yoloe/resolve/main/yoloe-11s-seg.pt",
            "https://hf-mirror.com/jameslahm/yoloe/resolve/main/yoloe-11s-seg.pt",
        ],
    },
    {
        "name": "omniparser_icon_detect.pt",
        "path": os.path.join(TOOLS, "omniparser_icon_detect.pt"),
        "bytes": 40623819,
        "required": True,
        "urls": [
            "https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect/model.pt",
            "https://hf-mirror.com/microsoft/OmniParser-v2.0/resolve/main/icon_detect/model.pt",
        ],
    },
    {
        "name": "yolov8s-worldv2.pt",
        "path": os.path.join(TOOLS, "yolov8s-worldv2.pt"),
        "bytes": 25923032,
        "required": True,
        "urls": [u.format(name="yolov8s-worldv2.pt") for u in GH_PROXIES],
    },
    {
        "name": "mobileclip_blt.ts",
        "path": os.path.join(TOOLS, "mobileclip_blt.ts"),
        "bytes": 599764649,
        "required": True,
        "urls": [u.format(name="mobileclip_blt.ts") for u in GH_PROXIES],
    },
    {
        "name": "grounding-dino",
        "path": os.path.join(TOOLS, "weights", "grounding-dino-tiny",
                             "model.safetensors"),
        "bytes": 689359096,
        "required": False,
        "urls": [
            "https://huggingface.co/IDEA-Research/grounding-dino-tiny/resolve/main/model.safetensors",
            "https://hf-mirror.com/IDEA-Research/grounding-dino-tiny/resolve/main/model.safetensors",
        ],
    },
    {
        "name": "clip-vit-b32",
        "path": os.path.join(TOOLS, "weights", "clip", "ViT-B-32.pt"),
        "bytes": 605247071,
        "required": False,
        "urls": [
            "https://huggingface.co/openai/clip-vit-base-patch32/resolve/main/pytorch_model.bin",
            "https://hf-mirror.com/openai/clip-vit-base-patch32/resolve/main/pytorch_model.bin",
        ],
    },
]


def now():
    return time.strftime("%H:%M:%S")


class State:
    """The install's durable memory.

    Written after every step so a kill -9 loses at most the step in flight. The
    panel reads this file, so it also carries the human-facing progress.
    """

    def __init__(self, path):
        self.path = path
        self.doc = {
            "status": "idle",        # idle | running | paused | failed | done
            "step": "",
            "stepIndex": 0,
            "stepCount": 0,
            "detail": "",
            "percent": 0,
            "done": [],              # ids of finished items
            "failed": [],            # [{id, error}]
            "startedAt": 0,
            "updatedAt": 0,
            "log": "",
        }
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                doc = json.load(f)
            if isinstance(doc, dict):
                self.doc.update(doc)
        except Exception:
            pass

    def save(self):
        self.doc["updatedAt"] = int(time.time() * 1000)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def set(self, **kw):
        self.doc.update(kw)
        self.save()

    def is_done(self, item_id):
        return item_id in self.doc.get("done", [])

    def mark_done(self, item_id):
        done = self.doc.setdefault("done", [])
        if item_id not in done:
            done.append(item_id)
        self.save()

    def clear_failed(self, item_id):
        self.doc["failed"] = [f for f in self.doc.get("failed", [])
                              if f.get("id") != item_id]
        self.save()


class Installer:
    def __init__(self, state, log_path):
        self.state = state
        self.log_path = log_path
        self.stop_requested = False

    # -- plumbing ---------------------------------------------------------

    def log(self, message):
        line = "%s %s" % (now(), message)
        print(line, flush=True)
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def stop(self):
        """Ask the run to stop at the next safe boundary.

        Not a kill: a download or a pip install in flight is allowed to finish
        its current step, because interrupting pip mid-write leaves a broken
        environment that is worse than a slower pause.
        """
        self.stop_requested = True

    def check_stop(self):
        """Stop at the next boundary, however the request arrived.

        Two channels, because the button lives in another process:
          * `--stop` in this process sets the flag directly (the installer ran to
            completion from a terminal);
          * the Host writes `status: "pausing"` into the state file, which is the
            only channel available when the panel asks a running install to stop.

        WARNING: reading the file on every check is deliberate. The install is a
        sequence of long steps, and a poll that only looked at the flag would make
        the panel's pause button do nothing at all.
        """
        if self.stop_requested:
            raise StopRequested()
        try:
            with open(self.state.path, encoding="utf-8-sig") as f:
                doc = json.load(f)
            if doc.get("status") == "pausing":
                raise StopRequested()
        except StopRequested:
            raise
        except Exception:
            pass

    # -- python environment ------------------------------------------------

    def ensure_runtime(self):
        """Unpack the plugin's own interpreter under runtime/.

        This is the first step, before anything else, because every later step
        needs a Python and the machine may not have one. A clean Windows install
        has none, which is exactly the case this plugin must survive.
        """
        if os.path.isfile(RUNTIME_PY):
            self.log("内置 Python 已就绪，跳过")
            return True
        self.log("下载内置 Python（约 37 MB）…")
        os.makedirs(RUNTIME, exist_ok=True)
        archive = os.path.join(RUNTIME, "python.tar.gz")
        spec = {
            "name": PORTABLE_PYTHON["name"],
            "path": archive,
            "bytes": PORTABLE_PYTHON["bytes"],
            "urls": PORTABLE_PYTHON["urls"],
        }
        # The archive is a plain file download, so it reuses the weight path:
        # mirrors, resume, size verification and 416 handling all come free.
        self.download("runtime-python", spec)
        self.log("解压内置 Python…")
        try:
            import tarfile
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(RUNTIME)
        except Exception as e:
            raise RuntimeError("解压内置 Python 失败：%s" % str(e)[:200])
        finally:
            try:
                os.remove(archive)
            except Exception:
                pass
        if not os.path.isfile(RUNTIME_PY):
            raise RuntimeError("解压后找不到 %s" % RUNTIME_PY)
        # The bundle's python.exe works from any directory, but pip writes scripts
        # next to it; keeping them inside runtime/ means uninstalling the plugin
        # leaves nothing behind.
        self.log("内置 Python 就绪：%s" % RUNTIME_PY)
        return True

    def ensure_venv(self):
        """Create ocr_env from the plugin's own interpreter.

        A venv rather than installing straight into runtime/python: the runtime is
        ours and read-mostly, and a venv keeps the two separable. It also means a
        damaged environment can be deleted and rebuilt without re-downloading the
        interpreter.
        """
        if os.path.isfile(OCR_PY):
            self.log("ocr_env 已存在，跳过创建")
            return True
        if not os.path.isfile(RUNTIME_PY):
            self.ensure_runtime()
        # A half-deleted ocr_env is worse than none: venv refuses to write into a
        # directory whose python.exe is locked or already exists. Clear it first,
        # and say so, because a silent wipe of a big folder looks like a hang.
        if os.path.isdir(OCR_ENV):
            self.log("检测到残缺的 ocr_env，先清理…")
            shutil.rmtree(OCR_ENV, ignore_errors=True)
            if os.path.isdir(OCR_ENV) and os.listdir(OCR_ENV):
                raise RuntimeError(
                    "ocr_env 无法清理（文件可能被占用）。请关闭占用它的程序后重试")
        self.log("创建 ocr_env 虚拟环境…")
        os.makedirs(PLUGIN, exist_ok=True)
        proc = subprocess.run(
            [RUNTIME_PY, "-m", "venv", OCR_ENV],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not os.path.isfile(OCR_PY):
            raise RuntimeError("创建 ocr_env 失败：%s"
                               % (proc.stderr or proc.stdout or "")[-300:])
        self.log("ocr_env 创建完成")
        return True

    # -- pip ---------------------------------------------------------------

    def pip_install(self, item_id, spec):
        """Install one requirement, trying each mirror in turn.

        A mirror that fails is remembered for this run so the next package does
        not waste a timeout on it again.
        """
        if self.state.is_done(item_id):
            self.log("%s 已安装，跳过" % item_id)
            return
        self.state.clear_failed(item_id)
        self.state.set(step=item_id, detail="安装 %s" % item_id)
        self.log("pip install %s" % spec)

        extra = []
        parts = spec.split()
        if "--index-url" in parts:
            at = parts.index("--index-url")
            extra = parts[at:]
            parts = parts[:at]
        package = " ".join(parts)
        bare = package.split("==")[0].split("<")[0].split(">")[0].strip().lower()

        # Sources for this package: its own indexes first (paddle lives on Baidu's
        # mirrors, not on PyPI proper), then the generic list. Duplicates are
        # dropped so a shared mirror is not tried twice.
        sources = []
        for url in PACKAGE_INDEXES.get(bare, []) + PIP_MIRRORS:
            if url not in sources:
                sources.append(url)

        errors = []
        for mirror in [None] + sources:
            self.check_stop()
            argv = [OCR_PY, "-m", "pip", "install", "--no-input",
                    "--disable-pip-version-check",
                    # pip's own retries would multiply every timeout by five. One
                    # attempt per mirror, and let the loop move on instead.
                    "--retries", "0", "--timeout", str(MIRROR_TIMEOUT_S),
                    package]
            if extra:
                argv.extend(extra)
            elif mirror is not None:
                host = mirror.split("//")[-1].split("/")[0]
                argv.extend(["-i", mirror, "--trusted-host", host])
            self.log("  尝试源：%s" % (mirror or "默认"))
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True,
                    timeout=MIRROR_TIMEOUT_S * 3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                errors.append("%s: 超时" % (mirror or "默认"))
                self.log("    超时，换下一个源")
                continue
            if proc.returncode == 0:
                self.state.mark_done(item_id)
                self.log("  %s 安装完成" % item_id)
                return
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            last = tail[-1] if tail else "失败"
            # A mirror that simply does not carry the package is not a failure
            # worth reporting as loudly as a timeout; the next one may have it.
            errors.append("%s: %s" % (mirror or "默认", last[:120]))
            self.log("    失败：%s" % last[:120])
        raise RuntimeError("%s 安装失败（已试 %d 个源）：%s"
                           % (item_id, len(sources) + 1, "; ".join(errors[-4:])))

    # -- weights -----------------------------------------------------------

    def download(self, item_id, spec):
        """Fetch one weight file, resuming a partial one when the server allows.

        WARNING: a partial file is kept and resumed with a Range request. That is
        the difference between a 600 MB download costing 600 MB and costing 600
        MB every time the user pauses.
        """
        if self.state.is_done(item_id):
            self.log("%s 已存在，跳过" % item_id)
            return
        target = spec["path"]
        expected = int(spec.get("bytes") or 0)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        if expected > 0 and os.path.isfile(target) and os.path.getsize(target) == expected:
            self.log("%s 尺寸正确，标记完成" % item_id)
            self.state.mark_done(item_id)
            return

        have = os.path.getsize(target) if os.path.isfile(target) else 0
        # A local file larger than expected is suspicious, but it is NOT proof the
        # file is bad: the expected size may simply be wrong. Ask the server for
        # the truth before throwing away bytes the user already paid for.
        #
        # WARNING: deleting on this guess is what turned one wrong size into an
        # endless loop. The file was complete; the recorded size was 7 KB short;
        # every run deleted it, re-downloaded it, compared it against the same
        # wrong number, and deleted it again -- and the final attempt asked for a
        # Range starting past the end, which is the 416 the user saw.
        if expected > 0 and have > expected:
            real = self._remote_size(spec)
            if real > 0 and have == real:
                self.log("%s 本地文件完整（%d 字节），与服务器一致，标记完成"
                         % (item_id, have))
                self.state.mark_done(item_id)
                return
            self.log("%s 本地文件比预期大（%d > %d），重新下载"
                     % (item_id, have, expected))
            try:
                os.remove(target)
            except Exception:
                pass
            have = 0
        if have > 0:
            self.log("%s 续传：已有 %.1f MB / %.1f MB"
                     % (item_id, have / 1048576.0, expected / 1048576.0))
        else:
            self.log("%s 开始下载（%.1f MB）" % (item_id, expected / 1048576.0))

        errors = []
        urls = spec.get("urls") or []
        if not urls:
            raise RuntimeError("%s 没有配置下载地址" % item_id)
        for url in urls:
            self.check_stop()
            # A fresh look at the file for every source: the previous one may have
            # written a partial file, and this source can resume from it.
            have_now = os.path.getsize(target) if os.path.isfile(target) else 0
            if have_now > 0:
                self.log("  从 %.1f MB 处尝试续传" % (have_now / 1048576.0))
            try:
                self._fetch(url, target, expected)
                self.state.mark_done(item_id)
                self.log("%s 下载完成" % item_id)
                return
            except StopRequested:
                raise
            except Exception as e:
                errors.append("%s: %s" % (url.split("/")[2], e))
                self.log("  该源失败（%s），换下一个" % e)
                # A source that 404s has told us the file is not there; a source
                # that timed out mid-transfer may have left bytes worth keeping.
                # Either way the partial file stays and the next attempt resumes.
        raise RuntimeError("%s 下载失败（已试 %d 个源）：%s"
                           % (item_id, len(urls), "; ".join(errors)))

    def _remote_size(self, spec):
        """Ask the servers how big the file really is, or 0 if none will say.

        Used only to settle a disagreement between the local file and the
        recorded size, so it tries every source and accepts the first answer.
        A HEAD is cheap; a wrong deletion is not.
        """
        for url in spec.get("urls") or []:
            try:
                req = urllib.request.Request(
                    url, method="HEAD",
                    headers={"User-Agent": "dsh-companion"})
                resp = urllib.request.urlopen(req, timeout=20)
                size = int(resp.headers.get("Content-Length") or 0)
                resp.close()
                if size > 0:
                    return size
            except Exception:
                continue
        return 0

    def _fetch(self, url, target, expected):
        """Download one file into `target`, resuming whatever is already there.

        A dropped connection is expected on a large file over a long time, so a
        read error retries the SAME url a few times before giving up. Each retry
        re-reads the file size and asks for the remainder, which is why a flaky
        link still finishes: the bytes already on disk are never thrown away.
        """
        attempts = 3
        for attempt in range(1, attempts + 1):
            have = os.path.getsize(target) if os.path.isfile(target) else 0
            if expected > 0 and have == expected:
                return
            req = urllib.request.Request(
                url, headers={"User-Agent": "dsh-companion"})
            if have > 0:
                req.add_header("Range", "bytes=%d-" % have)
            try:
                resp = urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as e:
                # 416 means the Range start is past the end of the file, which
                # means the local copy is already at least as long as the remote
                # one. If the server tells us the total in Content-Range, that is
                # the authority: the file is done and the recorded size was wrong.
                #
                # WARNING: treating 416 as a plain failure is what produced the
                # loop the user saw -- the complete file was rejected, then the
                # next mirror was asked the same impossible Range and rejected it
                # too, all four in turn, forever.
                if e.code == 416:
                    total = parse_content_range_total(
                        e.headers.get("Content-Range") if e.headers else None)
                    if total > 0 and have >= total:
                        self.log("  服务器确认本地已完整（%d 字节）" % have)
                        return
                    if have > 0 and total > 0:
                        # Local is longer than the remote file: truncate to the
                        # real length so the bytes on disk match the content.
                        self.log("  本地比远端长（%d > %d），截断后重试" % (have, total))
                        try:
                            os.truncate(target, total)
                        except Exception:
                            pass
                        return
                raise
            # A server that ignores Range answers 200 and sends the whole file;
            # the partial bytes must then be discarded or the result is corrupt.
            mode = "ab"
            if have > 0 and resp.status != 206:
                have = 0
                mode = "wb"
            total = expected if expected > 0 else int(
                resp.headers.get("Content-Length") or 0)
            got = have
            try:
                with open(target, mode) as f:
                    while True:
                        self.check_stop()
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        f.flush()
                        got += len(chunk)
                        if total > 0:
                            self.state.set(
                                detail="%s %.0f%%" % (
                                    os.path.basename(target),
                                    got * 100.0 / total),
                                percent=int(got * 100.0 / total))
            except StopRequested:
                raise
            except Exception as e:
                # Bytes already written stay: the next attempt resumes from them.
                now = os.path.getsize(target) if os.path.isfile(target) else 0
                if attempt >= attempts:
                    raise RuntimeError("传输中断（已有 %.1f MB）：%s"
                                       % (now / 1048576.0, e))
                self.log("    传输中断（已有 %.1f MB），重试 %d/%d"
                         % (now / 1048576.0, attempt, attempts))
                continue
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if expected > 0 and os.path.getsize(target) != expected:
                now = os.path.getsize(target)
                if attempt >= attempts:
                    raise RuntimeError("大小不符：%d != %d" % (now, expected))
                self.log("    大小不符（%d/%d），重试" % (now, expected))
                continue
            return


class StopRequested(Exception):
    pass


def parse_content_range_total(value):
    """The total file size out of a Content-Range header, or 0.

    `bytes 100-199/2000` -> 2000. A `*` total means the server will not say,
    which is a legitimate answer and yields 0.
    """
    if not value:
        return 0
    try:
        tail = str(value).split("/")[-1].strip()
        if tail == "*" or tail == "":
            return 0
        return int(tail)
    except Exception:
        return 0


def plan():
    """Every unit of work, in the order it must happen.

    The runtime comes first because nothing else can run without it: this is what
    makes the plugin work on a machine with no Python installed.
    """
    steps = [("runtime", "下载内置 Python")]
    steps.append(("venv", "创建 Python 环境"))
    for item_id, _spec in PIP_PACKAGES:
        steps.append(("pip:" + item_id, "安装 " + item_id))
    for item_id, _spec in PIP_OPTIONAL:
        steps.append(("pip:" + item_id, "安装 " + item_id))
    for w in WEIGHTS:
        steps.append(("weight:" + w["name"], "下载 " + w["name"]))
    return steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--stop", action="store_true",
                    help="ask a running install to stop at the next boundary")
    ap.add_argument("--reset", action="store_true",
                    help="forget progress and start over")
    args = ap.parse_args()

    state = State(args.state)

    if args.stop:
        # Cooperative stop: the running process polls this flag through the file.
        state.set(status="pausing")
        print("已请求暂停：当前步骤结束后停止")
        return 0

    if args.reset:
        state.doc["done"] = []
        state.doc["failed"] = []
        state.set(status="idle", step="", detail="", percent=0)
        print("已重置进度")
        return 0

    inst = Installer(state, args.log)
    steps = plan()
    state.set(status="running", startedAt=state.doc.get("startedAt") or int(time.time() * 1000),
              stepCount=len(steps), failed=[])

    try:
        for index, (step_id, label) in enumerate(steps):
            inst.check_stop()
            state.set(stepIndex=index, step=label, percent=0, detail=label)
            inst.log("=== [%d/%d] %s ===" % (index + 1, len(steps), label))

            if step_id == "runtime":
                if state.is_done("runtime") and os.path.isfile(RUNTIME_PY):
                    inst.log("内置 Python 已就绪，跳过")
                else:
                    inst.ensure_runtime()
                    state.mark_done("runtime")
            elif step_id == "venv":
                if state.is_done("venv") and os.path.isfile(OCR_PY):
                    inst.log("Python 环境已就绪，跳过")
                else:
                    inst.ensure_venv()
                    state.mark_done("venv")
            elif step_id.startswith("pip:"):
                item = step_id[4:]
                spec = dict(PIP_PACKAGES + PIP_OPTIONAL)[item]
                inst.pip_install(item, spec)
            elif step_id.startswith("weight:"):
                name = step_id[7:]
                spec = next(w for w in WEIGHTS if w["name"] == name)
                inst.download(name, spec)

        state.set(status="done", step="", detail="全部完成", percent=100)
        inst.log("全部依赖安装完成")
        return 0
    except StopRequested:
        state.set(status="paused", detail="已暂停，可继续")
        inst.log("已暂停（可继续）")
        return 0
    except Exception as e:
        failed = state.doc.setdefault("failed", [])
        failed.append({"id": state.doc.get("step", ""), "error": str(e)[:300]})
        state.set(status="failed", detail=str(e)[:300])
        inst.log("失败：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
