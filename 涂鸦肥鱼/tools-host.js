/**
 * DSH 伴随者 — 定位与历史工具。
 *
 * 这几件事以前要现写 bash 命令，现在都是正式工具：
 *   find     OCR / 差分位移 / 开放词汇检测，三选一，返回坐标文本
 *   history  列出 / 删除单张 / 清空归档结果
 *
 * 坐标一律由代码给出，视觉模型只负责「哪一块是我要的」。
 */
import { execFileSync } from 'node:child_process'
import { mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { homedir, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

// 模块目录：用 fileURLToPath，否则中文目录名会停在 %XX 编码态，
// 拼出来的 python.exe 路径根本不存在。
const HERE = dirname(fileURLToPath(import.meta.url))
const OCR_PY = join(HERE, 'ocr_env', 'Scripts', 'python.exe')
const TOOLS_DIR = join(HERE, 'tools')

/** 数据根目录：与 index.js 保持一致，放在插件目录下的 data/。 */
function rootDir() {
  return join(HERE, 'data')
}

export function historyDir() {
  return join(rootDir(), 'history')
}

/** 读 PNG 头拿宽高。 */
function pngSize(bytes) {
  try {
    if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50) {
      return { w: bytes.readUInt32BE(16), h: bytes.readUInt32BE(20) }
    }
  } catch { /* 不是 PNG */ }
  return null
}

/** 跑一个 Python 脚本，返回 stdout。
 *
 * 两个方法用的解释器不同：
 *   OCR 依赖 paddleocr，装在自己 的 ocr_env 里；
 *   检测依赖 torch + transformers，装在系统 Python 里。
 * 用错解释器就是 ModuleNotFoundError，所以按脚本名分开。
 */
/**
 * 截一张当前整屏，返回临时文件路径（catch 流程用）。
 *
 * ⚠ 这张图只在分析期间存在：调用方（find act）用完就删，不落进 data/。
 *   和 pencil act 的「产物不留」规则一致 —— 屏幕标注才是交付物。
 *
 * ⚠ 屏幕尺寸由 tools/screen.py 自己声明 DPI 感知后读，所以截出来就是物理像素，
 *   和画板用的坐标系一致。不要在这里再做任何缩放。
 */
async function captureScreenForAct() {
  const out = join(tmpdir(), `dsh-catch-${Date.now()}.png`)
  try {
    execFileSync(OCR_PY, [
      join(TOOLS_DIR, 'screen.py'), '--mode', 'capture', '--out', out,
    ], { windowsHide: true, timeout: 20000, stdio: ['ignore', 'ignore', 'ignore'] })
    return out
  } catch {
    return ''
  }
}

function runPython(script, args, timeoutMs = 180000) {
  // Everything runs on the interpreter bundled in this plugin. The old code
  // fell back to `python` from PATH for the torch-based detectors, which made
  // the plugin depend on whatever the machine happened to have installed; the
  // bundle already contains torch, so there is no reason to look outside.
  const exe = OCR_PY
  return execFileSync(exe, [join(TOOLS_DIR, script), ...args], {
    encoding: 'utf8',
    windowsHide: true,
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}


/** 解析 shift_diff.py 的块表。 */
function parseBlocks(stdout) {
  const lines = String(stdout).split(/\r?\n/u)
  const blocks = []
  let inTable = false
  for (const line of lines) {
    if (line.startsWith('id ') && line.includes('x')) {
      inTable = true
      continue
    }
    if (!inTable) continue
    if (line.startsWith('OUT') || line.startsWith('DIFF') || line.trim() === '') {
      inTable = false
      continue
    }
    const m = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)/u)
    if (m === null) continue
    blocks.push({
      id: Number(m[1]),
      x: Number(m[2]),
      y: Number(m[3]),
      w: Number(m[4]),
      h: Number(m[5]),
      x2: Number(m[6]),
      y2: Number(m[7]),
    })
  }
  return blocks
}

/** 两个框的重叠率（交并比）。 */
function iou(a, b) {
  const ix = Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x, b.x))
  const iy = Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y, b.y))
  const inter = ix * iy
  if (inter === 0) return 0
  const ua = (a.w ?? (a.x2 - a.x)) * (a.h ?? (a.y2 - a.y)) + (b.w ?? (b.x2 - b.x)) * (b.h ?? (b.y2 - b.y)) - inter
  return ua > 0 ? inter / ua : 0
}

