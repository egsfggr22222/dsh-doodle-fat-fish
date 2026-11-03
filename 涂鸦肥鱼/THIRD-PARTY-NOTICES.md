# 第三方代码与资源声明

本项目包含来自其他开源项目的代码与美术资源。以下逐项声明来源、许可与修改。

---

## ball/deskpet.py、ball/server.py

**来源链路**

```
Yifffan/dsh-plugin-whale-pet 0.2.9   （原始项目）
        ↓ fork
rongzi5/dsh-whale-pet                （本项目直接取用的版本）
        ↓ 修改
DSH 涂鸦肥鱼 / dsh-doodle-fat-fish   （本项目）
```

**许可**：MIT License

**版权归属**：原项目作者 Yifffan，以及 fork 作者 rongzi5。

**本项目所做的修改**

- 接入本插件的状态通道：读取 `status.json` 显示 agent 当前在做什么
- 单实例锁改为 `O_CREAT|O_EXCL` 锁文件（原实现下两个实例会同时启动）
- 启动参数改为 `--state-path-file`，避免中文路径经 ANSI 代码页变成乱码
- 移除对本插件目录之外路径的依赖，所有可执行内容都在插件内部
- 鲸鱼进程改由插件 Host 用 `ocr_env` 的解释器启动（`pythonnet` 只装在那里）

按 MIT 要求，上述版权声明与许可声明随本文件一并保留。

---

## ball/whale.ico 及 ball/dist/ 中的鲸鱼美术资源

**来源**：rongzi5/dsh-whale-pet

**许可**：[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

**署名**：鲸鱼美术资源由 Yifffan 与 rongzi5 的项目提供。

**修改说明**：`ball/dist/` 为重新构建的前端产物，图标资源保持原样未改动。

CC BY 4.0 要求署名、提供协议链接、并说明是否修改——以上三点均在此声明。

---

## 内置运行时依赖（不随仓库分发）

以下内容由面板的「下载依赖」在首次使用时安装，不进入版本库：

| 组件 | 许可 | 备注 |
|---|---|---|
| Python (python-build-standalone) | PSF License | |
| pywebview | BSD-3-Clause | |
| pythonnet | MIT | |
| PaddlePaddle / PaddleOCR | Apache-2.0 | |
| **Ultralytics YOLO** | **AGPL-3.0** | ⚠ 见下方提醒 |
| GroundingDINO | Apache-2.0 | |
| CLIP (OpenAI) | MIT | |
| MobileCLIP | Apple Sample Code License | |

> ⚠ **AGPL 提醒**：Ultralytics YOLO 采用 AGPL-3.0，具有传染性。如果你不能接受该协议，
> 请**不要**在依赖面板下载 `yoloe-11s-seg.pt`，改用其他检测后端（OCR 与 UI 检测不依赖它）。

---

## 依赖许可的两种情况（重要）

许可义务**取决于这个库是怎么进来的**，不能一概而论：

### 情况一：随包分发 → 必须声明

本项目 `package.json` **没有任何 dependencies**。所有第三方代码都在运行时下载，
不进入 npm 包。这是有意的设计：`files` 字段只包含手写代码，`npm pack` 出来
约 1.3 MB，没有一行别人的代码。

**结论：发布到 npm 时，除了鲸鱼那部分（见上文 MIT + CC BY 4.0），没有额外的分发义务。**

### 情况二：运行时由用户自己安装 → 义务在用户与库之间

面板的「下载依赖」会从 PyPI 装这些包。**它们不随本项目分发**，所以本项目的 MIT
许可不覆盖它们，用户安装后各自受自己的许可约束：

| 包 | 许可 | 影响 |
|---|---|---|
| numpy | BSD-3-Clause | 无 |
| pillow | MIT-CMU | 无 |
| opencv-python-headless | Apache-2.0 | 无 |
| paddleocr | Apache-2.0 | 无 |
| paddlepaddle | Apache-2.0 | 无 |
| pywebview | BSD-3-Clause | 无 |
| pythonnet | MIT | 无 |
| bottle | MIT | 无 |
| bottle-websocket | MIT | 无 |
| torch | BSD-3-Clause | 无 |
| **ultralytics** | **AGPL-3.0** | ⚠ 见下 |

> ⚠ **AGPL-3.0 是唯一需要注意的**。它要求：如果你把基于 ultralytics 的程序
> **通过网络提供服务**，也要公开源码。本地桌面使用不受影响。
>
> 想完全避开：在依赖面板下载时**跳过 `ultralytics` 和 `yoloe-11s-seg.pt`**。
> OCR 找字、UI 找控件、截图、画板都不依赖它，只有「YOLO 找物体」这一项会失效。

---

## 模型权重（运行时下载，不随包分发）

模型文件同样是第三方作品，各有各的许可：

| 文件 | 来源 | 许可 | 用途 |
|---|---|---|---|
| `yoloe-11s-seg.pt` | [jameslahm/yoloe](https://huggingface.co/jameslahm/yoloe) | **AGPL-3.0** | 物体检测（可跳过） |
| `omniparser_icon_detect.pt` | [microsoft/OmniParser-v2.0](https://huggingface.co/microsoft/OmniParser-v2.0) | MIT | UI 图标检测 |
| `yolov8s-worldv2.pt` | Ultralytics assets | **AGPL-3.0** | 版面检测（可跳过） |
| `mobileclip_blt.ts` | [apple/MobileCLIP](https://huggingface.co/apple/MobileCLIP) | Apple Sample Code License | 配合 YOLOE 的文本编码器 |
| `ViT-B-32.pt`（可选） | OpenAI CLIP | MIT | 可选 |
| `grounding-dino-tiny`（可选） | [IDEA-Research](https://huggingface.co/IDEA-Research/grounding-dino-tiny) | Apache-2.0 | 可选 |

> **只用 OCR 和 UI 检测的话，两个 AGPL 的模型都可以不下。**
> 依赖面板支持中途停止：看到 `yoloe-11s-seg.pt` / `yolov8s-worldv2.pt` 时点「暂停」即可，
> 已经装好的部分不会重下。

---

## 本项目原创部分

`index.js`、`client.js`、`tools-host.js`、`tools/*.py`、`skills/*` 为本项目原创，
采用 MIT 许可，见 [LICENSE](./LICENSE)。

