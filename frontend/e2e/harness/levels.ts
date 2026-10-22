/**
 * The real chart and the real Option Levels indicator (the module Flask serves
 * from strategies/indicators/writer_flow_live.js), on a synthetic session and a
 * stubbed /writerflow/api/levels, so e2e/option-levels.spec.ts can drive the
 * whole path in a browser without a broker login or a running backend.
 *
 * The synthetic walls put the put writers below the spot and the call writers
 * above it, with the heaviest on the round strikes, so the indicator must draw a
 * green line low and a red line high, the heaviest boldest.
 */
import * as core from 'openalgo-charts'
import * as indicators from 'openalgo-charts/indicators'

// The module is plain JS served at runtime, never bundled; the harness imports
// it straight from the repo so the test exercises the file Flask ships.
import registerWriterFlow from '../../../strategies/indicators/writer_flow_live.js'

const START = Date.UTC(2026, 9, 22, 3, 45) / 1000 // 22-Oct-2026 09:15 IST
const SPOT = 22_630
const STEP = 50

function candles(n: number, step: number): core.Bar[] {
  const out: core.Bar[] = []
  let p = SPOT + 10
  for (let k = 0; k < n; k++) {
    const t = START + k * step
    const open = p
    p += Math.sin(k * 0.3) * 18 + Math.cos(k * 0.07) * 8
    out.push({ time: t, open, high: Math.max(open, p) + 6, low: Math.min(open, p) - 6, close: p, volume: 100 })
  }
  return out
}
const bars = candles(220, 300)

// Synthetic positioning: puts hold the strikes below the spot, calls those above,
// heaviest on the round 500s, lighter on the 100s, thin elsewhere. 22,600 is
// shedding put writers (a weakening support) and 23,000 is gaining call writers.
const strikes = Array.from({ length: 20 }, (_, i) => 22_150 + i * STEP)
const weight = (k: number) => (k % 500 === 0 ? 1 : k % 100 === 0 ? 0.55 : 0.22)
const walls = strikes.map((k) => {
  const below = k <= SPOT
  return {
    strike: k,
    put: below ? 9_000_000 * weight(k) + 2_000_000 : 900_000,
    call: below ? 900_000 : 9_000_000 * weight(k) + 2_000_000,
    put_change: k === 22_600 ? -620_000 : 0,
    call_change: k === 23_000 ? 540_000 : 0,
    // The rolling-month strength the service sends: put writers persistent below
    // the spot, call writers above, heaviest on the round strikes.
    persist_put: below ? weight(k) : 0.08,
    persist_call: below ? 0.08 : weight(k),
  }
})

const levelsPayload = {
  status: 'success',
  underlying: 'NIFTY',
  expiry: '27OCT26',
  date: '2026-10-22',
  minute: '14:40',
  epoch: Math.floor(START + 210 * 300),
  spot: SPOT,
  step: STEP,
  oi_unit: 'quantity',
  thresholds: { touch_pct: 0.04, bounce_pct: 0.12, break_pct: 0.08, after_min: 20, chance: 0.4, min_judged: 20 },
  zones: [],
  walls,
  history: { epochs: [], support: [], resistance: [] },
  changes: [],
  gamma: { forward: SPOT, top_strike: 23_000, flip: 22_648, epochs: [], flips: [] },
  touches: [],
  stats: { days: 1 },
}

// Serve the levels API from memory; everything else passes through.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/writerflow/api/levels')) {
    return Promise.resolve(
      new Response(JSON.stringify(levelsPayload), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

// The app passes the module the chart library plus a few helpers; `nulls` comes
// from the indicators export. Register straight into core, as the app does.
const api = { ...core, ...indicators }
;(registerWriterFlow as (deps: Record<string, unknown>) => void)({
  ...api,
  registerIndicator: (descriptor: unknown) => core.registerIndicator(descriptor as never),
  onServerEvent: () => () => {},
  subscribeQuotes: () => () => {},
})

const chart = core.createChart(document.getElementById('chart') as HTMLElement)
chart.setTimezone('Asia/Kolkata')
const series = chart.addSeries('candlestick')
chart.setBarsProvider(async () => bars)
chart.setDataContext({ symbol: 'NIFTY', exchange: 'NSE_INDEX', interval: '5m' })
series.setData(bars)

const ind = chart.addIndicator('writer-flow-levels', { underlying: 'NIFTY' })

Object.assign(window, { core, chart, ind, walls })
;(window as unknown as { ready: boolean }).ready = true