/**
 * 去重 + 过滤，全在程序里做，不把冗余丢给上下文。
 *
 * - 同类且重叠率超过 thr 的框只留置信度最高的那个
 * - 置信度低于 minConf 的丢掉
 * - 面积小到离谱的丢掉（通常是噪点）
 */
function tidyBoxes(items, opts = {}) {
  const minConf = Number.isFinite(opts.minConf) ? opts.minConf : 0
  const thr = Number.isFinite(opts.thr) ? opts.thr : 0.5
  const minArea = Number.isFinite(opts.minArea) ? opts.minArea : 0

  const alive = items
    .filter((b) => Number.isFinite(b.x) && Number.isFinite(b.y))
    .map((b) => ({ ...b, w: b.w ?? (b.x2 - b.x), h: b.h ?? (b.y2 - b.y) }))
    .filter((b) => (b.conf ?? b.score ?? 1) >= minConf)
    .filter((b) => b.w * b.h >= minArea)
    .sort((a, b) => (b.conf ?? b.score ?? 0) - (a.conf ?? a.score ?? 0))

  const kept = []
  for (const box of alive) {
    const clash = kept.some((k) => iou(k, box) >= thr)
    if (!clash) kept.push(box)
  }
  kept.sort((a, b) => (Math.round(a.y / 40) - Math.round(b.y / 40)) || (a.x - b.x))
  return kept.map((b, i) => ({ id: i + 1, ...b }))
}


// ── find 工具 ───────────────────────────────────────────────────────────────

const FIND_DESCRIPTION = [
  'DSH 伴随者 — 找 (find): locate things in a picture and get their exact coordinates as text.',
  '',
  'Methods:',
  '  scan    三合一 — OCR text (red) + YOLOE objects (green) + UI elements (blue, on request)',
  '  blocks  差分位移 — split the picture into blocks; the table is the answer, the',
  '          diff.png is only a visual aid. The block list is also written to',
  '          blocks.json, so `mark { blockIds: [...] }` can take it directly.',
  '  grid    全图坐标网格 — every 75 original pixels, labelled with original coordinates',
  '  zoom    magnify around a point; the grid labels stay original coordinates',
  '',
  '⚠ zoom and grid are RETOUCH-ONLY: they exist for dsh-companion-retouch, which the',
  'user reaches only by saying the delivered marks were wrong. On a FIRST-round',
  'annotation task (user sent an image) they are FORBIDDEN — read the diff/scan',
  'output once, pick coordinates, mark, finish. If the object layer returns 0 items,',
  'use blocks or read the diff image yourself; NEVER zoom to "see better" and never',
  'eyeball magnified coordinates.',
  '',
  '`query` is ENGLISH ONLY — the detectors take raw terms and never translate.',
  'Pick from the COCO 80 classes and add 2-4 English synonyms, e.g.',
  '  "chicken, rooster, hen"   "handbag, purse, bag"   "sports ball, ball"',
  'A non-ASCII term is rejected instead of silently detecting nothing.',
  '',
  'Returns every match with x, y, w, h, x2, y2 — feed those straight to `mark`.',
].join('\n')

/** scan 的物体词只收英文：检测器不翻译，中文穿透过去等于一个词都不认。 */
const COCO_80_HINT = [
  'COCO 80 classes:',
  'person bicycle car motorcycle airplane bus train truck boat traffic light',
  'fire hydrant stop sign parking meter bench bird cat dog horse sheep cow',
  'elephant bear zebra giraffe backpack umbrella handbag tie suitcase frisbee',
  'skis snowboard sports ball kite baseball bat baseball glove skateboard',
  'surfboard tennis racket bottle wine glass cup fork knife spoon bowl banana',
  'apple sandwich orange broccoli carrot hot dog pizza donut cake chair couch',
  'potted plant bed dining table toilet tv laptop mouse remote keyboard',
  'cell phone microwave oven toaster sink refrigerator book clock vase',
  'scissors teddy bear hair drier toothbrush',
].join('\n')

