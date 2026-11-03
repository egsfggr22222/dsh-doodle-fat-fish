# DSH 涂鸦肥鱼

> 看图、标注、桌面画板三合一 —— 截图 → 标注 → 收尾。
> 中间图任务结束即删，只有标好的结果留在历史里。

[![npm version](https://img.shields.io/npm/v/dsh-doodle-fat-fish.svg)](https://www.npmjs.com/package/dsh-doodle-fat-fish)
[![license](https://img.shields.io/npm/l/dsh-doodle-fat-fish.svg)](./package.json)

---

## 它是什么

一个 DSH 插件，把三件事装进一个包里：

- **看** —— 截屏、截窗口、取用户发来的图
- **标** —— OCR 找字、UI 找控件、YOLO 找物体，然后在图上圈出来
- **画** —— 在桌面屏幕上直接画，笔迹跟着鼠标走

装好之后，你可以直接对 agent 说「把屏幕上那个按钮圈出来」，它就会截图、定位、标注、把结果贴回对话。

## 安装

### 从 npm 安装（推荐）

```bash
npm install dsh-doodle-fat-fish
```

或者在 DSH 的插件面板里搜索 `dsh-doodle-fat-fish` 安装。

### 从 GitHub 安装

```bash
npm install github:egsfggr22222/dsh-doodle-fat-fish
```

### 装完还要做两件事

**第一步：下载依赖**

插件本身只有约 600 KB 的手写代码。真正干活需要 Python 环境和检测模型，合计约 **2.6 GB**，不适合塞进包里，所以：

1. 打开 **设置 → DSH 涂鸦肥鱼 → 依赖**
2. 点 **下载依赖**

下载**支持暂停和续传**：中断后继续会从停下的那个依赖接着下，已经下好的不会重下。

**第二步：重启 DSH**

环境装完需要重启一次才会生效。

## 依赖清单

| 类别 | 内容 | 体积 |
|---|---|---|
| Python 环境 | `ocr_env/`（自动创建） | ~920 MB |
| 物体检测 | `yoloe-11s-seg.pt` + `mobileclip_blt.ts` | ~600 MB |
| UI 检测 | `omniparser_icon_detect.pt` | ~39 MB |
| 版面检测 | `yolov8s-worldv2.pt` | ~55 MB |
| 可选 | GroundingDINO、CLIP | ~1 GB |

下载渠道按可用性自动切换：pip 走清华源 → 阿里云 → 官方；权重走 Hugging Face → hf-mirror。

## 工具

| 工具 | 作用 |
|---|---|
| `look` | 截屏 / 截窗口 / 截桌面 |
| `now-image` | 取用户发来的图 |
| `find` | 在图上找东西（OCR + UI + 物体） |
| `mark` | 在图片上标注 |
| `finish` | 收尾：归档结果、清理中间图 |
| `pencil` | 在桌面屏幕上画 |
| `history` | 查看历史结果 |

## 桌面画板的手势

| 操作 | 功能 |
|---|---|
| 左键拖 | 画 |
| **Shift + 左键** | 擦除 |
| **Alt + 左键拖** | 选中文字 |
| **双击 CapsLock** | 黑白笔切换 |
| 滚轮 | 调笔宽 |

文字框的**背景是透明的，字本身不透明** —— 就像直接写在桌面上。

## 目录结构

```
index.js          Host 半：工具注册、路由、画板/鲸鱼/灵动岛管理
client.js         前端半：设置面板（历史 / 截图 / 画板 / 依赖）
tools-host.js     find 工具实现
tools/            全部 Python 实现
  board.py          桌面画板
  island.py         灵动岛（独立进程）
  install_deps.py   依赖下载器
  mark.py           标注渲染
  act.py            截图三合一
ball/             鲸鱼桌宠（deskpet.py + server.py）
skills/           给 agent 的技能说明
```

## 开发

```bash
git clone https://github.com/egsfggr22222/dsh-doodle-fat-fish.git
cd dsh-doodle-fat-fish
```

在 `~/.dsh/profiles/desktop/package.json` 里用 `link:` 指向本地目录：

```json
"dependencies": {
  "dsh-doodle-fat-fish": "link:C:/path/to/dsh-doodle-fat-fish"
}
```

再把 `"dsh-doodle-fat-fish"` 加进同一个文件的 `dsh.profile.bundles` 数组，重启 DSH。

## 注意事项

- **包名必须一致**：`package.json` 的 `name`、`cordis.patch.yml` 的 `id`/`name` 三处必须完全相同，否则 DSH 会报 `icon must remain inside its package directory`，整个设置面板会消失。
- **`.npmignore` 是必需的**：`package.json` 的 `files` 字段包含 `tools/` 和 `ball/`，而 `files` 会覆盖 `.gitignore`。没有 `.npmignore` 的话，`npm pack` 会把 3.7 GB 的权重和环境一起打包。

## 第三方代码与资源

桌面鲸鱼（`ball/`）来自其他开源项目：

```
Yifffan/dsh-plugin-whale-pet 0.2.9   （原始项目）
        ↓ fork
rongzi5/dsh-whale-pet                （本项目直接取用的版本）
        ↓ 修改
DSH 涂鸦肥鱼 / dsh-doodle-fat-fish   （本项目）
```

| 内容 | 许可 | 要求 |
|---|---|---|
| `ball/deskpet.py`、`ball/server.py` | **MIT** | 保留版权声明 |
| `ball/whale.ico`、`ball/dist/` 美术资源 | **CC BY 4.0** | 署名 + 协议链接 + 说明修改 |

**修改说明**：鲸鱼代码接入了本插件的状态通道、单实例锁改为锁文件、状态目录改由参数传入。
美术资源未改动。

完整声明见 [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md)。

> ⚠ **AGPL 提醒**：可选依赖中的 Ultralytics YOLO 采用 AGPL-3.0。如果不能接受该协议，
> 请不要下载 `yoloe-11s-seg.pt`（OCR 与 UI 检测不依赖它）。

## 许可证

本项目原创部分采用 MIT，见 [LICENSE](./LICENSE)。

