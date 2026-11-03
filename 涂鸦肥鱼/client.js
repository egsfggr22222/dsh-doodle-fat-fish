/**
 * dsh-companion — browser half: the settings panel.
 *
 * 一个面板取代原来两个（钛合金鱼眼 / 神笔鲸良）。用户看到的是「DSH 涂鸦肥鱼」。
 *
 * 三个标签页，功能与原插件一一对应，不做删减：
 *   history  历史  —— 已归档的标注结果（截图的「最近画面」不再展示）
 *   eye      截图  —— 真实屏幕尺寸、三种截图、可见窗口列表
 *   board    画板  —— 窗口开关、统计、拿铅笔/放铅笔、一键清除、重启窗口
 */
window.__ModuleLoader__.load({
  id: 'dsh-doodle-fat-fish',
  factory(require) {
    const React = require('react')
    const { createElement: h, useCallback, useEffect, useRef, useState } = React

    const NS = 'settings.dshDoodleFatFish'
    const ROUTE_EYE = '/api/titanium-fisheye'
    const ROUTE_BOARD = '/api/whale-pencil'
    const ROUTE_HISTORY = '/api/dsh-companion'
    const POLL_MS = 4000
    /** How often the open conversation re-claims itself to the Host. */
    const FOCUS_REPORT_MS = 3000

    const DICT = {
      zh: {
        'meta.title': 'DSH 涂鸦肥鱼',
        'meta.description': '看图、标注、画板三合一。图片不进桌面，只看你要的结果。',
        nav: 'DSH 涂鸦肥鱼',
        'page.title': 'DSH 涂鸦肥鱼',
        'page.description': '截图 → 标注 → 收尾。中间图任务结束即删，只有标注好的结果留在这里。',
        'tab.history': '历史',
        'tab.eye': '截图',
        'tab.board': '画板',
        'tab.deps': '依赖',

        'deps.title': '运行依赖',
        'deps.hint': '插件只带代码。Python 环境和检测模型共约 2.6 GB，第一次使用前需要下载一次。',
        'deps.ready': '已就绪，可以直接使用。',
        'deps.missing': '还缺：',
        'deps.download': '下载依赖',
        'deps.resume': '继续下载',
        'deps.pause': '暂停',
        'deps.reset': '重新开始',
        'deps.done': '全部安装完成。',
        'deps.paused': '已暂停。继续时会从上次停下的那个依赖接着下，已下好的不会重下。',
        'deps.failed': '下载失败',
        'deps.step': '当前步骤',
        'deps.progress': '进度',
        'deps.idle': '尚未开始。',
        'deps.running': '正在下载…',
        'deps.needRestart': '依赖装好后需要重启 DSH 才会生效。',
        'deps.needDeps': '需要先下载依赖',
        'deps.needDepsHint': '画板、截图、标注都依赖 Python 环境。请到「依赖」页点「下载依赖」，装完重启 DSH。',

        'history.title': '已归档的标注结果',
        'history.empty': '还没有结果。让 agent 标完一张图，它会出现在这里。',
        'history.clear': '清空历史',
        'history.count': '条结果',
        'history.hint': '只保留标注完成的图。截图和中间产物不会出现在这里。',

        'eye.title': '截图引擎',
        'eye.screen': '真实屏幕',
        'eye.screenHint': '由 Windows API 读出，与截图尺寸一致，永远是物理像素。',
        'eye.targets': '三种截图',
        'eye.target.screen': '全屏',
        'eye.target.screenHint': '人眼所见：被窗口挡住的就看不见。',
        'eye.target.desktop': '桌面',
        'eye.target.desktopHint': '壁纸和图标，被窗口盖住也能拍到。',
        'eye.target.window': '单窗口',
        'eye.target.windowHint': '目标窗口自己绘制内容，被遮挡也能拿到完整画面。',
        'eye.refresh': '刷新',
        'eye.windows': '可见窗口',
        'eye.windowsEmpty': '没有读到窗口列表。',

        'board.section.board': '画板',
        'board.window.title': '桌面窗口',
        'board.window.hint': '铺满桌面的透明画布。托盘里有「拿铅笔」；左键画红、右键擦、滚轮调粗细、ESC 退出。',
        'board.status.running': '运行中',
        'board.status.starting': '启动中',
        'board.status.stopped': '未启动',
        'board.status.error': '出错',
        'board.section.stats': '统计',
        'board.stats.agent': 'agent 图层',
        'board.stats.agentHint': '当前画布上 agent 画了多少个图元。',
        'board.stats.user': '用户笔画',
        'board.stats.userHint': '当前画布上用户画了多少笔。',
        'board.section.actions': '操作',
        'board.action.pencil': '拿铅笔',
        'board.action.pencilHint': '等于点托盘里的「拿铅笔」：关闭穿透、鼠标变铅笔。ESC 或「放铅笔」退出。',
        'board.action.pencilOn': '拿起铅笔',
        'board.action.pencilOff': '放下铅笔',
        'board.action.clear': '一键清除',
        'board.action.clearHint': '清掉画布上的所有内容：agent 的和你的。托盘菜单里也有这一项。',
        'board.action.restart': '重启桌面窗口',
        'board.action.restartHint': '窗口卡住或托盘图标不见了时用这个。',
        'board.tray.note': '托盘菜单：拿铅笔 / 一键清除 / 退出作画 / 退出',

        'common.loading': '读取中…',
        'common.error': '出错了',
        'common.retry': '重试',
      },
      en: {
        'meta.title': 'DSH Doodle Fat Fish',
        'meta.description': 'Look, mark, and draw in one place. Pictures never touch your Desktop.',
        nav: 'DSH Doodle Fat Fish',
        'page.title': 'DSH Doodle Fat Fish',
        'page.description': 'Capture → annotate → finish. Working copies are deleted; only finished marks stay.',
        'tab.history': 'History',
        'tab.eye': 'Capture',
        'tab.board': 'Board',
        'tab.deps': 'Dependencies',

        'deps.title': 'Runtime dependencies',
        'deps.hint': 'The plugin ships code only. The Python environment and detector models total about 2.6 GB and download once.',
        'deps.ready': 'Ready to use.',
        'deps.missing': 'Missing: ',
        'deps.download': 'Download dependencies',
        'deps.resume': 'Resume',
        'deps.pause': 'Pause',
        'deps.reset': 'Start over',
        'deps.done': 'All dependencies installed.',
        'deps.paused': 'Paused. Resuming continues from the item it stopped on; finished items are not downloaded again.',
        'deps.failed': 'Download failed',
        'deps.step': 'Current step',
        'deps.progress': 'Progress',
        'deps.idle': 'Not started yet.',
        'deps.running': 'Downloading…',
        'deps.needRestart': 'Restart DSH after installing for it to take effect.',
        'deps.needDeps': 'Dependencies needed',
        'deps.needDepsHint': 'The board, capture and marking all need the Python environment. Open the Dependencies tab and press Download, then restart DSH.',

        'history.title': 'Archived annotations',
        'history.empty': 'Nothing yet. Ask the agent to mark a picture and it lands here.',
        'history.clear': 'Clear history',
        'history.count': 'result(s)',
        'history.hint': 'Only finished annotations. Screenshots and working copies never appear here.',

        'eye.title': 'Capture engine',
        'eye.screen': 'Real screen',
        'eye.screenHint': 'Read from the Windows API, always physical pixels.',
        'eye.targets': 'Three targets',
        'eye.target.screen': 'Screen',
        'eye.target.screenHint': 'What a person sees: covered windows stay hidden.',
        'eye.target.desktop': 'Desktop',
        'eye.target.desktopHint': 'Wallpaper and icons, even under windows.',
        'eye.target.window': 'Window',
        'eye.target.windowHint': 'The window paints itself, so coverage does not matter.',
        'eye.refresh': 'Refresh',
        'eye.windows': 'Visible windows',
        'eye.windowsEmpty': 'No window list was returned.',

        'board.section.board': 'Board',
        'board.window.title': 'Desktop window',
        'board.window.hint': 'A transparent canvas over the Desktop. The tray holds Take the pencil: left draws red, right erases, wheel sets thickness, ESC leaves.',
        'board.status.running': 'Running',
        'board.status.starting': 'Starting',
        'board.status.stopped': 'Stopped',
        'board.status.error': 'Error',
        'board.section.stats': 'Statistics',
        'board.stats.agent': 'Agent layers',
        'board.stats.agentHint': 'How many primitives the agent has on the canvas.',
        'board.stats.user': 'User strokes',
        'board.stats.userHint': 'How many strokes the user has drawn.',
        'board.section.actions': 'Actions',
        'board.action.pencil': 'Take the pencil',
        'board.action.pencilHint': 'Same as the tray entry: turns off click-through and turns the pointer into a pencil.',
        'board.action.pencilOn': 'Take the pencil',
        'board.action.pencilOff': 'Drop the pencil',
        'board.action.clear': 'Clear everything',
        'board.action.clearHint': 'Removes every mark, yours and the agent\'s. Also in the tray menu.',
        'board.action.restart': 'Restart the window',
        'board.action.restartHint': 'Use this when the window is stuck or the tray icon vanished.',
        'board.tray.note': 'Tray menu: take the pencil / clear everything / stop drawing / quit',

        'common.loading': 'Loading…',
        'common.error': 'Something went wrong',
        'common.retry': 'Retry',
      },
    }

    let boundT = (key) => key

    const CSS = `
.dc_page{display:flex;flex-direction:column;gap:16px;font-size:14px}
.dc_head{display:flex;flex-direction:column;gap:4px}
.dc_title{font-size:16px;font-weight:600}
.dc_desc{font-size:13px;line-height:20px;color:var(--dsw-alias-label-secondary)}
.dc_tabs{display:flex;gap:4px;border-bottom:1px solid var(--dsw-alias-border-l1)}
.dc_tab{padding:8px 14px;font-size:13px;cursor:pointer;border:none;background:none;color:var(--dsw-alias-label-secondary);border-bottom:2px solid transparent}
.dc_tab[data-active="true"]{color:var(--dsw-alias-brand-primary);border-bottom-color:var(--dsw-alias-brand-primary);font-weight:600}
.dc_section{padding:18px 0 6px;font-size:12px;font-weight:600;letter-spacing:.02em;color:var(--dsw-alias-label-tertiary)}
.dc_row{display:flex;align-items:center;gap:24px;padding:12px 0;border-top:1px solid var(--dsw-alias-border-l1)}
.dc_rowText{flex:1;min-width:0}
.dc_rowTitle{font-size:14px;font-weight:500;line-height:22px}
.dc_rowHint{margin-top:2px;font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary)}
.dc_controls{display:flex;align-items:center;gap:10px;flex:none}
.dc_btn{padding:5px 12px;font-size:12px;border-radius:6px;border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-l2);color:var(--dsw-alias-label-primary);cursor:pointer}
.dc_btn:hover{background:var(--dsw-alias-bg-l3)}
.dc_btn[disabled]{opacity:.5;cursor:default}
.dc_btn_danger{color:var(--dsw-alias-state-error-primary)}
.dc_status{font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dc_status_error{color:var(--dsw-alias-state-error-primary)}
.dc_count{font-size:13px;line-height:18px;color:var(--dsw-alias-label-secondary);font-variant-numeric:tabular-nums;min-width:32px;text-align:right}
/* Switch 直接用原神笔鲸良的实现：外层 button 透明，内层 i 画轨道。
   直接把 ::after 挂在 button 上会被按钮底色盖住，显示成一个纯白色圆。 */
.dc_switch{display:inline-flex;align-items:center;cursor:pointer;user-select:none;-webkit-tap-highlight-color:transparent;background:0 0;border:0;padding:0;flex:none}
.dc_switch i{width:36px;height:20px;border-radius:999px;background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l2);box-shadow:inset 0 1px 3px rgb(0 0 0 / 8%);position:relative;transition:background .2s ease,border-color .2s ease;flex:none;display:block}
.dc_switch i::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--dsw-alias-label-secondary);box-shadow:0 1px 3px rgb(0 0 0 / 22%);transition:transform .26s cubic-bezier(.34,1.3,.64,1),background .2s ease}
.dc_switch[aria-checked="true"] i{background:var(--dsw-alias-state-business-primary);border-color:color-mix(in srgb,var(--dsw-alias-state-business-primary) 85%,#000)}
.dc_switch[aria-checked="true"] i::after{transform:translateX(16px);background:#fff}
.dc_switch:disabled{opacity:.5;cursor:default}
.dc_list{display:flex;flex-direction:column;gap:8px}
.dc_item{display:flex;gap:12px;align-items:center;padding:8px;border:1px solid var(--dsw-alias-border-l1);border-radius:8px;background:var(--dsw-alias-bg-l1);cursor:pointer;user-select:none}
.dc_item:hover{background:var(--dsw-alias-bg-l2);border-color:var(--dsw-alias-border-l2)}
.dc_item_open{border-color:var(--dsw-alias-brand-primary)}
.dc_expand{font-size:11px;color:var(--dsw-alias-brand-primary);flex:none}
.dc_preview{margin:8px 0;padding:8px;border:1px solid var(--dsw-alias-border-l1);border-radius:8px;background:var(--dsw-alias-bg-l1);text-align:center}
.dc_previewImg{display:block;max-width:100%;height:auto;border-radius:4px;margin:0 auto;cursor:zoom-in}
.dc_previewImg_full{max-width:none;cursor:zoom-out}
.dc_overlay{position:fixed;inset:0;background:rgb(0 0 0 / 72%);display:flex;align-items:center;justify-content:center;z-index:9999;cursor:zoom-out;padding:24px}
.dc_overlayImg{max-width:96vw;max-height:92vh;border-radius:6px;box-shadow:0 12px 48px rgb(0 0 0 / 50%)}
.dc_menu{position:fixed;z-index:10000;min-width:132px;padding:4px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);box-shadow:0 8px 24px rgb(0 0 0 / 32%)}
.dc_menuItem{display:block;width:100%;padding:7px 10px;font-size:12px;text-align:left;border:0;border-radius:5px;background:none;color:var(--dsw-alias-label-primary);cursor:pointer}
.dc_menuItem:hover{background:var(--dsw-alias-bg-l3)}
.dc_menuItem_danger{color:var(--dsw-alias-state-error-primary)}
.dc_thumb{width:96px;height:54px;object-fit:cover;border-radius:4px;background:var(--dsw-alias-bg-l3);flex-shrink:0}
.dc_itemText{display:flex;flex-direction:column;gap:2px;min-width:0}
.dc_itemName{font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dc_itemMeta{font-size:11px;color:var(--dsw-alias-label-tertiary)}
.dc_empty{padding:24px;text-align:center;font-size:13px;color:var(--dsw-alias-label-tertiary);border:1px dashed var(--dsw-alias-border-l1);border-radius:8px}
.dc_err{color:var(--dsw-alias-state-error-primary);font-size:12px}
.dc_foot{display:flex;justify-content:space-between;align-items:center;padding-top:12px;font-size:12px;color:var(--dsw-alias-label-tertiary)}
.dc_tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;background:var(--dsw-alias-bg-l3)}
.dc_win{font-size:12px;padding:6px 0;border-top:1px solid var(--dsw-alias-border-l1);display:flex;justify-content:space-between;gap:12px}
.dc_winName{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dc_winMeta{color:var(--dsw-alias-label-tertiary);flex:none;font-variant-numeric:tabular-nums}
`

    let styleEl = null

    /**
     * Tells the Host which conversation the user is looking at.
     *
     * Registered into a session-scoped slot, so it is mounted exactly while that
     * conversation is on screen and unmounted when the user switches away. That
     * mount/unmount is the only reliable signal the Host can get: the summon
     * must follow the user's attention, and "the newest session file" is a
     * guess that goes wrong the moment a second chat exists.
     *
     * WARNING: the session id arrives through the slot's `inject` callback, not
     * as a prop. The slot declaration is `inject: (sessionId, actions) => ({...})`
     * and those values land on the props object, so `props.sessionId` is where
     * it ends up -- but only when the inject ran for this occupant. Both shapes
     * are read here so a DSH version that passes it either way still works.
     *
     * It renders nothing -- the slot only exists to hand over the id.
     */
    function SessionFocus(props) {
      const sessionId = typeof props?.sessionId === 'string' && props.sessionId !== ''
        ? props.sessionId
        : (typeof props?.id === 'string' ? props.id : '')
      useEffect(() => {
        if (sessionId === '') return undefined
        let alive = true
        const report = () => {
          if (!alive) return
          void request(ROUTE_BOARD, '/active-session', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ sessionId }),
            timeout: 5000,
          }).catch(() => {})
        }
        report()
        // A conversation can stay open for a long time while the Host restarts
        // under it, so the claim is refreshed rather than sent once.
        const timer = setInterval(report, FOCUS_REPORT_MS)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [sessionId])
      return null
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    async function request(base, path, options) {
      const { timeout = 15000, ...rest } = options ?? {}
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeout)
      try {
        const response = await fetch(`${base}${path}`, {
          redirect: 'error',
          ...rest,
          signal: controller.signal,
        })
        const text = await response.text()
        const payload = text === '' ? {} : JSON.parse(text)
        if (!response.ok) throw new Error(String(payload?.error ?? `HTTP ${response.status}`))
        return payload
      } finally {
        clearTimeout(timer)
      }
    }

    function Row(props) {
      return h('div', { className: 'dc_row' },
        h('div', { className: 'dc_rowText' },
          h('div', { className: 'dc_rowTitle' }, props.title),
          props.hint === undefined ? null : h('div', { className: 'dc_rowHint' }, props.hint)),
        props.control === undefined ? null : h('div', { className: 'dc_controls' }, props.control))
    }

    function Switch(props) {
      return h('button', {
        type: 'button',
        className: 'dc_switch',
        role: 'switch',
        'aria-checked': props.checked === true ? 'true' : 'false',
        'aria-label': props.label,
        disabled: props.disabled === true,
        onClick: props.disabled === true ? undefined : props.onChange,
      }, h('i'))
    }

    // ── history tab ──────────────────────────────────────────────────────────

    function HistoryTab(props) {
      const { t } = props
      const [state, setState] = useState({ loading: true, files: [], dir: '', error: '' })
      const [open, setOpen] = useState('')
      const [menu, setMenu] = useState(null)
      const [zoom, setZoom] = useState('')

      const load = useCallback(async () => {
        try {
          const payload = await request(ROUTE_HISTORY, '/history')
          setState({
            loading: false,
            files: Array.isArray(payload?.files) ? payload.files : [],
            dir: String(payload?.dir ?? ''),
            error: '',
          })
        } catch (error) {
          setState({ loading: false, files: [], dir: '', error: String(error?.message ?? error) })
        }
      }, [])

      useEffect(() => { void load() }, [load])

      const clear = useCallback(async () => {
        try {
          await request(ROUTE_HISTORY, '/history', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ action: 'clear' }),
          })
          setOpen('')
          await load()
        } catch (error) {
          setState((s) => ({ ...s, error: String(error?.message ?? error) }))
        }
      }, [load])

      const removeOne = useCallback(async (name) => {
        try {
          await request(ROUTE_HISTORY, '/history/remove', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ name }),
          })
          if (open === name) setOpen('')
          await load()
        } catch (error) {
          setState((s) => ({ ...s, error: String(error?.message ?? error) }))
        }
      }, [load, open])

      // 右键菜单：点击任意处或按 Esc 关掉
      useEffect(() => {
        if (menu === null) return undefined
        const close = () => setMenu(null)
        const onKey = (e) => { if (e.key === 'Escape') setMenu(null) }
        window.addEventListener('click', close)
        window.addEventListener('keydown', onKey)
        return () => {
          window.removeEventListener('click', close)
          window.removeEventListener('keydown', onKey)
        }
      }, [menu])

      if (state.loading) return h('div', { className: 'dc_empty' }, t('common.loading'))
      if (state.error !== '') {
        return h('div', { className: 'dc_list' },
          h('div', { className: 'dc_err' }, `${t('common.error')}: ${state.error}`),
          h('button', { className: 'dc_btn', onClick: load }, t('common.retry')))
      }
      if (state.files.length === 0) return h('div', { className: 'dc_empty' }, t('history.empty'))

      const src = (name) => `${ROUTE_HISTORY}/history/file?name=${encodeURIComponent(name)}`

      const items = state.files.map((file) => h('div', {
        key: file.name,
        className: 'dc_item' + (open === file.name ? ' dc_item_open' : ''),
        onClick: () => setOpen(open === file.name ? '' : file.name),
        onContextMenu: (event) => {
          event.preventDefault()
          setMenu({ x: event.clientX, y: event.clientY, name: file.name })
        },
        title: '点击看图 · 右键删除',
      },
      h('img', { className: 'dc_thumb', src: src(file.name), alt: '' }),
      h('div', { className: 'dc_itemText' },
        h('div', { className: 'dc_itemName' }, file.name),
        h('div', { className: 'dc_itemMeta' },
          `${file.width}x${file.height} · ${Math.round((file.bytes ?? 0) / 1024)} KB`)),
      h('span', { className: 'dc_expand' }, open === file.name ? '收起' : '查看')))

      const preview = open === ''
        ? null
        : h('div', { className: 'dc_preview' },
          h('img', {
            className: 'dc_previewImg',
            src: src(open),
            alt: '',
            title: '点击看原尺寸',
            onClick: (event) => {
              event.stopPropagation()
              setZoom(src(open))
            },
          }))

      const foot = h('div', { className: 'dc_foot' },
        h('span', null, `${state.files.length} ${t('history.count')}`),
        h('button', { className: 'dc_btn dc_btn_danger', onClick: clear }, t('history.clear')))

      const contextMenu = menu === null
        ? null
        : h('div', {
          className: 'dc_menu',
          style: { left: `${menu.x}px`, top: `${menu.y}px` },
          onClick: (event) => event.stopPropagation(),
        },
        h('button', {
          className: 'dc_menuItem dc_menuItem_danger',
          onClick: () => { void removeOne(menu.name) },
        }, '删除这张'))

      const overlay = zoom === ''
        ? null
        : h('div', { className: 'dc_overlay', onClick: () => setZoom('') },
          h('img', { className: 'dc_overlayImg', src: zoom, alt: '' }))

      return h('div', null,
        h('div', { className: 'dc_list' }, items),
        preview,
        foot,
        contextMenu,
        overlay)
    }

    // ── eye tab ──────────────────────────────────────────────────────────────

    function EyeTab(props) {
      const { t } = props
      const [state, setState] = useState({ loading: true, screen: null, windows: [], recent: [], error: '' })
      const [busy, setBusy] = useState('')

      const load = useCallback(async () => {
        try {
          const info = await request(ROUTE_EYE, '/state')
          let windows = []
          try {
            const list = await request(ROUTE_EYE, '/capture', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ action: 'list' }),
              timeout: 20000,
            })
            windows = Array.isArray(list?.windows) ? list.windows : []
          } catch { /* 窗口列表失败不算致命 */ }
          setState({
            loading: false,
            screen: info?.screen ?? null,
            windows,
            recent: Array.isArray(info?.recent) ? info.recent : [],
            error: '',
          })
        } catch (error) {
          setState({ loading: false, screen: null, windows: [], recent: [], error: String(error?.message ?? error) })
        }
      }, [])

      useEffect(() => {
        void load()
        const timer = setInterval(() => {
          if (document.visibilityState === 'hidden') return
          void load()
        }, POLL_MS)
        return () => clearInterval(timer)
      }, [load])

      const shoot = useCallback(async (action) => {
        setBusy(action)
        try {
          await request(ROUTE_EYE, '/capture', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ action }),
            timeout: 30000,
          })
          await load()
        } catch (error) {
          setState((s) => ({ ...s, error: String(error?.message ?? error) }))
        } finally {
          setBusy('')
        }
      }, [load])

      if (state.loading) return h('div', { className: 'dc_empty' }, t('common.loading'))

      // ⚠ 「三种截图」那三行（全屏 / 桌面 / 单窗口）的 UI 已按要求移除。
      //   shoot() 和 targets 数组保留：接口还在，只是不再显示按钮。

      return h('div', null,
        state.error === '' ? null : h('div', { className: 'dc_err' }, state.error),

        h('div', { className: 'dc_section' }, t('eye.title')),
        h(Row, {
          title: t('eye.screen'),
          hint: t('eye.screenHint'),
          control: h('span', { className: 'dc_tag' },
            state.screen === null ? '—' : `${state.screen.w} × ${state.screen.h}`),
        }),

        h('div', { className: 'dc_section' }, t('eye.windows')),
        state.windows.length === 0
          ? h('div', { className: 'dc_empty' }, t('eye.windowsEmpty'))
          : state.windows.map((win, index) => h('div', {
            key: String(win.hwnd ?? index),
            className: 'dc_win',
          },
          h('span', { className: 'dc_winName' },
            (win.minimized === true ? '[最小化] ' : '') + String(win.title ?? '(untitled)')),
          h('span', { className: 'dc_winMeta' },
            `${win.w ?? 0}×${win.h ?? 0}`))),

        h('div', { className: 'dc_foot' },
          h('span', null, `${state.windows.length} window(s)`),
          h('button', { className: 'dc_btn', onClick: load }, t('eye.refresh'))))
    }

    // ── board tab（完整功能，与原神笔鲸良设置页一致） ─────────────────────────

    function BoardTab(props) {
      const { t } = props
      const [state, setState] = useState({
        loading: true,
        status: 'stopped',
        layers: 0,
        userStrokes: 0,
        error: '',
      })
      const [busy, setBusy] = useState(false)

      const load = useCallback(async () => {
        try {
          const info = await request(ROUTE_BOARD, '/state')
          const overlay = info?.overlay ?? {}
          setState({
            loading: false,
            status: String(overlay.status ?? info?.status ?? 'stopped'),
            layers: Array.isArray(info?.layers) ? info.layers.length : Number(info?.layerCount ?? 0),
            userStrokes: Number(info?.userStrokes ?? 0),
            error: overlay.error !== undefined && overlay.error !== '' ? String(overlay.error) : '',
          })
        } catch (error) {
          setState({ loading: false, status: 'error', layers: 0, userStrokes: 0, error: String(error?.message ?? error) })
        }
      }, [])

      useEffect(() => {
        void load()
        const timer = setInterval(() => {
          if (document.visibilityState === 'hidden') return
          void load()
        }, POLL_MS)
        return () => clearInterval(timer)
      }, [load])

      const call = useCallback(async (path, body) => {
        setBusy(true)
        try {
          await request(ROUTE_BOARD, path, {
            method: 'POST',
            headers: body === undefined ? undefined : { 'content-type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
            timeout: 30000,
          })
        } catch { /* 失败后靠 refresh 反映真实状态 */ }
        setBusy(false)
        await load()
      }, [load])

      if (state.loading) return h('div', { className: 'dc_empty' }, t('common.loading'))

      // A missing runtime is not a fault: the plugin ships code only, and the
      // environment comes from the dependency tab. Say so, and name the place to
      // go, instead of the bare "出错" that sent the user looking for a bug.
      const depsMissing = state.error === 'deps-missing'
      const statusText = depsMissing
        ? t('deps.needDeps')
        : state.status === 'running'
          ? t('board.status.running')
          : state.status === 'starting'
            ? t('board.status.starting')
            : state.status === 'error'
              ? t('board.status.error')
              : t('board.status.stopped')

      return h('div', null,
        state.error === '' || depsMissing ? null : h('div', { className: 'dc_err' }, state.error),
        depsMissing ? h('div', { className: 'dc_err' }, t('deps.needDepsHint')) : null,

        h('div', { className: 'dc_section' }, t('board.section.board')),
        h(Row, {
          title: t('board.window.title'),
          hint: t('board.window.hint'),
          control: [
            h('span', {
              key: 'st',
              className: state.status === 'error' ? 'dc_status dc_status_error' : 'dc_status',
            }, statusText),
            h(Switch, {
              key: 'sw',
              checked: state.status === 'running' || busy,
              disabled: busy,
              label: t('board.window.title'),
              onChange: () => { void call(state.status === 'running' ? '/overlay/stop' : '/overlay/start') },
            }),
          ],
        }),

        // ⚠ 「统计」「操作」「托盘菜单提示」「刷新按钮」都已按要求移除。
        //   只删界面：call() / load() / 各接口全部保留，仍然在跑。
      )
    }

    /**
     * Dependency install: the panel's one job that must survive a closed tab.
     *
     * The installer runs as its own process and keeps its progress in a state
     * file, so this tab only reads and asks. That is what makes "pause and
     * resume" honest: closing the panel does not cancel anything, and reopening
     * it shows the true position rather than a spinner.
     */
    function DepsTab(props) {
      const { t } = props
      const [state, setState] = useState({
        loading: true,
        status: 'idle',
        step: '',
        stepIndex: 0,
        stepCount: 0,
        detail: '',
        percent: 0,
        failed: [],
        installed: { ready: false, missing: [] },
      })
      const [busy, setBusy] = useState(false)

      const load = useCallback(async () => {
        try {
          const info = await request(ROUTE_BOARD, '/deps')
          setState({ loading: false, ...info })
        } catch (error) {
          setState((prev) => ({
            ...prev,
            loading: false,
            status: 'error',
            detail: String(error?.message ?? error),
          }))
        }
      }, [])

      useEffect(() => {
        void load()
        const timer = setInterval(() => {
          if (document.visibilityState === 'hidden') return
          void load()
        }, 1500)
        return () => clearInterval(timer)
      }, [load])

      const call = useCallback(async (path) => {
        setBusy(true)
        try {
          await request(ROUTE_BOARD, path, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: '{}',
            timeout: 20000,
          })
        } catch { /* 失败后靠 load() 反映真实状态 */ }
        setBusy(false)
        await load()
      }, [load])

      if (state.loading) return h('div', { className: 'dc_empty' }, t('common.loading'))

      const running = state.status === 'running' || state.status === 'pausing'
      const ready = state.installed !== undefined && state.installed.ready === true
      const missing = (state.installed !== undefined && Array.isArray(state.installed.missing))
        ? state.installed.missing
        : []
      const percent = Math.max(0, Math.min(100, Number(state.percent) || 0))
      const stepLine = state.step === ''
        ? ''
        : `${state.step} (${Number(state.stepIndex) + 1}/${state.stepCount})`

      let statusLine = t('deps.idle')
      if (state.status === 'done' || ready) statusLine = t('deps.done')
      else if (state.status === 'paused' || state.status === 'pausing') statusLine = t('deps.paused')
      else if (state.status === 'failed') statusLine = t('deps.failed')
      else if (running) statusLine = t('deps.running')

      return h('div', null,
        h('div', { className: 'dc_section' }, t('deps.title')),
        h(Row, { title: t('deps.hint'), hint: '' }),

        // Ready / missing, read from the filesystem rather than from the
        // installer's bookkeeping: that is the only answer that matters.
        h(Row, {
          title: ready ? t('deps.ready') : t('deps.missing') + missing.join(', '),
          hint: ready ? t('deps.needRestart') : '',
        }),

        h(Row, {
          title: t('deps.step'),
          hint: statusLine,
          control: h('span', { className: 'dc_status' }, stepLine),
        }),

        h(Row, {
          title: t('deps.progress'),
          hint: state.detail === '' ? '' : state.detail,
          control: h('span', { className: 'dc_status' }, `${percent}%`),
        }),

        // A plain bar: the panel already has no charting, and a percentage plus a
        // filled strip is enough to answer "is it moving".
        h('div', {
          style: {
            height: '6px',
            borderRadius: '3px',
            background: 'rgba(255,255,255,0.08)',
            overflow: 'hidden',
            margin: '8px 0 16px',
          },
        }, h('div', {
          style: {
            width: `${percent}%`,
            height: '100%',
            background: '#4ea1ff',
            transition: 'width 300ms ease',
          },
        })),

        state.failed.length === 0 ? null : h('div', { className: 'dc_err' },
          state.failed.map((item, index) => h('div', { key: String(index) },
            `${item.id}: ${item.error}`))),

        h('div', { style: { display: 'flex', gap: '8px', marginTop: '8px' } },
          h('button', {
            className: 'dc_btn',
            disabled: busy || running,
            onClick: () => { void call('/deps/start') },
          }, state.status === 'paused' || state.status === 'failed'
            ? t('deps.resume')
            : t('deps.download')),
          h('button', {
            className: 'dc_btn',
            disabled: busy || !running,
            onClick: () => { void call('/deps/stop') },
          }, t('deps.pause')),
          h('button', {
            className: 'dc_btn',
            disabled: busy || running,
            onClick: () => { void call('/deps/reset') },
          }, t('deps.reset'))),
      )
    }

    // ── page shell ───────────────────────────────────────────────────────────

    function CompanionPage(props) {
      const t = typeof props.t === 'function' ? props.t : boundT
      const [tab, setTab] = useState('history')

      const tabs = [
        { key: 'history', label: t('tab.history') },
        { key: 'eye', label: t('tab.eye') },
        { key: 'board', label: t('tab.board') },
        { key: 'deps', label: t('tab.deps') },
      ]

      let body = null
      if (tab === 'history') body = h(HistoryTab, { t })
      else if (tab === 'eye') body = h(EyeTab, { t })
      else if (tab === 'deps') body = h(DepsTab, { t })
      else body = h(BoardTab, { t })

      return h('div', { className: 'dc_page' },
        h('div', { className: 'dc_head' },
          h('div', { className: 'dc_title' }, t('page.title')),
          h('div', { className: 'dc_desc' }, t('page.description'))),
        h('div', { className: 'dc_tabs' },
          tabs.map((item) => h('button', {
            key: item.key,
            className: 'dc_tab',
            'data-active': String(tab === item.key),
            onClick: () => setTab(item.key),
          }, item.label))),
        body)
    }

    function apply(ctx) {
      const t = ctx.locale.bind(NS)
      boundT = t
      ctx.effect(() => ctx.locale.register(NS, { zh: DICT.zh, en: DICT.en }), 'dsh-companion: dictionaries')
      ctx.effect(() => {
        if (styleEl !== null) styleEl.remove()
        styleEl = document.createElement('style')
        styleEl.setAttribute('data-plugin', 'dsh-companion')
        styleEl.textContent = CSS
        document.head.appendChild(styleEl)
        return () => {
          if (styleEl !== null) styleEl.remove()
          styleEl = null
        }
      }, 'dsh-companion: styles')

      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'dsh-doodle-fat-fish',
        order: 45,
        label: () => t('nav'),
        locale: NS,
      }, (props) => h(CompanionPage, { ...props, t: typeof props?.t === 'function' ? props.t : t })))

      // The summon follows the conversation on screen. A session-scoped slot is
      // mounted exactly while its conversation is open, so reporting from here
      // is the honest answer to "which chat is the user looking at" -- and
      // unmounting when they switch away is what stops the old answer sticking.
      ctx.slots.inject('conversation.session.header.utilities', () => ctx.slots.register({
        name: 'conversation.session.header.utilities',
        id: 'dsh-doodle-fat-fish-focus',
        order: 900,
      }, (props) => h(SessionFocus, { sessionId: props?.sessionId })))
    }

    return { inject: ['slots', 'locale'], apply }
  },
})