export function registerFindTool(ctx, registry) {
  ctx.effect(() => registry.register({
    name: 'find',
    description: FIND_DESCRIPTION,
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        method: {
          type: 'string',
          enum: ['scan', 'blocks', 'grid', 'zoom', 'act'],
          description: 'act = 截图专用三合一(OCR+UI+差分，结果标到一张图上)；'
            + 'scan = 三合一(文字红/UI蓝/物体绿), blocks = 差分位移, '
            + 'grid = 全图坐标网格, zoom = 以一点为中心放大（坐标仍是原图坐标）。',
        },
        image: {
          type: 'string',
          description: 'Picture to search. Omit it together with method:"act" to capture the '
            + 'current screen yourself (the `catch` flow) — the screenshot is taken, analysed, '
            + 'and then thrown away.',
        },
        query: {
          type: 'string',
          description: 'scan 的物体词，只收英文，逗号分隔。从 COCO 80 类里选，再补 2~4 个英文同义词，'
            + '例 "chicken, rooster, hen"。检测器不翻译，中文会被直接拒绝。',
        },
        task: { type: 'string', description: 'Task workspace to write scratch output into.' },
        minScore: {
          type: 'number',
          description: '物体层的最低置信度，0..1。默认 0.25（YOLO 官方默认）。文字层和 UI 层不受它影响，永远全返回。',
        },
        layers: {
          type: 'string',
          description: 'scan 要返回哪几层，逗号分隔：text / ui / obj。任意组合。'
            + '默认 "text,obj"：UI 层只在要看/点按钮时才传。',
        },
        detail: {
          type: 'boolean',
          description: 'scan: 返回完整三层明细。默认只回摘要和目标物体框，'
            + '免得一次几千 token 灌进上下文。',
        },
        gridSize: { type: 'number', description: 'grid/zoom: 网格间距，单位是原图像素。默认 75。' },
        cx: { type: 'number', description: 'zoom: 放大中心在原图上的 x。' },
        cy: { type: 'number', description: 'zoom: 放大中心在原图上的 y。' },
        factor: { type: 'number', description: 'zoom: 放大倍数，默认 4。' },
      },
      required: ['method', 'image'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          method: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          count: { type: 'integer' },
          matches: { type: 'array', items: { type: 'object', additionalProperties: true } },
          reason: { type: 'string' },
          detail: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value === undefined || value === null) return []
        if (value.ok === false) {
          return [{ type: 'text', text: `find: ${value.reason}${value.detail ? ` — ${value.detail}` : ''}` }]
        }

        // 三合一：给的是三色图和分层统计，不是一列框。
        if (value.method === 'scan') {
          const fmt = (b) => `(${b.x},${b.y})-(${b.x2},${b.y2})`
          const c = value.counts ?? {}
          const out = [`scan ${value.width}x${value.height}  ${value.path}`]

          const texts = Array.isArray(value.text) ? value.text : null
          if (texts !== null) {
            out.push(`--- 文字 ${texts.length} ---`)
            for (const b of texts) out.push(`${b.id}. ${fmt(b)} ${b.t ?? ''}`)
          } else if (c.text) {
            out.push(`文字 ${c.text} 条（未返回）`)
          }

          const uis = Array.isArray(value.ui) ? value.ui : null
          if (uis !== null) {
            out.push(`--- UI ${uis.length} ---`)
            for (const b of uis) out.push(`${b.id}. ${fmt(b)}`)
          } else if (c.ui) {
            out.push(`UI ${c.ui} 条（未返回）`)
          }

          const objs = Array.isArray(value.obj) ? value.obj : null
          if (objs !== null) {
            out.push(`--- 物体 ${objs.length} (${(value.terms ?? []).join(',')}) ---`)
            for (const b of objs) out.push(`${b.id}. ${fmt(b)} ${b.l ?? ''}`)
          } else if (c.obj) {
            out.push(`物体 ${c.obj} 条（未返回）`)
          }

          if (Array.isArray(value.warnings) && value.warnings.length > 0) {
            out.push(`警告 ${value.warnings.join('; ')}`)
          }
          return [{ type: 'text', text: out.join(String.fromCharCode(10)) }]
        }
        // grid / zoom：给的是带坐标网格的图。
        if (value.method === 'grid' || value.method === 'zoom') {
          const crop = value.crop
          const lines = [
            `find(${value.method}): 原图 ${value.width}x${value.height}`,
            crop === null
              ? '显示区域: 全图'
              : `显示区域: 原图 (${crop.x},${crop.y}) 起 ${crop.w}x${crop.h}`,
            value.scale === null || value.scale === undefined
              ? ''
              : `放大 ${Number(value.scale).toFixed(2)}x · 网格间距 ${value.step} 原图像素`,
            `图: ${value.path}`,
            '网格标签就是原图坐标，直接读，不用换算。看不够细就用 zoom 继续放大。',
          ].filter((l) => l !== '')
          return [{ type: 'text', text: lines.join('\n') }]
        }

        // blocks：差分位移的块表。
        const rows = Array.isArray(value.matches) ? value.matches : []
        if (rows.length === 0) {
          return [{ type: 'text', text: `find(${value.method}): no match in ${value.width}x${value.height}` }]
        }
        // 块表必须完整给出去。以前截断到 30 条，剩下的块（往往正是第二排、
        // 第三排的卡片）看不见，调用方就会以为「表不全」，转去放大重找 —— 白费工。
        // 块表就是差分的全部产出，一条都不能少。
        const lines = rows.map((m) => {
          const head = `${m.id}. (${m.x},${m.y})-(${m.x2},${m.y2})  ${m.w}x${m.h}`
          const meta = m.text !== undefined
            ? `  conf=${Number(m.conf ?? 0).toFixed(3)}  ${m.text}`
            : m.label !== undefined
              ? `  score=${Number(m.score ?? 0).toFixed(3)}  ${m.label}`
              : ''
          return head + meta
        })
        return [{
          type: 'text',
          text: `find(${value.method}): ${rows.length} block(s) in ${value.width}x${value.height}\n`
            + `${lines.join('\n')}\n图: ${value.path ?? '(none)'}`
            + (value.blocksJson ? `\n块表: ${value.blocksJson} — 可直接传 mark { blockIds: [...] }` : ''),
        }]
      },
    },
    async execute(args) {
      const method = String(args?.method ?? '')
      let image = typeof args?.image === 'string' && args.image.trim() !== '' ? args.image.trim() : ''
      // catch 流程：method:"act" 且没给 image → 自己截一张当前屏幕。
      //
      // 这是用户明确授权的入口（技能名 catch）：不需要他先按呼唤、也不需要
      // 他贴图，我可以主动看一眼当前桌面。截图落在临时目录，分析完就删，
      // 不留下任何文件 —— 和 pencil act 的产物规则一致。
      let ownShot = ''
      if (image === '' && method === 'act') {
        ownShot = await captureScreenForAct()
        if (ownShot === '') throw new Error('find act: screen capture failed')
        image = ownShot
      }
      if (image === '') throw new Error('find: pass `image`')
      const query = String(args?.query ?? '').trim()
      const minScore = Number.isFinite(args?.minScore) ? Number(args.minScore) : 0.25
      const gridSize = Number.isFinite(args?.gridSize) ? Number(args.gridSize) : 75
      const cx = Number.isFinite(args?.cx) ? Number(args.cx) : 0
      const cy = Number.isFinite(args?.cy) ? Number(args.cy) : 0
      const factor = Number.isFinite(args?.factor) ? Number(args.factor) : 4

      let bytes
      try {
        bytes = await readFile(image)
      } catch (error) {
        throw new Error(`find: cannot read ${image} — ${String(error?.message ?? error).slice(0, 160)}`)
      }
      const size = pngSize(bytes)
      if (size === null) throw new Error('find: only PNG pictures are supported here')

      // scan 的物体词必须是英文。检测器把词原样透传给模型，
      // 中文传进去就是一个都认不出来 —— 那种失败很安静，只会得到「物体 0 个」，
      // 调用方会以为图里真没有。所以在这里直接拦下来，连检测都不跑。
      if (method === 'scan' && query !== '') {
        const bad = query.split(',').map((t) => t.trim()).filter((t) => t !== '' && !/^[\x20-\x7e]+$/u.test(t))
        if (bad.length > 0) {
          throw new Error(`find scan: query must be English — rejected "${bad.join(', ')}". `
            + 'Detectors take raw terms and never translate. '
            + `Pick a COCO 80 class plus 2-4 English synonyms, e.g. "chicken, rooster, hen".\n${COCO_80_HINT}`)
        }
      }

      const scratch = join(rootDir(), 'work', args?.task ?? 'loose', 'find')
      await mkdir(scratch, { recursive: true })
      const scratchPng = join(scratch, 'diff.png')

      try {
        if (method === 'act') {
          // 截图专用：OCR + UI 在原图检测，结果标到差分图上，只交一张图。
          //
          // 和 scan + blocks 两步的区别：
          //   scan   在原图上画框，是一张「原图 + 彩色框」
          //   blocks 另给一张 diff.png
          //   两边要来回对照，坐标容易对错。
          // act 把 OCR 的原文和 UI 的框直接叠在 diff.png 上，
          // 一次看完就知道「这块是什么、上面写的是什么」。
          //
          // ⚠ 产物不落盘、不归档：act.png 只是「给我看的一次性材料」，
          //   看完就该消失。真有价值的是随后画到屏幕上的那些图元。
          //   所以输出目录用系统临时目录，并且在返回前就删掉 ——
          //   只把路径交给调用方去 read_image，读完之后目录里什么都不剩。
          //   （以前写在 data/work/<task>/find/ 下面，可 pencil act 不走
          //    finish，那目录就一直留着，越积越多。）
          const outDir = join(tmpdir(), `dsh-act-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)
          await mkdir(outDir, { recursive: true })
          const out = runPython('act.py', [image, outDir], 600000)
          const jsonPath = join(outDir, 'act.json')
          let doc = null
          try {
            doc = JSON.parse(await readFile(jsonPath, 'utf8'))
          } catch {
            await rm(outDir, { recursive: true, force: true })
            throw new Error(`find: act produced no json — ${out.split('\n').slice(-3).join(' ')}`)
          }
          const blocks = Array.isArray(doc?.blocks) ? doc.blocks : []
          const actPng = String(doc?.act_png ?? join(outDir, 'act.png'))
          // 自己截的那张原图用完就删：它只是 act.png 的原料，
          // 调用方要看的只有 act.png。
          if (ownShot !== '') await rm(ownShot, { force: true }).catch(() => {})
          return {
            ok: true,
            method,
            width: doc?.image?.width ?? size.w,
            height: doc?.image?.height ?? size.h,
            count: blocks.length,
            matches: blocks,
            path: actPng,
            // 只给路径，不写块表文件：act 的结果是一次性的，
            // 调用方要坐标就从 matches / texts / uis 里直接读。
            counts: doc?.counts ?? {},
            texts: Array.isArray(doc?.texts) ? doc.texts : [],
            uis: Array.isArray(doc?.uis) ? doc.uis : [],
            warnings: Array.isArray(doc?.warnings) ? doc.warnings : [],
            // 这张图只看一次，看完由调用方 finish 或下次 act 覆盖；
            // 这里记下目录，方便排障时知道去哪儿找（正常情况下会被清掉）。
            scratchDir: outDir,
          }
        }

        if (method === 'scan') {
          // 三合一：OCR + UI + 开放词汇，各跑各的，汇成一张三色图和一份结构化文档。
          const outDir = join(scratch, 'scan')
          await mkdir(outDir, { recursive: true })
          const argv = [image, outDir]
          if (query !== '') argv.push('--object-terms', query)
          const out = runPython('scan.py', argv, 600000)
          const jsonPath = join(outDir, 'scan.json')
          let doc = null
          try {
            doc = JSON.parse(await readFile(jsonPath, 'utf8'))
          } catch {
            throw new Error(`find: scan produced no json — ${out.split('\n').slice(-3).join(' ')}`)
          }
          const layers = doc?.layers ?? {}
          const png = String(doc?.scan_png ?? join(outDir, 'scan.png'))

          // 默认只回摘要：完整三层数据一次 16KB（≈4000 token），不该进上下文。
          // 要明细必须显式传 detail: true。
          const rawText = Array.isArray(layers?.text?.items) ? layers.text.items : []
          const rawUi = Array.isArray(layers?.ui?.items) ? layers.ui.items : []
          const rawObj = Array.isArray(layers?.object?.items) ? layers.object.items : []

          // 三层的过滤规则不同：
          //   文字 / UI —— 不过滤置信度，检测器说有什么就有什么
          //   物体     —— 按 minScore 卡，默认 0.25（YOLO 官方值）
          // 三层都只去掉几乎完全重叠的重复框（同一位置报两次）。
          const dedupe = (items, thr, minConf) => tidyBoxes(items, { minConf, thr, minArea: 0 })
          const textBoxes = dedupe(rawText, 0.92, 0)
            .map((b) => ({ id: b.id, x: b.x, y: b.y, x2: b.x2, y2: b.y2, t: b.text, c: b.conf }))
          const uiBoxes = dedupe(rawUi, 0.92, 0)
            .map((b) => ({ id: b.id, x: b.x, y: b.y, x2: b.x2, y2: b.y2, c: b.conf }))
          const objAll = dedupe(rawObj, 0.92, 0)
          const objBoxes = objAll
            .filter((b) => (b.conf ?? 0) >= minScore)
            .map((b) => ({ id: b.id, x: b.x, y: b.y, x2: b.x2, y2: b.y2, l: b.label, c: b.conf }))

          // 要哪几层由调用方说：text / ui / obj，逗号分隔，任意组合。
          // 不传就是三层全返回。没选的层字段直接不出现（undefined 不是合法 JSON）。
          // 默认只回 text + obj：UI 层是「要点击某个控件」时才用的，
          // 标物体时它只是噪声。要 UI 层就显式传 layers:"ui" 或 "text,ui,obj"。
          const only = String(args?.layers ?? 'text,obj')
            .split(',').map((t) => t.trim().toLowerCase()).filter((t) => t !== '')
          const want = (k) => only.includes(k)

          const payload = {
            ok: true,
            method,
            width: size.w,
            height: size.h,
            path: png,
            counts: {
              text: textBoxes.length,
              ui: uiBoxes.length,
              obj: objBoxes.length,
            },
            count: textBoxes.length + uiBoxes.length + objBoxes.length,
            terms: layers?.object?.terms ?? [],
          }
          if (want('text')) payload.text = textBoxes
          if (want('ui')) payload.ui = uiBoxes
          if (want('obj')) payload.obj = objBoxes
          if (Array.isArray(doc?.warnings) && doc.warnings.length > 0) payload.warnings = doc.warnings
          return payload
        }

        if (method === 'blocks') {          const out = runPython('shift_diff.py', [image, scratch])
          const blocks = parseBlocks(out)
          // 块表落盘：mark 要按块号取坐标，就得有一份程序可读的副本。
          // 只回文本的话，调用方只能手抄坐标，块表等于白算。
          const blocksJson = join(scratch, 'blocks.json')
          await writeFile(blocksJson, JSON.stringify({
            image,
            width: size.w,
            height: size.h,
            count: blocks.length,
            blocks,
          }, null, 1), 'utf8')
          return {
            ok: true, method, width: size.w, height: size.h,
            count: blocks.length, matches: blocks,
            path: join(scratch, 'diff.png'),
            blocksJson,
          }
        }

        if (method === 'grid' || method === 'zoom') {
          // 坐标网格：grid 看全图，zoom 以 (cx,cy) 为中心放大。
          // 两者画的都是原图像素坐标，所以放大后读到的新数字仍是原图坐标。
          const outPng = join(scratch, `${method}.png`)
          const argv = [method, image, outPng, '--grid', String(gridSize)]
          if (method === 'zoom') {
            argv.push('--cx', String(cx), '--cy', String(cy), '--factor', String(factor))
          }
          const out = runPython('grid_zoom.py', argv, 240000)
          const crop = /CROP (\d+) (\d+) (\d+) (\d+)/u.exec(out)
          const scaled = /SCALE ([\d.]+)\s+STEP ([\d.]+)/u.exec(out)
          return {
            ok: true,
            method,
            width: size.w,
            height: size.h,
            path: outPng,
            crop: crop === null ? null : {
              x: Number(crop[1]), y: Number(crop[2]),
              w: Number(crop[3]), h: Number(crop[4]),
            },
            scale: scaled === null ? null : Number(scaled[1]),
            step: scaled === null ? null : Number(scaled[2]),
            count: 0,
            matches: [],
          }
        }

        throw new Error(`find: unknown method ${JSON.stringify(method)}`)
      } finally {
        // 这里以前会删 diff.png —— 但 blocks 刚把它当返回值给出去了，
        // 删掉就是一个「返回了却不存在」的路径，读必然失败。
        // 中间图一律由 finish 统一清理，任务没结束前都留着。
        void scratchPng
      }
    },
  }), 'dsh-companion: find tool')
}

// ── history 工具（列出 / 删单张 / 清空） ────────────────────────────────────

export async function listHistory() {
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
    files.push({ name: entry.name, path: full, width: dims.w, height: dims.h, bytes: info.size })
  }
  files.sort((a, b) => (a.name < b.name ? 1 : -1))
  return { ok: true, dir, files }
}

export async function removeHistoryEntry(name) {
  const safe = String(name ?? '')
  if (safe === '' || safe.includes('/') || safe.includes(String.fromCharCode(92)) || safe.includes('..')) {
    throw new Error('history: bad name')
  }
  if (!safe.endsWith('.png')) throw new Error('history: not a png')
  await rm(join(historyDir(), safe), { force: true })
  return { ok: true, removed: safe }
}

export async function clearHistory() {
  const dir = historyDir()
  const entries = await readdir(dir).catch(() => [])
  let cleared = 0
  for (const name of entries) {
    if (!name.endsWith('.png')) continue
    await rm(join(dir, name), { force: true }).catch(() => {})
    cleared += 1
  }
  return { ok: true, cleared }
}
