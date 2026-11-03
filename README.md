# DSH Doodle Fat Fish

> Screenshot, annotate, and draw on your desktop — all in one plugin.
> Working images are deleted when a task ends; only finished marks stay in history.

[中文readme](https://github.com/egsfggr22222/dsh-doodle-fat-fish/blob/main/%E6%B6%82%E9%B8%A6%E8%82%A5%E9%B1%BC/README.md)

---

## What it is

A DSH plugin that packs three things into one package:

- **Look** — capture the screen, a single window, or the picture the user sent
- **Mark** — find text with OCR, find controls with UI detection, find objects with YOLO, then circle them on the image
- **Draw** — paint directly on the desktop, with the pen following the mouse

Once installed, you can simply tell the agent "circle that button on the screen" and it will capture, locate, annotate, and paste the result back into the conversation.

## Install

### From npm (recommended)

```bash
npm install dsh-doodle-fat-fish
```

Or search for `dsh-doodle-fat-fish` in the DSH plugin panel.

### From GitHub

```bash
npm install github:egsfggr22222/dsh-doodle-fat-fish
```

### Two more steps after installing

**Step one: download the dependencies**

The plugin itself is only about 600 KB of hand-written code. Actually doing the work needs a Python environment and detection models, roughly **2.6 GB** in total, which does not belong inside the package. So:

1. Open **Settings → DSH Doodle Fat Fish → Dependencies**
2. Press **Download**

Downloads **support pause and resume**: an interrupted run continues from the item it stopped on, and finished items are never downloaded again.

**Step two: restart DSH**

The environment needs one restart to take effect.

## Dependency list

| Part | Contents | Size |
|---|---|---|
| Python environment | `ocr_env/` (created automatically) | ~920 MB |
| Object detection | `yoloe-11s-seg.pt` + `mobileclip_blt.ts` | ~600 MB |
| UI detection | `omniparser_icon_detect.pt` | ~39 MB |
| Layout detection | `yolov8s-worldv2.pt` | ~55 MB |
| Optional | GroundingDINO, CLIP | ~1 GB |

Download channels switch automatically by availability: pip goes Tsinghua → Aliyun → official; weights go Hugging Face → hf-mirror.

## Tools

| Tool | Purpose |
|---|---|
| `look` | Screen / window / desktop capture |
| `now-image` | Take the picture the user sent |
| `find` | Find things in a picture (OCR + UI + objects) |
| `mark` | Annotate on an image |
| `finish` | Wrap up: archive the result, clean the working copies |
| `pencil` | Draw on the desktop screen |
| `history` | Browse past results |

## Desktop board gestures

| Action | Effect |
|---|---|
| Left-drag | Draw |
| **Shift + left-click** | Erase |
| **Alt + left-drag** | Select text |
| **Double-tap CapsLock** | Switch the pen between black and white |
| Scroll wheel | Adjust pen width |

A text box has a **transparent backing while the glyphs stay fully opaque** — as if written directly on the desktop.

## Directory structure

```
index.js          Host half: tool registration, routes, board/whale/island management
client.js         Client half: settings panel (history / capture / board / dependencies)
tools-host.js     The find tool implementation
tools/            All Python implementations
  board.py          Desktop board
  island.py         Dynamic island (separate process)
  install_deps.py   Dependency downloader
  mark.py           Annotation rendering
  act.py            Capture in one call
ball/             Whale desk pet (deskpet.py + server.py)
skills/           Skill documents for the agent
```

## Development

```bash
git clone https://github.com/egsfggr22222/dsh-doodle-fat-fish.git
cd dsh-doodle-fat-fish
```

Point at the local directory with `link:` in `~/.dsh/profiles/desktop/package.json`:

```json
"dependencies": {
  "dsh-doodle-fat-fish": "link:C:/path/to/dsh-doodle-fat-fish"
}
```

Then add `"dsh-doodle-fat-fish"` to the `dsh.profile.bundles` array in the same file, and restart DSH.

## Notes

- **The package name must match everywhere**: `package.json`'s `name`, and `cordis.patch.yml`'s `id`/`name`, must all be exactly the same, otherwise DSH reports `icon must remain inside its package directory` and the whole settings panel disappears.
- **`.npmignore` is required**: the `files` field in `package.json` includes `tools/` and `ball/`, and `files` overrides `.gitignore`. Without `.npmignore`, `npm pack` would bundle the 3.7 GB of weights and the environment.

## Third-party code and assets

The desktop whale (`ball/`) comes from other open-source projects:

```
Yifffan/dsh-plugin-whale-pet 0.2.9   (original project)
        ↓ fork
rongzi5/dsh-whale-pet                (the version taken here)
        ↓ modified
DSH Doodle Fat Fish / dsh-doodle-fat-fish   (this project)
```

| Contents | Licence | Requirement |
|---|---|---|
| `ball/deskpet.py`, `ball/server.py` | **MIT** | Keep the copyright notice |
| `ball/whale.ico`, artwork in `ball/dist/` | **CC BY 4.0** | Attribution + licence link + statement of changes |

**Changes made**: the whale code was wired into this plugin's state channel, its single-instance guard was changed to a lock file, and the state directory now arrives as a parameter.
The artwork is unmodified.

See [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md) for the full statement.

> ⚠ **AGPL note**: Ultralytics YOLO, among the optional dependencies, is licensed AGPL-3.0. If that licence does not suit you,
> do not download `yoloe-11s-seg.pt` (OCR and UI detection do not depend on it).

## Licence

The original parts of this project are MIT — see [LICENSE](./LICENSE).
