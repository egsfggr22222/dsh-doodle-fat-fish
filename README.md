# Whale's Pencil

[中文 README](https://github.com/egsfggr22222/dsh-doodle-fat-fish/blob/main/%E6%B6%82%E9%B8%A6%E8%82%A5%E9%B1%BC/README.md)

A transparent canvas that **covers the entire desktop**. The agent can draw in any colour, and you draw with a pencil picked up from the floating whale — **either side can erase what the other has drawn**, so you can talk to each other by pointing at things.

Nothing is drawn inside the DSH window: the canvas is a full-screen window on the desktop, and DSH sits beneath it.

---

## Four interface elements

| Element | What it is | When it appears |
|---|---|---|
| **Floating whale** | A 52×52 desk pet. Double-click to open the canvas, right-click for the menu | Always on screen, draggable anywhere |
| **Canvas** | A full-screen transparent layer for drawing, writing and erasing | Enter by double-clicking the whale, `Esc` to leave |
| **Dynamic island** | A status capsule at the top centre of the screen | Appears while the agent works, disappears when it finishes |
| **Menus** | 2 items on the whale, 4 on the canvas | Right-click |

## The floating whale

- **Double left-click** → open the canvas (the whale hides)
- **Single left-click** → nothing, which guards against accidents
- **Drag** → move it anywhere; native OS dragging, fully responsive
- **Right-click** → `Summon agent` / `Clear all`

The whale can be dragged to a screen edge and will hang off it, but never further than 16 px — it cannot be dragged out of reach.

> The whale is a **separate window**, not painted on the canvas. The canvas is click-through while idle, so anything painted on it could not be clicked.

## Canvas controls

| Action | Effect |
|---|---|
| **Left-drag** | Draw |
| **Shift + left-click** | Eraser — clears the pixels under the cursor |
| **Alt + left-drag** | Select text (drag to highlight, `Ctrl+C` to copy) |
| **Double-tap CapsLock** | Switch the pen between black and white |
| **Right-click** | Open the menu — **does not erase** |
| **Scroll wheel** | Adjust thickness (1–300 px), with the width shown beside the cursor |
| **Esc** | Leave the canvas; the whale returns |

Canvas menu (4 items):

```
Text box
─────────
Summon agent
Clear all
─────────
Exit drawing
```

**"Text box" opens its input at the right-click position**, not in a fixed corner.

## Text is a copyable container

Text is not a picture — it is a real text container. **Your text and the agent's text behave identically:**

| Action | Effect |
|---|---|
| Leave draw mode, hold `Alt` | Cursor becomes an I-beam over the words |
| Drag | Selects character by character |
| `Ctrl + A` | Select all |
| **`Ctrl + C`** | **Copies to the system clipboard** |

The boxes are read-only, so selection and copying are native behaviour, and typing into them does nothing.

The backing of a committed box is transparent while the glyphs stay fully opaque — words written directly on the desktop, with no frame around them.

## Entering text

Right-click → "Text box" → an input appears where you clicked:

- **Enter** → new line (genuinely multi-line)
- **Click outside the box** → commit
- **Esc** → cancel

On commit it becomes a text label and is written to `user.json` for the agent to read.

## Dynamic island

A translucent capsule at the **top centre of the screen** showing what the agent is doing:

| State | Shows | Dot colour |
|---|---|---|
| Thinking | `Thinking 5s` | Grey-blue |
| Reading | `Reading eye 3s` | Cyan |
| Calling a tool | `Calling pencil 12s` | Green |
| Idle | **Disappears entirely** | — |

**A stop button sits on its right** — clicking it aborts the agent's task and the island vanishes immediately.

The island is a **separate window** so it can be clicked (the canvas itself is click-through).

## Summoning the agent

Right-click the whale (or the canvas menu) → "Summon agent":

1. The window **captures a full-screen screenshot** and delivers it to the model through the attachment service
2. It also writes `summon.json`
3. When the Host reads it, it delivers a prompt into **the session that most recently used the pencil**
4. The agent wakes up with the screenshot attached

The delivery result is written to `summon-ack.json`; success is `{"ok":true}`, failure carries an `error` — nothing is silent.

## Agent drawing

```
pencil { action: "draw",  items: [...] }        // any colour
pencil { action: "erase", ids: ["a3-1"] }       // name precisely
pencil { action: "erase", rects: [{...}] }      // by region
pencil { action: "scene" }                      // see what's on the canvas
pencil { action: "annotate", source: "img.png", items: [...] }
pencil { action: "overlay", overlay: "start" }
```

Primitives: `rect / ellipse / path / arrow / text / image / pixels`. Coordinates default to **desktop physical pixels**, with `(0,0)` at the top-left of the screen; `unit: "window"` uses screen percentages.

**The agent cannot clear everything at once** — wiping the whole canvas would overturn the user's work, and only the user may do that. If the agent wants to remove its own things, it uses `erase` to name them precisely.

## Image annotation

```
pencil { action: "annotate", source: "original.png", items: [{type:"arrow", ...}] }
→ writes Processed image/original-pencil-20261019135723.png
```

The original is untouched; the result is saved separately. All output goes into the `Processed image` folder.

## Why it is designed this way

### Three separate windows

| Window | Purpose | Clicks |
|---|---|---|
| Canvas | Drawing | Only while drawing; click-through otherwise |
| Floating whale | Entry point | Always |
| Dynamic island | Status | Always (stop button) |

**They must be separate.** The canvas is click-through while idle, so a button painted on it could never be clicked.

### File channels

The two processes communicate through files, one writer per file:

| File | Writer | Reader |
|---|---|---|
| `scene.json` | Host (agent) | window |
| `user.json` | window (user) | Host |
| `commands.json` | Host | window |
| `clear.txt` | window | Host |
| `stop.txt` | window (stop button) | Host |
| `status.json` | Host | window (dynamic island) |

**Why the stop button uses a file instead of HTTP**: the DSH web service only admits requests carrying a live session. The board window is a separate process with no session, so an HTTP request comes back 401.

### Heartbeat

The window checks `alive.txt` every second and **closes itself** if it has not been updated for more than 6 seconds — so if the Host crashes, it will not leave an always-on-top transparent window on the desktop.

## Five pitfalls worth remembering

These were all hit during development; each one costs an afternoon if you meet it fresh.

**1. `WS_EX_TRANSPARENT` makes a window unhittable, not merely invisible**

A click-through window cannot be clicked, dragged over, or selected from — at all. Anything that needs the mouse (a text box, a button) must be a **separate window**, and "inert" should be expressed as opacity or a colour key instead.

**2. A colour key and a layered surface are mutually exclusive**

`-transparentcolor` installs a colour key; `UpdateLayeredWindow` installs per-pixel alpha. Both on the same window means one of them silently stops working. Pick one per window.

**3. High-DPI scaling must be converted**

At 125% scaling, drawing directly in physical pixels comes out 25% too large. The window works in its own logical units and converts once at the boundary.

**4. `ctypes` needs explicit `argtypes` for 64-bit handles**

Without them, handles are truncated to 32 bits and become negative. Mask every handle to the low 32 bits before use.

**5. A borderless window cannot be dragged with `DragMove()`**

`DragMove()` needs a caption or a resize frame. A frameless window has neither, so it **fails silently**. Use `ReleaseCapture()` + `SendMessage(WM_SYSCOMMAND, SC_MOVE)` instead.

## Structure

| File | Role |
|---|---|
| `index.js` | Host: scene, `pencil` tool, routing, window lifecycle, dynamic-island state |
| `client.js` | Browser: settings page |
| `tools/board.py` | Desktop window: canvas + floating whale + dynamic island |
| `tools/island.py` | The status capsule, as its own process |
| `ball/deskpet.py` | The whale desk pet |
| `tools/annotate.py` | Image composition |
| `tools/capture.py` | Screen, window and desktop capture |
| `tools/*.py` | Detection: OCR, UI elements, objects, layout |
| `skills/` | The skill documents the agent reads |

State directory: `~/.dsh/whale-pencil/`

## Known limitations

- Only runs on **Windows**.
- Drawing mode catches the mouse across the entire desktop (necessary to draw at all).
- The user's pen has two colours, black and white (double-tap CapsLock); the agent can draw in any colour.
- Multi-monitor setups span the virtual screen, but the coordinate origin is the top-left of the primary monitor.
- Text selection is driven manually by the board (because the canvas owns the mouse). It is character-accurate but not a full text box: no drag-and-drop, no context menu.
- The first run downloads roughly 2.6 GB of runtime and models; see the dependencies page in the settings panel.
