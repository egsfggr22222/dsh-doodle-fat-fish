/**
 * DSH 涂鸦肥鱼 (DSH Doodle Fat Fish) — 看图、标注、画板三合一。
 *
 * 三件事合成一个插件：
 *   1. 截图（钛合金鱼眼）：给 agent 一双真眼睛，全屏/窗口/桌面都能拍
 *   2. 标注（直觉标注）：把「那个东西在哪」变成一张标好的图
 *   3. 画板（神笔鲸良）：桌面透明画布，用户和你共用一个界面说话
 *
 * 文件纪律（本插件的核心约束）：
 *   - 图片**绝不**落在桌面或用户目录上
 *   - 一次任务的所有中间图放在 <插件目录>/data/work/<taskId>/，任务结束就删
 *   - 只有**标注完成的结果图**进 <插件目录>/data/history/
 *   - 用户发来的图落在 <插件目录>/data/inbox/
 *   - 截图本身不保留历史；直接桌面绘制（pencil）也不保留任何历史
 *
 * @module dsh-companion
 */
import { execFileSync } from 'node:child_process'
import { copyFile, mkdir, open, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { mkdirSync, readFileSync, rmSync, writeFileSync, appendFileSync, chmodSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { homedir, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { zstdDecompressSync } from 'node:zlib'
import { registerFindTool } from './tools-host.js'
// ── 画板（原神笔鲸良）所需的额外导入 ────────────────────────────────────────
// 画板路由与安装标识
const ROUTE = '/api/whale-pencil'
const INSTALL_ID = 'whale-pencil'

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, writeFileSync as writeFileSyncSync } from 'node:fs'
import { rename } from 'node:fs/promises'
import { hostname } from 'node:os'
import { basename } from 'node:path'


const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * The plugin's own Python interpreter.
 *
 * WARNING: everything this plugin runs must live INSIDE this folder. The old
 * code called `powershell.exe` (outside) and `python` from PATH (outside); both
 * made the plugin depend on whatever the machine happened to have installed, and
 * a missing or different tool outside the folder broke the plugin with no way to
 * see why. ocr_env/ ships with the plugin, so the interpreter is part of it.
 */
const BUNDLED_PYTHON = join(HERE, 'ocr_env', 'Scripts', 'python.exe')
const BUNDLED_PYTHONW = join(HERE, 'ocr_env', 'Scripts', 'pythonw.exe')

/** Resolve the interpreter once. Throws if the bundle is missing. */
function pythonExe() {
  if (existsSync(BUNDLED_PYTHON)) return BUNDLED_PYTHON
  throw new Error(`bundled python missing: ${BUNDLED_PYTHON}`)
}

/** Same, but the windowless build for anything that opens a window. */
function pythonwExe() {
  if (existsSync(BUNDLED_PYTHONW)) return BUNDLED_PYTHONW
  return pythonExe()
}

// The board and the image tools all live in tools/ and run on the bundled
// Python. No PowerShell, no system python: everything this plugin executes is
// inside its own folder.
const MARK_TIMEOUT_MS = 30000
const CAPTURE_TIMEOUT_MS = 20000

/** 插件数据根目录。 */
/**
 * 数据根目录：放在插件自己旁边，不是 ~/.dsh。
 *
 * 原因：DSH 的图片预览只认会话工作区内的路径，~/.dsh 下的图在面板里
 * 和对话里都打不开（显示“图片无法预览”）。插件本身就在桌面上，
 * 把产物存在它旁边既能正常预览，也不会把文件洒到桌面根目录。
 */
function rootDir() {
  return join(HERE, 'data')
}

/** 一次任务的工作区：中间图放这里，任务结束整个删掉。 */
function workDir(taskId) {
  return join(rootDir(), 'work', taskId)
}

/** 历史区：只放标注完成的结果图。 */
function historyDir() {
  return join(rootDir(), 'history')
}

/** 列出历史目录里的 PNG，附带尺寸。 */
async function listHistoryEntries(dir) {
  const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
  const files = []
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.png')) continue
    const full = join(dir, entry.name)
    const info = await stat(full).catch(() => null)
    if (info === null) continue
    let dims = { w: 0, h: 0 }
    try {
      dims = pngSize(await readFile(full)) ?? dims
    } catch { /* 读不出来就当 0 */ }
    files.push({ name: entry.name, path: full, width: dims.w, height: dims.h, bytes: info.size })
  }
  files.sort((a, b) => (a.name < b.name ? 1 : -1))
  return files
}

let taskSeq = 0
const aliveTasks = new Set()

/**
 * 每个任务累积的标注。
 *
 * 关键规则（用户定的）：
 *   - 标注不立刻归档，也不删任何东西；都累积在这里
 *   - 后续步骤（差分、放大）一律基于**原图**，不是标注后的图
 *   - 直到 finish 才把累积的标注一次性画到原图上，输出唯一一张结果图
 *
 * 结构：{ source, label, shapes: [...], strokes: 次数 }
 */
const taskMarks = new Map()

function markOf(taskId) {
  if (!taskMarks.has(taskId)) {
    taskMarks.set(taskId, { source: '', label: '', shapes: [], rounds: 0 })
  }
  return taskMarks.get(taskId)
}

/** 开一个任务工作区，返回 id。 */
async function openTask() {
  taskSeq += 1
  const id = `${Date.now()}-${taskSeq}`
  await mkdir(workDir(id), { recursive: true })
  aliveTasks.add(id)
  return id
}

/** 关掉一个任务，删掉它的全部中间图。 */
async function closeTask(taskId) {
  if (!taskId) return
  aliveTasks.delete(taskId)
  await rm(workDir(taskId), { recursive: true, force: true }).catch(() => {})
}

/** 把一张标注完成的图存进历史，返回历史里的路径。 */
async function archiveResult(file, label) {
  await mkdir(historyDir(), { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/gu, '-').slice(0, 19)
  // 文件名保留中文：\w 只匹配 ASCII，中文会被抹成下划线，所以单独放行 CJK。
  const safe = String(label ?? 'result')
    .replace(/[^\w.一-鿿-]+/gu, '_')
    .replace(/^_+|_+$/gu, '')
    .slice(0, 40) || 'result'
  const target = join(historyDir(), `${stamp}_${safe}.png`)
  const bytes = await readFile(file)
  await writeFile(target, bytes)
  return target
}

/**
 * 源图队列：只保留最近 5 张。
 *
 * 标注完不立刻删源图 —— 标错了还能拿它重标。
 * 但也不能无限堆，所以按 FIFO 挤：第 6 张进来时，最旧的那张被删掉。
 */
const SOURCE_KEEP = 5

async function pushSource(file) {
  const dir = join(rootDir(), 'sources')
  await mkdir(dir, { recursive: true })
  const base = basename(file)
  const target = join(dir, `${Date.now()}_${base}`)
  try {
    await copyFile(file, target)
  } catch {
    return { kept: 0, added: '' }
  }

  // 不按扩展名过滤：源图可能没有后缀（例如从会话记录里取出的 blob），
  // 只统计 .png 会让那些文件永远留在队列里、永远不被删除。
  const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
  const names = entries
    .filter((e) => e.isFile())
    .map((e) => e.name)
    .sort()

  while (names.length > SOURCE_KEEP) {
    const oldest = names.shift()
    await rm(join(dir, oldest), { force: true }).catch(() => {})
  }
  return { kept: names.length, added: target }
}

// ── 截图 ────────────────────────────────────────────────────────────────────

/**
 * 跑 tools/capture.py，把目标拍成 PNG 写进任务工作区。
 *
 * 截图不留在桌面，也不进历史：它只是这一次任务的输入。
 */
function runCapture(mode, options = {}) {
  const frame = join(tmpdir(), `companion-capture-${process.pid}-${Date.now()}.png`)
  const args = [
    join(HERE, 'tools', 'capture.py'),
    '--mode', mode,
    '--out', frame,
  ]
  if (options.hwnd !== undefined) args.push('--hwnd', String(options.hwnd))
  if (options.title !== undefined) args.push('--title', String(options.title))

  try {
    const out = execFileSync(pythonExe(), args, {
      encoding: 'utf8',
      windowsHide: true,
      timeout: CAPTURE_TIMEOUT_MS,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const line = out.trim().split(/\r?\n/u).filter((l) => l.trim() !== '').pop() ?? ''
    return { ok: true, ...JSON.parse(line), frame }
  } catch (error) {
    const detail = String(error?.stderr ?? error?.message ?? error).slice(0, 400).trim()
    return { ok: false, reason: 'capture-failed', detail }
  }
}

/** 读 PNG 头拿真实宽高（物理像素）。 */
function pngSize(bytes) {
  try {
    if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50) {
      return { w: bytes.readUInt32BE(16), h: bytes.readUInt32BE(20) }
    }
  } catch { /* 不是 PNG */ }
  return null
}

// ── 标注 ────────────────────────────────────────────────────────────────────

const TAU = Math.PI * 2

function num(value, fallback = NaN) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value)
  return fallback
}

function point(value) {
  if (Array.isArray(value) && value.length >= 2) {
    const x = num(value[0])
    const y = num(value[1])
    return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null
  }
  if (value !== null && typeof value === 'object') {
    const x = num(value.x)
    const y = num(value.y)
    return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null
  }
  return null
}

function ellipsePerimeter(a, b) {
  return Math.PI * (3 * (a + b) - Math.sqrt((3 * a + b) * (a + 3 * b)))
}

function semiMajorFromFoci(c, perimeter) {
  if (!(perimeter > 0)) return null
  const floor = c <= 0 ? 0 : 4 * c
  if (perimeter <= floor) return null
  let lo = Math.max(c, 1e-6)
  let hi = Math.max(perimeter / TAU, lo) * 2 + c
  while (ellipsePerimeter(hi, Math.sqrt(Math.max(hi * hi - c * c, 0))) < perimeter && hi < 1e9) hi *= 2
  for (let i = 0; i < 200; i++) {
    const mid = (lo + hi) / 2
    const b = Math.sqrt(Math.max(mid * mid - c * c, 0))
    if (ellipsePerimeter(mid, b) < perimeter) lo = mid
    else hi = mid
    if (hi - lo < 1e-9) break
  }
  return (lo + hi) / 2
}

function normalizeShape(raw) {
  if (raw === null || typeof raw !== 'object') return { error: 'shape must be an object' }
  const kind = String(raw.kind ?? raw.type ?? '').trim().toLowerCase()

  const common = {}
  for (const key of ['stroke', 'fill', 'dash', 'label']) {
    if (typeof raw[key] === 'string' && raw[key] !== '') common[key] = raw[key]
  }
  const strokeWidth = num(raw.strokeWidth ?? raw.width)
  if (Number.isFinite(strokeWidth) && strokeWidth > 0) common.strokeWidth = strokeWidth
  const radius = num(raw.radius ?? raw.cornerRadius)
  if (Number.isFinite(radius) && radius > 0) common.radius = radius

  if (kind === 'box' || kind === 'rect' || kind === 'rectangle' || kind === '框') {
    let xs = []
    let ys = []
    const p1 = point(raw.p1 ?? raw.from ?? raw.start)
    const p2 = point(raw.p2 ?? raw.to ?? raw.end)
    if (Array.isArray(raw.points) && raw.points.length > 0) {
      for (const entry of raw.points) {
        const p = point(entry)
        if (p === null) return { error: 'box.points must hold [x,y] pairs' }
        xs.push(p.x)
        ys.push(p.y)
      }
    } else if (Array.isArray(raw.corners) && raw.corners.length > 0) {
      for (const entry of raw.corners) {
        const p = point(entry)
        if (p === null) return { error: 'box.corners must hold [x,y] pairs' }
        xs.push(p.x)
        ys.push(p.y)
      }
    } else if (p1 !== null && p2 !== null) {
      xs = [p1.x, p2.x]
      ys = [p1.y, p2.y]
    } else {
      const x = num(raw.x)
      const y = num(raw.y)
      const w = num(raw.w ?? raw.widthSize)
      const h = num(raw.h ?? raw.heightSize)
      if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(w) && Number.isFinite(h)) {
        xs = [x, x + w]
        ys = [y, y + h]
      } else {
        const x1 = num(raw.x1 ?? raw.left)
        const y1 = num(raw.y1 ?? raw.top)
        const x2 = num(raw.x2 ?? raw.right)
        const y2 = num(raw.y2 ?? raw.bottom)
        if (![x1, y1, x2, y2].every(Number.isFinite)) {
          return { error: 'box needs x,y,w,h — or two corners, or a point list' }
        }
        xs = [x1, x2]
        ys = [y1, y2]
      }
    }
    const left = Math.min(...xs)
    const top = Math.min(...ys)
    const w = Math.max(...xs) - left
    const h = Math.max(...ys) - top
    if (!(w > 0) || !(h > 0)) return { error: 'box has no area' }
    return { type: 'rect', x: left, y: top, w, h, ...common }
  }

  if (kind === 'circle' || kind === '圆') {
    const center = point(raw.center) ?? (Number.isFinite(num(raw.cx)) && Number.isFinite(num(raw.cy))
      ? { x: num(raw.cx), y: num(raw.cy) }
      : null)
    const through = raw.through ?? raw.diameterPoints ?? raw.ends
    let r = num(raw.r ?? raw.radiusSize)
    let cx = center?.x
    let cy = center?.y

    if (Array.isArray(through) && through.length === 2) {
      const a = point(through[0])
      const b = point(through[1])
      if (a === null || b === null) return { error: 'circle.through must hold two [x,y] points' }
      cx = (a.x + b.x) / 2
      cy = (a.y + b.y) / 2
      r = Math.hypot(b.x - a.x, b.y - a.y) / 2
    }
    if (!Number.isFinite(r)) {
      const diameter = num(raw.diameter)
      if (Number.isFinite(diameter) && diameter > 0) r = diameter / 2
    }
    if (!Number.isFinite(r)) {
      const perimeter = num(raw.circumference ?? raw.perimeter ?? raw.周长)
      if (Number.isFinite(perimeter) && perimeter > 0) r = perimeter / TAU
    }
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) return { error: 'circle needs a centre: center:[x,y] or cx,cy' }
    if (!Number.isFinite(r) || r <= 0) return { error: 'circle needs r, diameter, or circumference' }
    return { type: 'circle', cx, cy, r, ...common }
  }

  if (kind === 'ellipse' || kind === '椭圆') {
    let cx = num(raw.cx)
    let cy = num(raw.cy)
    const center = point(raw.center)
    if (center !== null) {
      cx = center.x
      cy = center.y
    }
    let a = num(raw.a ?? raw.rx ?? raw.semiMajor)
    let b = num(raw.b ?? raw.ry ?? raw.semiMinor)
    let angle = num(raw.angle, 0)

    const foci = raw.foci ?? raw.focus ?? raw.focusPoints
    if (Array.isArray(foci) && foci.length === 2) {
      const f1 = point(foci[0])
      const f2 = point(foci[1])
      if (f1 === null || f2 === null) return { error: 'ellipse.foci must hold two [x,y] points' }
      cx = (f1.x + f2.x) / 2
      cy = (f1.y + f2.y) / 2
      const c = Math.hypot(f2.x - f1.x, f2.y - f1.y) / 2
      angle = (Math.atan2(f2.y - f1.y, f2.x - f1.x) * 180) / Math.PI
      const perimeter = num(raw.circumference ?? raw.perimeter ?? raw.周长)
      if (Number.isFinite(perimeter) && perimeter > 0) {
        const major = semiMajorFromFoci(c, perimeter)
        if (major === null) {
          return {
            error: `ellipse perimeter ${perimeter} is too short for foci ${(2 * c).toFixed(2)} apart `
              + `(the shortest such ellipse is ${(4 * c).toFixed(2)})`,
          }
        }
        a = major
        b = Math.sqrt(Math.max(major * major - c * c, 0))
      } else if (!Number.isFinite(a) || a <= 0) {
        return { error: 'ellipse from foci needs a circumference (or an explicit a)' }
      } else if (!Number.isFinite(b) || b <= 0) {
        b = Math.sqrt(Math.max(a * a - c * c, 0))
      }
    }

    if (!Number.isFinite(cx) || !Number.isFinite(cy)) return { error: 'ellipse needs a centre, or two foci' }
    if (!Number.isFinite(a) || a <= 0) return { error: 'ellipse needs a semi-major axis a' }
    if (!Number.isFinite(b) || b <= 0) {
      const perimeter = num(raw.circumference ?? raw.perimeter ?? raw.周长)
      if (!Number.isFinite(perimeter) || perimeter <= 0) return { error: 'ellipse needs a semi-minor axis b' }
      let lo = 0
      let hi = perimeter / TAU
      while (ellipsePerimeter(a, hi) < perimeter && hi < 1e9) hi *= 2
      for (let i = 0; i < 200; i++) {
        const mid = (lo + hi) / 2
        if (ellipsePerimeter(a, mid) < perimeter) lo = mid
        else hi = mid
        if (hi - lo < 1e-9) break
      }
      b = (lo + hi) / 2
    }
    if (!Number.isFinite(b) || b <= 0) return { error: 'ellipse has no area' }
    return { type: 'ellipse', cx, cy, a, b, angle, ...common }
  }

  if (kind === 'text' || kind === '文字') {
    const text = String(raw.text ?? '')
    if (text === '') return { error: 'text needs text' }
    const p = point(raw.at ?? raw.position) ?? { x: num(raw.x, 0), y: num(raw.y, 0) }
    const size = num(raw.size)
    const record = { type: 'text', x: p.x, y: p.y, text }
    if (Number.isFinite(size) && size > 0) record.size = size
    if (typeof raw.color === 'string' && raw.color !== '') record.stroke = raw.color
    return record
  }

  if (kind === 'dot' || kind === '点') {
    const p = point(raw.at ?? raw.position) ?? { x: num(raw.x), y: num(raw.y) }
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return { error: 'dot needs a position' }
    return { type: 'circle', cx: p.x, cy: p.y, r: num(raw.r, 4), fill: raw.fill ?? raw.stroke ?? '#ff3860', stroke: raw.stroke ?? '#ff3860' }
  }

  return { error: `unknown shape kind ${JSON.stringify(kind)}` }
}

function runMark(spec) {
  const specFile = join(tmpdir(), `companion-mark-${process.pid}-${Date.now()}.json`)
  mkdirSync(tmpdir(), { recursive: true })
  writeFileSync(specFile, JSON.stringify(spec), 'utf8')
  try {
    const out = execFileSync(pythonExe(), [
      join(HERE, 'tools', 'mark.py'), '--spec', specFile,
    ], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: MARK_TIMEOUT_MS,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const line = out.trim().split(/\r?\n/u).filter((l) => l.trim() !== '').pop() ?? ''
    return JSON.parse(line)
  } catch (error) {
    const detail = String(error?.stderr ?? error?.message ?? error).slice(0, 400).trim()
    return { ok: false, reason: 'render-failed', detail }
  } finally {
    rmSync(specFile, { force: true })
  }
}

// ── 工具：look（截图 + 一次任务） ───────────────────────────────────────────

const LOOK_DESCRIPTION = [
  'DSH 涂鸦肥鱼 — 看 (look): capture the screen, one window, or the desktop as a real image.',
  '',
  'Every look opens a task workspace under <插件目录>/data/work/: intermediate pictures',
  'live there and are deleted when you call finish. Nothing is written to the Desktop.',
  '',
  'Targets:',
  '  screen    what a person sees: the topmost pixels, so a covering window hides what is beneath',
  '  desktop   the wallpaper and desktop icons, visible even where windows cover the desktop',
  '  window    one visible window\'s full content, painted by the window itself',
  '  list      every visible window with its exact bounds; use it to pick a target',
  '  info      the real screen size and origin',
  '',
  'The result reports `path` (inside the task workspace), `width`, `height` and `task`.',
  'Pass that path to `mark` to annotate it; call `finish` when the whole job is done.',
].join('\n')

function registerLookTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'look',
    description: LOOK_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        action: {
          type: 'string',
          enum: ['screen', 'desktop', 'window', 'list', 'info'],
          description: 'Which capture to take, or list the windows to pick from.',
        },
        hwnd: { type: 'integer', description: 'For action window: the window handle from `list`.' },
        title: { type: 'string', description: 'For action window: the window title (exact match wins, substring works when unique).' },
        task: { type: 'string', description: 'Continue an existing task workspace instead of opening a new one.' },
      },
      required: ['action'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          task: { type: 'string' },
          path: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          screen: { type: 'object', additionalProperties: true },
          reason: { type: 'string' },
          detail: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        if (value.ok === false) {
          return [{ type: 'text', text: `look: ${value.reason}${value.detail ? ` — ${value.detail}` : ''}` }]
        }
        // list / info 不产图：回报窗口清单与屏幕尺寸。
        if (value.path === undefined) {
          const windows = Array.isArray(value.windows) ? value.windows : []
          if (windows.length > 0) {
            const lines = windows.slice(0, 20).map((w) => {
              const title = String(w?.title ?? '(untitled)')
              const box = `${w?.x ?? 0},${w?.y ?? 0} ${w?.w ?? 0}x${w?.h ?? 0}`
              const mark = w?.foreground === true ? ' *' : ''
              return `  #${w?.hwnd ?? '?'}  ${box}  ${title}${mark}`
            })
            return [{
              type: 'text',
              text: `look: ${windows.length} window(s), screen ${value.screen?.w ?? '?'}x${value.screen?.h ?? '?'}\n`
                + lines.join('\n'),
            }]
          }
          const screen = value.screen ?? {}
          return [{
            type: 'text',
            text: `look: screen ${screen.w ?? value.width ?? '?'}x${screen.h ?? value.height ?? '?'}`,
          }]
        }
        return [{
          type: 'text',
          text: `look: ${value.width}x${value.height} at ${value.path} (task ${value.task}). `
            + 'Open it with read_image, or pass it to mark.',
        }]
      },
    },
    async execute(args) {
      const action = String(args?.action ?? '')
      const taskId = typeof args?.task === 'string' && args.task.trim() !== ''
        ? args.task.trim()
        : await openTask()

      if (action === 'list' || action === 'info') {
        const result = runCapture(action, {})
        if (result.ok !== true) {
          throw new Error(`look: ${result.reason}${result.detail ? ` — ${result.detail}` : ''}`)
        }
        if (result.frame) await rm(result.frame, { force: true }).catch(() => {})
        const payload = { ok: true, task: taskId }
        if (result.width !== undefined) payload.width = result.width
        if (result.height !== undefined) payload.height = result.height
        if (result.screen !== undefined) payload.screen = result.screen
        if (result.windows !== undefined) payload.windows = result.windows
        return payload
      }

      const result = runCapture(action, { hwnd: args?.hwnd, title: args?.title })
      if (result.ok !== true) {
        throw new Error(`look: ${result.reason}${result.detail ? ` — ${result.detail}` : ''}`)
      }

      const bytes = await readFile(result.frame)
      await rm(result.frame, { force: true }).catch(() => {})
      const size = pngSize(bytes)
      if (size === null) throw new Error('look: the capture engine wrote something that is not a PNG')

      const target = join(workDir(taskId), `${action}-${Date.now()}.png`)
      await writeFile(target, bytes)

      const payload = { ok: true, task: taskId, path: target, width: size.w, height: size.h }
      if (result.screen !== undefined) payload.screen = result.screen
      return payload
    },
  }), 'dsh-companion: look tool')
}

