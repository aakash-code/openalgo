/**
 * Writer Flow on the chart: what option writers did at each strike, through
 * the day, drawn where it happened.
 *
 * A strike is a price and the record is minute by minute, so the picture lines
 * up with the candles without any translation:
 *
 * - Behind the candles, a band at each strike price, one cell per step. Green
 *   is flow that helps the underlying (call writers covering, put writers
 *   adding), red is flow that weighs on it (call writers adding, put writers
 *   covering). The stronger the colour, the more open interest moved.
 * - At the right edge, each strike's current streak: a bar as long as the open
 *   interest it has moved, coloured the same way, with how many minutes it has
 *   run. Hover a strike for the reading in words.
 * - Diamonds where a writing or covering streak reached 10, 15 and 20 minutes.
 *
 * Writer Flow Totals, in its own pane: the call and put open interest change
 * since the record began, and the net of the two for every bar.
 *
 * Everything comes from /writerflow/api/flow, which reads the OI Flow
 * recorder's database (OI_FLOW_RECORDER_ENABLED in .env). Opening the chart
 * makes no broker call. Separate from the OI Profile; neither reads the other.
 *
 * Strikes sit at their own price, so on a futures chart, which trades at a
 * premium to the index, the bands sit that premium below where an index chart
 * would put them. The index chart is the one these read best on.
 */

const RED = '#ef4444'
const GREEN = '#22c55e'
const NEUTRAL = '#94a3b8'
const TOOLTIP_BG = 'rgba(23, 23, 23, 0.94)'
const TOOLTIP_FG = '#f8fafc'
const HOVER_ID = 'writer-flow-strike:'
const EVENT_ID = 'writer-flow-event:'
const REFRESH_MS = 60 * 1000
const SETTINGS_POLL_MS = 1000
// A symbol that is not recorded yet starts recording only once the chart has
// stayed on it this long. Flicking through ten stocks would otherwise start
// ten recordings and some 600 history calls to the broker, for symbols looked
// at for a second each.
const START_AFTER_MS = 15 * 1000

// How each reading is said to a trader. `bias` comes from the server: +1 helps
// the underlying, -1 weighs on it, 0 says nothing about direction.
const READING = {
  writing: 'writers adding',
  covering: 'writers covering',
  buying: 'buyers adding',
  unwinding: 'buyers exiting',
  adding: 'open interest rising',
  reducing: 'open interest falling',
  steady: 'no change',
}

