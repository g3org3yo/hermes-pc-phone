/**
 * Hermes desktop plugin: "PC - Phone".
 *
 * The PC half of the Hermes Android app (`handoff.mesh_broker`) controlled from
 * inside the desktop app:
 *   - a chip at the BOTTOM of the window (statusbar, right side): one click
 *     starts the broker, or opens its pairing console when it already runs;
 *     the dot is live — green = listening, dim = stopped;
 *   - a sidebar row "PC - Phone" opens a full page: status, paired devices, task
 *     counters, Start / Stop / Restart + QR / Open console, and the last log
 *     lines.
 *
 * The renderer cannot spawn processes, so every action goes through this
 * plugin's backend: dashboard/plugin_api.py -> /api/plugins/pc-phone/.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */
import { cn, haptic, host, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const ID = 'pc-phone'
const POLL_MS = 4000

// ctx.rest is only available inside register(); stash it for handlers/hooks.
let rest = null

function isMountError(err) {
  const msg = String((err && err.message) || err || '')
  return msg.includes('404') || msg.toLowerCase().includes('not found')
}

async function call(path, opts) {
  if (!rest) throw new Error('The plugin bridge is not available yet.')
  return rest(path, opts)
}

/** {state: 'checking'|'on'|'off'|'nomount', data, error, busy, refresh} */
function useMesh(pollMs) {
  const [state, setState] = useState('checking')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let alive = true
    let timer = null
    const probe = async () => {
      try {
        const res = await call('/status')
        if (!alive) return
        setData(res)
        setError(null)
        setState(res && res.running ? 'on' : 'off')
      } catch (err) {
        if (!alive) return
        setError(String((err && err.message) || err))
        setState(isMountError(err) ? 'nomount' : 'off')
      }
      timer = setTimeout(probe, pollMs)
    }
    probe()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [pollMs, tick])

  return { state, data, error, busy, setBusy, refresh }
}

/** Run a backend action with the busy flag + a notification on failure. */
async function act(mesh, path, okMessage) {
  haptic('tap')
  mesh.setBusy(true)
  try {
    const res = await call(path, { method: 'POST' })
    if (res && res.ok) {
      if (okMessage) host.notify({ kind: 'info', message: typeof okMessage === 'function' ? okMessage(res) : okMessage })
    } else {
      host.notify({ kind: 'error', message: (res && res.error) || 'That action failed.' })
    }
    return res
  } catch (err) {
    host.notify({ kind: 'error', message: String((err && err.message) || err) })
    return null
  } finally {
    mesh.setBusy(false)
    mesh.refresh()
  }
}

async function startMesh(mesh) {
  const res = await act(
    mesh,
    '/start',
    (r) => (r && r.already_running ? 'The link was already on.' : `The link is on (pid ${r && r.pid}).`)
  )
  return res
}

async function stopMesh(mesh) {
  return act(mesh, '/stop', 'The link is off.')
}

async function restartMesh(mesh) {
  return act(mesh, '/restart', 'Restarted — a fresh 5-minute pairing window is open.')
}

async function pairMesh(mesh) {
  const res = await act(mesh, '/pair')
  if (res && res.console_url) {
    host.notify({ kind: 'info', message: `Pairing QR: ${res.console_url} — scan it from the Android app.` })
  }
  return res
}

/** One click on the chip = ON/OFF. Right-click = fresh pairing QR. */
async function toggleMesh(mesh) {
  if (mesh.state === 'on') return stopMesh(mesh)
  return startMesh(mesh)
}

function openConsole(data) {
  haptic('tap')
  const url = data && data.console_url
  if (url) {
    try {
      if (host.openExternal) host.openExternal(url)
      else window.open(url, '_blank')
    } catch {
      host.notify({ kind: 'info', message: `Console: ${url}` })
    }
  } else {
    host.notify({ kind: 'info', message: 'No console URL yet — restart the link for a fresh QR.' })
  }
}