// ── 用户发来的图（now-image / image-save） ─────────────────────────────────
//
// 用户贴进对话的图片有确定路径，不需要去缓存目录里靠特征猜：
// 会话记录里有 `attachmentId = sha256:<64 hex>`，附件仓按「前 2 位当目录、
// 其余当文件名」存放。读会话记录拿 id，再拼路径，一步到位。

/** DSH 家目录。 */
function dshHome() {
  const explicit = String(process.env.DSH_HOME ?? '').trim()
  return explicit !== '' ? explicit : join(homedir(), '.dsh')
}

function sessionsRoot() {
  return join(dshHome(), 'sessions')
}

function objectsRoot() {
  return join(dshHome(), 'attachments', 'v1', 'objects')
}

/** 用户发来的图落到这里；finish 会清空它。 */
function inboxDir() {
  return join(rootDir(), 'inbox')
}

/** `sha256:<hex>` -> 附件仓里的绝对路径；格式不对返回空串。 */
function objectPath(attachmentId) {
  const hex = String(attachmentId ?? '').split(':').pop().trim().toLowerCase()
  if (!/^[0-9a-f]{64}$/u.test(hex)) return ''
  return join(objectsRoot(), hex.slice(0, 2), hex)
}

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

/**
 * 解压会话文件。
 *
 * 会话文件是**多帧** zstd 拼接（每写一段就是新的一帧），
 * zstdDecompressSync 直接吃整个文件只会解出第一帧，后面几百帧全丢。
 * 所以先按魔数切帧，再逐帧解。魔数在压缩数据里极少出现，真撞上了
 * 就合并到下一帧重试，而不是把整份记录判为坏文件。
 */
function readSessionText(buf) {
  if (buf.length === 0) return ''
  if (buf.indexOf(ZSTD_MAGIC) !== 0) return buf.toString('utf8')

  const offsets = []
  let at = 0
  while (at < buf.length) {
    const found = buf.indexOf(ZSTD_MAGIC, at)
    if (found < 0) break
    offsets.push(found)
    at = found + ZSTD_MAGIC.length
  }

  let text = ''
  let start = 0
  while (start < offsets.length) {
    let end = start + 1
    let decoded = null
    while (end <= offsets.length) {
      const from = offsets[start]
      const to = end < offsets.length ? offsets[end] : buf.length
      try {
        decoded = zstdDecompressSync(buf.subarray(from, to)).toString('utf8')
        break
      } catch {
        end += 1
      }
    }
    if (decoded === null) break
    text += decoded
    start = end
  }
  return text
}

/** 所有会话文件，最新的排前面。 */
async function sessionFiles() {
  const root = sessionsRoot()
  const out = []
  const workspaces = await readdir(root, { withFileTypes: true }).catch(() => [])
  for (const workspace of workspaces) {
    if (!workspace.isDirectory()) continue
    const wdir = join(root, workspace.name)
    const sessions = await readdir(wdir, { withFileTypes: true }).catch(() => [])
    for (const session of sessions) {
      if (!session.isDirectory()) continue
      const file = join(wdir, session.name, 'session.v4.jsonl.zstd')
      const info = await stat(file).catch(() => null)
      if (info === null || !info.isFile()) continue
      out.push({ sessionId: session.name, workspace: workspace.name, path: file, mtimeMs: info.mtimeMs })
    }
  }
  out.sort((a, b) => b.mtimeMs - a.mtimeMs)
  return out
}

/**
 * 选一个会话。
 *
 * 留空时优先用当前会话（插件跑在 DSH 里，环境里有 DSH_SESSION_ID），
 * 拿不到才退到「最近修改的那个」——「最近修改」在多会话并行时不一定对。
 */
async function pickSession(session) {
  const all = await sessionFiles()
  if (all.length === 0) throw new Error(`找不到任何会话文件：${sessionsRoot()}`)
  const needle = String(session ?? '').trim()
  if (needle === '') {
    const current = String(process.env.DSH_SESSION_ID ?? '').trim()
    if (current !== '') {
      const hit = all.find((item) => item.sessionId === current)
      if (hit !== undefined) return hit
    }
    return all[0]
  }
  const hit = all.find((item) => item.sessionId.includes(needle) || item.path.includes(needle))
  if (hit === undefined) throw new Error(`没有匹配这个字符串的会话：${needle}`)
  return hit
}

/** 会话里用户发来的全部图片，按发来的先后排序。 */
async function imagesInSession(session) {
  const info = await pickSession(session)
  const text = readSessionText(await readFile(info.path))
  const images = []
  for (const line of text.split(/\r?\n/u)) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) continue
    let event
    try { event = JSON.parse(trimmed) } catch { continue }
    if (event?.type !== 'user/message') continue
    const content = event?.data?.content
    if (!Array.isArray(content)) continue
    for (const item of content) {
      if (item?.type !== 'image') continue
      const attachment = item.attachment ?? {}
      const attachmentId = String(attachment.attachmentId ?? '')
      images.push({
        seq: event.seq,
        time: event.time,
        name: String(attachment.name ?? ''),
        mediaType: String(attachment.mediaType ?? ''),
        width: attachment.width,
        height: attachment.height,
        bytes: attachment.bytes,
        attachmentId,
        path: objectPath(attachmentId),
      })
    }
  }
  images.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  return { session: info, images }
}

/**
 * 把用户发来的某张图落到 data/inbox/，返回可直接交给 mark 的路径。
 *
 * index：-1 最近一张（默认）；0 第一张；正数按 1 起。
 */
async function saveUserImage(args) {
  const data = await imagesInSession(args?.session)
  const list = data.images
  if (list.length === 0) {
    throw new Error(`这个会话里没有用户发来的图片：${data.session.sessionId}`)
  }

  const raw = args?.index
  const index = Number.isInteger(raw) ? raw : -1
  let picked
  if (index === -1) {
    picked = list[list.length - 1]
  } else if (index === 0) {
    picked = list[0]
  } else if (index > 0) {
    if (index > list.length) {
      throw new Error(`一共只有 ${list.length} 张图片，取不到第 ${index} 张`)
    }
    picked = list[index - 1]
  } else {
    throw new Error('index 只能是 -1（最近一张）、0（第一张）或从 1 开始的正数')
  }

  if (picked.path === '') {
    throw new Error(`图片的 attachmentId 不是合法哈希：${picked.attachmentId}`)
  }
  const source = await stat(picked.path).catch(() => null)
  if (source === null || !source.isFile()) {
    throw new Error(`附件仓里没有这张图（可能已被清理）：${picked.path}`)
  }

  const stamp = new Date().toISOString().replace(/[:.]/gu, '-').slice(0, 19)
  let target = String(args?.output ?? '').trim()
  if (target === '') target = join(inboxDir(), `user-image-${stamp}-${index}.png`)
  await mkdir(dirname(target), { recursive: true })

  if (existsSync(target) && args?.overwrite !== true) {
    const dot = target.lastIndexOf('.')
    const stem = dot > 0 ? target.slice(0, dot) : target
    const ext = dot > 0 ? target.slice(dot) : ''
    let counter = 2
    while (existsSync(`${stem}-${counter}${ext}`)) counter += 1
    target = `${stem}-${counter}${ext}`
  }

  // WARNING: DSH marks its attachment originals READ-ONLY, and copyFile over an
  // existing read-only target fails with EPERM even when the caller asked for
  // overwrite (the flag only bypasses the exists-check above, never the
  // attribute). Clear the bit before copying whenever overwriting is intended.
  if (args?.overwrite === true && existsSync(target)) {
    try { chmodSync(target, 0o666) } catch { /* not read-only, or gone */ }
  }
  await copyFile(picked.path, target)
  const bytes = await readFile(target)
  const size = pngSize(bytes)

  return {
    ok: true,
    path: target,
    name: picked.name !== '' ? picked.name : basename(target),
    width: size?.w ?? picked.width,
    height: size?.h ?? picked.height,
    bytes: bytes.length,
    mediaType: picked.mediaType,
    sessionId: data.session.sessionId,
    seq: picked.seq,
    index,
    total: list.length,
  }
}

const NOW_IMAGE_DESCRIPTION = [
  "DSH Companion — now-image: the picture the user just sent, as a real file path.",
  '',
  'Use this as step 0 whenever the user pastes a picture and asks something about',
  'it ("mark X", "where is Y"). It reads the session record, takes the',
  'attachment id, and copies the original into the companion inbox. Nothing is',
  'guessed from cache filenames or image dimensions.',
  '',
  'Returns `path` — pass that straight to `find` / `mark` as `image`, and every',
  'coordinate you give afterwards is that picture\'s own pixels.',
  '',
  'Arguments:',
  '  session  a session id or path fragment; empty uses the current session',
  '  index    -1 the newest picture (default), 0 the first, or 1-based',
  '  task     reuse an existing task workspace instead of opening a new one',
  '',
  'This is the same lookup as `image-save`; `image-save` just also takes an',
  'explicit `output` path.',
].join('\n')

function registerNowImageTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'now-image',
    description: NOW_IMAGE_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        session: { type: 'string', description: 'Session id or path fragment; empty uses the current session.' },
        index: {
          type: 'integer',
          description: '-1 the newest picture (default), 0 the first, or 1-based position.',
        },
        task: { type: 'string', description: 'Reuse an existing task workspace.' },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          task: { type: 'string' },
          path: { type: 'string' },
          name: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          bytes: { type: 'integer' },
          sessionId: { type: 'string' },
          seq: { type: 'integer' },
          index: { type: 'integer' },
          total: { type: 'integer' },
          reason: { type: 'string' },
          detail: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        if (value.ok === false) {
          return [{ type: 'text', text: `now-image: ${value.reason}${value.detail ? ` — ${value.detail}` : ''}` }]
        }
        return [{
          type: 'text',
          text: `now-image: ${value.width}x${value.height} at ${value.path}`
            + `(会话里第 ${value.index === -1 ? value.total : value.index}/${value.total} 张, task ${value.task}).`
            + ' Pass this path to find / mark as `image`.'
            + ' ⚠ This inbox path dies at `finish` — a LATER turn must call now-image again (or use data/sources/), never reuse an old path.',
        }]
      },
    },
    async execute(args) {
      const taskId = typeof args?.task === 'string' && args.task.trim() !== ''
        ? args.task.trim()
        : await openTask()
      const saved = await saveUserImage(args)
      return { ...saved, task: taskId }
    },
  }), 'dsh-companion: now-image tool')
}

const IMAGE_SAVE_DESCRIPTION = [
  "DSH Companion — image-save: same lookup as now-image, but you choose where the",
  'file lands.',
  '',
  'Default output is the companion inbox; pass `output` to write somewhere else.',
  'The returned `path` is what `mark` wants as `image`.',
].join('\n')

function registerImageSaveTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'image-save',
    description: IMAGE_SAVE_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        session: { type: 'string', description: 'Session id or path fragment; empty uses the current session.' },
        index: {
          type: 'integer',
          description: '-1 the newest picture (default), 0 the first, or 1-based position.',
        },
        output: { type: 'string', description: 'Target file path; empty writes into the companion inbox.' },
        overwrite: { type: 'boolean', description: 'Overwrite the target when it already exists.' },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          path: { type: 'string' },
          name: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          bytes: { type: 'integer' },
          sessionId: { type: 'string' },
          seq: { type: 'integer' },
          index: { type: 'integer' },
          total: { type: 'integer' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        return [{
          type: 'text',
          text: `image-save: ${value.width}x${value.height} written to ${value.path}`,
        }]
      },
    },
    async execute(args) {
      return saveUserImage(args)
    },
  }), 'dsh-companion: image-save tool')
}

// ── 工具：mark（标注 + 归档历史） ──────────────────────────────────────────

const MARK_DESCRIPTION = [
  '适用范围（先读这段再决定用不用本工具）：',
  '',
  '本工具画在【图片文件】上 —— 产出一张新的 PNG 图片文件。',
  '',
  '✅ 用 mark 的场合：',
  '  * 用户发来一张图片，要求标注 / 找东西 / 圈出来（产物是标好的图）',
  '  * 配合 find / look 的结果在截图上标注',
  '  * 任何「交付物是一张图片」的标注任务',
  '',
  '❌ 不要用 mark 的场合：',
  '  * 用户要求在【桌面屏幕上】画 → 用 pencil（字段是 items+type，不是 shapes+kind）',
  '',
  '⚠ 两个绘图工具字段不同，禁止混用：mark 用 shapes+kind，pencil 用 items+type。',
  '',
  'DSH 涂鸦肥鱼 — 标 (mark): send coordinates, get a marked picture.',
  '',
  'This tool only draws. It does not look for anything: locating the target is',
  'the job of `find` (scan / blocks / grid / zoom), which hands you exact',
  'coordinates. Mark those.',
  '',
  'Workflow: find first, then mark. Read one picture to decide WHICH match is',
  'the target you want; take the coordinates themselves from `find`.',
  '',
  'Send `shapes`, each with a `kind`:',
  '  box      points:[[x,y],...] (its bounding box), or x,y,w,h, or x1,y1,x2,y2',
  '  circle   center:[x,y] with r, diameter, or circumference',
  '  ellipse  foci:[[x,y],[x,y]] with circumference',
  '  text     x, y, text, size, color',
  '  dot      x, y, r',
  '',
  'Pass `image` (a path from `look`, or any picture) and the coordinates are that',
  "picture's own pixels.",
  '',
  'Shapes accumulate: call mark repeatedly, then call `finish` once to render the',
  'single final picture. `mark { reset: true }` throws the accumulated shapes away.',
  'Nothing is archived until finish runs.',
].join('\n')

function registerMarkTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'mark',
    description: MARK_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        shapes: {
          type: 'array',
          description: 'What to draw, in drawing order.',
          items: {
            type: 'object',
            additionalProperties: true,
            properties: {
              kind: { type: 'string', enum: ['box', 'circle', 'ellipse', 'text', 'dot'] },
              x: { type: 'number' },
              y: { type: 'number' },
              w: { type: 'number' },
              h: { type: 'number' },
              x1: { type: 'number' },
              y1: { type: 'number' },
              x2: { type: 'number' },
              y2: { type: 'number' },
              points: { type: 'array', items: { type: 'array', items: { type: 'number' } } },
              center: { type: 'array', items: { type: 'number' } },
              cx: { type: 'number' },
              cy: { type: 'number' },
              r: { type: 'number' },
              diameter: { type: 'number' },
              circumference: { type: 'number' },
              foci: { type: 'array', items: { type: 'array', items: { type: 'number' } } },
              a: { type: 'number' },
              b: { type: 'number' },
              angle: { type: 'number' },
              through: { type: 'array', items: { type: 'array', items: { type: 'number' } } },
              text: { type: 'string' },
              size: { type: 'number' },
              color: { type: 'string' },
              stroke: { type: 'string' },
              strokeWidth: { type: 'number' },
              fill: { type: 'string' },
              dash: { type: 'string', enum: ['solid', 'dash', 'dot'] },
              radius: { type: 'number' },
              label: { type: 'string' },
            },
            required: ['kind'],
          },
        },
        image: { type: 'string', description: 'Picture to draw on.' },
        blockIds: {
          type: 'array',
          items: { type: 'integer' },
          description: '差分块号，来自 find { method: "blocks" } 的块表。'
            + '程序按块号读 work/<task>/find/blocks.json 取出坐标，直接转成 box。'
            + '可以和 shapes 同时传：块已框住的用 blockIds，块没框住或框不准的用 shapes 手写。',
        },
        task: { type: 'string', description: 'Task workspace this annotation belongs to.' },
        label: { type: 'string', description: 'Short name used in the archived filename.' },
        reset: {
          type: 'boolean',
          description: '放弃这个任务已累积的全部标注，回到原图重新开始。'
            + '一种方法标错了就 reset，再换下一种方法 —— 失败的标注不会被画出来。',
        },
        color: { type: 'string', description: 'Default outline colour. Defaults to #00e0ff.' },
      },
      required: ['shapes'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          path: { type: 'string' },
          archived: { type: 'string' },
          drawn: { type: 'integer' },
          skipped: { type: 'array', items: { type: 'string' } },
          reason: { type: 'string' },
          detail: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        if (value.ok === false) {
          return [{ type: 'text', text: `mark: ${value.reason}${value.detail ? ` — ${value.detail}` : ''}` }]
        }
        if (value.reset === true) {
          return [{
            type: 'text',
            text: `mark: 已清空 ${value.dropped} 个标注，回到原图重新开始。`,
          }]
        }
        const skipped = Array.isArray(value.skipped) && value.skipped.length > 0
          ? ` (跳过 ${value.skipped.length})`
          : ''
        const fromBlocks = Array.isArray(value.blockIds) && value.blockIds.length > 0
          ? `，其中 ${value.blockIds.length} 个取自差分块 ${value.blockIds.join(',')}`
          : ''
        return [{
          type: 'text',
          text: `mark: 记下 ${value.added} 个${fromBlocks}${skipped}，累计 ${value.total} 个（第 ${value.rounds} 轮）。`
            + '标完再调 finish 出图；标错了用 reset 丢弃重来。',
        }]
      },
    },
    async execute(args) {
      const taskIdEarly = typeof args?.task === 'string' && args.task.trim() !== ''
        ? args.task.trim()
        : 'loose'

      // reset：放弃这一轮的全部标注，回到原图重来。
      // 用错了方法（三合一没标对、差分没标对）就该 reset 再换下一招，
      // 不能把失败的标注累积到最后一起画出来。
      if (args?.reset === true) {
        const dropped = taskMarks.get(taskIdEarly)
        taskMarks.delete(taskIdEarly)
        return {
          ok: true,
          task: taskIdEarly,
          reset: true,
          dropped: dropped === undefined ? 0 : dropped.shapes.length,
          note: '已清空这一轮的标注，回到原图重新开始。',
        }
      }

      const incoming = Array.isArray(args?.shapes) ? args.shapes : []
      const shapes = []
      const rejected = []
      for (const raw of incoming) {
        const record = normalizeShape(raw)
        if (record.error !== undefined) rejected.push(record.error)
        else shapes.push(record)
      }

      const taskId = typeof args?.task === 'string' && args.task.trim() !== ''
        ? args.task.trim()
        : 'loose'

      // 差分块号 -> box。块表是 find { method: "blocks" } 落盘的，
      // 调用方只报块号，坐标由程序取 —— 块框本来就是程序算出来的，
      // 让模型手抄一遍只会把准确数字变成大概数字。
      // 块没框住的目标仍然走 shapes 手写，两种来源合并提交。
      const wanted = Array.isArray(args?.blockIds) ? args.blockIds : []
      const takenIds = []
      if (wanted.length > 0) {
        const tablePath = join(workDir(taskId), 'find', 'blocks.json')
        let table = null
        try {
          table = JSON.parse(await readFile(tablePath, 'utf8'))
        } catch {
          throw new Error(`mark: 找不到块表 ${tablePath} —— 先调 find { method: "blocks", task: "${taskId}" }`)
        }
        const byId = new Map()
        for (const block of Array.isArray(table?.blocks) ? table.blocks : []) byId.set(Number(block.id), block)
        for (const raw of wanted) {
          const id = Number(raw)
          const block = byId.get(id)
          if (block === undefined) {
            rejected.push(`块号 ${id} 不在块表里（共 ${byId.size} 块）`)
            continue
          }
          shapes.push({ kind: 'box', x: block.x, y: block.y, w: block.w, h: block.h })
          takenIds.push(id)
        }
      }

      if (shapes.length === 0) {
        throw new Error(`mark: no shape could be understood — ${rejected.join('; ')}`)
      }

      const image = typeof args?.image === 'string' && args.image.trim() !== ''
        ? args.image.trim()
        : undefined
      if (image === undefined) throw new Error('mark: pass `image` — the picture to annotate')

      // 只累积，不画、不归档、不删任何东西。
      // 真正的输出图在 finish 里一次性生成 —— 那时才画到原图上。
      const acc = markOf(taskId)
      if (acc.source === '') acc.source = image
      if (typeof args?.label === 'string' && args.label !== '') acc.label = args.label
      if (typeof args?.color === 'string' && args.color !== '') acc.color = args.color
      acc.shapes.push(...shapes)
      acc.rounds += 1

      return {
        ok: true,
        task: taskId,
        added: shapes.length,
        total: acc.shapes.length,
        rounds: acc.rounds,
        source: acc.source,
        blockIds: takenIds,
        skipped: rejected,
        note: '已记下，尚未出图。全部标完后调 finish 一次性生成最终图。',
      }
    },
  }), 'dsh-companion: mark tool')
}


// ── 工具：finish（收尾：清掉中间图，保留历史） ─────────────────────────────

function registerFinishTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'finish',
    description: 'DSH 涂鸦肥鱼 — 收尾 (finish): delete a task workspace and keep only the archived results. '
      + 'Call it when the whole job is done. Screenshots and working copies are removed; '
      + 'anything already archived to history stays.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        task: {
          type: 'string',
          description: '本次任务的标识，自定义字符串即可（例如一个短名）。'
            + '不填就清理全部已完成任务。不需要先调 look 拿 id。',
        },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          removed: { type: 'array', items: { type: 'string' } },
          produced: { type: 'array', items: { type: 'object', additionalProperties: true } },
          inboxCleared: { type: 'integer' },
          history: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        const made = Array.isArray(value.produced) ? value.produced : []
        if (made.length === 0) {
          return [{ type: 'text', text: 'finish: 没有待出图的标注。' }]
        }
        const lines = made.map((m) => `${m.drawn} 个标注 → ${m.archived}`)
        return [{
          type: 'text',
          text: `finish: 出了 ${made.length} 张最终图${String.fromCharCode(10)}${lines.join(String.fromCharCode(10))}`,
        }]
      },
    },
    async execute(args) {
      const removed = []
      const produced = []

      // 先把累积的标注一次性画到原图上，这是整个任务唯一的一张输出图。
      const targets = typeof args?.task === 'string' && args.task.trim() !== ''
        ? [args.task.trim()]
        : [...taskMarks.keys()]

      for (const id of targets) {
        const acc = taskMarks.get(id)
        if (acc === undefined || acc.shapes.length === 0) {
          taskMarks.delete(id)
          continue
        }
        const work = workDir(id)
        await mkdir(work, { recursive: true })
        const out = join(work, `final-${Date.now()}.png`)
        const spec = { output: out, shapes: acc.shapes, source: acc.source }
        if (typeof acc.color === 'string' && acc.color !== '') spec.color = acc.color

        const result = runMark(spec)
        if (result?.ok === true) {
          const archived = await archiveResult(out, acc.label !== '' ? acc.label : id)
          produced.push({ task: id, archived, drawn: result.drawn ?? acc.shapes.length })
          // 源图进 FIFO 队列（最多 5 张），不立即删 —— 标错了还能重标。
          if (acc.source !== '') await pushSource(acc.source)
        }
        await rm(out, { force: true }).catch(() => {})
        taskMarks.delete(id)
      }

      // 再清掉工作区
      // 清理工作区：不管 task 传的什么，work/ 下所有目录一律清掉。
      //
      // 以前是「传了 task 就只删 work/<task>」，结果 find 用的默认目录是
      // loose（没有 task 时），finish 删的却是 work/<别的名字>，loose 就
      // 永远留下来了。这个插件同一时间只跑一个任务，所以收尾就该清空整个
      // work/ —— 不留任何中间图。
      for (const id of [...aliveTasks]) await closeTask(id)

      const workRoot = join(rootDir(), 'work')
      const entries = await readdir(workRoot, { withFileTypes: true }).catch(() => [])
      for (const entry of entries) {
        if (!entry.isDirectory()) continue
        await rm(join(workRoot, entry.name), { recursive: true, force: true }).catch(() => {})
        removed.push(entry.name)
      }

      // inbox 里是用户发来的原图副本。任务结束就没用了 —— 真正要留的
      // 已经进了 sources 队列（最多 5 张）。不清就会一路涨下去。
      let inboxCleared = 0
      const inbox = join(rootDir(), 'inbox')
      const inboxEntries = await readdir(inbox, { withFileTypes: true }).catch(() => [])
      for (const entry of inboxEntries) {
        if (!entry.isFile()) continue
        await rm(join(inbox, entry.name), { force: true }).catch(() => {})
        inboxCleared++
      }

      // 两段式报告：归档做了什么、清理做了什么，分开摆清楚。
      // 归档出图了、工作区清空了，才算这一轮真的结束。
      // 删完再核对一次，别拿删除前的读数当结论。
      const leftover = await readdir(workRoot, { withFileTypes: true }).catch(() => [])
      const archived = produced.length > 0
      const cleaned = leftover.filter((e) => e.isDirectory()).length === 0
      return {
        ok: true,
        completed: archived && cleaned,
        archived,
        cleaned,
        produced,
        removed,
        inboxCleared,
        report: (archived
          ? `已归档 ${produced.length} 张：${produced.map((m) => m.archived).join(', ')}`
          : '归档：没有待出图的标注')
          + '｜'
          + (cleaned
            ? `已清理工作区：${removed.length > 0 ? removed.join(', ') : '（本来就没有）'}`
            : `清理未完：${removed.join(', ')}`),
        history: historyDir(),
      }
    },
  }), 'dsh-companion: finish tool')
}

// ── 历史 ────────────────────────────────────────────────────────────────────

function registerHistoryTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'history',
    description: 'DSH 涂鸦肥鱼 — 历史 (history): list archived annotation results, or clear them. '
      + 'Only finished annotations are kept here; screenshots and desktop-board strokes never are.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        action: {
          type: 'string',
          enum: ['list', 'clear', 'remove'],
          description: 'list (default), clear everything, or remove one entry.',
        },
        name: { type: 'string', description: 'For action remove: the exact file name to delete.' },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          dir: { type: 'string' },
          files: { type: 'array', items: { type: 'object', additionalProperties: true } },
          cleared: { type: 'integer' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        const files = Array.isArray(value.files) ? value.files : []
        if (files.length === 0) return [{ type: 'text', text: 'history: empty (' + value.dir + ')' }]
        const lines = files.map((f) => `${f.name}  ${f.width}x${f.height}  ${f.bytes} bytes`)
        return [{ type: 'text', text: 'history: ' + files.length + ' result(s) in ' + value.dir + '\n' + lines.join('\n') }]
      },
    },
    async execute(args) {
      const action = String(args?.action ?? 'list')
      const dir = historyDir()
      if (action === 'clear') {
        const entries = await readdir(dir).catch(() => [])
        let cleared = 0
        for (const name of entries) {
          if (!name.endsWith('.png')) continue
          await rm(join(dir, name), { force: true }).catch(() => {})
          cleared += 1
        }
        return { ok: true, dir, files: [], cleared, removed: [] }
      }
      if (action === 'remove') {
        const name = String(args?.name ?? '')
        if (name === '' || name.includes('/') || name.includes(String.fromCharCode(92)) || name.includes('..')) {
          throw new Error('history: bad name')
        }
        if (!name.endsWith('.png')) throw new Error('history: not a png')
        await rm(join(dir, name), { force: true })
        const after = await listHistoryEntries(dir)
        return { ok: true, dir, files: after, removed: [name] }
      }
      const files = await listHistoryEntries(dir)
      return { ok: true, dir, files, removed: [] }
    },
  }), 'dsh-companion: history tool')
}


// ── HTTP 路由：历史列表与缩略图 ─────────────────────────────────────────────
//
// 伴随者的历史面板要读归档结果，所以 Host 侧必须开一条路由。它只暴露一个
// 动作：列出历史目录里的 PNG；以及按名字取单张图给 <img> 用。

const COMPANION_ROUTE = '/api/dsh-companion'
/** 画板路由前缀：面板「画板」页读它（状态、拿铅笔、清除、重启窗口）。 */
/** 鱼眼路由前缀：面板「截图」页读屏幕尺寸与窗口列表。 */
const EYE_ROUTE = '/api/titanium-fisheye'

async function listHistory() {
  const dir = historyDir()
  const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
  const files = []
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.png')) continue
    const full = join(dir, entry.name)
    const info = await stat(full).catch(() => null)
    if (info === null) continue
    let dims = { w: 0, h: 0 }
    try {
      dims = pngSize(await readFile(full)) ?? dims
    } catch { /* 读不出来就当 0 */ }
    files.push({ name: entry.name, width: dims.w, height: dims.h, bytes: info.size })
  }
  files.sort((a, b) => (a.name < b.name ? 1 : -1))
  return { ok: true, dir, files }
}


function sendPng(res, bytes) {
  res.writeHead(200, {
    'content-type': 'image/png',
    'cache-control': 'no-store',
    'content-length': bytes.length,
  })
  res.end(bytes)
}

async function readBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  const text = Buffer.concat(chunks).toString('utf8').trim()
  if (text === '') return {}
  return JSON.parse(text)
}

/** 鱼眼路由：面板「截图」页读屏幕尺寸、窗口列表，点按钮触发截图。 */
async function eyeRoute(req, res, path) {
  const method = String(req.method ?? 'GET').toUpperCase()

  if (method === 'GET' && (path === '/' || path === '/state')) {
    const info = runCapture('info', {})
    if (info.frame) await rm(info.frame, { force: true }).catch(() => {})
    return send(res, 200, {
      ok: info.ok === true,
      screen: info.screen ?? null,
      monitorCount: info.monitorCount ?? 0,
      recent: [],
    })
  }

  if (method === 'POST' && path === '/capture') {
    let body
    try {
      body = await readBody(req)
    } catch {
      return send(res, 400, { error: 'invalid json' })
    }
    const action = String(body?.action ?? '')
    if (action === 'list' || action === 'info') {
      const result = runCapture(action, {})
      if (result.frame) await rm(result.frame, { force: true }).catch(() => {})
      if (result.ok !== true) {
        return send(res, 200, { ok: false, reason: result.reason, detail: result.detail })
      }
      const payload = { ok: true }
      if (result.windows !== undefined) payload.windows = result.windows
      if (result.width !== undefined) payload.width = result.width
      if (result.height !== undefined) payload.height = result.height
      if (result.screen !== undefined) payload.screen = result.screen
      return send(res, 200, payload)
    }
    const result = runCapture(action, { hwnd: body?.hwnd, title: body?.title })
    if (result.ok !== true) {
      return send(res, 200, { ok: false, reason: result.reason, detail: result.detail })
    }
    await rm(result.frame, { force: true }).catch(() => {})
    return send(res, 200, { ok: true, width: result.width, height: result.height })
  }

  return send(res, 404, { error: 'not found' })
}

/** 历史路由：面板「历史」页读已归档结果。 */
async function historyRoute(req, res, path) {
  const method = String(req.method ?? 'GET').toUpperCase()
  const url = new URL(req.url ?? '/', 'http://localhost')

  if (path === '/history' || path === '/') {
    if (method === 'POST') {
      const dir = historyDir()
      const entries = await readdir(dir).catch(() => [])
      let cleared = 0
      for (const name of entries) {
        if (!name.endsWith('.png')) continue
        await rm(join(dir, name), { force: true }).catch(() => {})
        cleared += 1
      }
      return send(res, 200, { ok: true, cleared })
    }
    return send(res, 200, await listHistory())
  }

  if (path === '/history/remove') {
    if (method !== 'POST') return send(res, 405, { error: 'method not allowed' })
    let body
    try {
      body = await readBody(req)
    } catch {
      return send(res, 400, { error: 'invalid json' })
    }
    const name = String(body?.name ?? '')
    if (name === '' || name.includes('/') || name.includes(String.fromCharCode(92)) || name.includes('..')) {
      return send(res, 400, { error: 'bad name' })
    }
    if (!name.endsWith('.png')) return send(res, 400, { error: 'not a png' })
    await rm(join(historyDir(), name), { force: true }).catch(() => {})
    return send(res, 200, { ok: true, removed: name })
  }

  if (path === '/history/file') {
    const name = url.searchParams.get('name') ?? ''
    if (name === '' || name.includes('/') || name.includes(String.fromCharCode(92)) || name.includes('..')) {
      return send(res, 400, { error: 'bad name' })
    }
    if (!name.endsWith('.png')) return send(res, 400, { error: 'not a png' })
    try {
      return sendPng(res, await readFile(join(historyDir(), name)))
    } catch {
      return send(res, 404, { error: 'not found' })
    }
  }

  return send(res, 404, { error: 'not found' })
}

/** 前缀分派：同一个 handler 挂到两个前缀上。 */
async function companionRoute(req, res) {
  const url = new URL(req.url ?? '/', 'http://localhost')
  const full = url.pathname
  try {
    if (full.startsWith(EYE_ROUTE)) {
      const rest = full.slice(EYE_ROUTE.length).replace(/\/+$/u, '') || '/'
      return await eyeRoute(req, res, rest)
    }
    const rest = full.slice(COMPANION_ROUTE.length).replace(/\/+$/u, '') || '/'
    return await historyRoute(req, res, rest)
  } catch (error) {
    return send(res, 500, { error: String(error?.message ?? error).slice(0, 200) })
  }
}

// ── 挂载 ────────────────────────────────────────────────────────────────────

// ══════════════════════════════════════════════════════════════════════════
// 画板部分（原 神笔鲸良 / whale-pencil）—— 已并入本文档
// ══════════════════════════════════════════════════════════════════════════

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))
/** The desktop window's file name, resolved through locateScript(). */
/** The image compositor's file name, resolved through locateScript(). */
/** Hostnames a same-machine caller can legitimately use. */
const LOOPBACK_NAMES = new Set(['127.0.0.1', '[::1]', '::1', 'localhost'])
/** A scene is a few thousand primitives at most; a bitmap is the big part. */
const MAX_BODY_BYTES = 24 * 1024 * 1024
/** How long a start may take before it is reported as failed. */
const START_MS = 20000
/** How often the window's pid file is checked while starting. */
const START_POLL_MS = 200
/** How often the Host refreshes the heartbeat the window watches. */
const HEARTBEAT_MS = 1000
/**
 * How long a tool call may stay "in flight" before the island stops believing it.
 *
 * A completion event that never arrives would otherwise pin the readout to a tool
 * that finished long ago. No real call runs this long, and a stale tool name is
 * more misleading than the plain "thinking" it falls back to.
 */
const STALE_TOOL_MS = 10 * 60 * 1000
/**
 * Where this installation lives, once known.
 *
 * A plugin directory can be renamed or moved after install, which breaks both
 * the path Node loaded and every absolute path a symlink recorded. A marker file
 * with an unguessable name is written into the package, and its location is
 * resolved by reading the NTFS master file table, the mechanism Everything uses:
 * the name is the identity, and the path is whatever that name resolves to now.
 */
let installDir = MODULE_DIR
/** Where the marker file is, and why it is not known yet. */
let locateState = { ok: false, reason: 'not-attempted', detail: '' }

// ── the marker file ──────────────────────────────────────────────────────────

/**
 * This installation's marker name.
 *
 * Derived from the machine name and the package, so it is stable for one
 * installation but unguessable elsewhere, and carries no extension so a name
 * search cannot confuse it with a real file type. Everything else in the name
 * is fixed by us, so this never asks the OS for anything: a throw here would
 * happen while the plugin is loading, and loading must not fail.
 */
function markerName() {
  const seed = `${process.env.COMPUTERNAME ?? hostname()}\u0000${INSTALL_ID}`
  return `wp${createHash('sha256').update(seed).digest('hex').slice(0, 32)}`
}

/** The marker path beside this file, whether or not it exists. */
const markerPath = () => join(MODULE_DIR, markerName())

// ── locating by name ─────────────────────────────────────────────────────────

/**
 * Ask tools/locate.py where the marker lives now.
 *
 * That script queries Everything over its WM_COPYDATA IPC, which is how a
 * renamed or moved folder is still found: the marker's name is the identity and
 * its current path is whatever the index answers. Everything is a third-party
 * program outside this plugin, so this path is strictly OPTIONAL: when it is not
 * running the reason is reported and the plugin carries on without relocation
 * recovery rather than degrading silently.
 */
function findMarker() {
  const script = join(HERE, 'tools', 'locate.py')
  if (!existsSync(script)) return { ok: false, reason: 'locate-missing', detail: script }
  try {
    const out = execFileSync(pythonExe(), [
      script, '--find', markerName(),
    ], { encoding: 'utf8', windowsHide: true, timeout: 20000, stdio: ['ignore', 'pipe', 'ignore'] })
    const line = out.trim().split(/\r?\n/u).filter((l) => l.trim() !== '').pop() ?? ''
    const parsed = JSON.parse(line)
    if (parsed?.ok !== true) {
      return { ok: false, reason: String(parsed?.reason ?? 'unknown'), detail: String(parsed?.detail ?? '') }
    }
    const paths = Array.isArray(parsed.paths) ? parsed.paths.filter((p) => typeof p === 'string') : []
    if (paths.length === 0) return { ok: false, reason: 'marker-not-found', detail: '' }
    // The marker sits beside index.js, so its parent is the package directory.
    return { ok: true, reason: '', detail: '', directory: dirname(paths[0]) }
  } catch (error) {
    return { ok: false, reason: 'locate-failed', detail: String(error?.message ?? error).slice(0, 300) }
  }
}

/**
 * The directory holding the plugin's scripts.
 *
 * The path Node loaded is tried first because it is free and usually right. The
 * Everything search is only a recovery path for a folder that was renamed or
 * moved, and it is OPTIONAL: Everything is a third-party program outside this
 * plugin, so when it is not running the plugin must still work -- it simply
 * cannot find itself after being moved. That is reported, never guessed around.
 */
function loadInstallDir() {
  if (existsSync(join(MODULE_DIR, 'tools', 'board.py'))) {
    installDir = MODULE_DIR
    locateState = { ok: true, reason: '', detail: '', source: 'module-path' }
    return MODULE_DIR
  }
  const found = findMarker()
  if (found.ok === true) {
    installDir = found.directory
    locateState = { ok: true, reason: '', detail: '', source: 'everything' }
    return found.directory
  }
  locateState = { ok: false, reason: found.reason, detail: found.detail }
  return ''
}

/** An absolute path to one of the plugin's scripts, or '' when it is missing. */
function locateScript(name) {
  if (installDir === '') loadInstallDir()
  if (installDir === '') return ''
  const file = join(installDir, name)
  return existsSync(file) ? file : ''
}

// ── where annotated images go ────────────────────────────────────────────────

/**
 * The folder that holds every image this plugin produces.
 *
 * Annotated pictures are output, not source, so they belong in one place instead
 * of landing beside whatever file the user happened to send. Keeping them under
 * a fixed name also makes them easy to find and easy to clear.
 */
const OUTPUT_DIR_NAME = 'Processed image'

/** The output folder beside the plugin, created on demand. */
function outputDir() {
  const base = installDir === '' ? MODULE_DIR : installDir
  return join(base, OUTPUT_DIR_NAME)
}

/**
 * Choose where an annotated image is written.
 *
 * An explicit `output` always wins. Otherwise the picture is named after its
 * source and placed in the output folder, never beside the original.
 */
function resolveOutputPath(source, explicit) {
  if (typeof explicit === 'string' && explicit !== '') return explicit
  const stem = basename(source).replace(/\.[A-Za-z0-9]+$/u, '')
  const stamp = new Date().toISOString().replace(/[-:T]/gu, '').slice(0, 14)
  return join(outputDir(), `${stem}-pencil-${stamp}.png`)
}

/** Create the marker when it is absent; returns its path, or '' on failure. */
function ensureMarker() {
  const file = markerPath()
  try {
    if (!existsSync(file)) writeFileSync(file, `${markerName()}\n`, 'utf8')
    return file
  } catch {
    return ''
  }
}

/** DSH_HOME, read lazily so a late environment still lands in the right place. */
function homePath(...parts) {
  return join(process.env.DSH_HOME || join(homedir(), '.dsh'), ...parts)
}

/** The files that make up the board. */
const dir = () => homePath('whale-pencil')
const sceneFile = () => join(dir(), 'scene.json')
const userFile = () => join(dir(), 'user.json')
const aliveFile = () => join(dir(), 'alive.txt')
const windowFile = () => join(dir(), 'window.txt')
const commandsFile = () => join(dir(), 'commands.json')
const clearFile = () => join(dir(), 'clear.txt')
const labelsFile = () => join(dir(), 'labels.json')
/**
 * Where pencil.ps1 writes its trace.
 *
 * WARNING: pencil.ps1 only logs when it is given -Log; Note() returns
 * immediately when the path is empty. The spawn list below used to omit it, so
 * the board ran completely silent: the file existed (written by older runs) but
 * stopped growing, which reads exactly like "the board is hung" when you are
 * diagnosing a stuck whale. One missing argument cost an hour of chasing the
 * wrong process.
 */
const boardLogFile = () => join(dir(), 'board.log')
/** The island's own trace; separate from the board's so one cannot bury the other. */
const islandLogFile = () => join(dir(), 'island.log')
const summonFile = () => join(dir(), 'summon.json')
/**
 * The stop request, written by the window and consumed by the Host.
 *
 * The island's stop button cannot use HTTP: the browser carrier admits only
 * requests that carry a live session, and a separate PowerShell process has none.
 * A file needs no handshake and matches how the clear signal already travels in
 * this direction, so the two channels stay one mechanism.
 */
const stopFile = () => join(dir(), 'stop.txt')
/**
 * What the agent is doing right now.
 *
 * The window draws this as the island at the top of the screen, so the user can
 * see thinking and tool work without watching the chat. Written on every state
 * change and read by a poll, which keeps the two processes decoupled.
 */
const statusFile = () => join(dir(), 'status.json')
/**
 * Dependency installation: its durable progress, and the installer's own trace.
 *
 * The plugin ships without the heavy half -- the Python environment and the
 * detector weights are ~2.6 GB. The user presses "下载依赖" in the panel and
 * tools/install_deps.py fetches what is missing, resuming where it stopped.
 */
const installStateFile = () => join(dir(), 'install-state.json')
const installLogFile = () => join(dir(), 'install.log')

/** The installer process, when one is running from this Host instance. */
let depsProcess = null

/** What the panel shows: the installer's own state file, plus whether we spawned it. */
async function depsState() {
  const raw = await readFileOrNull(installStateFile())
  let doc = null
  if (raw !== null) {
    try { doc = JSON.parse(raw) } catch { doc = null }
  }
  const running = depsProcess !== null && depsProcess.exitCode === null
  const state = doc !== null && typeof doc === 'object' ? doc : {}
  return {
    ok: true,
    // A state file that says "running" but whose process is gone is a lie: the
    // Host was restarted, or the installer died. Reporting it as paused lets the
    // user press continue instead of waiting for a progress bar that never moves.
    status: running ? (state.status ?? 'running') : (state.status === 'running' ? 'paused' : (state.status ?? 'idle')),
    step: state.step ?? '',
    stepIndex: state.stepIndex ?? 0,
    stepCount: state.stepCount ?? 0,
    detail: state.detail ?? '',
    percent: state.percent ?? 0,
    done: Array.isArray(state.done) ? state.done : [],
    failed: Array.isArray(state.failed) ? state.failed : [],
    updatedAt: state.updatedAt ?? 0,
    installed: await depsInstalled(),
  }
}

