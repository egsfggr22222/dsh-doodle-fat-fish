/**
 * Fetch and unpack the plugin's own Python, then hand over to the installer.
 *
 * WHY THIS EXISTS
 * ---------------
 * The plugin must work on a Windows machine with no Python installed. But the
 * thing that installs Python is a Python program, so something has to run before
 * any Python exists. That something is Node: DSH already ships it, so it is the
 * one runtime guaranteed to be present.
 *
 * This script does exactly three things -- download the portable interpreter,
 * unpack it under runtime/, and re-run install_deps.py with it -- and then gets
 * out of the way. Every later run skips all of this because runtime/ exists.
 *
 * State is shared with the installer: both write the same state file, so the
 * panel shows one continuous progress bar rather than two phases that reset.
 */

import { spawn } from 'node:child_process'
import { createWriteStream, existsSync, mkdirSync, rmSync, statSync } from 'node:fs'
import { appendFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { pipeline } from 'node:stream/promises'
import { Readable } from 'node:stream'
import { execFileSync } from 'node:child_process'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = dirname(HERE)
const RUNTIME = join(PLUGIN, 'runtime')
const RUNTIME_PY = join(RUNTIME, 'python', 'python.exe')
const INSTALLER = join(HERE, 'install_deps.py')

/** Same source list as install_deps.py: GitHub direct, then two proxies. */
const PYTHON_VERSION = '3.10.16'
const ARCHIVE_NAME = `cpython-${PYTHON_VERSION}+20250115-x86_64-pc-windows-msvc-shared-install_only.tar.gz`
const GITHUB = 'https://github.com/astral-sh/python-build-standalone/releases/download/20250115/'
const URLS = [
  GITHUB + ARCHIVE_NAME,
  'https://ghproxy.net/' + GITHUB + ARCHIVE_NAME,
  'https://gh-proxy.com/' + GITHUB + ARCHIVE_NAME,
]
const EXPECTED_BYTES = 39020000

let statePath = ''
let logPath = ''

function log(message) {
  const line = `${new Date().toTimeString().slice(0, 8)} ${message}`
  process.stdout.write(`${line}\n`)
  if (logPath === '') return
  try { appendFileSync(logPath, `${line}\n`) } catch { /* best effort */ }
}

/** Read the shared state file, so this phase can add to the installer's progress. */
function readState() {
  try {
    const text = execFileSync(process.execPath, ['-e',
      `const fs=require('fs');try{process.stdout.write(fs.readFileSync(${JSON.stringify(statePath)},'utf8'))}catch{}`,
    ], { encoding: 'utf8' })
    return text === '' ? {} : JSON.parse(text)
  } catch {
    return {}
  }
}

function writeState(patch) {
  const doc = { ...readState(), ...patch, updatedAt: Date.now() }
  try {
    mkdirSync(dirname(statePath), { recursive: true })
    writeFileSync(statePath, JSON.stringify(doc, null, 1), 'utf8')
  } catch { /* the installer will report the real error */ }
}

/**
 * Download one URL to a file, resuming if the server allows.
 *
 * Deliberately simple compared with the Python downloader: this runs once, on a
 * file whose failure is not fatal (the next URL is tried), so a plain stream is
 * enough.
 */
async function download(url, target) {
  const response = await fetch(url, {
    redirect: 'follow',
    headers: { 'User-Agent': 'dsh-doodle-fat-fish' },
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const total = Number(response.headers.get('content-length') ?? 0) || EXPECTED_BYTES
  let got = 0
  const body = Readable.fromWeb(response.body)
  body.on('data', (chunk) => {
    got += chunk.length
    if (total > 0) {
      const percent = Math.round((got * 100) / total)
      writeState({
        detail: `内置 Python ${percent}%`,
        percent: Math.min(99, percent),
      })
    }
  })
  mkdirSync(dirname(target), { recursive: true })
  await pipeline(body, createWriteStream(target))
  const size = statSync(target).size
  if (size < 1024 * 1024) throw new Error(`文件过小：${size} 字节`)
  return size
}

/** Unpack the tarball. Windows 10+ ships bsdtar, which handles .tar.gz. */
function unpack(archive) {
  execFileSync('tar', ['-xzf', archive, '-C', RUNTIME], {
    windowsHide: true,
    stdio: 'ignore',
  })
}

async function main() {
  const args = process.argv.slice(2)
  const at = (name) => {
    const index = args.indexOf(name)
    return index >= 0 ? args[index + 1] : ''
  }
  statePath = at('--state')
  logPath = at('--log')

  if (statePath === '') {
    process.stderr.write('bootstrap: --state is required\n')
    return 2
  }

  // Already there: nothing to bootstrap, go straight to the installer.
  if (existsSync(RUNTIME_PY)) {
    log('内置 Python 已存在，直接进入安装器')
    return handOver()
  }

  writeState({
    status: 'running',
    step: '下载内置 Python',
    stepIndex: 0,
    stepCount: 19,
    detail: '正在下载内置 Python',
    percent: 0,
    startedAt: Date.now(),
  })

  const archive = join(RUNTIME, 'python.tar.gz')
  let lastError = ''
  for (const url of URLS) {
    try {
      log(`下载内置 Python：${new URL(url).host}`)
      await download(url, archive)
      log('下载完成，解压中…')
      unpack(archive)
      rmSync(archive, { force: true })
      if (!existsSync(RUNTIME_PY)) throw new Error('解压后找不到 python.exe')
      log(`内置 Python 就绪：${RUNTIME_PY}`)
      return handOver()
    } catch (error) {
      lastError = String(error?.message ?? error)
      log(`该源失败（${lastError}），换下一个`)
      rmSync(archive, { force: true })
    }
  }

  writeState({
    status: 'failed',
    detail: `内置 Python 下载失败：${lastError}`.slice(0, 300),
    failed: [{ id: 'runtime', error: lastError.slice(0, 300) }],
  })
  log(`内置 Python 下载失败：${lastError}`)
  return 1
}

/** Re-run the installer now that a Python exists. */
function handOver() {
  if (!existsSync(INSTALLER)) {
    log('install_deps.py 缺失，无法继续')
    return 1
  }
  log('交给安装器继续')
  const child = spawn(RUNTIME_PY, [INSTALLER,
    '--state', statePath,
    '--log', logPath,
  ], { windowsHide: true, stdio: 'ignore' })
  child.on('exit', (code) => {
    log(`安装器退出：${code ?? 0}`)
  })
  return 0
}

main().then((code) => {
  process.exitCode = code
}).catch((error) => {
  log(`bootstrap 失败：${error?.message ?? error}`)
  writeState({ status: 'failed', detail: String(error?.message ?? error).slice(0, 300) })
  process.exitCode = 1
})