/** First-run setup: clone the PC companion + make its isolated venv, live log. */
function SetupCard() {
  const [st, setSt] = useState(null)
  const [log, setLog] = useState([])
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await call('/setup')
      setSt(s)
      const l = await call('/setup/log?tail=40')
      setLog((l && l.lines) || [])
    } catch {
      /* backend not mounted yet */
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 2500)
    return () => clearInterval(t)
  }, [load])

  if (!st || st.ready) return null

  const start = async () => {
    haptic('tap')
    setBusy(true)
    try {
      const res = await call('/setup/install', { method: 'POST' })
      if (!res || !res.ok) host.notify({ kind: 'error', message: (res && res.error) || 'Setup failed.' })
    } catch (err) {
      host.notify({ kind: 'error', message: String((err && err.message) || err) })
    } finally {
      setBusy(false)
      load()
    }
  }

  const missing = []
  if (!st.repo) missing.push('the PC companion source')
  if (!st.python) missing.push('its isolated Python venv')
  if (st.repo && st.python && !st.deps_ok) missing.push('Python packages (PyNaCl, zeroconf, qrcode)')

  return jsxs('div', {
    className:
      'max-w-2xl rounded border border-(--ui-warning,#fbbf24) bg-(--chrome-action-hover) px-3 py-2 leading-relaxed text-(--ui-text-secondary)',
    children: [
      jsx('div', { className: 'font-medium', children: 'One-time setup needed' }),
      jsx('div', {
        className: 'mt-1 text-(--ui-text-tertiary)',
        children: `Missing: ${missing.join(', ')}. The setup below clones the open-source PC companion into your Hermes home and builds a private venv for it — nothing else on your system is touched.`
      }),
      jsxs('div', {
        className: 'mt-2 flex items-center gap-2',
        children: [
          btn(st.running || busy ? 'Setting up…' : 'Run setup', start, st.running || busy),
          jsx('span', {
            className: 'text-(--ui-text-quaternary)',
            children: st.step || ''
          })
        ]
      }),
      log.length
        ? jsx('pre', {
            className:
              'mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded border border-(--ui-stroke-secondary) bg-(--chrome-action-hover) p-2 font-mono text-[0.6875rem] leading-relaxed text-(--ui-text-tertiary)',
            children: log.join('\n')
          })
        : null,
      st.error ? jsx('div', { className: 'mt-1 text-(--ui-danger,#f87171)', children: st.error }) : null
    ]
  })
}

function MeshChip() {
  const mesh = useMesh(POLL_MS)
  const { state, busy } = mesh
  const dot =
    state === 'on'
      ? 'bg-(--ui-success,#4ade80)'
      : state === 'nomount'
        ? 'bg-(--ui-warning,#fbbf24)'
        : 'bg-(--ui-text-quaternary)'
  const label = 'PC - Phone'
  const title =
    state === 'on'
      ? 'PC ↔ Phone: on — click to turn it off (right-click = new QR for the phone)'
      : state === 'nomount'
        ? 'The plugin backend is not loaded yet — open the PC - Phone page'
        : 'PC ↔ Phone: off — click to turn it on (link this PC to your phone)'

  return jsx('button', {
    type: 'button',
    title,
    'aria-pressed': state === 'on',
    className: cn(
      'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors',
      state === 'on'
        ? 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground'
        : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
      busy && 'opacity-60'
    ),
    onClick: () => toggleMesh(mesh),
    onContextMenu: (event) => {
      if (event && event.preventDefault) event.preventDefault()
      pairMesh(mesh)
    },
    children: [jsx('span', { className: cn('inline-block h-1.5 w-1.5 rounded-full', dot) }), label]
  })
}

function Row({ k, v }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-3 border-b border-(--ui-stroke-secondary) py-1',
    children: [
      jsx('span', { className: 'text-(--ui-text-tertiary)', children: k }),
      jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: String(v) })
    ]
  })
}