/**
 * Whether the plugin can actually run right now.
 *
 * Checked against the filesystem, not against the installer's bookkeeping: the
 * only question that matters is whether the interpreter and the weights the
 * tools open are present.
 */
async function depsInstalled() {
  const python = join(HERE, 'ocr_env', 'Scripts', 'python.exe')
  const required = [
    join(HERE, 'tools', 'yoloe-11s-seg.pt'),
    join(HERE, 'tools', 'omniparser_icon_detect.pt'),
    join(HERE, 'tools', 'yolov8s-worldv2.pt'),
    join(HERE, 'tools', 'mobileclip_blt.ts'),
  ]
  const missing = []
  if (!existsSync(python)) missing.push('ocr_env')
  for (const file of required) {
    if (!existsSync(file)) missing.push(basename(file))
  }
  return { ready: missing.length === 0, missing }
}

/** Start (or resume) the installer. Refuses to start a second one. */
async function startDeps() {
  if (depsProcess !== null && depsProcess.exitCode === null) {
    return { ok: false, error: 'already-running' }
  }
  await mkdir(dir(), { recursive: true })

  // Two possible runners, chosen by whether the plugin's interpreter exists yet.
  //
  //   no runtime/  -> the Node bootstrap fetches and unpacks Python, then re-runs
  //                   this installer with it (see tools/bootstrap.js)
  //   runtime/     -> Python runs install_deps.py directly
  //
  // The split exists so the first run works on a machine with no Python. Without
  // it, the installer would need the very thing it is about to install.
  const launcher = await bootstrapPython()
  if (launcher === '') {
    return runBootstrap()
  }

  const script = join(HERE, 'tools', 'install_deps.py')
  if (!existsSync(script)) return { ok: false, error: 'install_deps.py missing' }
  try {
    depsProcess = spawn(launcher, [
      script,
      '--state', installStateFile(),
      '--log', installLogFile(),
    ], { windowsHide: true, stdio: 'ignore', detached: false })
    depsProcess.once('error', () => { depsProcess = null })
    depsProcess.once('exit', () => { depsProcess = null })
  } catch (error) {
    depsProcess = null
    return { ok: false, error: String(error?.message ?? error).slice(0, 200) }
  }
  return { ok: true, started: true }
}

/**
 * The interpreter that runs the installer.
 *
 * WARNING: this cannot be "a Python on the machine". The plugin must work on a
 * clean Windows install, where there is no Python at all -- and the installer's
 * first job is to download one. So the bootstrap runs in Node, which DSH already
 * provides, and only switches to Python once runtime/ exists.
 *
 * Order: the plugin's own runtime, then the environment it built, then a system
 * Python if one happens to be present (a dev machine may have one).
 */
/**
 * The interpreter that runs the installer.
 *
 * WARNING: this cannot be "a Python on the machine". The plugin must work on a
 * clean Windows install, where there is no Python at all -- and the installer's
 * first job is to download one. So the bootstrap runs in Node, which DSH already
 * provides, and only switches to Python once runtime/ exists.
 *
 * Order: the plugin's own runtime, then the environment it built, then a system
 * Python if one happens to be present (a dev machine may have one).
 */
async function bootstrapPython() {
  const runtime = join(HERE, 'runtime', 'python', 'python.exe')
  if (existsSync(runtime)) return runtime
  const venv = join(HERE, 'ocr_env', 'Scripts', 'python.exe')
  if (existsSync(venv)) return venv
  try {
    const out = execFileSync('where', ['python'], { encoding: 'utf8', windowsHide: true })
    const first = String(out).split(/\r?\n/u).map((l) => l.trim()).find((l) => l !== '')
    if (first !== undefined && existsSync(first)) return first
  } catch { /* not on PATH */ }
  return ''
}

/**
 * Run the installer without needing Python: the Node bootstrap.
 *
 * Everything the installer needs to do before it has a Python is small -- fetch a
 * tarball, unpack it, then hand over. Node can do all three, so the first run on a
 * clean machine works, and every later run goes straight to Python.
 */
async function runBootstrap() {
  const script = join(HERE, 'tools', 'bootstrap.js')
  if (!existsSync(script)) return { ok: false, error: 'bootstrap.js missing' }
  try {
    const child = spawn(process.execPath, [script,
      '--state', installStateFile(),
      '--log', installLogFile(),
    ], { windowsHide: true, stdio: 'ignore' })
    depsProcess = child
    child.once('error', () => { depsProcess = null })
    child.once('exit', () => { depsProcess = null })
    return { ok: true, started: true }
  } catch (error) {
    depsProcess = null
    return { ok: false, error: String(error?.message ?? error).slice(0, 200) }
  }
}

/** Stop the installer at its next safe boundary. */
async function stopDeps() {
  // The installer polls its state file for this flag, so a stop works even when
  // the process was started by a previous Host instance.
  const raw = await readFileOrNull(installStateFile())
  if (raw === null) return { ok: false, error: 'nothing-to-stop' }
  try {
    const doc = JSON.parse(raw)
    doc.status = 'pausing'
    doc.detail = '正在暂停…'
    await writeAtomic(installStateFile(), JSON.stringify(doc, null, 1))
  } catch {
    return { ok: false, error: 'state-unreadable' }
  }
  return { ok: true }
}

/** Forget progress so the next start begins from the top. */
async function resetDeps() {
  if (depsProcess !== null && depsProcess.exitCode === null) {
    return { ok: false, error: 'stop-first' }
  }
  await rm(installStateFile(), { force: true }).catch(() => {})
  return { ok: true }
}

/** The scene document: what the agent painted. */
const EMPTY_SCENE = Object.freeze({ enabled: false, unit: 'screen', layers: [] })

let scene = structuredClone(EMPTY_SCENE)
let revision = 0
/** Bumped by every erase command; the window compares it with its own. */
let commandRev = 0
/** Last clear tick observed from the window, so a restart does not clear. */
let lastClear = 0
/** Last stop tick consumed, so a stale file does not cancel a new turn. */
let lastStop = 0
let ready = Promise.resolve()
let writing = Promise.resolve()
let overlay = { status: 'stopped', pid: 0, error: '' }
let overlayWork = Promise.resolve()
/**
 * Whether a board SHOULD be running.
 *
 * True at startup and whenever the board is started; set false only when the user
 * stops it from the panel. The watchdog below reads it, and that flag is the whole
 * difference between "the board died, bring it back" and "the user put the pencil
 * down, leave it alone".
 */
let overlayWanted = true
/** How many consecutive restart attempts have failed, for a backoff. */
let overlayFailures = 0
let overlayWatchdog = null
let heartbeat = null
let watcher = null
/** Watches the user's erase record so the Host's scene sheds erased marks. */
let eraseWatcher = null
let summonWatcher = null
let connectionOf = () => undefined
/** The session that most recently used the pencil, so a summon has somewhere to go. */
let sessionId = ''
/**
 * The conversation the user is actually looking at, reported by the Client half.
 *
 * Only the browser knows where the user's attention is; the Host can see tool
 * callers but not the open panel. So the Client claims the board per render and
 * this is the freshest claim.
 */
let activeSession = ''
/** When `activeSession` was last claimed, so a stale claim can expire. */
let activeSessionAt = 0
/**
 * How long a claim stays believable.
 *
 * The Client refreshes its claim every few seconds while a conversation is on
 * screen, so a claim older than this means the user left that conversation (or
 * the page closed). Without an expiry the first conversation ever opened would
 * win every summon for the rest of the session -- measured: summons kept landing
 * in an older chat after the user moved to a new one.
 */
const ACTIVE_SESSION_TTL_MS = 12000
/** Last summon timestamp already delivered. */
let lastSummon = 0
/** Late-bound `sessionController`, used to turn a summon into a prompt. */
let controllerOf = () => undefined
/** Late-bound `attachments`, used to commit a screenshot without a file. */
let attachmentsOf = () => undefined
/** Late-bound `sessions`, used to find a conversation when nobody claimed one. */
let sessionsOf = () => undefined

// ── values ────────────────────────────────────────────────────────────────────

/** Clamp a number into a range, or undefined when it is not a finite number. */
function bnum(value, min, max) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return Math.min(max, Math.max(min, value))
}

/** A CSS colour we are willing to paint. */
function color(value, fallback) {
  if (typeof value !== 'string') return fallback
  const text = value.trim()
  if (text === '' || text.length > 64) return fallback
  if (/^#[0-9a-fA-F]{3,8}$/u.test(text)) return text
  if (/^rgba?\([\d\s.,%]+\)$/u.test(text)) return text
  if (/^[a-zA-Z]+$/u.test(text)) return text
  return fallback
}

/** A point list, clamped to a sane length. */
function points(value) {
  if (!Array.isArray(value)) return null
  const out = []
  for (const entry of value.slice(0, 8000)) {
    const x = bnum(entry?.x ?? entry?.[0], -1e6, 1e6)
    const y = bnum(entry?.y ?? entry?.[1], -1e6, 1e6)
    if (x === undefined || y === undefined) continue
    out.push([Math.round(x * 100) / 100, Math.round(y * 100) / 100])
  }
  return out.length === 0 ? null : out
}

/** A stable id for one primitive, so erasing can name exactly what it removes. */
function makeId(value, index) {
  if (typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/u.test(value)) return value
  return `a${revision}-${index}`
}

/** One primitive, or null when it carries nothing drawable. */
function sanitizeItem(value, index = 0) {
  if (value === null || typeof value !== 'object') return null
  const type = typeof value.type === 'string' ? value.type : ''
  const id = makeId(value.id, index)
  switch (type) {
    case 'rect': {
      const x = bnum(value.x, -1e6, 1e6)
      const y = bnum(value.y, -1e6, 1e6)
      const w = bnum(value.w ?? value.width, -1e6, 1e6)
      const h = bnum(value.h ?? value.height, -1e6, 1e6)
      if ([x, y, w, h].some((part) => part === undefined)) return null
      return {
        id, type, x, y, w, h,
        stroke: color(value.stroke, '#FFFFFF'),
        fill: color(value.fill, 'none'),
        width: bnum(value.width ?? 3, 0, 64) ?? 3,
        radius: bnum(value.radius, 0, 400) ?? 8,
      }
    }
    case 'ellipse': {
      const x = bnum(value.x, -1e6, 1e6)
      const y = bnum(value.y, -1e6, 1e6)
      const w = bnum(value.w ?? value.width, -1e6, 1e6)
      const h = bnum(value.h ?? value.height, -1e6, 1e6)
      if ([x, y, w, h].some((part) => part === undefined)) return null
      return {
        id, type, x, y, w, h,
        stroke: color(value.stroke, '#FFFFFF'),
        fill: color(value.fill, 'none'),
        width: bnum(value.width ?? 3, 0, 64) ?? 3,
      }
    }
    case 'path': {
      const list = points(value.points)
      if (list === null) return null
      return { id, type, points: list, stroke: color(value.stroke, '#FFFFFF'), width: bnum(value.width ?? 4, 0.5, 64) ?? 4 }
    }
    case 'arrow': {
      const x1 = bnum(value.x1, -1e6, 1e6)
      const y1 = bnum(value.y1, -1e6, 1e6)
      const x2 = bnum(value.x2, -1e6, 1e6)
      const y2 = bnum(value.y2, -1e6, 1e6)
      if ([x1, y1, x2, y2].some((part) => part === undefined)) return null
      return {
        id, type, x1, y1, x2, y2,
        stroke: color(value.stroke, '#FFFFFF'),
        width: bnum(value.width ?? 4, 0.5, 64) ?? 4,
        head: bnum(value.head ?? 18, 2, 200) ?? 18,
      }
    }
    case 'text': {
      const x = bnum(value.x, -1e6, 1e6)
      const y = bnum(value.y, -1e6, 1e6)
      if (x === undefined || y === undefined) return null
      const text = typeof value.text === 'string' ? value.text.slice(0, 4000) : ''
      if (text === '') return null
      return {
        id, type, x, y, text,
        size: bnum(value.size ?? 18, 6, 400) ?? 18,
        color: color(value.color, '#FFFFFF'),
        align: value.align === 'center' || value.align === 'right' ? value.align : 'left',
        weight: value.weight === 'bold' ? 'bold' : 'normal',
      }
    }
    case 'image': {
      const src = typeof value.src === 'string' ? value.src : ''
      if (!src.startsWith('data:image/') || src.length > 20 * 1024 * 1024) return null
      return {
        id, type,
        x: bnum(value.x ?? 0, -1e6, 1e6) ?? 0,
        y: bnum(value.y ?? 0, -1e6, 1e6) ?? 0,
        w: bnum(value.w ?? value.width, 0, 1e6) ?? 0,
        h: bnum(value.h ?? value.height, 0, 1e6) ?? 0,
        src,
        alpha: bnum(value.alpha ?? 1, 0, 1) ?? 1,
      }
    }
    case 'pixels': {
      const data = typeof value.data === 'string' ? value.data : ''
      const w = bnum(value.w, 1, 4096)
      const h = bnum(value.h, 1, 4096)
      if (data === '' || w === undefined || h === undefined) return null
      if (data.length > 16 * 1024 * 1024) return null
      return {
        id, type,
        x: bnum(value.x ?? 0, -1e6, 1e6) ?? 0,
        y: bnum(value.y ?? 0, -1e6, 1e6) ?? 0,
        w: Math.round(w), h: Math.round(h), data,
      }
    }
    default:
      return null
  }
}

/** A bounding box for one primitive, used by rectangular erase. */
function boxOf(item) {
  switch (item.type) {
    case 'rect':
    case 'ellipse':
    case 'image':
    case 'pixels':
      return { x: item.x, y: item.y, w: item.w, h: item.h }
    case 'text':
      return { x: item.x, y: item.y, w: Math.max(120, item.text.length * item.size * 0.7), h: item.size * 1.6 }
    case 'arrow':
      return {
        x: Math.min(item.x1, item.x2),
        y: Math.min(item.y1, item.y2),
        w: Math.abs(item.x2 - item.x1),
        h: Math.abs(item.y2 - item.y1),
      }
    case 'path': {
      const xs = item.points.map((p) => p[0])
      const ys = item.points.map((p) => p[1])
      const x = Math.min(...xs)
      const y = Math.min(...ys)
      return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y }
    }
    default:
      return null
  }
}

/** Whether two boxes overlap. */
function overlaps(a, b) {
  return !(a.x + a.w < b.x || a.x > b.x + b.w || a.y + a.h < b.y || a.y > b.y + b.h)
}

// ── persistence ───────────────────────────────────────────────────────────────

async function writeAtomic(file, body) {
  await mkdir(dirname(file), { recursive: true })
  const temp = `${file}.${process.pid}.tmp`
  await writeFile(temp, body, 'utf8')
  await rename(temp, file)
}

async function readFileOrNull(file) {
  try {
    return await readFile(file, 'utf8')
  } catch {
    return null
  }
}

/** Persist the agent's scene and hand the same bytes to the window. */
function saveScene() {
  const body = JSON.stringify({ ...scene, revision }, null, 2) + '\n'
  writing = writing.then(() => writeAtomic(sceneFile(), body)).catch(() => {})
  return writing
}

/** What the window already knows about the user's strokes. */
async function readUser() {
  const raw = await readFileOrNull(userFile())
  if (raw === null) return { revision: 0, strokes: [], texts: [], hidden: [] }
  try {
    const parsed = JSON.parse(raw)
    return {
      revision: Number.isFinite(parsed?.revision) ? parsed.revision : 0,
      strokes: Array.isArray(parsed?.strokes) ? parsed.strokes : [],
      texts: Array.isArray(parsed?.texts) ? parsed.texts : [],
      hidden: Array.isArray(parsed?.hidden) ? parsed.hidden : [],
    }
  } catch {
    return { revision: 0, strokes: [], texts: [], hidden: [] }
  }
}

/** Send one erase instruction to the window. */
function sendCommand(erase) {
  commandRev += 1
  const body = JSON.stringify({ rev: commandRev, erase }, null, 2) + '\n'
  return writeAtomic(commandsFile(), body).catch(() => {})
}

/**
 * Drop whatever the user erased from the Host's own scene.
 *
 * The window hides erased marks in its own list, but that list dies with the
 * process: close the board, reopen it, and every erased mark would come back
 * because `scene.json` still holds it. So the Host folds the user's hidden ids
 * into the scene before the window is launched, and the marks are gone for good.
 */
async function pruneErased() {
  const user = await readUser()
  if (user.hidden.length === 0) return
  const hidden = new Set(user.hidden)
  const kept = scene.layers.filter((item) => !hidden.has(item.id))
  if (kept.length === scene.layers.length) return
  scene = { ...scene, layers: kept }
  revision += 1
  await saveScene()
}

/**
 * Arm or disarm the user's pencil from the Host side.
 *
 * The tray is the user's normal door, but a tray icon can be hidden in the
 * Windows overflow area, so the settings page offers the same switch.
 */
function sendMode(mode) {
  commandRev += 1
  const body = JSON.stringify({ rev: commandRev, mode }, null, 2) + '\n'
  return writeAtomic(commandsFile(), body).catch(() => {})
}

// ── the heartbeat and the clear watcher ───────────────────────────────────────

function startHeartbeat() {
  if (heartbeat !== null) return
  const beat = () => { void writeAtomic(aliveFile(), String(Date.now())).catch(() => {}) }
  beat()
  heartbeat = setInterval(beat, HEARTBEAT_MS)
  if (typeof heartbeat.unref === 'function') heartbeat.unref()
}

function stopHeartbeat() {
  if (heartbeat === null) return
  clearInterval(heartbeat)
  heartbeat = null
}

/**
 * The user's "clear everything" must also clear the agent's marks, and the
 * agent's marks are Host-owned: the window can only announce that it happened.
 */
function startClearWatcher() {
  if (watcher !== null) return
  watcher = setInterval(async () => {
    const raw = await readFileOrNull(clearFile())
    if (raw === null) return
    const tick = Number.parseInt(raw.trim(), 10)
    if (!Number.isInteger(tick) || tick <= lastClear) return
    lastClear = tick
    scene = { ...scene, layers: [] }
    revision += 1
    await saveScene()
    // The window already dropped its own marks; clearing its record too keeps a
    // later erase from pruning ids that no longer exist.
    await rm(userFile(), { force: true }).catch(() => {})
  }, 700)
  if (typeof watcher.unref === 'function') watcher.unref()
}

function stopClearWatcher() {
  if (watcher === null) return
  clearInterval(watcher)
  watcher = null
}

/**
 * Keep the Host's scene free of marks the user erased while the board is open.
 *
 * `pruneErased` runs before every launch, but a long-lived board would keep
 * erased marks in `scene.json` until the next restart. This watches the
 * window's own record so the file matches what the user sees, and so the agent's
 * `scene` read does not offer marks that are no longer there.
 */
function startEraseWatcher() {
  if (eraseWatcher !== null) return
  let lastHidden = ''
  eraseWatcher = setInterval(async () => {
    const user = await readUser()
    const key = user.hidden.join(',')
    if (key === lastHidden) return
    lastHidden = key
    if (user.hidden.length === 0) return
    await pruneErased()
  }, 900)
  if (typeof eraseWatcher.unref === 'function') eraseWatcher.unref()
}

function stopEraseWatcher() {
  if (eraseWatcher === null) return
  clearInterval(eraseWatcher)
  eraseWatcher = null
}

// ── summon ────────────────────────────────────────────────────────────────────

/**
 * Rules waiting to be delivered with the next model step.
 *
 * These are NOT conversation messages. The board's rules are context the agent
 * needs, not something the user typed, so they travel through the prompt's
 * runtime-context channel: model-visible, but absent from the transcript. Cleared
 * on read, so an unrelated later turn never inherits an instruction about a
 * screenshot that is no longer the subject.
 *
 * A developer message was tried first and rejected by the session validator:
 * "developer/message and developer role must occur together" — a role alone cannot
 * be injected through a step's message list, which carries only user messages.
 */
const pendingRules = new Map()

/**
 * Which conversation a summon should wake.
 *
 * Three sources, in order of how well they know the user's attention:
 *
 *   1. `activeSession` — the Client half reports the panel on screen.
 *   2. `sessionId`     — the last agent that used the pencil.
 *   3. the sessions service — whatever is running or most recently touched.
 *
 * The third exists because the board's own panel no longer renders: the
 * floating ball is a standalone window, so no Client half is left to claim a
 * session. Without a fallback a summon fails with `no-active-session` while a
 * conversation is plainly open (that is exactly what happened).
 */
async function pickSummonTarget() {
  // A "new chat" screen is a blank session: the Host creates it the moment the
  // user opens one, and it has no messages yet. It must be checked FIRST, before
  // any claim. The Client reports the conversation whose header slot is mounted,
  // and on a fresh "new chat" that is still the conversation the user just left:
  // measured, the claim said d75875af while a blank 6ebaefe1 sat at the top of
  // the controller list, so the summon went to the old chat.
  const blank = await blankSessionId()
  if (blank !== '') return blank

  // The Client's claim wins while it is fresh: it is the only source that knows
  // which conversation is actually on screen, and it refreshes itself while that
  // conversation stays open. A stale claim is ignored, or the first chat ever
  // opened would keep winning after the user moved on.
  if (activeSession !== '' && Date.now() - activeSessionAt <= ACTIVE_SESSION_TTL_MS) {
    return activeSession
  }
  if (activeSession !== '') {
    activeSession = ''
    activeSessionAt = 0
  }

  // WARNING: the target must be a session the Host still has OPEN. Picking the
  // freshest file on disk is not enough -- a closed conversation leaves its log
  // behind, and delivering to it fails with `session "..." not found`, which
  // looks exactly like "the button does nothing".
  //
  // So the live list decides membership, and the last user message decides order
  // among those: the conversation the user is typing in is both open and spoken
  // in.
  const live = await liveSessionIds()
  if (live.length > 0) {
    const freshest = await freshestSessionId(live)
    if (freshest !== '') return freshest
    return live[live.length - 1]
  }

  // Nothing live is visible through the service. The last agent that used the
  // pencil is the only remaining clue.
  return sessionId
}