function formatLakh(value) {
  const n = Number(value) || 0
  const sign = n > 0 ? '+' : n < 0 ? '-' : ''
  const abs = Math.abs(n)
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}K`
  return `${sign}${Math.round(abs)}`
}

// One colour per action, so call writers adding and put writers adding are
// never the same green or red at a glance. Calls warm and cold (red adding,
// sky blue covering), puts green and amber; buyers violet and pink; a move
// with no premium change grey.
const ACTION_COLORS = {
  'CE:writing': '#ef4444',
  'CE:covering': '#38bdf8',
  'PE:writing': '#22c55e',
  'PE:covering': '#f59e0b',
  buying: '#a78bfa',
  unwinding: '#f472b6',
  adding: '#64748b',
  reducing: '#64748b',
}

// The legend, in the order a trader reads it.
const LEGEND = [
  ['CE:writing', 'Call writers adding'],
  ['CE:covering', 'Call writers covering'],
  ['PE:writing', 'Put writers adding'],
  ['PE:covering', 'Put writers covering'],
  ['buying', 'Buyers adding'],
  ['unwinding', 'Buyers exiting'],
  ['adding', 'OI moving, side unclear'],
]

// The words on a streak bar: who, what, how long. "Call cover 8m".
const SHORT = {
  writing: 'write',
  covering: 'cover',
  buying: 'buy',
  unwinding: 'exit',
  adding: '+OI',
  reducing: '-OI',
}

function actionColor(side, cls) {
  return ACTION_COLORS[`${side}:${cls}`] ?? ACTION_COLORS[cls] ?? NEUTRAL
}

// Open interest as the broker quotes it: lakh quantity on NSE and BSE, lots
// on MCX (the server reads which from the data, oi_unit). Never "contracts":
// the exchange counts one lot as one contract, 65 times fewer for NIFTY.
function formatOi(value, unit) {
  if (unit !== 'lots') return formatLakh(value)
  const n = Math.round(Number(value) || 0)
  return `${n > 0 ? '+' : ''}${n.toLocaleString('en-IN')} lots`
}

function biasColor(bias) {
  return bias > 0 ? GREEN : bias < 0 ? RED : NEUTRAL
}

/** The chart's own symbol is the default: NIFTY30OCT25FUT -> NIFTY. */
function resolveUnderlying(settings, symbol) {
  const typed = String(settings.underlying ?? '').trim().toUpperCase()
  if (typed) return typed.replace(/[^A-Z0-9&-]/g, '')
  return String(symbol ?? '')
    .toUpperCase()
    .replace(/\d{2}[A-Z]{3}\d{2}FUT$/, '')
    .replace(/\d{2}[A-Z]{3}\d{2}[\d.]*(?:CE|PE)$/, '')
    .replace(/[^A-Z0-9&-]/g, '')
}

function stepOf(settings) {
  const s = String(settings.step ?? 'auto')
  return ['auto', '60', '180', '300'].includes(s) ? s : 'auto'
}

/**
 * One request per underlying and step, shared by every instance and both
 * indicators: the heatmap and the totals pane on one chart, or two charts of
 * NIFTY, ask the server once a beat between them. Bounded, oldest dropped.
 */
const answers = new Map()
// The loader's server-event listener (lib/serverEvents.ts), if this OpenAlgo
// has one. The server announces each recorded minute with UPDATE_EVENT, and an
// open chart fetches it at once instead of at its next 60-second poll.
let onServerEvent = null
const UPDATE_EVENT = 'writer_flow_update'
const ANSWERS_MAX = 8

function fetchFlow(underlying, step, maxAgeMs, exchange = '') {
  const url =
    `/writerflow/api/flow?underlying=${encodeURIComponent(underlying)}` +
    `&step=${encodeURIComponent(step)}&all_strikes=1&lite=1` +
    // The chart's own exchange: an underlying it shows that is not being
    // recorded yet starts recording, with the day so far filled from history.
    (exchange ? `&exchange=${encodeURIComponent(exchange)}` : '')
  return fetchShared(`${underlying}|${step}|${exchange}`, url, maxAgeMs)
}

/** The option levels for an underlying: read from the record, never starts one. */
function fetchLevels(underlying, _step, maxAgeMs) {
  return fetchShared(
    `levels|${underlying}`,
    `/writerflow/api/levels?underlying=${encodeURIComponent(underlying)}`,
    maxAgeMs,
  )
}

function fetchShared(key, url, maxAgeMs) {
  const held = answers.get(key)
  if (held && (held.promise || Date.now() - held.at < maxAgeMs)) return held.promise || Promise.resolve(held.body)
  const promise = fetch(url, { credentials: 'same-origin' })
    .then(async (res) => {
      const body = await res.json().catch(() => null)
      if (!body) throw new Error('unreadable answer')
      return body
    })
    .then((body) => {
      answers.delete(key)
      answers.set(key, { at: Date.now(), body, promise: null })
      while (answers.size > ANSWERS_MAX) answers.delete(answers.keys().next().value)
      return body
    })
    .catch((err) => {
      answers.delete(key)
      throw err
    })
  answers.set(key, { at: 0, body: null, promise })
  return promise
}

/** Drops every shared answer. For tests, which each expect a cold start. */
export function forgetFlowAnswers() {
  answers.clear()
}

/**
 * The poll loop both indicators share in shape: ask now, ask again every beat
 * while the tab is in front, start over when the underlying or step changes.
 * `apply(body)` receives each answer; `fail(message)` a reason in words.
 */
function startPolling(ctx, apply, fail, fetcher = fetchFlow) {
  let cancelled = false
  let timer = null
  let watchedKey = ''
  let keySince = 0
  let startTimer = null

  const keyOf = () => {
    const settings = ctx.settings()
    const exchange = String(ctx.dataContext?.()?.exchange ?? '').toUpperCase().replace(/[^A-Z_]/g, '')
    return `${resolveUnderlying(settings, ctx.symbol?.())}|${stepOf(settings)}|${exchange}`
  }

  const run = async ({ fresh = false } = {}) => {
    const key = keyOf()
    if (key !== watchedKey) {
      keySince = Date.now()
      if (startTimer) clearTimeout(startTimer)
      // Ask once more when the wait is over, so a recording starts then
      // rather than at the next minute's beat.
      startTimer = setTimeout(() => {
        startTimer = null
        if (!cancelled && !document.hidden && keyOf() === watchedKey) run()
      }, START_AFTER_MS + 100)
    }
    watchedKey = key
    const [underlying, step, chartExchange] = key.split('|')
    if (!underlying) {
      fail('Choose an underlying in the settings, such as NIFTY.')
      return
    }
    // Until the wait is over the request only reads what is recorded.
    const settled = Date.now() - keySince >= START_AFTER_MS
    const exchange = settled ? chartExchange : ''
    try {
      // A fresh ask follows the server's word that a new minute is in, so a
      // shared answer from moments ago is not good enough.
      const body = await fetcher(underlying, step, fresh ? 0 : REFRESH_MS / 2, exchange)
      if (cancelled || key !== watchedKey) return
      if (body.status === 'success') apply(body)
      else if (!settled && chartExchange && body.recorder_enabled)
        fail(`Not recorded yet. Stay on this chart a few seconds and Writer Flow starts recording ${underlying}.`)
      else fail(body.message || 'Nothing recorded for this yet.')
    } catch {
      if (!cancelled) fail('Could not reach OpenAlgo. Trying again in a minute.')
    }
  }

  const beat = () => {
    if (cancelled) return
    if (!document.hidden) run()
    timer = setTimeout(beat, REFRESH_MS)
  }

  const watcher = setInterval(() => {
    if (cancelled || keyOf() === watchedKey) return
    run()
  }, SETTINGS_POLL_MS)

  const onVisible = () => {
    if (!document.hidden) run()
  }
  document.addEventListener('visibilitychange', onVisible)

  // A new minute for this chart's underlying: fetch it now, and push the
  // poll back a full beat, since it would only fetch the same minute again.
  // The poll stays as the safety net for a missed announcement.
  const stopHearing =
    typeof onServerEvent === 'function'
      ? onServerEvent(UPDATE_EVENT, (data) => {
          if (cancelled || document.hidden) return
          if (!data || data.underlying !== watchedKey.split('|')[0]) return
          run({ fresh: true })
          if (timer) clearTimeout(timer)
          timer = setTimeout(beat, REFRESH_MS)
        })
      : null

  run()
  timer = setTimeout(beat, REFRESH_MS)

  return () => {
    cancelled = true
    if (timer) clearTimeout(timer)
    if (startTimer) clearTimeout(startTimer)
    clearInterval(watcher)
    document.removeEventListener('visibilitychange', onVisible)
    stopHearing?.()
  }
}

/** Value of one heatmap cell for the chosen view, signed by bias. */
/** The size that reads as full colour: the 95th percentile, so one outlier does not wash out the rest. */
function heatScale(body, view) {
  const all = []
  const sides = view === 'calls' ? ['ce'] : view === 'puts' ? ['pe'] : ['ce', 'pe']
  for (const side of sides) {
    for (const row of body.heat[side]) {
      for (const v of row) if (v) all.push(Math.abs(v))
    }
  }
  if (!all.length) return 1
  all.sort((a, b) => a - b)
  return all[Math.min(all.length - 1, Math.floor(all.length * 0.95))] || 1
}

function strikeGap(strikes) {
  const gaps = []
  for (let i = 1; i < strikes.length; i++) gaps.push(strikes[i] - strikes[i - 1])
  gaps.sort((a, b) => a - b)
  return gaps.length ? gaps[Math.floor(gaps.length / 2)] : 50
}

function sideText(label, side, unit) {
  if (!side) return `${label}: not recorded`
  if (!side.observed) return `${label}: out of the recorded window since ${side.last_seen ?? '-'}`
  if (side.cls === 'steady') return `${label}: no change for ${Math.round(side.steady_minutes)} min`
  return `${label}: ${READING[side.cls] ?? side.cls} for ${Math.round(side.streak_minutes)} min, ${formatOi(side.streak_oi_change, unit)}`
}

// The pane's right edge, shared by the studies pinned to it. OI Profile holds
// the strip beside the price axis and Writer Flow draws to its left; drawn
// without knowing of each other, Writer Flow's current streaks sat on top of
// the open interest bars and its newest strips ran underneath them. Each
// holder registers a function giving its width for the frame being drawn, so
// the two always agree, keyed by the pane's canvas so charts in a grid never
// move each other. The same few lines live in both files: an indicator is
// loaded on its own and cannot import another.
const RIGHT_EDGE = (globalThis.__oaRightEdge ??= new WeakMap())

export function holdRightEdge(canvas, owner, widthFor) {
  let holders = RIGHT_EDGE.get(canvas)
  if (!holders) RIGHT_EDGE.set(canvas, (holders = new Map()))
  holders.set(owner, widthFor)
}

export function releaseRightEdge(canvas, owner) {
  RIGHT_EDGE.get(canvas)?.delete(owner)
}

export function rightEdgeHeld(canvas, rc, except) {
  let px = 0
  for (const [owner, widthFor] of RIGHT_EDGE.get(canvas) ?? []) {
    if (owner === except) continue
    try {
      px = Math.max(px, Number(widthFor(rc)) || 0)
    } catch {
      // A holder that cannot answer holds nothing this frame.
    }
  }
  return px
}

export default function ({ registerIndicator, nulls, onServerEvent: listen }) {
  onServerEvent = typeof listen === 'function' ? listen : null
  const underlyingInput = {
    key: 'underlying', type: 'text', label: 'Underlying', default: '', group: 'Instrument',
    tooltip: "Left blank, the chart's own symbol is used: NIFTY futures read NIFTY.",
  }
  const stepInput = {
    key: 'step', type: 'select', label: 'Step', default: 'auto', group: 'Instrument',
    tooltip: 'Auto follows how often the exchange updates open interest for this underlying.',
    options: [
      { label: 'Auto', value: 'auto' },
      { label: '1 minute', value: '60' },
      { label: '3 minutes', value: '180' },
      { label: '5 minutes', value: '300' },
    ],
  }

  registerIndicator({
    id: 'writer-flow-live',
    name: 'Writer Flow',
    category: 'Custom',
    placement: 'onchart',
    // Each cell is placed by its own time, so it needs no Timeframe row.
    noTimeframe: true,

    inputs: [
      underlyingInput,
      stepInput,
      {
        key: 'view', type: 'select', label: 'Show', default: 'both', group: 'Display',
        options: [
          { label: 'Calls and puts', value: 'both' },
          { label: 'Calls only', value: 'calls' },
          { label: 'Puts only', value: 'puts' },
        ],
      },
      { key: 'heatmap', type: 'boolean', label: 'Flow Behind Candles', default: true, group: 'Display' },
      { key: 'streaks', type: 'boolean', label: 'Current Streaks At Right Edge', default: true, group: 'Display' },
      { key: 'events', type: 'boolean', label: 'Streak Markers (10, 15, 20 min)', default: true, group: 'Display' },
      { key: 'opacity', type: 'number', label: 'Flow Opacity (%)', default: 35, min: 10, max: 100, step: 5, group: 'Display' },
      {
        key: 'minStreak', type: 'number', label: 'Label Streaks From (min)', default: 5, min: 1, max: 60, step: 1, group: 'Display',
        tooltip: 'Shorter streaks are drawn without their minutes, so the column stays readable.',
      },
      { key: 'barWidth', type: 'number', label: 'Max Streak Bar Width (px)', default: 110, min: 30, max: 300, step: 10, group: 'Display' },
      {
        key: 'colours', type: 'select', label: 'Streak Colours', default: 'action', group: 'Display',
        tooltip: 'By action: a colour each for call writers adding or covering and put writers adding or covering. By direction: green helps the index, red weighs on it.',
        options: [
          { label: 'By action', value: 'action' },
          { label: 'By direction (green / red)', value: 'direction' },
        ],
      },
      { key: 'legend', type: 'boolean', label: 'Colour Key', default: true, group: 'Display' },
    ],

    // The contract needs one plot; the drawing is the two primitives below.
    plots: [{ key: 'wf', type: 'line', title: 'Writer Flow', style: { visible: false } }],

    calc(bars) {
      return { wf: nulls(new Array(bars.length).fill(NaN)) }
    },

    attach(ctx) {
      // edgeHeld: the right-edge strip another study holds (OI Profile), read
      // on every draw; streaks sit left of it and strips stop at it.
      const state = { body: null, scale: 1, scaleFor: '', problem: null, updatedAt: 0, edgeHeld: 0 }
      let hoverPoint = null

      const timeX = (rc, t) => rc.timeScale.indexToX(rc.dataLayer.timeToIndexFloat(t))

      const scaleFor = (view) => {
        const key = `${state.updatedAt}|${view}`
        if (state.scaleFor !== key && state.body) {
          state.scale = heatScale(state.body, view)
          state.scaleFor = key
        }
        return state.scale
      }

      // Each strike as a pair of strips hugging its line: calls just above,
      // puts just below. Capped, so a chart zoomed in to a few strikes keeps
      // its candles readable instead of turning into blocks of colour.
      const STRIP_MAX_PX = 16
      const bands = (rc) => {
        const body = state.body
        if (!body) return []
        const gap = strikeGap(body.strikes)
        const out = []
        for (let i = 0; i < body.strikes.length; i++) {
          const strike = body.strikes[i]
          const y = rc.priceScale.priceToY(strike)
          if (y == null || Number.isNaN(y)) continue
          const gapPx = Math.abs(rc.priceScale.priceToY(strike + gap) - y)
          const strip = Math.max(1, Math.min(STRIP_MAX_PX, gapPx * 0.4))
          if (y + strip < 0 || y - strip > rc.plotHeight) continue
          out.push({ i, strike, y, strip, top: y - strip, bottom: y + strip })
        }
        return out
      }

      // Where a side is drawn in a band: calls above the line, puts below,
      // and a single view centred on it.
      const sideRect = (b, side, view) => {
        if (view !== 'both') return { top: b.y - b.strip / 2, h: b.strip }
        return side === 'ce' ? { top: b.y - b.strip, h: b.strip - 0.5 } : { top: b.y + 0.5, h: b.strip - 0.5 }
      }

      const streakLayout = (rc, settings) => {
        const body = state.body
        if (!body || settings.streaks === false) return null
        const width = Math.max(30, Math.floor(Number(settings.barWidth) || 110))
        const right = rc.plotWidth - 4 - state.edgeHeld
        const view = String(settings.view || 'both')
        let max = 1
        for (const row of body.ladder) {
          for (const side of ['ce', 'pe']) {
            const s = row[side]
            if (s?.observed && s.cls !== 'steady') max = Math.max(max, Math.abs(s.streak_oi_change || 0))
          }
        }
        const byStrike = new Map(body.ladder.map((r) => [r.strike, r]))
        const rows = []
        for (const b of bands(rc)) {
          const row = byStrike.get(b.strike)
          if (!row || !row.observed) continue
          rows.push({ ...b, row })
        }
        return { rows, right, left: right - width, width, max, view }
      }

      const heatPrimitive = {
        zOrder() {
          return 'bottom'
        },
        draw(c, rc) {
          const settings = ctx.settings()
          state.edgeHeld = rightEdgeHeld(c, rc)
          const body = state.body
          if (!body || settings.heatmap === false) return
          const stripEnd = rc.plotWidth - state.edgeHeld
          const dpr = rc.dpr || 1
          const view = String(settings.view || 'both')
          const scale = scaleFor(view)
          const alpha = Math.min(1, Math.max(0.1, (Number(settings.opacity) || 35) / 100))
          const half = rc.timeScale.barSpacing / 2
          const times = body.times_epoch
          const stepS = Number(body.step_seconds) || 60
          // x for every step once, not once per strike.
          const xs = times.map((t) => timeX(rc, t) - half)
          const xEnd = timeX(rc, times[times.length - 1] + stepS) - half
          const sides = view === 'calls' ? ['ce'] : view === 'puts' ? ['pe'] : ['ce', 'pe']
          // Below this a move is noise against the day's flow and is left
          // out, so the strips show where something happened.
          const floor = scale * 0.08
          c.save()
          for (const b of bands(rc)) {
            for (const side of sides) {
              const row = body.heat[side][b.i]
              if (!row) continue
              const { top, h } = sideRect(b, side, view)
              for (let j = 0; j < times.length; j++) {
                const v = row[j]
                if (!v || Math.abs(v) < floor) continue
                const x0 = xs[j]
                const x1 = j + 1 < times.length ? xs[j + 1] : xEnd
                const x1c = Math.min(x1, stripEnd)
                if (x1c < 0 || x0 > stripEnd || !(x1c > x0)) continue
                c.globalAlpha = alpha * Math.min(1, 0.25 + (0.75 * Math.abs(v)) / scale)
                c.fillStyle = v > 0 ? GREEN : RED
                c.fillRect(x0 * dpr, top * dpr, Math.max(1, x1c - x0) * dpr, h * dpr)
              }
            }
          }
          c.restore()
        },
      }

      const drawTooltip = (c, rc, lines) => {
        if (!hoverPoint) return
        const dpr = rc.dpr || 1
        const fontPx = 11
        const pad = 6
        c.font = `${fontPx * dpr}px sans-serif`
        const w = Math.max(...lines.map((l) => c.measureText(l).width / dpr))
        const h = lines.length * (fontPx + 4) + pad * 2
        const x = Math.max(4, hoverPoint.x - w - pad * 2 - 14)
        const y = Math.min(rc.plotHeight - h - 4, Math.max(4, hoverPoint.y - h / 2))
        c.globalAlpha = 1
        c.fillStyle = TOOLTIP_BG
        c.beginPath()
        c.roundRect(x * dpr, y * dpr, (w + pad * 2) * dpr, h * dpr, 4 * dpr)
        c.fill()
        c.fillStyle = TOOLTIP_FG
        c.textBaseline = 'top'
        lines.forEach((l, k) => c.fillText(l, (x + pad) * dpr, (y + pad + k * (fontPx + 4)) * dpr))
      }

      // Where each visible streak marker sits this frame.
      const visibleEvents = (rc, settings) => {
        const body = state.body
        if (!body || settings.events === false) return []
        const view = String(settings.view || 'both')
        const out = []
        body.events.forEach((e, index) => {
          if (view === 'calls' && e.side !== 'CE') return
          if (view === 'puts' && e.side !== 'PE') return
          const x = timeX(rc, e.epoch)
          const y = rc.priceScale.priceToY(e.strike)
          const end = rc.plotWidth - state.edgeHeld
          if (y == null || Number.isNaN(y) || x < 0 || x > end || y < 0 || y > rc.plotHeight) return
          out.push({ e, index, x, y, r: e.minutes >= 20 ? 6 : e.minutes >= 15 ? 4.5 : 3 })
        })
        return out
      }

      const eventAt = (rc, settings, x, y) => {
        let best = null
        for (const v of visibleEvents(rc, settings)) {
          const d = Math.hypot(x - v.x, y - v.y)
          if (d <= v.r + 3 && (!best || d < best.distance)) best = { index: v.index, distance: d }
        }
        return best
      }

      const streakColor = (settings, side, s) =>
        settings.colours === 'direction' ? biasColor(s.bias) : actionColor(side, s.cls)

      const overlayPrimitive = {
        zOrder() {
          return 'top'
        },
        hitTest(x, y, rc) {
          const settings = ctx.settings()
          // A streak marker under the pointer names itself.
          const ev = eventAt(rc, settings, x, y)
          if (ev) {
            hoverPoint = { x, y }
            return { externalId: `${EVENT_ID}${ev.index}`, zOrder: 'top', distance: ev.distance }
          }
          const geom = streakLayout(rc, settings)
          if (!geom || x < geom.left || x > geom.right) return null
          const hit = geom.rows.find((r) => y >= r.top && y <= r.bottom)
          if (!hit) return null
          hoverPoint = { x, y }
          return { externalId: `${HOVER_ID}${hit.strike}`, zOrder: 'top', distance: Math.abs(y - hit.y) }
        },
        draw(c, rc) {
          const settings = ctx.settings()
          const dpr = rc.dpr || 1
          state.edgeHeld = rightEdgeHeld(c, rc)
          const body = state.body
          c.save()
          c.textBaseline = 'middle'

          for (const { e, x, y, r } of visibleEvents(rc, settings)) {
            {
              c.fillStyle = streakColor(settings, e.side.toLowerCase() === 'ce' ? 'CE' : 'PE', { cls: e.kind, bias: e.bias })
              c.strokeStyle = rc.theme?.background || '#ffffff'
              c.lineWidth = dpr
              c.beginPath()
              c.moveTo(x * dpr, (y - r) * dpr)
              c.lineTo((x + r) * dpr, y * dpr)
              c.lineTo(x * dpr, (y + r) * dpr)
              c.lineTo((x - r) * dpr, y * dpr)
              c.closePath()
              c.fill()
              c.stroke()
            }
          }

          const geom = streakLayout(rc, settings)
          if (geom) {
            const minStreak = Math.max(1, Number(settings.minStreak) || 5)
            const sides = geom.view === 'calls' ? ['ce'] : geom.view === 'puts' ? ['pe'] : ['ce', 'pe']
            c.font = `${10 * dpr}px sans-serif`
            for (const r of geom.rows) {
              sides.forEach((key) => {
                const s = r.row[key]
                if (!s || !s.observed) return
                const { top, h } = sideRect(r, key, geom.view)
                const mid = Math.min(rc.plotHeight - 6, Math.max(6, top + h / 2))
                if (s.cls === 'steady') {
                  c.globalAlpha = 0.6
                  c.fillStyle = NEUTRAL
                  c.fillRect((geom.right - 3) * dpr, (mid - 1) * dpr, 3 * dpr, 2 * dpr)
                  return
                }
                const len = Math.max(3, (geom.width * Math.abs(s.streak_oi_change || 0)) / geom.max)
                c.globalAlpha = 0.85
                c.fillStyle = streakColor(settings, key.toUpperCase(), s)
                c.fillRect((geom.right - len) * dpr, top * dpr, len * dpr, h * dpr)
                if (s.streak_minutes >= minStreak && h >= 5) {
                  const text = `${key === 'ce' ? 'Call' : 'Put'} ${SHORT[s.cls] ?? s.cls} ${Math.round(s.streak_minutes)}m`
                  const tw = c.measureText(text).width / dpr
                  c.globalAlpha = 1
                  c.fillStyle = rc.theme?.axisText || TOOLTIP_FG
                  c.fillText(text, (geom.right - len - tw - 4) * dpr, mid * dpr)
                }
              })
            }

            const hovered = rc.hoverId && String(rc.hoverId).startsWith(HOVER_ID)
              ? Number(String(rc.hoverId).slice(HOVER_ID.length))
              : null
            const row = hovered != null ? body.ladder.find((r) => r.strike === hovered) : null
            if (row) {
              drawTooltip(c, rc, [
                `${row.strike}${row.atm ? '  (ATM)' : ''}`,
                sideText('Calls', row.ce, body.oi_unit),
                sideText('Puts', row.pe, body.oi_unit),
              ])
            }
          }

          const evHover = rc.hoverId && String(rc.hoverId).startsWith(EVENT_ID)
            ? body?.events[Number(String(rc.hoverId).slice(EVENT_ID.length))]
            : null
          if (evHover) {
            drawTooltip(c, rc, [
              `${evHover.time}  ${evHover.strike} ${evHover.side === 'CE' ? 'call' : 'put'}`,
              `${evHover.side === 'CE' ? 'Call' : 'Put'} ${READING[evHover.kind] ?? evHover.kind} for ${evHover.minutes} minutes in a row`,
              `Open interest moved ${formatOi(evHover.oi_change, body.oi_unit)} over the streak`,
              'Marker size: small 10 min, medium 15 min, large 20 min',
            ])
          }

          // The colour key, just above the status line.
          if (body && settings.legend !== false) {
            const items = settings.colours === 'direction'
              ? [[GREEN, 'Helps the index'], [RED, 'Weighs on the index'], [NEUTRAL, 'Side unclear']]
              : LEGEND.map(([k, label]) => [ACTION_COLORS[k], label])
            c.font = `${10 * dpr}px sans-serif`
            c.textBaseline = 'middle'
            let lx = 8
            const ly = rc.plotHeight - 30
            c.globalAlpha = 0.75
            c.fillStyle = TOOLTIP_BG
            const total = items.reduce((w, [, label]) => w + 16 + c.measureText(label).width / dpr + 10, 0)
            c.beginPath()
            c.roundRect(4 * dpr, (ly - 9) * dpr, (total + 8) * dpr, 18 * dpr, 4 * dpr)
            c.fill()
            c.globalAlpha = 1
            for (const [color, label] of items) {
              c.fillStyle = color
              c.fillRect(lx * dpr, (ly - 4) * dpr, 10 * dpr, 8 * dpr)
              c.fillStyle = TOOLTIP_FG
              c.fillText(label, (lx + 14) * dpr, ly * dpr)
              lx += 16 + c.measureText(label).width / dpr + 10
            }
          }

          // The one line of words: what is drawn, or why nothing is.
          const text = state.problem
            ? `Writer Flow: ${state.problem}`
            : body
              ? `Writer Flow ${body.underlying} ${body.expiry}, ${Math.round((body.step_seconds || 60) / 60)} min steps, OI in ${body.oi_unit === 'lots' ? 'lots' : 'lakh qty'}, ${body.times[body.times.length - 1] ?? ''}`
              : 'Writer Flow: loading'
          c.font = `${11 * dpr}px sans-serif`
          c.globalAlpha = 0.8
          c.fillStyle = state.problem ? '#eab308' : rc.theme?.axisText || NEUTRAL
          c.textBaseline = 'bottom'
          c.fillText(text, 8 * dpr, (rc.plotHeight - 6) * dpr)
          c.restore()
        },
      }

      ctx.addPrimitive(heatPrimitive)
      ctx.addPrimitive(overlayPrimitive)

      const stop = startPolling(
        ctx,
        (body) => {
          state.body = body
          state.problem = null
          state.updatedAt = Date.now()
          ctx.requestRecompute()
        },
        (message) => {
          state.problem = message
          ctx.requestRecompute()
        },
      )

      return () => {
        stop()
        ctx.removePrimitive(heatPrimitive)
        ctx.removePrimitive(overlayPrimitive)
      }
    },
  })

  registerIndicator({
    id: 'writer-flow-totals',
    name: 'Writer Flow Totals',
    category: 'Custom',
    placement: 'pane',
    noTimeframe: true,

    inputs: [underlyingInput, stepInput],

    plots: [
      { key: 'calls', type: 'line', title: 'Call OI added since open', style: { color: RED, lineWidth: 2 } },
      { key: 'puts', type: 'line', title: 'Put OI added since open', style: { color: GREEN, lineWidth: 2 } },
      {
        key: 'net',
        type: 'histogram',
        title: 'This bar: put OI minus call OI',
        // Its own hidden scale: a bar's change is a sliver of the running totals,
        // and on theirs it would not show at all. Only right, left, '' and
        // overlay:<name> are scales; any other name is dropped without a word.
        priceScaleId: 'overlay:writer-flow-net',
        style: { color: GREEN, base: 0 },
        colorBy: ({ value }) => (value >= 0 ? GREEN : RED),
      },
    ],

    // Bars before the record began, or after its last step, are gaps.
    calc(bars, settings, store) {
      const n = bars.length
      const calls = new Array(n).fill(null)
      const puts = new Array(n).fill(null)
      const net = new Array(n).fill(null)
      const totals = store?.writerFlowTotals
      if (!Array.isArray(totals) || totals.length === 0) return { calls, puts, net }
      // Lakh quantity on NSE and BSE; lots, as MCX quotes them, on MCX.
      const scale = store?.writerFlowUnit === 'lots' ? 1 : 1e5
      const first = totals[0].epoch
      const last = totals[totals.length - 1].epoch
      let k = 0
      let ceSum = 0
      let peSum = 0
      for (let i = 0; i < n; i++) {
        const start = bars[i].time
        const end = i + 1 < n ? bars[i + 1].time : Number.POSITIVE_INFINITY
        if (end <= first || start > last) {
          // Skip steps that fall before this bar, without counting them.
          while (k < totals.length && totals[k].epoch < start) {
            ceSum += totals[k].ce_change || 0
            peSum += totals[k].pe_change || 0
            k++
          }
          continue
        }
        let ce = 0
        let pe = 0
        while (k < totals.length && totals[k].epoch < end) {
          if (totals[k].epoch >= start) {
            ce += totals[k].ce_change || 0
            pe += totals[k].pe_change || 0
          }
          ceSum += totals[k].ce_change || 0
          peSum += totals[k].pe_change || 0
          k++
        }
        calls[i] = ceSum / scale
        puts[i] = peSum / scale
        net[i] = (pe - ce) / scale
      }
      return { calls, puts, net }
    },

    attach(ctx) {
      return startPolling(
        ctx,
        (body) => {
          ctx.store.writerFlowTotals = body.totals
          ctx.store.writerFlowUnit = body.oi_unit
          ctx.requestRecompute()
        },
        () => {
          ctx.store.writerFlowTotals = null
          ctx.requestRecompute()
        },
      )
    },
  })

  registerIndicator(levelsIndicator(underlyingInput, nulls))
}

// ------------------------------------------------------------- option levels

const LEVELS_CELL = 'option-levels-strike:'
const MAX_LEVELS = 4 // the most lines drawn on each side, strongest first
const LEVEL_SHARE = 0.33 // a second level shows only if it holds this share of the strongest
const LINE_MIN = 1 // px, the weakest level drawn
const LINE_MAX = 6 // px, the strongest wall of the recorded cycle

/** A level's line thickness from its month strength, across the full range so
 * the dominant wall is unmistakable; the primary on each side gets a touch more. */
function lineWeight(cell) {
  return Math.min(
    LINE_MAX,
    LINE_MIN + (LINE_MAX - LINE_MIN) * (cell.strength || 0) + (cell.primary ? 0.5 : 0)
  )
}

function formatStrike(k) {
  return Number(k).toLocaleString('en-IN', { maximumFractionDigits: 1 })
}

/** Open interest held, without a sign: lakh quantity on NSE and BSE, lots on MCX. */
function formatSize(value, unit) {
  const n = Math.abs(Number(value) || 0)
  if (unit === 'lots') return `${Math.round(n).toLocaleString('en-IN')} lots`
  return formatLakh(n).replace(/^\+/, '') // a held amount, not a change: no sign
}

/**
 * Option levels: the few levels where option writers actually sit, from
 * /writerflow/api/levels, drawn as clean lines rather than a wall of colour:
 *
 * - A thin line at each heavy wall: green where put writers hold the open
 *   interest (support), red where call writers hold it (resistance). Only the
 *   strongest on each side and any other holding a real share of it are drawn,
 *   so a trader sees the level that matters, not every strike.
 * - Thicker and brighter the more it has held over the recorded month (recent
 *   days count more), so the primary level on each side is the one positions
 *   built through the cycle; its label is filled, the rest outlined.
 * - Solid while the wall holds, dashed while writers are leaving it, with a
 *   bright tick where they are adding: the level ranking is the month, the solid
 *   or dashed state is today, minute by minute, as the open interest changes.
 * - A faint dashed line at the gamma flip: where price tends to stick, not a wall.
 *
 * It describes where writers sit; it does not forecast. Hover a level for its
 * put and call open interest and how each is changing, in words.
 */
function levelsIndicator(underlyingInput, nulls) {
  return {
    id: 'writer-flow-levels',
    name: 'Option Levels',
    category: 'Custom',
    placement: 'onchart',
    noTimeframe: true,

    inputs: [
      underlyingInput,
      { key: 'grow', type: 'boolean', label: 'Highlight Writers Adding', default: true, group: 'Display',
        tooltip: 'A strike where writers are adding over the last 10 minutes is shaded darker; one they are leaving is faded.' },
      { key: 'labels', type: 'boolean', label: 'Strike Labels', default: true, group: 'Display' },
      { key: 'gamma', type: 'boolean', label: 'Gamma Flip', default: true, group: 'Display',
        tooltip: "Where the options' net gamma changes sign. Price tends to stick and chop near it; it is not a wall." },
      { key: 'opacity', type: 'number', label: 'Strength (%)', default: 70, min: 20, max: 100, step: 5, group: 'Display' },
    ],

    plots: [{ key: 'lv', type: 'line', title: 'Option Levels', style: { visible: false } }],

    calc(bars) {
      return { lv: nulls(new Array(bars.length).fill(NaN)) }
    },

    attach(ctx) {
      // cells: where each strike band was drawn last frame, for hover and labels.
      const state = { body: null, problem: null, edgeHeld: 0, cells: [] }
      let hoverPoint = null

      const timeX = (rc, t) => rc.timeScale.indexToX(rc.dataLayer.timeToIndexFloat(t))
      // Put writers hold support, so green; call writers hold resistance, so red.
      const colorOf = (rc, role) =>
        (role === 'support' ? rc.theme?.upColor : rc.theme?.downColor) || rc.theme?.axisText
      const yOf = (rc, price) => {
        const y = rc.priceScale.priceToY(price)
        return y == null || Number.isNaN(y) ? null : y
      }

      // One cell per strike: its colour, how strong it is against the biggest
      // wall on the chart, and whether writers are adding or leaving.
      const strikeCells = (rc) => {
        const body = state.body
        const walls = body?.walls
        if (!walls || !walls.length) return []
        const peak = Math.max(1, ...walls.map((w) => Math.max(w.put, w.call)))
        const half = (Number(body.step) || 0) / 2
        const out = []
        for (const w of walls) {
          // Side is today's positioning (put writers below the price defend it,
          // call writers above cap it), so a level never shows green above the
          // price. Strength is the month: how strongly that side has held this
          // strike over the cycle, so the primary level is the one positions
          // built through, not just today's. Without a month yet, fall back to today.
          const support = w.put >= w.call
          const role = support ? 'support' : 'resistance'
          const pPut = w.persist_put ?? w.put / peak
          const pCall = w.persist_call ?? w.call / peak
          const oi = support ? w.put : w.call
          const change = support ? w.put_change : w.call_change
          const top = yOf(rc, w.strike + half)
          const bottom = yOf(rc, w.strike - half)
          if (top == null || bottom == null) continue
          out.push({
            strike: w.strike,
            role,
            oi,
            put: w.put,
            call: w.call,
            put_change: w.put_change,
            call_change: w.call_change,
            change,
            strength: Math.max(0, Math.min(1, support ? pPut : pCall)),
            adding: change > peak * 0.02,
            leaving: change < -peak * 0.02,
            top: Math.min(top, bottom),
            bottom: Math.max(top, bottom),
          })
        }
        return out
      }

      // The levels worth drawing: the heaviest walls, not every strike. Each
      // side keeps its strongest (the primary level a trader watches) and any
      // other that holds a real share of it, so the chart reads as a few clear
      // lines rather than a wall of colour.
      const significant = (cells) => {
        const out = []
        for (const role of ['support', 'resistance']) {
          const side = cells.filter((cell) => cell.role === role).sort((a, b) => b.strength - a.strength)
          side.slice(0, MAX_LEVELS).forEach((cell, i) => {
            if (i === 0 || cell.strength >= LEVEL_SHARE) out.push({ ...cell, primary: i === 0 })
          })
        }
        return out
      }

      const behind = {
        zOrder() {
          return 'bottom'
        },
        draw(c, rc) {
          const settings = ctx.settings()
          state.edgeHeld = rightEdgeHeld(c, rc)
          const body = state.body
          if (!body) return
          const dpr = rc.dpr || 1
          const width = rc.plotWidth
          c.save()

          // A line at each heavy wall: green support, red resistance. Its
          // thickness and brightness track the month strength across a wide
          // range, so the dominant level stands out at a glance: a thread at the
          // weak end, a bold band near LINE_MAX at the strongest. Solid while it
          // holds, dashed while writers leave it.
          const cap = Math.max(0.3, Math.min(1, (Number(settings.opacity) || 70) / 100))
          const end = width - state.edgeHeld
          state.cells = significant(strikeCells(rc))
          for (const cell of state.cells) {
            const y = (cell.top + cell.bottom) / 2
            if (y < 0 || y > rc.plotHeight) continue
            c.strokeStyle = colorOf(rc, cell.role)
            c.lineWidth = lineWeight(cell) * dpr
            c.globalAlpha = Math.min(0.96, cap * (0.4 + 0.6 * cell.strength) * (cell.adding ? 1.12 : 1))
            c.setLineDash(settings.grow !== false && cell.leaving ? [7 * dpr, 4 * dpr] : [])
            c.beginPath()
            c.moveTo(0, y * dpr)
            c.lineTo(end * dpr, y * dpr)
            c.stroke()
            // Writers adding here: a solid tick at the right edge, full strength.
            if (settings.grow !== false && cell.adding) {
              c.setLineDash([])
              c.globalAlpha = Math.min(0.95, 0.6 + 0.4 * cell.strength)
              c.lineWidth = Math.max(3 * dpr, c.lineWidth)
              c.beginPath()
              c.moveTo((end - 14) * dpr, y * dpr)
              c.lineTo(end * dpr, y * dpr)
              c.stroke()
            }
          }
          c.setLineDash([])

          // The gamma flip through the day, read every few minutes.
          const flips = body.gamma?.flips || []
          if (settings.gamma !== false && flips.length) {
            const end = width - state.edgeHeld
            const gxs = body.gamma.epochs.map((t) => timeX(rc, t))
            c.strokeStyle = rc.theme?.lineColor || rc.theme?.axisText
            c.lineWidth = dpr
            c.globalAlpha = 0.55
            c.setLineDash([2 * dpr, 4 * dpr])
            c.beginPath()
            let open = false
            for (let j = 0; j < flips.length; j++) {
              const y = flips[j] == null ? null : yOf(rc, flips[j])
              const x0 = Math.max(0, gxs[j])
              const x1 = Math.min(end, j + 1 < flips.length ? gxs[j + 1] : end)
              if (y == null || !(x1 > x0)) {
                open = false
                continue
              }
              if (!open) c.moveTo(x0 * dpr, y * dpr)
              else c.lineTo(x0 * dpr, y * dpr)
              c.lineTo(x1 * dpr, y * dpr)
              open = true
            }
            c.stroke()
            c.setLineDash([])
          }
          c.restore()
        },
      }

      const drawTooltip = (c, rc, lines) => {
        if (!hoverPoint) return
        const dpr = rc.dpr || 1
        const fontPx = 11
        const pad = 6
        c.font = `${fontPx * dpr}px sans-serif`
        const w = Math.max(...lines.map((l) => c.measureText(l).width / dpr))
        const h = lines.length * (fontPx + 4) + pad * 2
        const x = Math.max(4, hoverPoint.x - w - pad * 2 - 14)
        const y = Math.min(rc.plotHeight - h - 4, Math.max(4, hoverPoint.y - h / 2))
        c.globalAlpha = 0.96
        c.fillStyle = rc.theme?.background || TOOLTIP_BG
        c.strokeStyle = rc.theme?.grid || rc.theme?.axisLine || TOOLTIP_FG
        c.lineWidth = dpr
        c.beginPath()
        c.roundRect(x * dpr, y * dpr, (w + pad * 2) * dpr, h * dpr, 4 * dpr)
        c.fill()
        c.stroke()
        c.globalAlpha = 1
        c.fillStyle = rc.theme?.axisText || TOOLTIP_FG
        c.textBaseline = 'top'
        lines.forEach((l, k) => c.fillText(l, (x + pad) * dpr, (y + pad + k * (fontPx + 4)) * dpr))
      }

      const cellLines = (body, cell) => {
        const unit = body.oi_unit
        const strong = cell.role === 'support' ? 'Put writers' : 'Call writers'
        const moving = cell.adding
          ? `${cell.role === 'support' ? 'Puts' : 'Calls'} adding here (${formatOi(cell.change, unit)} in 10 min)`
          : cell.leaving
            ? `${cell.role === 'support' ? 'Puts' : 'Calls'} leaving here (${formatOi(cell.change, unit)} in 10 min)`
            : 'Holding, little change in 10 min'
        return [
          `${formatStrike(cell.strike)}  ${cell.role === 'support' ? 'support' : 'resistance'}`,
          `${strong} ${formatSize(cell.oi, unit)} hold this strike`,
          `Puts ${formatSize(cell.put, unit)}, calls ${formatSize(cell.call, unit)}`,
          moving,
          'Green support, red resistance. Thickness is a month of positioning; dashed means writers leaving now. Not a forecast.',
        ]
      }

      const front = {
        zOrder() {
          return 'top'
        },
        hitTest(x, y, rc) {
          // Only the right margin answers, so the crosshair and price readout
          // keep the rest of the chart. Hover a strike there for its writers.
          if (x < rc.plotWidth - state.edgeHeld - 170) return null
          for (const cell of state.cells) {
            if (y >= cell.top - 1 && y <= cell.bottom + 1) {
              hoverPoint = { x, y }
              return { externalId: `${LEVELS_CELL}${cell.strike}`, zOrder: 'top', distance: 0 }
            }
          }
          return null
        },
        draw(c, rc) {
          const settings = ctx.settings()
          const dpr = rc.dpr || 1
          state.edgeHeld = rightEdgeHeld(c, rc)
          const body = state.body
          c.save()

          // A label at the right edge on each drawn level, in its colour, the
          // primary one on each side filled and the rest outlined, so the level
          // a trader watches is named and ranked at a glance.
          if (body && settings.labels !== false && state.cells.length) {
            const named = [...state.cells].sort((a, b) => (a.top + a.bottom) / 2 - (b.top + b.bottom) / 2)
            for (let i = 1; i < named.length; i++) {
              const prev = (named[i - 1].top + named[i - 1].bottom) / 2
              if ((named[i].top + named[i].bottom) / 2 - prev < 15) named[i]._y = prev + 15
            }
            c.font = `${10 * dpr}px sans-serif`
            c.textBaseline = 'middle'
            const right = rc.plotWidth - 6 - state.edgeHeld
            for (const cell of named) {
              const color = colorOf(rc, cell.role)
              const y = Math.max(8, Math.min(rc.plotHeight - 8, cell._y ?? (cell.top + cell.bottom) / 2))
              const text = formatStrike(cell.strike)
              const w = c.measureText(text).width / dpr + 10
              const x = right - w
              c.beginPath()
              c.roundRect(x * dpr, (y - 8) * dpr, w * dpr, 16 * dpr, 3 * dpr)
              if (cell.primary) {
                c.globalAlpha = 0.96
                c.fillStyle = color
                c.fill()
                c.globalAlpha = 1
                c.fillStyle = rc.theme?.background || '#ffffff'
              } else {
                c.globalAlpha = 0.85
                c.fillStyle = rc.theme?.background || '#131722'
                c.fill()
                c.strokeStyle = color
                c.lineWidth = dpr
                c.setLineDash([])
                c.stroke()
                c.fillStyle = color
              }
              c.fillText(text, (x + 5) * dpr, y * dpr)
            }
          }

          // The gamma flip's price, small, beside its line.
          const flip = body?.gamma?.flip
          if (body && flip != null && settings.gamma !== false) {
            const y = yOf(rc, flip)
            if (y != null && y > 8 && y < rc.plotHeight - 8) {
              const text = `Gamma flip ${formatStrike(Math.round(flip))}`
              const right = rc.plotWidth - 6 - state.edgeHeld
              c.font = `${10 * dpr}px sans-serif`
              c.textBaseline = 'middle'
              c.globalAlpha = 0.8
              c.fillStyle = rc.theme?.lineColor || rc.theme?.axisText
              c.fillText(text, (right - c.measureText(text).width / dpr) * dpr, (y - 9) * dpr)
            }
          }

          // Hover: one strike's writers, in words.
          const hover = rc.hoverId ? String(rc.hoverId) : ''
          if (body && hover.startsWith(LEVELS_CELL)) {
            const strike = Number(hover.slice(LEVELS_CELL.length))
            const cell = state.cells.find((cc) => cc.strike === strike)
            if (cell) drawTooltip(c, rc, cellLines(body, cell))
          }

          // The one line of words, bottom right: what is drawn, or why nothing is.
          const text = state.problem
            ? `Option levels: ${state.problem}`
            : body
              ? `Option levels ${body.underlying} ${body.expiry}, ${body.minute}`
              : 'Option levels: loading'
          c.font = `${11 * dpr}px sans-serif`
          c.textBaseline = 'bottom'
          c.globalAlpha = 0.8
          c.fillStyle = rc.theme?.axisText || NEUTRAL
          const tw = c.measureText(text).width / dpr
          c.fillText(text, (rc.plotWidth - 8 - state.edgeHeld - tw) * dpr, (rc.plotHeight - 6) * dpr)
          c.restore()
        },
      }
      ctx.addPrimitive(behind)
      ctx.addPrimitive(front)

      const stop = startPolling(
        ctx,
        (body) => {
          state.body = body
          state.problem = null
          ctx.requestRecompute()
        },
        (message) => {
          state.problem = message
          ctx.requestRecompute()
        },
        fetchLevels,
      )

      return () => {
        stop()
        ctx.removePrimitive(behind)
        ctx.removePrimitive(front)
      }
    },
  }
}