function MeshPage() {
  const mesh = useMesh(POLL_MS)
  const { state, data, error, busy } = mesh
  const [logLines, setLogLines] = useState([])

  const loadLog = useCallback(async () => {
    try {
      const res = await call('/log?tail=30')
      setLogLines((res && res.lines) || [])
    } catch {
      setLogLines([])
    }
  }, [])

  useEffect(() => {
    loadLog()
  }, [loadLog, data && data.running])

  const badge = state === 'on' ? 'Running' : state === 'nomount' ? 'Backend not mounted' : state === 'checking' ? 'Checking…' : 'Stopped'
  const badgeTone =
    state === 'on'
      ? 'text-(--ui-success,#4ade80)'
      : state === 'nomount'
        ? 'text-(--ui-warning,#fbbf24)'
        : 'text-(--ui-text-tertiary)'

  const btn = (label, fn, primary, disabled) =>
    jsx('button', {
      type: 'button',
      disabled: disabled || busy,
      onClick: fn,
      className: cn(
        'rounded border px-3 py-1.5 text-xs transition-colors disabled:opacity-50',
        primary
          ? 'border-(--ui-accent) text-(--ui-accent) hover:bg-(--chrome-action-hover)'
          : 'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      children: label
    })

  return jsxs('div', {
    className: 'flex h-full w-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center justify-between gap-2 border-b border-(--ui-stroke-secondary) px-3 py-1.5 text-xs',
        children: [
          jsxs('span', {
            className: 'flex items-center gap-2 font-medium text-(--ui-text-secondary)',
            children: ['PC ↔ Phone (Hermes mobile)', jsx('span', { className: badgeTone, children: badge })]
          }),
          jsx('span', {
            className: 'text-(--ui-text-quaternary)',
            children: data && data.pid ? `pid ${data.pid} · :${data.port}` : 'not listening'
          })
        ]
      }),
      jsxs('div', {
        className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-auto px-4 py-4 text-xs',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              btn('Start', () => startMesh(mesh), state !== 'on', state === 'on'),
              btn('Restart + QR', () => pairMesh(mesh), state === 'on'),
              btn('Open console', () => openConsole(data), false, state !== 'on'),
              btn('Stop', () => stopMesh(mesh), false, state !== 'on')
            ]
          }),
          jsx(SetupCard, {}),
          jsx('div', {
            className: 'max-w-2xl leading-relaxed text-(--ui-text-tertiary)',
            children:
              'PC - Phone drives the PC-side companion of the Hermes-agent Android app: pair once by QR and the phone can dispatch tasks to `hermes` on this computer and hand conversations back and forth — NaCl-encrypted, LAN only, no cloud. "Restart + QR" opens a fresh 5-minute pairing window.'
          }),
          state === 'nomount'
            ? jsxs('div', {
                className:
                  'max-w-2xl rounded border border-(--ui-stroke-secondary) bg-(--chrome-action-hover) px-3 py-2 leading-relaxed text-(--ui-text-secondary)',
                children: [
                  jsx('div', { className: 'font-medium', children: 'The plugin backend is not loaded yet.' }),
                  jsx('div', {
                    className: 'mt-1 text-(--ui-text-tertiary)',
                    children: 'The /api/plugins/pc-phone/ routes load with the backend — one restart is enough.'
                  }),
                  jsx('div', {
                    className: 'mt-2',
                    children: btn('Restart backend', () => {
                      haptic('tap')
                      if (host.restartGateway) host.restartGateway()
                    })
                  })
                ]
              })
            : null,
          error && state !== 'nomount'
            ? jsx('div', { className: 'max-w-2xl text-(--ui-danger,#f87171)', children: `Error: ${error}` })
            : null,
          jsxs('div', {
            className: 'max-w-2xl',
            children: [
              jsx('div', { className: 'mb-1 font-medium text-(--ui-text-secondary)', children: 'Details' }),
              jsx(Row, { k: 'bind', v: data && data.host ? `${data.host}:${data.port}` : '—' }),
              jsx(Row, { k: 'paired devices', v: data ? data.paired : '—' }),
              jsx(Row, { k: 'peer ids', v: data && data.peers && data.peers.length ? data.peers.join(', ') : '—' }),
              jsx(Row, { k: 'tasks / results', v: data ? `${data.tasks} / ${data.results}` : '—' }),
              jsx(Row, { k: 'console', v: (data && data.console_url) || '—' }),
              jsx(Row, { k: 'repo', v: (data && data.repo) || '—' }),
              jsx(Row, { k: 'python', v: (data && data.python) || '—' }),
              jsx(Row, { k: 'log', v: (data && data.log) || '—' })
            ]
          }),
          jsx('div', {
            className: 'max-w-2xl truncate text-(--ui-text-quaternary)',
            children: 'Pairing survives restarts. Delete id.key and the phone needs a new QR.'
          }),
          jsxs('div', {
            className: 'max-w-2xl',
            children: [
              jsxs('div', {
                className: 'mb-1 flex items-center justify-between font-medium text-(--ui-text-secondary)',
                children: ['broker.log (last 30 lines)', btn('Refresh', loadLog)]
              }),
              jsx('pre', {
                className:
                  'max-h-64 overflow-auto rounded border border-(--ui-stroke-secondary) bg-(--chrome-action-hover) p-2 font-mono text-[0.6875rem] leading-relaxed text-(--ui-text-tertiary)',
                children: logLines.length ? logLines.join('\n') : '(empty)'
              })
            ]
          }),
          data && data.last_error
            ? jsx('div', { className: 'max-w-2xl text-(--ui-danger,#f87171)', children: `Last error: ${data.last_error}` })
            : null
        ]
      })
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'PC - Phone',
  register(ctx) {
    rest = ctx.rest

    ctx.register({ id: 'route', area: ROUTES_AREA, data: { path: '/mesh-button' }, render: () => jsx(MeshPage, {}) })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/mesh-button', label: 'PC - Phone', codicon: 'device-mobile' } })
    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 205, render: () => jsx(MeshChip, {}) })
  }
}