/**
 * Write down what the Host can actually see about sessions, once per summon.
 *
 * This exists because the target has been wrong repeatedly and every attempt to
 * infer the data's shape from the DSH bundle was a guess that turned out wrong.
 * One line of truth beats another round of speculation: after the next summon,
 * `summon-facts.json` says exactly which calls returned what.
 */
async function dumpSummonFacts() {
  const facts = {
    at: Date.now(),
    activeSession,
    activeSessionAgeMs: activeSessionAt > 0 ? Date.now() - activeSessionAt : -1,
    lastToolSession: sessionId,
    hasController: controllerOf() !== undefined,
    hasSessions: sessionsOf() !== undefined,
    controllerList: null,
    controllerError: '',
    sessionsSnapshot: null,
    sessionsError: '',
    blank: '',
    live: [],
  }
  const controller = controllerOf()
  if (controller !== undefined && typeof controller.list === 'function') {
    try {
      const result = await controller.list()
      // Keep the shape, not just the ids: the shape is what has been wrong.
      facts.controllerList = {
        type: Array.isArray(result) ? 'array' : typeof result,
        keys: result !== null && typeof result === 'object' ? Object.keys(result) : [],
        itemCount: Array.isArray(result?.items) ? result.items.length : -1,
        items: Array.isArray(result?.items)
          ? result.items.slice(0, 12).map((item) => ({
            sessionId: item?.sessionId,
            blank: item?.blank,
            running: item?.running,
            agentAvailable: item?.agentAvailable,
            updatedAt: item?.updatedAt,
            title: item?.title,
          }))
          : null,
      }
    } catch (error) {
      facts.controllerError = String(error?.message ?? error).slice(0, 200)
    }
  }
  const sessions = sessionsOf()
  if (sessions !== undefined) {
    try {
      const snapshot = sessions.list?.getSnapshot?.()
      facts.sessionsSnapshot = {
        type: typeof snapshot,
        keys: snapshot !== null && typeof snapshot === 'object' ? Object.keys(snapshot) : [],
        byIdKeys: snapshot?.byId !== null && typeof snapshot?.byId === 'object'
          ? Object.keys(snapshot.byId).slice(0, 12)
          : [],
      }
    } catch (error) {
      facts.sessionsError = String(error?.message ?? error).slice(0, 200)
    }
  }
  try { facts.blank = await blankSessionId() } catch { /* ignore */ }
  try { facts.live = await liveSessionIds() } catch { /* ignore */ }
  await writeAtomic(join(dir(), 'summon-facts.json'), JSON.stringify(facts, null, 2)).catch(() => {})
}

/**
 * Every session summary the Host knows about, as a flat list.
 *
 * The authoritative source is `sessionController.list()`, which returns
 * `{ items: [...] }` where each item carries `sessionId`, `blank`, `updatedAt`
 * and `running`. The sessions service's `getSnapshot().byId` is a client
 * projection and does not reliably carry blank rows, which is why an earlier
 * version could never see the "new chat" the user was sitting on.
 */
async function sessionSummaries() {
  const controller = controllerOf()
  if (controller === undefined || typeof controller.list !== 'function') return []
  try {
    const result = await controller.list()
    const items = result?.items
    if (Array.isArray(items)) return items
    if (Array.isArray(result)) return result
    return []
  } catch {
    return []
  }
}

/**
 * The id of a blank (never-messaged) session the user is sitting on, if any.
 *
 * A blank session is the provisional "New chat" row: the Host creates it when the
 * user opens one, and it becomes a real conversation on its first message. The
 * summon is that first message, so this is the right target whenever it exists.
 *
 * WARNING: if several blank sessions exist, the most recently touched one wins.
 * Picking an arbitrary one would deliver into a tab the user is not looking at.
 */
async function blankSessionId() {
  const items = await sessionSummaries()
  let best = ''
  let bestAt = -1
  for (const item of items) {
    if (item?.blank !== true) continue
    const id = typeof item.sessionId === 'string' ? item.sessionId : ''
    if (id === '') continue
    const at = typeof item.updatedAt === 'number' ? item.updatedAt : 0
    if (at >= bestAt) {
      bestAt = at
      best = id
    }
  }
  return best
}

/**
 * Ids of the sessions the Host currently holds open.
 *
 * Same authoritative source as blankSessionId: the controller's list, whose
 * items carry `sessionId` and `agentAvailable`. Only a session that owns a live
 * agent can accept a prompt; delivering to a persisted-but-closed one fails with
 * "session not found", which reads to the user as "the button did nothing".
 */
async function liveSessionIds() {
  const items = await sessionSummaries()
  const ids = []
  for (const item of items) {
    const id = typeof item?.sessionId === 'string' ? item.sessionId : ''
    if (id === '') continue
    if (item.agentAvailable === false) continue
    ids.push(id)
  }
  return ids
}

/**
 * The open session whose log was written to most recently.
 *
 * `allowed` limits the search to ids the Host still has open, so a leftover log
 * from a closed conversation can never win. The layout is two levels deep --
 * sessions/<workspace>/session-<id>/ -- and an unreadable workspace is skipped
 * rather than aborting the search.
 *
 * WARNING: the ranking is the LAST USER MESSAGE, not the file's mtime. The agent
 * writes to its own session log constantly (every tool call), so mtime always
 * favours whichever conversation the agent happens to be working in -- which is
 * exactly the wrong answer when the user has moved to a new chat. Only what the
 * USER typed counts as attention.
 */
async function freshestSessionId(allowed) {
  const want = new Set(Array.isArray(allowed) ? allowed : [])
  try {
    const root = sessionsRoot()
    let best = ''
    let bestTime = 0
    const workspaces = await readdir(root, { withFileTypes: true }).catch(() => [])
    for (const workspace of workspaces) {
      if (!workspace.isDirectory()) continue
      const base = join(root, workspace.name)
      const dirs = await readdir(base, { withFileTypes: true }).catch(() => [])
      for (const dir of dirs) {
        if (!dir.isDirectory()) continue
        // The directory is named `session-<id>`; only an id the Host still has
        // open may win, or delivery fails with "session not found".
        const name = /^session-(.+)$/u.exec(dir.name)
        if (name === null) continue
        if (want.size > 0 && !want.has(name[1])) continue
        const file = join(base, dir.name, 'session.v4.jsonl.zstd')
        const info = await stat(file).catch(() => null)
        if (info === null || !info.isFile()) continue
        // A quick mtime pre-filter: a session nobody touched in the last few
        // minutes cannot be the one the user is typing in, and skipping it saves
        // decompressing every log on the machine.
        if (bestTime > 0 && info.mtimeMs < bestTime - 60000) continue
        const at = await lastUserMessageAt(file)
        const when = at > 0 ? at : info.mtimeMs
        if (when > bestTime) {
          bestTime = when
          best = name[1]
        }
      }
    }
    return best
  } catch {
    return ''
  }
}

/** When the user last said something in this session, or 0 when they never did. */
/**
 * When the user last said something in this session, or 0 when they never did.
 *
 * WARNING: reads the TAIL of the log, not the whole file. The newest user message
 * is at the end, and `readSessionText` decompresses synchronously -- reading every
 * session's full log on a summon blocked the Node event loop for about a second,
 * which is long enough to miss the heartbeat that the board and the whale watch.
 * A 256 KB tail holds far more than the last message.
 */
const TAIL_BYTES = 262144

async function lastUserMessageAt(file) {
  let handle
  try {
    handle = await open(file, 'r')
  } catch {
    return 0
  }
  let buf
  try {
    const size = (await handle.stat()).size
    const want = Math.min(size, TAIL_BYTES)
    const out = Buffer.alloc(want)
    const { bytesRead } = await handle.read(out, 0, want, size - want)
    buf = out.subarray(0, bytesRead)
  } catch {
    return 0
  } finally {
    await handle.close().catch(() => {})
  }
  let text = ''
  try {
    text = readSessionText(buf)
  } catch {
    return 0
  }
  let newest = 0
  for (const line of text.split(/\r?\n/u)) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) continue
    if (!trimmed.includes('user/message')) continue
    let event
    try { event = JSON.parse(trimmed) } catch { continue }
    if (event?.type !== 'user/message') continue
    const time = typeof event.time === 'number' ? event.time : 0
    if (time > newest) newest = time
  }
  return newest
}

/**
 * The user has no way to start a turn by speaking, so the tray lends them one:
 * the window captures the screen and this delivers it to the Session the user is
 * looking at.
 *
 * The target is whichever conversation reported itself active last: the Client
 * half claims the board whenever a chat is on screen, so `activeSession` follows
 * the user rather than whichever session happened to call a tool most recently.
 * The screenshot never touches disk. It is committed through the attachment
 * service and admitted into the prompt as an image, which is also what makes it
 * visible to a model that accepts images.
 */
async function deliverSummon(record) {
  const controller = controllerOf()
  if (controller === undefined || typeof controller.prompt !== 'function') return

  const id = await pickSummonTarget()
  if (id === '') {
    // No conversation to deliver to. The user asked to talk, so one is created
    // rather than the press being swallowed -- "nothing happened" is the worst
    // possible answer to a button.
    const created = await createSessionForSummon()
    if (created === '') {
      await noteSummon({ at: record?.at, ok: false, error: 'no-active-session' })
      return
    }
    return deliverTo(created, record, controller)
  }

  return deliverTo(id, record, controller)
}

/**
 * Ask the Host for a new conversation.
 *
 * Used when the user summons from a blank "new chat" screen: there is no id to
 * deliver to yet, and the request is plainly meant to open one. Returns '' when
 * the service cannot create one, so the caller can report the failure instead of
 * pretending.
 */
async function createSessionForSummon() {
  const controller = controllerOf()
  if (controller === undefined) return ''
  // The controller creates a session in the active workspace and returns its id;
  // the sessions service is the older path and is tried second.
  try {
    const created = await controller.create?.({})
    const id = typeof created?.sessionId === 'string'
      ? created.sessionId
      : (typeof created?.id === 'string' ? created.id : '')
    if (id !== '') return id
  } catch { /* fall through */ }
  const sessions = sessionsOf()
  if (sessions === undefined) return ''
  try {
    const created = await sessions.create?.()
    return typeof created?.id === 'string' ? created.id : ''
  } catch {
    return ''
  }
}

/** Deliver one summon to a known session. */
async function deliverTo(id, record, controller) {
  const shot = await captureScreen()
  if (shot === null) {
    await noteSummon({ at: record?.at, ok: false, sessionId: id, error: 'capture-failed' })
    return
  }

  // 坐标怎么来：act.png 上画了坐标网格，网格数字就是真实桌面坐标。
  //
  //   所以这里**不提「截图多大」这件事**。提了只会让模型误以为要申报尺寸、
  //   要自己换算。事实是：读网格 → 发数字 → 程序原样画，中间没有任何缩放。
  const space = '看图时用 act.png 上的坐标网格读位置，那些数字就是真实桌面坐标。'

  // The prompt is split in two on purpose.
  //
  // The user sees what they actually did: their board text and the screenshot.
  // Everything that is instruction — where the screenshot's coordinates live, how
  // to read red, how far to verify — is developer context, not something the user
  // typed. Sending it as a user message would put words in the user's mouth and
  // fill the chat pane with a wall of rules every time they ring the bell.
  const rules = [
    '【托盘呼唤】用户在画板上按了「呼唤 agent」，截了一张屏幕贴在下面。',
    '',
    '这张截图属于 **pencil act**（不是用户直接发图让你标注的 pict act）。',
    '流程是「材料进 → 我看 → 决定 → 发指令 → 屏幕出图」，产物只有屏幕上的那些标记，',
    '**不保存任何东西**（没有最终图、没有归档、不调 finish）。',
    '',
    '  1. now-image 拿到图片落盘路径',
    '  2. find { method: "act", image: <path>, task: "pencil-xxx" }',
    '     这一条命令自动跑完 OCR + UI + 差分，把文字和控件标到一张图上',
    '  3. read_image 它返回的 act.png —— 只看一眼，这是给你判断的材料',
    '  4. 结合差分块 + OCR 文字 + UI 框，**你自己决定**框哪块、在哪写字',
    '  5. pencil { action:"draw", items:[...] } 把决定发给程序，屏幕出图',
    '',
    space,
    '',
    '画板上的红线和红字都是用户画的、用户打的，那就是用户在跟你说话：',
    '圈住一块 = 「这个」，箭头 = 「那个方向」，红字 = 他的问题。',
    '这一轮你可以在画板上作答 —— 因为【托盘呼唤】本身就是「请在屏幕上回我」。',
    '先 pencil scene 读他的笔画和文字坐标，再用 pencil draw 在他指的地方写字、画框、画箭头。',
    '你的笔迹不要用红色，免得和用户的混在一起。',
    '',
    '⚠ 这条许可只对**这一轮**有效。下一轮如果用户没有【托盘呼唤】、没有发图要求标注、',
    '也没有明确说「画在屏幕上」，就一个字都不许画 —— 只在聊天里回答。',
    '「我上次画过了」不是理由，「用户口头答应了」也不是许可。',
    '',
    '【坐标怎么来 —— 只读网格，不要估算，不要申报尺寸】',
    '',
    'act.png 上画了坐标网格，每 100 像素一条线，四条边都标着数字。',
    '那些数字**就是真实桌面坐标**（网格是用原图像素画的），所以你读到什么，',
    '就发什么 —— pencil draw 不做任何换算，画到哪儿就是哪儿。',
    '',
    '步骤：在 act.png 上找到目标 → 看它压在 x 哪两条网格线之间、y 哪两条之间 →',
    '取中间值 → 直接发给 pencil draw。例：目标在 x=1200 与 1300 之间偏左、',
    'y=500 与 600 之间居中 → 发 {x:1225, y:550}。',
    '',
    '不要申报 view，不要自己乘系数，不要去猜「我这张图是不是被压缩了」——',
    '那些都是多余的，网格数字已经就是答案。',
    '',
    '【怎么定位】看图上大致位置就够了，**用你自己的眼睛估个大概，直接画**。',
    '不要为了“更精确”去写脚本、查注册表、读内存、做像素分析——那是绕远路，',
    '用户看着你写一堆文件只会更困惑。圈出大致范围即可，误差几十像素完全没关系。',
    '如果桌面被窗口挡住看不全，装上钛合金鱼眼就用 eye { action: "desktop" } 拿干净桌面；',
    '没装就照实告知用户，别自己造工具。',
    '',
    '【画完最多校验三次】',
    '画完可以截图看一眼，但**最多三次**。看到大致对了就停下回话。',
    '不要一遍遍擦掉重画去追几个像素的偏差——那会让用户一直等你，',
    '而且越改越乱。三次之后无论是否完美，都停下来把结果告诉用户。',
  ].join('\n')

  // What the user typed on the board is theirs to see, so it is the visible part.
  const boardText = userTextOf(await readUserBoard())
  const content = []
  if (boardText !== '') content.push({ type: 'text', text: boardText })

  const attachments = attachmentsOf()
  if (attachments !== undefined) {
    try {
      content.push({
        type: 'image',
        mediaType: 'image/png',
        data: Buffer.from(shot).toString('base64'),
        name: 'screen.png',
      })
    } catch {
      // Falling through sends the text alone; the user is told by the ack.
    }
  }

  // A summon must carry something visible or the turn opens on an empty bubble.
  if (content.length === 0) {
    content.push({ type: 'text', text: '（呼唤 agent：看看我的画板）' })
  }

  try {
    await controller.prompt({
      requestId: `whale-pencil-summon-${Date.now()}`,
      sessionId: id,
      mode: 'queue',
      content,
    }, new AbortController().signal)

    // The rules ride along as hidden context for the step this summon opens. The
    // pre-step hook below attaches them as a developer message, so the agent is
    // told what to do without the transcript pretending the user said it.
    pendingRules.set(id, rules)
    await noteSummon({ at: record?.at, ok: true, sessionId: id, image: content.length > 0 })
  } catch (error) {
    pendingRules.delete(id)
    const message = String(error?.message ?? error).slice(0, 300)
    // WARNING: a stale id is the common failure ("session ... not found"), and
    // giving up on the first miss is what made the button look dead. The other
    // live sessions are tried before the summon is reported as failed: the user
    // pressed a button and expects the words to land SOMEWHERE they can see.
    const others = (await liveSessionIds()).filter((other) => other !== id)
    for (const other of others.reverse()) {
      try {
        await controller.prompt({
          requestId: `whale-pencil-summon-${Date.now()}-${other.slice(0, 8)}`,
          sessionId: other,
          mode: 'queue',
          content,
        }, new AbortController().signal)
        pendingRules.set(other, rules)
        await noteSummon({ at: record?.at, ok: true, sessionId: other, image: content.length > 0, retriedFrom: id })
        return
      } catch {
        // Try the next one.
      }
    }
    // Nothing accepted it: say so instead of failing silently, so the reason is
    // inspectable. The facts dump goes with the failure -- that is when the shape
    // of the session data is worth recording, and not on every successful summon.
    await dumpSummonFacts().catch(() => {})
    await noteSummon({
      at: record?.at,
      ok: false,
      sessionId: id,
      error: message,
    })
  }
}


/** Read the board's user file, tolerating every way it can be absent or broken. */
async function readUserBoard() {
  const raw = await readFileOrNull(userFile())
  if (raw === null) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

/**
 * The user's text boxes in reading order, numbered from 1.
 *
 * Reading order, not creation order: a box added last but placed at the top is
 * the first thing the user reads, and the agent should meet it first too. Boxes
 * within a line-height of each other count as one row, so a short box to the left
 * of a tall one is still read before it.
 *
 * One list serves both the summon message and the `pencil scene` result, so the
 * numbers the agent sees in the chat are the numbers it sees when it inspects the
 * board — two different numberings would be worse than none.
 */
function userTextBoxes(board) {
  const texts = Array.isArray(board?.texts) ? board.texts : []
  const boxes = []
  for (const entry of texts) {
    const text = typeof entry?.text === 'string' ? entry.text.trim() : ''
    if (text === '') continue
    boxes.push({
      id: typeof entry?.id === 'string' ? entry.id : '',
      text,
      x: Number.isFinite(entry?.x) ? entry.x : 0,
      y: Number.isFinite(entry?.y) ? entry.y : 0,
    })
  }
  boxes.sort((a, b) => {
    if (Math.abs(a.y - b.y) > 24) return a.y - b.y
    return a.x - b.x
  })
  return boxes.map((box, index) => ({ ...box, seq: index + 1 }))
}

/** The user's typed text on the board, numbered and laid out for the agent. */
function userTextOf(board) {
  const boxes = userTextBoxes(board)
  if (boxes.length === 0) return ''
  if (boxes.length === 1) return boxes[0].text
  return boxes.map((box) => `【文本框 ${box.seq}】\n${box.text}`).join('\n\n')
}

/** Record the delivery outcome where the window can show it. */
async function noteSummon(result) {
  await writeAtomic(join(dir(), 'summon-ack.json'), JSON.stringify(result)).catch(() => {})
}

// ── the real screen ──────────────────────────────────────────────────────────

/**
 * The desktop as Windows reports it, and a capture of it.
 *
 * Both are measured by tools/screen.py, which declares DPI awareness before
 * reading anything. A DPI-unaware process is shown a scaled-down desktop —
 * 1536x864 on a 125% display with a 1920x1080 panel — and every coordinate taken
 * from that is wrong by the scale factor. Asking the OS keeps this correct on
 * any monitor at any scale instead of assuming one machine's numbers.
 */
function screenScript() {
  return join(HERE, 'tools', 'screen.py')
}

/** The desktop in physical pixels: {w,h,x,y}, or null when it cannot be read. */
function probeScreen() {
  try {
    const out = execFileSync(pythonExe(), [
      screenScript(), '--mode', 'size',
    ], { encoding: 'utf8', windowsHide: true, timeout: 20000, stdio: ['ignore', 'pipe', 'ignore'] })
    const line = out.trim().split(/\r?\n/u).filter((l) => l.trim() !== '').pop() ?? ''
    const match = /^(\d+)x(\d+)@(-?\d+),(-?\d+)$/u.exec(line)
    if (match === null) return null
    const w = Number(match[1])
    const h = Number(match[2])
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null
    return { w, h, x: Number(match[3]), y: Number(match[4]) }
  } catch {
    return null
  }
}

/**
 * The cached desktop size.
 *
 * Probing costs a process spawn, so the answer is kept for the life of the
 * plugin. A failed probe is not cached: the next call retries rather than
 * reporting a permanent failure that never happened.
 */
let screenCache = null
function screenSize() {
  if (screenCache !== null) return screenCache
  screenCache = probeScreen()
  return screenCache
}

/** Pixel dimensions of an encoded PNG, read from its header. */
function imageSize(bytes) {
  try {
    if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50) {
      return { w: bytes.readUInt32BE(16), h: bytes.readUInt32BE(20) }
    }
  } catch { /* not a PNG header */ }
  return null
}

/**
 * Grab the whole desktop as PNG bytes.
 *
 * tools/screen.py captures at physical resolution, so the picture is in the same
 * coordinate space the board draws in and its pixels can be used directly.
 * The file lives only long enough to be read: no screenshot survives on disk.
 */
async function captureScreen() {
  const frame = join(tmpdir(), `whale-pencil-${process.pid}-${Date.now()}.png`)
  try {
    await new Promise((resolve, reject) => {
      const child = spawn(pythonExe(), [
        screenScript(), '--mode', 'capture', '--out', frame,
      ], { windowsHide: true, stdio: 'ignore' })
      const timer = setTimeout(() => {
        try { child.kill() } catch { /* gone */ }
        reject(new Error('capture timed out'))
      }, 20000)
      child.once('error', (error) => { clearTimeout(timer); reject(error) })
      child.once('exit', (code) => {
        clearTimeout(timer)
        if (code === 0) resolve()
        else reject(new Error(`capture exited ${code}`))
      })
    })
    return await readFile(frame)
  } catch {
    return null
  } finally {
    await rm(frame, { force: true }).catch(() => {})
  }
}



function startSummonWatcher() {
  if (summonWatcher !== null) return
  // WARNING: seed lastSummon from whatever is already on disk BEFORE watching.
  // lastSummon starts at 0, so a summon.json left over from a previous run looked
  // like a brand new request and was delivered on every start: the user got an
  // agent waking up saying it had been summoned, and the island lit up, every time
  // DSH launched. Measured: the file on disk was two hours old and still fired.
  //
  // A summon is an event, not a standing request -- one that was not delivered
  // while the Host was down is stale by definition, so it is consumed silently.
  //
  // Read it SYNCHRONOUSLY: an async seed would race the first interval tick and
  // the stale summon would still get through.
  try {
    const raw = readFileSync(summonFile(), 'utf8')
    const at = Number(JSON.parse(raw)?.at ?? 0)
    if (Number.isFinite(at) && at > lastSummon) lastSummon = at
  } catch { /* no file yet, or a corrupt one: start from zero */ }
  summonWatcher = setInterval(async () => {
    const raw = await readFileOrNull(summonFile())
    if (raw === null) return
    let record
    try {
      record = JSON.parse(raw)
    } catch {
      return
    }
    const at = Number(record?.at ?? 0)
    if (!Number.isFinite(at) || at <= lastSummon) return
    lastSummon = at
    await deliverSummon(record)
  }, 800)
  if (typeof summonWatcher.unref === 'function') summonWatcher.unref()
}

function stopSummonWatcher() {
  if (summonWatcher === null) return
  clearInterval(summonWatcher)
  summonWatcher = null
}

// ── the desktop window ────────────────────────────────────────────────────────

async function publishedPid() {
  const raw = await readFileOrNull(windowFile())
  if (raw === null) return 0
  const pid = Number.parseInt(raw.trim(), 10)
  return Number.isInteger(pid) && pid > 0 ? pid : 0
}

function alivePid(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code === 'EPERM'
  }
}

/**
 * Whether the board is really on screen.
 *
 * The in-memory status alone is not evidence: a window can die between ticks
 * (a reload, a crash, a stale launch) and leave `status: 'running'` pointing at
 * nothing, which reads as "drawn" while the desktop stays empty. So the pid file
 * the window publishes is the authority, and a dead pid takes the stale state
 * with it.
 */
async function overlayRunning() {
  const pid = await publishedPid()
  if (pid <= 0) {
    if (overlay.status === 'running') overlay = { status: 'stopped', pid: 0, error: '' }
    return false
  }
  if (!alivePid(pid)) {
    await rm(windowFile(), { force: true }).catch(() => {})
    overlay = { status: 'stopped', pid: 0, error: '' }
    return false
  }
  if (overlay.status !== 'running' || overlay.pid !== pid) overlay = { status: 'running', pid, error: '' }
  return true
}

/**
 * Start the island, a program of its own.
 *
 * It is deliberately not part of the board: the board is a full-screen
 * click-through overlay whose whole job is drawing, and the island is a small
 * readout with a button that must receive clicks. They share two files and
 * nothing else, so a fault in either cannot take the other down, and either can
 * be restarted alone.
 *
 * Safe to call repeatedly: the island exits on its own when it finds another one
 * running (it holds a mutex), so a duplicate spawn costs nothing.
 */
let islandStarted = false
/**
 * Whether the island was told to stop.
 *
 * The pill's own stop button writes stop.txt; the Host clears it when it consumes
 * the request. A deliberate stop must not be undone by the watchdog, or the button
 * would appear to do nothing.
 */
let islandStopped = false
/** Watchdog that keeps the pill alive; see startIslandWatchdog. */
let islandWatchdog = null
/** Consecutive restart attempts that did not produce a live island. */
let islandFailures = 0
/** When the last restart was attempted, for the backoff. */
let islandLastTry = 0
function startIsland() {
  if (islandStarted) return
  islandStarted = true
  try {
    const script = join(HERE, 'tools', 'island.py')
    if (!existsSync(script)) {
      islandStarted = false
      return
    }
    const child = spawn(pythonwExe(), [
      script,
      '--status', statusFile(),
      '--stop-signal', stopFile(),
      '--labels', labelsFile(),
      '--log', islandLogFile(),
    ], { windowsHide: true, stdio: 'ignore', detached: true })
    child.once('error', () => { islandStarted = false })
    child.unref()
    // Watch it from now on: the pill is a separate process and can die without
    // anyone asking it to. A deliberate stop sets islandStopped and the watchdog
    // respects that.
    islandStopped = false
    startIslandWatchdog()
  } catch {
    islandStarted = false
  }
}

/**
 * The pid the island published in its lock file, or 0.
 *
 * The island claims island.lock with O_EXCL and writes its own pid, so this is the
 * only durable answer to "is a pill alive right now". Checking a flag the Host set
 * at spawn time is not enough: the process can die for any reason and the flag
 * would still say yes.
 */
async function islandPid() {
  const raw = await readFileOrNull(join(dir(), 'island.lock'))
  if (raw === null) return 0
  const pid = Number.parseInt(raw.trim().split(',')[0], 10)
  return Number.isInteger(pid) && pid > 0 ? pid : 0
}

/**
 * Bring the island back when it dies on its own.
 *
 * WHY THIS EXISTS: the pill is a separate process and nothing watched it. When it
 * exited -- a stale stop.txt, a crash, a killed task -- it stayed gone for hours
 * while the Host kept writing status.json, so the user saw "the island stopped
 * showing" with no way to get it back except a reload.
 *
 * A deliberate stop is respected: pressing the pill's stop button sets
 * islandStopped, and the watchdog leaves it alone until the next start.
 */
function startIslandWatchdog() {
  if (islandWatchdog !== null) return
  islandWatchdog = setInterval(() => { void islandWatchdogTick() }, 5000)
  if (typeof islandWatchdog.unref === 'function') islandWatchdog.unref()
}

function stopIslandWatchdog() {
  if (islandWatchdog === null) return
  clearInterval(islandWatchdog)
  islandWatchdog = null
}

async function islandWatchdogTick() {
  if (islandStopped) return 'stopped'
  const pid = await islandPid()
  if (pid > 0 && alivePid(pid)) {
    islandFailures = 0
    return 'healthy'
  }
  // Back off after repeated failures: an island that cannot start (missing
  // runtime, a broken environment) must not be retried every five seconds forever.
  const wait = Math.min(60, 5 * (islandFailures + 1))
  if (islandFailures > 0 && Date.now() - islandLastTry < wait * 1000) return 'backoff'
  islandFailures += 1
  islandLastTry = Date.now()
  islandStarted = false
  startIsland()
  return 'restarted'
}


/**
 * Make sure the board exists, starting it when it does not.
 *
 * Every path that touches the scene calls this first: painting into a file no
 * window is watching looks like success and shows the user nothing.
 */
async function ensureOverlay() {
  if (await overlayRunning()) return true
  await startOverlay()
  return await overlayRunning()
}

/**
 * Bring the board back when it dies on its own.
 *
 * WHY THIS EXISTS: the board can exit without anyone asking it to -- it used to
 * quit whenever the Host's heartbeat went stale for six seconds, which a blocked
 * event loop could cause. When it went, the whale went too, because the whale
 * outlives the board only as long as the board is there to keep an eye on it, and
 * nothing was watching the board itself. The plugin stayed alive with a fresh
 * heartbeat and both pencil processes dead, which is exactly what the user saw:
 * the whale simply stopped existing.
 *
 * The board owns the whale's life, so reviving the board revives the whale. A
 * user-requested stop sets overlayWanted = false and is left alone.
 *
 * Backoff on repeated failure: a board that cannot start (missing runtime, a
 * broken environment) must not be retried every few seconds forever.
 */
/**
 * One watchdog pass: revive the board if it should be running and is not.
 *
 * Split out from the interval so it can be tested directly -- the decision is
 * the part worth verifying, and an interval callback is not callable.
 */
async function overlayWatchdogTick() {
  if (!overlayWanted) return 'not-wanted'
  if (overlay.status === 'starting') return 'starting'
  if (await overlayRunning()) {
    overlayFailures = 0
    return 'healthy'
  }
  const wait = Math.min(60, 5 * (overlayFailures + 1))
  if (overlayFailures > 0 && Date.now() - (overlay.lastTryAt ?? 0) < wait * 1000) {
    return 'backoff'
  }
  overlayFailures += 1
  const now = Date.now()
  overlay = { ...overlay, lastTryAt: now }
  try {
    await startOverlay()
  } catch { /* reported through overlay.status */ }
  return 'restarted'
}

function startOverlayWatchdog() {
  if (overlayWatchdog !== null) return
  overlayWatchdog = setInterval(() => { void overlayWatchdogTick() }, 5000)
  if (typeof overlayWatchdog.unref === 'function') overlayWatchdog.unref()
}

function stopOverlayWatchdog() {
  if (overlayWatchdog === null) return
  clearInterval(overlayWatchdog)
  overlayWatchdog = null
}

/** The tray and the board in one process, launched so it keeps its own station. */
function startOverlay() {  overlayWork = overlayWork.then(async () => {
    if (await overlayRunning()) return
    if (process.platform !== 'win32') {
      overlay = { status: 'error', pid: 0, error: 'unsupported-platform' }
      return
    }
    overlay = { status: 'starting', pid: 0, error: '' }
    await mkdir(dir(), { recursive: true })
    // An erase the user made before this window existed must not come back: the
    // scene handed to a fresh window is the scene minus everything already gone.
    await pruneErased()

    // The board script is inside the plugin, so a missing one means the
    // installation is damaged -- say so before spending 20 s on a timeout.
    const boardScript = join(HERE, 'tools', 'board.py')
    if (!existsSync(boardScript)) {
      overlay = { status: 'error', pid: 0, error: `tools/board.py missing` }
      return
    }
    if (!existsSync(BUNDLED_PYTHONW) && !existsSync(BUNDLED_PYTHON)) {
      // Not a failure: the plugin ships code only, and the Python environment is
      // installed from the panel's dependency tab. Saying "出错" here told the
      // user something was broken when nothing was -- it just was not set up yet.
      overlay = {
        status: 'error',
        pid: 0,
        error: 'deps-missing',
        hint: '需要先在「依赖」页下载运行环境',
      }
      return
    }

    await writeAtomic(labelsFile(), JSON.stringify({
      pencil: '拿铅笔',
      call: '呼唤 agent',
      clear: '一键清除',
      exitDraw: '退出作画',
      quit: '退出',
      trayTitle: "User's Pencil",
      widthSuffix: 'px',
      islandThinking: '思考中',
      islandTool: '调用',
      islandRead: '读取',
      islandStop: '停止 agent',
      // The floating ball replaced the tray icon and the hotkey.
      ball: '神笔鲸良：左键开始绘制，右键菜单',
      textBox: '文本框',
      selectText: '选择文字',
      editText: '编辑文字',
      deleteText: '删除',
    }, null, 2))
    // Start from a clean slate: a status left over from a previous run would make
    // the island claim work that is not happening.
    await writeAtomic(statusFile(), JSON.stringify(islandStatus)).catch(() => {})
    await writeAtomic(sceneFile(), JSON.stringify({ ...scene, revision }, null, 2) + '\n')
    await rm(windowFile(), { force: true }).catch(() => {})
    startHeartbeat()
    startClearWatcher()
    startEraseWatcher()
    startSummonWatcher()

    // The board is a Python/tkinter overlay launched with the bundle's own
    // pythonw.exe: no shell, no console window, no PowerShell.
    //
    // WARNING: pythonw.exe, not python.exe. python.exe is a console-subsystem
    // program, so Windows gives it a console and a taskbar button appears for
    // it; pythonw has no console at all. The WPF board needed a `cmd /c start`
    // dance for the same reason (a directly-started PowerShell shared the Host's
    // window station and its window was torn down with it) -- a separate
    // process started with detached:true does not have that problem.
    const launcher = spawn(pythonwExe(), [
      join(HERE, 'tools', 'board.py'),
      '--scene', sceneFile(),
      '--users', userFile(),
      '--alive', aliveFile(),
      '--window', windowFile(),
      '--commands', commandsFile(),
      '--clear-signal', clearFile(),
      '--labels', labelsFile(),
      '--status', statusFile(),
      '--stop-signal', stopFile(),
      '--log', boardLogFile(),
    ], { windowsHide: true, stdio: 'ignore', detached: true })
    launcher.once('error', (error) => {
      stopHeartbeat()
      overlay = { status: 'error', pid: 0, error: String(error?.message ?? error) }
    })
    launcher.unref()

    // The island is its own program, started here and never owned by the board.
    // It reads status.json and writes stop.txt; that is the whole contract, so
    // either side can be restarted without disturbing the other.
    startIsland()

    const deadline = Date.now() + START_MS
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, START_POLL_MS))
      const pid = await publishedPid()
      if (pid > 0) {
        overlay = { status: 'running', pid, error: '' }
        overlayWanted = true
        overlayFailures = 0
        return
      }
    }
    stopHeartbeat()
    overlay = { status: 'error', pid: 0, error: 'start-timeout' }
  }).catch((error) => {
    stopHeartbeat()
    overlay = { status: 'error', pid: 0, error: String(error?.message ?? error) }
  })
  return overlayWork.then(() => overlay)
}

function stopOverlay() {
  // The user asked for this. The watchdog must not undo it.
  overlayWanted = false
  overlayFailures = 0
  overlayWork = overlayWork.then(async () => {
    stopHeartbeat()
    stopClearWatcher()
    stopEraseWatcher()
    stopSummonWatcher()
    const pid = overlay.pid > 0 ? overlay.pid : await publishedPid()
    const deadline = Date.now() + 4000
    while (Date.now() < deadline) {
      if (pid <= 0 || !alivePid(pid)) break
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
    if (pid > 0 && alivePid(pid)) {
      try { process.kill(pid) } catch { /* nothing left */ }
    }
    overlay = { status: 'stopped', pid: 0, error: '' }
    await rm(aliveFile(), { force: true }).catch(() => {})
    await rm(windowFile(), { force: true }).catch(() => {})
    await stopBall()
  }).catch(() => {})
  return overlayWork.then(() => overlay)
}

function overlayView() {
  return { status: overlay.status, error: overlay.error, pid: overlay.pid, revision }
}

/**
 * End the whale ball child process, if one is running.
 *
 * pencil.ps1 starts and watches the ball, and normally stops it on the way out.
 * That path needs the window to close cleanly — a killed board never runs its
 * `finally`, so the ball is checked here as well. Otherwise stopping the plugin
 * would leave a whale swimming on the desktop with nothing behind it.
 */
/**
 * ⚠ 现在**不主动杀鲸鱼**，只清掉「画板已经停了」这件事的痕迹。
 *
 * 改动原因：画板的生命周期绑在 DSH 窗口上 —— 用户把 DSH 缩到托盘、
 * 或者某个命令让窗口重建，插件就会停画板。以前这里顺手把鲸鱼也杀了，
 * 于是出现「DSH 缩托盘 → 鲸鱼消失 → 再打开 → 鲸鱼重来 → 又被杀」的
 * 反复开关（用户原话：不断重启关闭、不断重启关闭）。
 *
 * 鲸鱼的存活判据由 deskpet.py 自己负责：
 *   · DSH 窗口没了（board_watch 查 EnumWindows）→ 它自己演完动画退出
 *   · 还开着 → 它就一直在，和画板是否在跑无关
 *
 * 所以这个函数现在只做清理，不做处决。真要收工由鲸鱼自己判断。
 * 保留函数名是因为调用点还散在几处，改成空实现比删掉调用更安全。
 */
async function stopBall() {
  // 只删过期的状态记录，不动进程。
  // （留着它会误导下次启动：以为有鲸鱼在跑而不再拉起。）
  const file = join(dir(), 'ball', 'ball-status.json')
  const raw = await readFileOrNull(file)
  if (raw === null) return
  let record
  try {
    record = JSON.parse(raw)
  } catch {
    await rm(file, { force: true }).catch(() => {})
    return
  }
  const pid = Number(record?.pid ?? 0)
  // 进程真的没了才删记录；还活着就留着，下次复用。
  if (!Number.isInteger(pid) || pid <= 0 || !alivePid(pid)) {
    await rm(file, { force: true }).catch(() => {})
  }
}

// ── annotate ──────────────────────────────────────────────────────────────────

/**
 * Composite marks onto an image and write a new PNG.
 *
 * Runs as a separate PowerShell: System.Drawing is already part of Windows, so
 * this needs no image library in Node, and the source file is never touched.
 * The spec travels as a file rather than on stdin, because a pipe is the wrong
 * channel for non-ASCII text.
 */
async function annotate(source, items, output, unit) {
  const spec = JSON.stringify({ source, output, unit: unit === 'window' ? 'window' : 'screen', items }, null, 2)
  const specFile = join(dir(), `annotate-${process.pid}-${Date.now()}.json`)
  await writeAtomic(specFile, spec)
  try {
    return await new Promise((resolve, reject) => {
      const child = spawn(pythonExe(), [
        join(HERE, 'tools', 'annotate.py'), '--spec', specFile,
      ], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
      let out = ''
      let err = ''
      const timer = setTimeout(() => {
        try { child.kill() } catch { /* gone */ }
        reject(new Error('annotate timed out'))
      }, 60000)
      child.stdout.on('data', (d) => { out += d })
      child.stderr.on('data', (d) => { err += d })
      child.once('error', (error) => { clearTimeout(timer); reject(error) })
      child.once('exit', (code) => {
        clearTimeout(timer)
        if (code === 0) resolve(out.trim())
        else reject(new Error((err || out || `annotate exited ${code}`).slice(0, 600)))
      })
    })
  } finally {
    await rm(specFile, { force: true }).catch(() => {})
  }
}

// ── skills ────────────────────────────────────────────────────────────────────

/**
 * The two skills that explain the board to a fresh agent.
 *
 * Their bodies live beside this file so they can change without touching logic,
 * and are read once at startup: a skill that cannot be read is skipped rather
 * than failing the plugin.
 */
const SKILLS = [
  {
    name: 'pencil-board',
    description: 'How to read the shared desktop board: red marks are the user. Drawing is allowed only when this turn authorises it.',
    whenToUse: 'When reading the board, or when this turn explicitly asks for a mark on screen.',
    path: join(MODULE_DIR, 'skills', 'pencil-board.md'),
  },
  {
    name: 'pencil-summon',
    description: 'What a tray summon means and how to answer the screen it sends: read the board, then draw back on it.',
    whenToUse: 'When a message arrives with 【托盘呼唤】 and a screenshot.',
    path: join(MODULE_DIR, 'skills', 'pencil-summon.md'),
  },
  {
    name: 'pencil-act',
    description: '托盘「呼唤 agent」截图的标准流程：一条 find { method:"act" } 跑完 OCR+UI+差分，只看 act.png，再按网格读数发给 pencil draw。',
    whenToUse: '收到带 【托盘呼唤】 的消息、里面附了整屏截图时。区别于 pict act（用户直接发图 → dsh-companion）。',
    path: join(MODULE_DIR, 'skills', 'pencil-act.md'),
  },
  {
    name: 'catch',
    description: '我自己截一张当前屏幕：find { method:"act" } 不传 image，程序自截 → OCR+UI+坐标网格 → act.png，用完即删。',
    whenToUse: '用户明确让我看当前屏幕、或问题必须看桌面才能答时。没有明确请求不要截。',
    path: join(MODULE_DIR, 'skills', 'catch.md'),
  },
].map((skill) => {
  let content = ''
  try {
    content = readFileSync(skill.path, 'utf8')
  } catch {
    content = ''
  }
  return { ...skill, content }
}).filter((skill) => skill.content !== '')

// ── the island's status ──────────────────────────────────────────────────────

/**
 * What the island shows.
 *
 * `busy` is false when no agent is running, which is what makes the island
 * disappear: the hint is only worth screen space while there is work happening.
 * `tool` names the call in flight, so "calling pencil" and "thinking" are
 * distinguishable instead of one vague spinner.
 */
let islandStatus = {
  busy: false,
  phase: 'idle',      // idle | thinking | tool | read
  tool: '',
  detail: '',
  since: 0,
  updated: 0,
  // The conversation this work belongs to. The island shows it in the brief
  // expanded beat when it appears, so the user can tell at a glance which chat
  // the agent is busy with.
  conversation: '',
}

/** Pending write, coalesced so a burst of events costs one file write. */
let statusWrite = null

function publishStatus(patch) {
  islandStatus = {
    ...islandStatus,
    ...patch,
    updated: Date.now(),
  }
  if (statusWrite !== null) return
  // Coalesce: many events arrive together, and the window polls anyway.
  statusWrite = setTimeout(() => {
    statusWrite = null
    const body = JSON.stringify(islandStatus)
    void writeAtomic(statusFile(), body).catch(() => {})
  }, 60)
  if (typeof statusWrite.unref === 'function') statusWrite.unref()
}

/** A tool name worth showing: trimmed, and never an empty bubble. */
function toolLabel(name) {
  if (typeof name !== 'string') return ''
  const clean = name.trim()
  if (clean === '') return ''
  return clean.length > 40 ? `${clean.slice(0, 40)}…` : clean
}

/**
 * Tools that read rather than act.
 *
 * The distinction matters to someone glancing at the island: "reading" means the
 * agent is looking at something and the next thing that happens is its answer;
 * any other call means it is changing or producing something. The list is by
 * name prefix so a plugin's own reader-bots are covered too.
 */
const READ_TOOL_PATTERNS = [
  /^read$/iu,
  /^read_/iu,
  /_read$/iu,
  /^eye$/iu,
  /^glob$/iu,
  /^grep$/iu,
  /^search/iu,
  /^web_fetch$/iu,
  /^web_search$/iu,
  /^list_/iu,
  /_list$/iu,
  /^session_search$/iu,
  /^session_event_search$/iu,
  /^session_trace$/iu,
  /^inspect/iu,
]

/** Whether this tool call is a read rather than an action. */
function isReadTool(name) {
  if (typeof name !== 'string') return false
  return READ_TOOL_PATTERNS.some((pattern) => pattern.test(name))
}

/**
 * Follow every running agent and publish what it is doing.
 *
 * Several agents can run at once, so the state is derived from a set rather than
 * one variable: a tool finishing in one session must not clear the island while
 * another session is still working.
 */
const runningAgents = new Set()
/**
 * Session ids in the order they started running.
 *
 * The stop button needs a session to cancel, and the island only shows a name, so
 * the running set alone is not enough. First-in is the one the island is
 * describing, which is the one a stop should act on.
 */
const runningOrder = []
/**
 * Tools in flight, keyed by agent id.
 *
 * The value carries the display label and whether the call reads or acts, so the
 * island can say "reading eye" instead of one flat "calling".
 */
const activeTools = new Map()

/**
 * The conversation label, cached per session.
 *
 * WARNING: this used to be a single `cachedConversation` string that was only
 * written when a label was found. An empty result therefore cached nothing, so
 * the very next agent event re-ran the lookup -- and the lookup decompresses
 * session logs SYNCHRONOUSLY, on the Node event loop, for every session on the
 * machine. Measured: 57 sessions / 33 MB of zstd / 94 MB of text / 1089 ms of
 * blocked event loop, repeated on every tool call.
 *
 * That is what killed the whale. The plugin's heartbeat is a `setInterval`, so a
 * blocked event loop stops it; the board watches the heartbeat, declared it stale
 * after six seconds, and exited; the whale was watching the board's heartbeat and
 * followed it out. The user saw a whale that vanished when the agent looked at an
 * image, and the two symptoms had one cause.
 *
 * So: every outcome is cached, including "there is no label". A conversation's
 * first message never changes, and a session with no title never grows one.
 */
const conversationCache = new Map()
const conversationPending = new Set()

function conversationNow() {
  const id = activeSession !== '' ? activeSession : sessionId
  if (id === '') return ''
  const cached = conversationCache.get(id)
  if (cached !== undefined) return cached
  if (conversationPending.has(id)) return ''
  conversationPending.add(id)
  void conversationLabel(id)
    .then((label) => { conversationCache.set(id, label) })
    .catch(() => { conversationCache.set(id, '') })
    .finally(() => { conversationPending.delete(id) })
  return ''
}

function refreshStatus() {
  // The conversation label rides along with every publish, so the island's
  // expanded beat can name the chat without a second channel.
  const conversation = conversationNow()
  if (runningAgents.size === 0) {
    if (islandStatus.busy) publishStatus({ busy: false, phase: 'idle', tool: '', detail: '', since: 0, conversation })
    return
  }
  if (activeTools.size > 0) {
    // A tool whose completion never arrived would otherwise pin the island to it
    // indefinitely. Nothing legitimately runs this long, and showing a stale tool
    // is worse than showing "thinking".
    const now = Date.now()
    for (const [key, entry] of [...activeTools]) {
      if (Number.isFinite(entry.at) && now - entry.at > STALE_TOOL_MS) activeTools.delete(key)
    }
  }
  if (activeTools.size > 0) {
    const entry = [...activeTools.values()][0]
    const phase = entry.read === true ? 'read' : 'tool'
    if (islandStatus.phase !== phase || islandStatus.tool !== entry.label
        || islandStatus.conversation !== conversation) {
      publishStatus({ busy: true, phase, tool: entry.label, detail: '', since: entry.at ?? Date.now(), conversation })
    }
    return
  }
  if (islandStatus.phase !== 'thinking' || islandStatus.conversation !== conversation) {
    publishStatus({ busy: true, phase: 'thinking', tool: '', detail: '', since: Date.now(), conversation })
  }
}

/**
 * The conversation's title, as DSH shows it in the sidebar.
 *
 * That title is `sessions.list.getSnapshot().byId[id].title` -- a dictionary, not
 * an array, which is why an earlier version looking for it in a list always came
 * back empty and the pill showed "thinking" where the name belonged.
 *
 * A blank (never-messaged) session has no title, so the first user message is the
 * fallback: it is what the user would call the chat anyway.
 */
async function conversationLabel(sessionId) {
  const id = typeof sessionId === 'string' && sessionId !== ''
    ? sessionId
    : (activeSession !== '' ? activeSession : sessionId)
  if (typeof id !== 'string' || id === '') return ''
  const title = sessionTitleOf(id)
  if (title !== '') return title
  try {
    const text = await firstUserText(id)
    if (text !== '') return text
  } catch { /* fall through */ }
  return ''
}

/** The Host's own title for a session, or '' when it has none yet. */
function sessionTitleOf(id) {
  try {
    const sessions = sessionsOf()
    const snapshot = sessions?.list?.getSnapshot?.()
    const byId = snapshot?.byId
    if (byId === null || typeof byId !== 'object') return ''
    const entry = byId[id]
    const title = typeof entry?.title === 'string' ? entry.title.trim() : ''
    if (title === '') return ''
    return title.length > 34 ? `${title.slice(0, 34)}…` : title
  } catch {
    return ''
  }
}

/**
 * The first thing the user said in a session, trimmed to one short line.
 *
 * WARNING: this reads the HEAD of the log, not the whole file, and that is the
 * point. The first user message is at the beginning; decompressing the rest is
 * pure cost, and the decompression is synchronous -- it blocks the Node event
 * loop, which stops the heartbeat, which kills the board and the whale. A
 * 256 KB prefix is far more than the first message needs and keeps the work
 * bounded no matter how long the conversation has grown.
 */
const TITLE_PREFIX_BYTES = 262144

async function firstUserText(sessionId) {
  const root = sessionsRoot()
  const workspaces = await readdir(root, { withFileTypes: true }).catch(() => [])
  for (const workspace of workspaces) {
    if (!workspace.isDirectory()) continue
    const file = join(root, workspace.name, `session-${sessionId}`, 'session.v4.jsonl.zstd')
    let handle
    try {
      handle = await open(file, 'r')
    } catch {
      continue
    }
    let buf
    try {
      // Read the head only. A short read is normal and expected for a new
      // conversation; a long one is truncated on purpose.
      const size = (await handle.stat()).size
      const want = Math.min(size, TITLE_PREFIX_BYTES)
      const out = Buffer.alloc(want)
      const { bytesRead } = await handle.read(out, 0, want, 0)
      buf = out.subarray(0, bytesRead)
    } catch {
      continue
    } finally {
      await handle.close().catch(() => {})
    }
    const text = readSessionText(buf)
    for (const line of text.split(/\r?\n/u)) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('{')) continue
      let event
      try { event = JSON.parse(trimmed) } catch { continue }
      if (event?.type !== 'user/message') continue
      const content = event?.data?.content
      if (!Array.isArray(content)) continue
      for (const item of content) {
        if (item?.type !== 'text') continue
        const said = typeof item.text === 'string' ? item.text.trim() : ''
        if (said === '') continue
        // One line: a title is a label, not a paragraph.
        const oneLine = said.split(/\r?\n/u)[0].trim()
        return oneLine.length > 34 ? `${oneLine.slice(0, 34)}…` : oneLine
      }
    }
  }
  return ''
}

// ── the tool ─────────────────────────────────────────────────────────────────

const TOOL_DESCRIPTION = [
  '适用范围（先读这段再决定用不用本工具）：',
  '',
  '本工具画在【桌面屏幕】上 —— 用户实时看得到，标记留在屏幕上。',
  '',
  '✅ 用 pencil 的场合：',
  '  * 用户要求在桌面上画 / 标一下 / 圈出来（画在屏幕上，不在图片文件里）',
  '  * 【托盘呼唤】带来的整屏截图，要在屏幕上作答',
  '',
  '❌ 不要用 pencil 的场合：',
  '  * 交付物是一张【图片文件】（用户发图要求标注、要产出标好的图）',
  '    → 用 mark（字段是 shapes+kind，不是 items+type）',
  '  * 普通提问、改代码、讨论规则 → 只回话，不画',
  '',
  '⚠ 两个绘图工具字段不同，禁止混用：pencil 用 items+type，mark 用 shapes+kind。',
  '',
  "神笔鲸良 (Whale's Pencil): draw on the desktop, above every program, in any colour.",
  'The user watches the screen, so what you paint is visible immediately and stays',
  'until erased. The pointer passes through the marks, so they never block a click.',
  '',
  'Read red as the user. Red strokes and red text on the board are what the user',
  'drew and typed with the tray pencil, so treat them as the user talking to you.',
  'A circled area means "this", an arrow means "that way", red text is their question.',
  'Your own marks are never red.',
  '',
  'DRAW ONLY ON DEFICIT INSTRUCTION. Before calling `draw`, the current user message',
  'must itself authorise it, in one of exactly three ways:',
  '  1. it explicitly asks you to draw on the screen ("画在屏幕上", "标一下", "圈出来")',
  '  2. it attaches an image AND asks for annotation / location ("找出图中的马")',
  '  3. it opens with 【托盘呼唤】 — the user rang the bell from the board, which is a',
  '     request to answer on the board',
  '',
  'Anything else — a plain question, a code change, a discussion about rules, or your',
  'own sense that a diagram would help — means do NOT draw. Answer in chat only.',
  'A permission from an earlier turn does not carry over. Saying "ok, I will remember"',
  'grants nothing: that promise is about editing skills and code, not about drawing.',
  '',
  'Once drawing is authorised, keep colours few and consistent, and contrast with what',
  'is underneath: one or two hues per annotation, and a colour only when it carries',
  'meaning. A mark that blends into the background is a mark that was not drawn.',
  '',
  'Marks only reach the screen through the board window. `draw` checks that window',
  'and starts it when it is down, and every result carries `onScreen`: false means',
  'the marks exist but nobody can see them, so say so instead of claiming success.',
  '',
  'Actions:',
  '  draw      append primitives (see `items`); brings the board up if needed.',
  '            Returns `drawn` ids and `bounds`, so a later erase can name them.',
  '  erase     remove marks. `ids` removes exactly those primitives; `rects`',
  '            removes EVERY mark overlapping each rect, including the user\'s',
  '            strokes and your own fresh work — prefer ids for a targeted fix,',
  '            and check the returned `erasedCount` against what you intended.',
  '  scene     read the board: your primitives, the user\'s strokes and texts',
  '  annotate  composite marks onto an image file and return a new PNG path',
  '  overlay   start or stop the desktop window ("start" | "stop")',
  '',
  'Primitives (screen = desktop pixels, the default; window = percent of screen).',
  'Every result reports `screen` with this machine\'s real resolution and origin,',
  'read from the OS: never assume 1920x1080, and never rescale coordinates taken',
  'from a screenshot — the screenshot and the board share one coordinate space.',
  '  {type:"rect", x,y,w,h, stroke, fill, width, radius}',
  '  {type:"ellipse", x,y,w,h, stroke, fill, width}',
  '  {type:"path", points:[[x,y],...], stroke, width}   freehand stroke',
  '  {type:"arrow", x1,y1,x2,y2, stroke, width, head}',
  '  {type:"text", x,y, text, size, color, align, weight}',
  '  {type:"image", x,y,w,h, src:"data:image/png;base64,..."}',
  '  {type:"pixels", x,y,w,h, data:"<base64 RGBA>"}      every pixel chosen by you',
  '',
  'FORMAT IS STRICT — an invalid primitive is dropped SILENTLY, and a batch where',
  'every primitive is invalid fails with "needs at least one drawable primitive".',
  'Verified against the validator, item by item:',
  '  * coordinates as STRINGS ("x":"530") are dropped — send JSON numbers (x:530)',
  '  * invented types ("circle") are dropped — the seven names are exact:',
  '    rect, ellipse, path, arrow, text, image, pixels (a circle is an ellipse',
  '    with w==h)',
  '  * text with an empty string is dropped — write the words or skip the item',
  '  * everything else is lenient: w/width and h/height are equivalent, path',
  '    points accept [[x,y],...] or [{"x":..,"y":..},...], colours accept #hex,',
  '    rgba(...) and English names',
  '  * fields placed outside the item are lost — every primitive goes INSIDE',
  '    items:[{...}]',
  'Before sending: every type one of the seven? every coordinate a bare number?',
  'If draw fails with that error, FIX THE FORMAT and resend — do not switch tools,',
  'do not start analysing screenshots.',
  '',
  'Use `annotate` when the user sends an image and wants it back with your marks:',
  'it renders onto the picture itself instead of the screen. Results go to the',
  'plugin\'s "Processed image" folder, never beside the original and never into',
  'the plugin root, so output stays in one predictable place.',
  '',
  'Coordinates are used exactly as given — there is no scaling anywhere.',
  'Read numbers off the coordinate grid drawn on act.png (every 100 px, labelled',
  'on all four edges): those numbers ARE real desktop pixels, so whatever you read',
  'is what gets drawn. Do not report a picture size, do not multiply by a factor.',
].join('\n')

function registerPencilTool(ctx) {
  const registry = typeof ctx.get === 'function' ? ctx.get('tools') : ctx.tools
  if (registry === undefined || typeof registry.register !== 'function') return

  ctx.effect(() => registry.register({
    name: 'pencil',
    description: TOOL_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        action: {
          type: 'string',
          enum: ['draw', 'erase', 'scene', 'annotate', 'overlay'],
          description: 'draw, erase, read the board, annotate an image, or start/stop the window.',
        },
        items: {
          type: 'array',
          description: 'Primitives to append (draw) or to stamp onto an image (annotate).',
          items: { type: 'object', additionalProperties: true },
        },
        ids: {
          type: 'array',
          description: 'Primitive ids to erase.',
          items: { type: 'string' },
        },
        rects: {
          type: 'array',
          description: 'Erase anything overlapping these screen rectangles: [{x,y,w,h}].',
          items: { type: 'object', additionalProperties: true },
        },
        unit: {
          type: 'string',
          enum: ['screen', 'window'],
          description: 'Coordinate unit. Defaults to screen (desktop pixels).',
        },
        source: {
          type: 'string',
          description: 'For annotate: the image file to draw on.',
        },
        output: {
          type: 'string',
          description: 'For annotate: exact path to write. Defaults to the plugin\'s "Processed image" folder.',
        },
        overlay: {
          type: 'string',
          enum: ['start', 'stop'],
          description: 'For action overlay: start or stop the desktop window.',
        },
      },
      required: ['action'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          revision: { type: 'integer' },
          agentMarks: { type: 'integer' },
          userStrokes: { type: 'integer' },
          userTexts: { type: 'integer' },
          overlay: { type: 'string' },
          onScreen: { type: 'boolean' },
          output: { type: 'string' },
          layers: { type: 'array', items: { type: 'object', additionalProperties: true } },
          strokes: { type: 'array', items: { type: 'object', additionalProperties: true } },
          texts: { type: 'array', items: { type: 'object', additionalProperties: true } },
          hidden: { type: 'array', items: { type: 'string' } },
          drawn: { type: 'array', items: { type: 'string' } },
          drawnCount: { type: 'integer' },
          dropped: { type: 'integer' },
          bounds: { type: 'array', items: { type: 'object', additionalProperties: true } },
          erased: { type: 'array', items: { type: 'string' } },
          erasedCount: { type: 'integer' },
          requested: { type: 'object', additionalProperties: true },
        },
      },
      render: (_args, value) => {
        if (value.output !== undefined && value.output !== '') {
          return [{ type: 'text', text: `pencil: wrote ${value.output}` }]
        }
        // The screen size and the landed bounds belong in the readable text: a
        // model that only sees a count cannot notice that its marks went to the
        // wrong place, which is exactly how a coordinate error survives.
        const parts = [
          `${value.agentMarks} agent mark(s)`,
          `${value.userStrokes} user stroke(s)`,
          `${value.userTexts ?? 0} user text(s)`,
          value.onScreen === false ? 'NOT ON SCREEN — the board window is down' : 'on screen',
        ]
        if (value.screen !== undefined && value.screen !== null) {
          parts.push(`screen ${value.screen.w}x${value.screen.h}`)
        }
        if (value.drawnCount !== undefined) {
          parts.push(`drew ${value.drawnCount}: ${(value.drawn ?? []).join(', ')}${value.dropped > 0 ? ` (${value.dropped} dropped, board full)` : ''}`)
        }
        if (Array.isArray(value.bounds) && value.bounds.length > 0) {
          const boxes = value.bounds
            .slice(0, 8)
            .map((b) => `${Math.round(b.x)},${Math.round(b.y)} ${Math.round(b.w)}x${Math.round(b.h)}`)
          parts.push(`landed at ${boxes.join(' | ')}${value.bounds.length > 8 ? ` (+${value.bounds.length - 8} more)` : ''}`)
        }
        if (value.erasedCount !== undefined) {
          parts.push(`erased ${value.erasedCount}${value.erasedCount > 0 ? `: ${(value.erased ?? []).join(', ')}` : ''}`)
        }
        return [{ type: 'text', text: `pencil: ${parts.join('; ')}.` }]
      },
    },
    async execute(args, exec) {
      await ready
      // Remember who asked, so a tray summon knows which session to wake.
      const caller = exec?.agent?.id
      if (typeof caller === 'string' && caller !== '') sessionId = caller
      const action = typeof args?.action === 'string' ? args.action : ''
      const summary = async () => {
        const user = await readUser()
        return {
          revision,
          agentMarks: scene.layers.length,
          userStrokes: user.strokes.length,
          userTexts: user.texts.length,
          overlay: overlay.status,
          // Marks live in a file until a live window renders them; this is the
          // difference between "I painted" and "the user can see it".
          onScreen: await overlayRunning(),
          // The coordinate space every `unit: "screen"` value lives in. Reported
          // from the OS, so a different monitor or scale answers for itself.
          screen: screenSize(),
        }
      }

      if (action === 'scene') {
        const user = await readUser()
        return {
          ...(await summary()),
          unit: scene.unit,
          layers: scene.layers,
          // What the user drew and typed, in the same units as the scene.
          strokes: user.strokes.map((s) => ({ id: s.id, width: s.width, points: s.points })),
          // Numbered in reading order, same numbers the summon message used.
          texts: userTextBoxes(user),
          hidden: user.hidden,
        }
      }

      if (action === 'overlay') {
        if (args?.overlay === 'stop') await stopOverlay()
        else if (args?.overlay === 'start') await startOverlay()
        else throw new Error('overlay needs `overlay: "start"` or `"stop"`')
        return summary()
      }

      if (action === 'annotate') {
        const source = typeof args?.source === 'string' ? args.source : ''
        if (source === '') throw new Error('annotate needs `source` (an image file path)')
        // 坐标原样使用，不做任何缩放（理由同 draw：见下面的注释）。
        const items = (Array.isArray(args?.items) ? args.items : [])
          .map(sanitizeItem)
          .filter(Boolean)
        if (items.length === 0) throw new Error('annotate needs at least one drawable primitive in `items`')
        const output = resolveOutputPath(source, args?.output)
        const written = await annotate(source, items, output, args?.unit)
        return { ...(await summary()), output: written }
      }

      if (action === 'erase') {
        const ids = Array.isArray(args?.ids) ? args.ids.map(String) : []
        const rects = (Array.isArray(args?.rects) ? args.rects : [])
          .map((r) => ({
            x: bnum(r?.x, -1e6, 1e6),
            y: bnum(r?.y, -1e6, 1e6),
            w: bnum(r?.w ?? r?.width, 0, 1e6),
            h: bnum(r?.h ?? r?.height, 0, 1e6),
          }))
          .filter((r) => r.x !== undefined && r.y !== undefined && r.w !== undefined && r.h !== undefined)
        if (ids.length === 0 && rects.length === 0) throw new Error('erase needs `ids` or `rects`')

        const before = scene.layers.length
        const removed = []
        const kept = scene.layers.filter((item) => {
          if (ids.includes(item.id)) { removed.push(item.id); return false }
          const box = boxOf(item)
          if (box !== null && rects.some((r) => overlaps(box, r))) { removed.push(item.id); return false }
          return true
        })
        if (kept.length !== before) {
          scene = { ...scene, layers: kept }
          revision += 1
          await saveScene()
        }
        // The user's strokes belong to the window; it applies the same erase.
        await sendCommand({ ids, rects })
        // Report what actually disappeared. Without this the caller only sees the
        // total drop and cannot tell an intended removal from an over-wide rect.
        return { ...(await summary()), erased: removed, erasedCount: removed.length, requested: { ids, rects } }
      }

      if (action !== 'draw') throw new Error(`unknown pencil action ${JSON.stringify(action)}`)

      const raw = Array.isArray(args?.items) ? args.items : []
      // 坐标一律**不缩放**，原样交给画板。
      //
      // ⚠ 这里曾经有一套 view 换算：调用方申报「我那张图多大」，程序按
      //   真实屏幕比例乘系数。那是我自己发明的，本质是在猜别人的压缩比。
      //
      //   现在 act.png 上画了坐标网格，调用方读到的数字**就是真实屏幕
      //   坐标**（网格是用原图像素画的）。再乘一次系数只会让它偏掉 ——
      //   读数 1225 乘 1.25 变成 1531，整体偏了 25%，正是用户反复看到的
      //   「标了但位置不对」。
      //
      //   所以：拿到什么坐标就画什么坐标。不做任何换算。
      const added = raw
        .map(sanitizeItem)
        .filter((item) => item !== null)
      if (added.length === 0) throw new Error('draw needs at least one drawable primitive in `items`')
      if (args?.unit === 'screen' || args?.unit === 'window') scene = { ...scene, unit: args.unit }
      const trimmed = [...scene.layers, ...added].slice(-4000)
      // The list keeps only the newest 4000, so report what survived rather than
      // what was sent: an id that fell off the front is not there to erase.
      const keptIds = new Set(trimmed.map((item) => item.id))
      scene = { ...scene, enabled: true, layers: trimmed }
      revision += 1
      await saveScene()
      // Paint only means "on screen" once a live window owns the scene file.
      await ensureOverlay()
      // ids let the caller undo exactly what it just drew instead of guessing a
      // rectangle and taking everything nearby with it.
      const drawnIds = added.map((item) => item.id).filter((id) => keptIds.has(id))
      return {
        ...(await summary()),
        drawn: drawnIds,
        drawnCount: drawnIds.length,
        dropped: added.length - drawnIds.length,
        bounds: added.map((item) => boxOf(item)).filter((b) => b !== null),
      }
    },
  }), 'whale-pencil: pencil tool')
}

// ── routes ────────────────────────────────────────────────────────────────────

function send(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  res.end(body)
}

async function readJsonBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new Error('body too large')
    chunks.push(chunk)
  }
  const text = Buffer.concat(chunks).toString('utf8').trim()
  if (text === '') return {}
  const parsed = JSON.parse(text)
  return parsed !== null && typeof parsed === 'object' ? parsed : {}
}

async function view() {
  const user = await readUser()
  return {
    enabled: scene.enabled,
    unit: scene.unit,
    layers: scene.layers,
    revision,
    userStrokes: user.strokes.length,
    userTexts: user.texts.length,
    ...overlayView(),
    platform: process.platform,
    // How the plugin found itself: useful when a move or rename is suspected.
    installDir: locateState.ok ? installDir : '',
    locate: { ok: locateState.ok, reason: locateState.reason, source: locateState.source ?? '' },
  }
}

async function route(req, res) {
  await ready
  const rejection = rejectionFor(req)
  if (rejection !== undefined) {
    return send(res, rejection, { error: rejection === 403 ? 'forbidden' : 'unauthorized' })
  }
  const url = new URL(req.url ?? '/', 'http://localhost')
  const path = url.pathname.slice(ROUTE.length).replace(/\/+$/u, '') || '/'
  const method = String(req.method ?? 'GET').toUpperCase()

  try {
    if (method === 'GET' && (path === '/' || path === '/state' || path === '/scene')) {
      return send(res, 200, { ...(await view()), activeSession })
    }
    // The Client half owns the answer to "which conversation is the user in":
    // it renders inside that conversation and posts its id here, and keeps
    // posting while that conversation stays open. An empty id means the user
    // left it, so the claim is dropped rather than kept as a stale answer.
    if (method === 'POST' && path === '/active-session') {
      const body = await readJsonBody(req)
      const id = typeof body.sessionId === 'string' ? body.sessionId.slice(0, 120) : ''
      if (id !== '') {
        activeSession = id
        activeSessionAt = Date.now()
      } else if (body.release === true) {
        activeSession = ''
        activeSessionAt = 0
      }
      return send(res, 200, { ok: true, activeSession })
    }
    if (method === 'POST' && path === '/draw') {
      const body = await readJsonBody(req)
      const raw = Array.isArray(body.items) ? body.items : [body]
      const added = raw.map(sanitizeItem).filter((item) => item !== null)
      if (added.length === 0) return send(res, 400, { error: 'nothing drawable' })
      if (body.unit === 'screen' || body.unit === 'window') scene = { ...scene, unit: body.unit }
      scene = { ...scene, enabled: true, layers: [...scene.layers, ...added].slice(-4000) }
      revision += 1
      await saveScene()
      await ensureOverlay()
      return send(res, 200, await view())
    }
    if (method === 'POST' && path === '/overlay/start') {
      await startOverlay()
      return send(res, 200, await view())
    }
    // Dependency installation. Three verbs: read progress, start/resume, stop.
    // The installer is a separate process because a 2.6 GB download must not
    // block the Host, and because a cancelled install has to survive the panel
    // being closed.
    if (method === 'GET' && path === '/deps') {
      return send(res, 200, await depsState())
    }
    if (method === 'POST' && path === '/deps/start') {
      const result = await startDeps()
      return send(res, result.ok === true ? 200 : 409, { ...result, ...(await depsState()) })
    }
    if (method === 'POST' && path === '/deps/stop') {
      const result = await stopDeps()
      return send(res, 200, { ...result, ...(await depsState()) })
    }
    if (method === 'POST' && path === '/deps/reset') {
      const result = await resetDeps()
      return send(res, 200, { ...result, ...(await depsState()) })
    }
    if (method === 'POST' && path === '/overlay/stop') {
      await stopOverlay()
      return send(res, 200, await view())
    }
    // The settings page speaks for the user, who may clear; the agent's tool
    // deliberately has no such action.
    if (method === 'POST' && path === '/clear') {
      scene = { ...scene, layers: [] }
      revision += 1
      await saveScene()
      await sendClearSignal()
      return send(res, 200, await view())
    }
    // Take or drop the pencil without going through the tray icon.
    if (method === 'POST' && path === '/mode') {
      const body = await readJsonBody(req)
      const mode = body.mode === 'ink' ? 'ink' : 'idle'
      await ensureOverlay()
      await sendMode(mode)
      return send(res, 200, await view())
    }
    // The stop button on the island, over HTTP for callers that can reach the
    // carrier. The window itself uses the file channel below, because the carrier
    // admits only requests carrying a live session.
    if (method === 'POST' && path === '/stop') {
      const result = performStop()
      return send(res, result.ok === true ? 200 : 503, result)
    }
    return send(res, 404, { error: 'not found' })
  } catch (error) {
    return send(res, 400, { error: String(error?.message ?? error).slice(0, 300) })
  }
}

/**
 * The user cleared from the settings page: drop the agent's marks and tell the
 * window to drop the user's, through the same signal its tray menu writes.
 */
async function sendClearSignal() {
  const tick = Date.now()
  lastClear = tick
  await writeAtomic(clearFile(), String(tick)).catch(() => {})
  await rm(userFile(), { force: true }).catch(() => {})
}

/**
 * Cancel every running turn.
 *
 * Shared by the HTTP route and the island's stop file so both paths behave the
 * same. The status is cleared at once rather than after the cancel unwinds: a
 * stop button that lingers on screen after the user pressed it reads as broken.
 */
function performStop() {
  const controller = controllerOf()
  const targets = [...runningOrder]
  const result = { ok: true, stopped: 0, failures: [] }

  if (targets.length > 0 && (controller === undefined || typeof controller.cancel !== 'function')) {
    result.ok = false
    result.failures.push('no-session-controller')
  } else {
    for (const sessionId of targets) {
      try {
        controller.cancel({ sessionId })
        result.stopped += 1
      } catch (error) {
        result.failures.push(String(error?.message ?? error).slice(0, 120))
      }
    }
  }

  runningAgents.clear()
  runningOrder.length = 0
  activeTools.clear()
  publishStatus({ busy: false, phase: 'idle', tool: '', detail: '', since: 0 })
  return result
}

export function applyBoard(ctx) {
  connectionOf = () => (typeof ctx.get === 'function' ? ctx.get('connection') : undefined)
  ready = (async () => {
    // The marker is the installation's identity: write it once, then the name is
    // enough to find this folder again even after it is renamed or moved.
    ensureMarker()
    loadInstallDir()
    const raw = await readFileOrNull(sceneFile())
    if (raw !== null) {
      try {
        const parsed = JSON.parse(raw)
        scene = {
          enabled: parsed?.enabled === true,
          unit: parsed?.unit === 'window' ? 'window' : 'screen',
          layers: Array.isArray(parsed?.layers)
            ? parsed.layers.map((item, index) => sanitizeItem(item, index)).filter(Boolean)
            : [],
        }
        revision = Number.isFinite(parsed?.revision) ? parsed.revision : 1
      } catch {
        scene = structuredClone(EMPTY_SCENE)
      }
    }
    // A clear that happened before this process started must not replay.
    const clearRaw = await readFileOrNull(clearFile())
    if (clearRaw !== null) {
      const tick = Number.parseInt(clearRaw.trim(), 10)
      if (Number.isInteger(tick)) lastClear = tick
    }
  })()

  ctx.inject(['webServer'], (scoped) => {
    // ⚠ 三个前缀都必须在**同一个 inject 回调**里注册。
    //
    //   面板是合并版：一个 client.js 里三个页面，各请求各的前缀
    //     /api/whale-pencil    「画板」页
    //     /api/dsh-companion   「历史」页
    //     /api/titanium-fisheye「截图」页
    //   只注册第一个，后两页就永远拿 404 → 前端 JSON.parse("not found")
    //   报「Unexpected token 'o'」。
    //
    //   以前把后两个放在另一个 ctx.inject(['webServer']) 里，那个回调
    //   进去了但 effect 里的注册没执行（日志只到 scoped.effect = function
    //   就断了）。放在这里跟能工作的那个并列，最稳。
    scoped.effect(() => {
      const disposers = [
        scoped.webServer.register({ kind: 'prefix', path: ROUTE, handler: route }),
        scoped.webServer.register({ kind: 'prefix', path: COMPANION_ROUTE, handler: companionRoute }),
        scoped.webServer.register({ kind: 'prefix', path: EYE_ROUTE, handler: companionRoute }),
      ]
      return () => {
        for (const d of disposers) {
          if (typeof d === 'function') d()
        }
      }
    }, 'whale-pencil: routes')
  })

  ctx.inject(['tools'], (scoped) => {
    registerPencilTool(scoped)
  })

  // The island follows the agent so the user can see work happening without
  // watching the chat. Three events are enough: status tells us whether anything
  // is running, and the tool pair says which call is in flight.
  ctx.effect(() => {
    const onStatus = ({ agent, status } = {}) => {
      const id = typeof agent?.id === 'string' ? agent.id : ''
      if (id === '') return
      if (status === 'running') {
        runningAgents.add(id)
        if (!runningOrder.includes(id)) runningOrder.push(id)
      } else {
        runningAgents.delete(id)
        const at = runningOrder.indexOf(id)
        if (at >= 0) runningOrder.splice(at, 1)
        // An idle agent cannot have a tool in flight. Clearing here as well as in
        // post-execute is what stops a missed completion from pinning the island
        // to a tool that finished minutes ago.
        activeTools.delete(id)
      }
      refreshStatus()
    }

    const onPreTool = (exec, next) => {
      const id = typeof exec?.agent?.id === 'string' ? exec.agent.id : ''
      const label = toolLabel(exec?.name)
      if (id !== '' && label !== '') {
        activeTools.set(id, { label, read: isReadTool(exec?.name), at: Date.now() })
        refreshStatus()
      }
      // This is a waterfall: declining to call next() would block the tool.
      return next()
    }

    const onPostTool = (exec, result, next) => {
      const id = typeof exec?.agent?.id === 'string' ? exec.agent.id : ''
      if (id !== '') {
        activeTools.delete(id)
        refreshStatus()
      }
      return next()
    }

    const disposers = [
      ctx.on('agent/status', onStatus),
      ctx.on('tools/pre-execute', onPreTool),
      ctx.on('tools/post-execute', onPostTool),
    ]

    // A watchdog over the event stream. A missed `idle` (a reload that rebinds the
    // listeners mid-turn, a session that vanished without unwinding) would leave
    // the island lit forever, and a status readout stuck on screen is worse than
    // no readout at all. Nothing running means nothing to show.
    const watchdog = setInterval(() => {
      if (runningAgents.size === 0 && islandStatus.busy) {
        publishStatus({ busy: false, phase: 'idle', tool: '', detail: '', since: 0 })
      }
    }, 2000)
    if (typeof watchdog.unref === 'function') watchdog.unref()

    // The island's stop button writes a tick here. Polling rather than watching
    // keeps this portable and costs one stat per interval; the comparison against
    // lastStop is what makes a leftover file harmless.
    const stopWatch = setInterval(() => {
      void (async () => {
        const raw = await readFileOrNull(stopFile())
        if (raw === null) return
        const tick = Number.parseInt(raw.trim(), 10)
        if (!Number.isInteger(tick) || tick <= lastStop) return
        lastStop = tick
        // The user pressed the pill's stop button: do not let the watchdog bring
        // it straight back, or the button would look broken.
        islandStopped = true
        performStop()
      })().catch(() => {})
    }, 400)
    if (typeof stopWatch.unref === 'function') stopWatch.unref()

    return () => {
      clearInterval(watchdog)
      clearInterval(stopWatch)
      for (const dispose of disposers) {
        try { dispose() } catch { /* already gone */ }
      }
      runningAgents.clear()
      runningOrder.length = 0
      activeTools.clear()
      publishStatus({ busy: false, phase: 'idle', tool: '', detail: '', since: 0 })
    }
  }, 'whale-pencil: island status')

  // Deliver a summon's rules as runtime context.
  //
  // This is the channel DSH reserves for facts the model needs but did not receive
  // as a message: the text is assembled into the request, and the conversation pane
  // keeps showing only what the user actually did. The provider is asked on every
  // assembly, so it simply reports nothing once the summon has been consumed.
  ctx.inject(['systemPrompt'], (scoped) => {
    scoped.effect(() => scoped.systemPrompt.context({
      name: 'whale-pencil-summon',
      order: 950,
      text: () => {
        if (pendingRules.size === 0) return ''
        // Every pending entry is delivered on the next assembly, then forgotten.
        const blocks = [...pendingRules.values()]
        pendingRules.clear()
        return blocks.join('\n\n')
      },
    }), 'whale-pencil: summon context')
  })

  // The board only works if both hands keep talking. A standing reminder makes
  // that true even before the first tool call, and keeps the red=user rule from
  // living only inside the tool definition.
  ctx.inject(['systemPrompt'], (scoped) => {
    scoped.effect(() => scoped.systemPrompt.section({
      name: 'whale-pencil',
      order: 640,
      text: [
        '## 神笔鲸良 (Whale\'s Pencil)',
        '',
        'The desktop may carry a shared drawing board (tool `pencil`). Read red as the user:',
        'red strokes and red text are what the user drew and typed with the tray pencil,',
        'so they are the user talking to you — a circled area means "this", an arrow means',
        '"that way", red text is their question or their answer.',
        '',
        '### 画之前先过闸门（硬规则）',
        '',
        '**没有明确指令，一个字都不许往画板上画。** 这不是建议，是准入条件。',
        '',
        '只有下面三种情况可以调用 `pencil { action: "draw" }`，其余一律不行：',
        '',
        '1. 用户**这一条消息**明确要求你在屏幕上画（「画在屏幕上」「标一下」「圈出来」）',
        '2. 用户**这一条消息**发了图片，并且要求标注 / 找东西',
        '3. 消息开头带【托盘呼唤】（那是用户在画板上按了呼唤，本身就是要你作答）',
        '',
        '以下情况**绝对不能画**：',
        '',
        '| 情况 | 举例 |',
        '|---|---|',
        '| 用户只是问问题 | 「这是什么」「为什么会闪」→ 只在聊天里回答 |',
        '| 用户说别的事 | 「加个功能」「改个 bug」→ 只改代码 |',
        '| 用户在讨论流程 / 规则 | 「你要先 xx 再 yy」→ 只回话 |',
        '| 你自己觉得画出来更清楚 | 「画个箭头他可能更好懂」→ 不许画 |',
        '| 用户上一轮让画过 | 上一轮的许可不延续到这一轮 |',
        '',
        '「我口头答应了用户」**不构成许可**。答应改的是 skill 和代码，不是让你以后见谁都画。',
        '',
        '### 允许画之后',
        '',
        '许可成立时，才谈得上风格：把回复放在用户看的地方（他圈的那块旁边、箭头指的方向），',
        '一个 `text`、一个框、一支箭头就够。**你的笔迹永远不是红色** —— 红是用户的专属色。',
        '',
        'Keep colours few and consistent, and make them contrast with whatever is',
        'underneath: one or two hues per annotation is enough, and a mark that blends',
        'into the background is a mark that was not drawn. Reach for more colours only',
        'when the colour itself carries meaning (distinguishing categories, matching an',
        'element you are pointing at).',
      ].join('\n'),
    }), 'whale-pencil: prompt section')
  })

  // A summon becomes a prompt in the session that last used the pencil.
  ctx.inject(['sessionController'], (scoped) => {
    controllerOf = () => scoped.sessionController
    ctx.effect(() => {
      startSummonWatcher()
      return () => stopSummonWatcher()
    }, 'whale-pencil: summon watcher')
  })

  // The sessions service is the summon's fallback target: with the board panel
  // gone (the floating ball is a standalone window), no Client half is left to
  // report `activeSession`, so "which conversation is open" has to be answered
  // from the Host side.
  ctx.inject(['sessions'], (scoped) => {
    sessionsOf = () => (typeof scoped.get === 'function' ? scoped.get('sessions') : scoped.sessions)
  })

  // Two skills carry the part of the contract a tool description cannot: what
  // the board means, and what to do when the user rings the bell. They load on
  // demand, so their length costs nothing until a board session asks for them.
  ctx.inject(['skills'], (scoped) => {
    for (const entry of SKILLS) {
      scoped.effect(() => scoped.skills.register({
        ...entry,
        source: 'custom',
        provider: 'whale-pencil',
        invocation: { modelInvocable: true, userInvocable: true },
      }), `whale-pencil: skill ${entry.name}`)
    }
  })

  // Screenshots ride the attachment service into the prompt, so no capture is
  // ever written to disk and an image-capable model receives real pixels.
  ctx.inject(['attachments'], (scoped) => {
    attachmentsOf = () => scoped.attachments
  })

  // The tray icon is the user's door into the board: it appears with the plugin.
  // Only a window this instance started is shut down here; killing whatever the
  // pid file names would fight a newer instance during a reload.
  ctx.effect(() => () => {
    const own = overlay.pid
    stopHeartbeat()
    stopOverlayWatchdog()
    stopClearWatcher()
    stopEraseWatcher()
    stopSummonWatcher()
    overlay = { status: 'stopped', pid: 0, error: '' }
    if (own > 0 && alivePid(own)) {
      try { process.kill(own) } catch { /* already gone */ }
    }
  }, 'whale-pencil: teardown')

  void (async () => {
    await ready
    // A reload runs the previous instance's teardown first; give it room before
    // claiming the tray, or the two windows race for it.
    await new Promise((resolve) => setTimeout(resolve, 800))
    if (overlay.status === 'running') return
    await startOverlay()
    // Watch the board for the rest of the session: if it dies unexpectedly, put
    // it back. The board owns the whale, so this is also what brings the whale
    // back after a Host hiccup.
    startOverlayWatchdog()
  })()
}

// ── the trust fence ───────────────────────────────────────────────────────────

function rejectionFor(req) {
  const connection = connectionOf()
  const admit = connection?.admit
  if (typeof admit === 'function') {
    try {
      const admission = admit.call(connection, req)
      const status = admission && typeof admission === 'object' ? admission.rejection : undefined
      return status
    } catch {
      return 503
    }
  }
  return structuralReason(req) === undefined ? undefined : 403
}

function structuralReason(req) {
  const host = authorityOf(req?.headers?.host, 'http')
  if (host === null || !LOOPBACK_NAMES.has(host.hostname)) return 'host-not-loopback'
  if (String(req?.headers?.['sec-fetch-site'] ?? '').toLowerCase() === 'cross-site') return 'cross-site'
  for (const header of ['origin', 'referer']) {
    const raw = req?.headers?.[header]
    if (typeof raw !== 'string' || raw.trim() === '') continue
    const authority = authorityOf(raw.trim())
    if (authority === null) return `${header}-invalid`
    if (authority.scheme !== host.scheme || authority.hostname !== host.hostname || authority.port !== host.port) {
      return `${header}-mismatch`
    }
  }
  return undefined
}

function authorityOf(value, defaultScheme) {
  if (typeof value !== 'string' || value.trim() === '') return null
  let url
  try {
    url = new URL(value.includes('://') ? value.trim() : `${defaultScheme ?? 'http'}://${value.trim()}`)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  const port = url.port === '' ? (url.protocol === 'https:' ? '443' : '80') : url.port
  return { scheme: url.protocol.replace(':', ''), hostname: url.hostname.toLowerCase(), port }
}


export function apply(ctx) {
  applyBoard(ctx)

  ctx.inject(['tools'], (scoped) => {
    const registry = typeof scoped.get === 'function' ? scoped.get('tools') : scoped.tools
    if (registry === undefined || typeof registry.register !== 'function') return
    registerLookTool(scoped, registry)
    registerNowImageTool(scoped, registry)
    registerImageSaveTool(scoped, registry)
    registerFindTool(scoped, registry)
    registerMarkTool(scoped, registry)
    registerFinishTool(scoped, registry)
    registerHistoryTool(scoped, registry)
  })

  ctx.inject(['skills'], (scoped) => {
    if (scoped.skills === undefined || typeof scoped.skills.register !== 'function') return
    let content = ''
    try {
      content = readFileSync(join(HERE, 'skills', 'companion.md'), 'utf8')
    } catch {
      content = ''
    }
    if (content === '') return
    scoped.effect(() => scoped.skills.register({
      name: 'dsh-companion',
      // WARNING: the skill catalog renders ONLY `name` + `description` to the
      // model (verified in the DSH app bundle; `whenToUse` is never surfaced).
      // Every gate the agent must see therefore lives in this string, first:
      // the load-before-acting rule, the trigger, and the first-round bans.
      description: '适用：用户发来一张图片，要求标注/找东西/圈出来。'
        + '硬门禁：必须先 skill 加载本技能正文，加载前执行 now-image/find/mark 属违规。'
        + '首轮只看 diff.png 直接标，禁止 zoom/grid（那是补标专属）。',
      whenToUse: '有且只有一种情况加载：用户发来一张图片。'
        + '用户没发图不加载；我要看屏幕、桌面、某个窗口时用 look，不加载；'
        + '用户对上一次标注结果有明确不满时加载 dsh-companion-retouch，不加载本技能。',
      source: 'custom',
      provider: 'dsh-doodle-fat-fish',
      invocation: { modelInvocable: true, userInvocable: true },
      content,
    }), 'dsh-companion: skill')

    // 补标是独立的一份：只在用户看过交付图、明确说「没标全 / 漏了 / 偏了」
    // 之后才加载。主流程到 finish 就结束，不往下走 —— 把放大那套写进主流程，
    // 模型就会自己找理由一路做下去。
    let retouch = ''
    try {
      retouch = readFileSync(join(HERE, 'skills', 'retouch.md'), 'utf8')
    } catch {
      retouch = ''
    }
    if (retouch === '') return
    scoped.effect(() => scoped.skills.register({
      name: 'dsh-companion-retouch',
      // Same catalog constraint as above: only description reaches the model.
      description: '适用：用户看过上一轮交付的标注图，明确表达不满（漏了/没标全/框偏了/标错了/重来）。'
        + '这是唯一允许看原图、允许 grid/zoom 仔细找的场景。'
        + '用户没提不满就不加载；先 skill 加载本技能正文再动手。',
      whenToUse: '有且只有一种情况加载：用户对上一次交付的标注结果表达明确不满或异议'
        + '（漏了 / 没标全 / 框偏了 / 标错了 / 重来 / 看不清）。'
        + '用户只说「好」「嗯」「可以」、提出新问题、或发来新图片，一律不加载。'
        + '判不准时按不加载处理。',
      source: 'custom',
      provider: 'dsh-doodle-fat-fish',
      invocation: { modelInvocable: true, userInvocable: true },
      content: retouch,
    }), 'dsh-companion: retouch skill')
  })
}
