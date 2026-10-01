/**
 * The real chart, the real built-in indicators and the real Timeframe wrapper,
 * on synthetic NSE sessions, so e2e/indicator-timeframe.spec.ts can drive the
 * whole path in a browser without a broker login.
 */
import * as core from 'openalgo-charts'
import 'openalgo-charts/indicators'

import { applyTimeframeToAll } from '../../src/lib/trading/indicatorTimeframe'

const DAY1 = Date.UTC(2026, 8, 28, 3, 45) / 1000 // Mon 09:15 IST

function sessions(days: number, step: number): core.Bar[] {
  const out: core.Bar[] = []
  let p = 24_000
  for (let d = 0, day = 0; out.length < (days * 22_500) / step; d++) {
    const dow = new Date((DAY1 + d * 86_400) * 1000).getUTCDay()
    if (dow === 0 || dow === 6) continue
    for (let k = 0; k < 22_500 / step; k++) {
      const time = DAY1 + d * 86_400 + k * step
      const open = p
      p += Math.sin((day * 400 + k) * 0.21) * 12 + Math.cos(k * 0.05) * 4
      out.push({ time, open, high: Math.max(open, p) + 5, low: Math.min(open, p) - 5, close: p, volume: 100 })
    }
    day++
  }
  return out
}

/** The broker's higher chart: periods anchored on each session's 09:15. */
function fold(bars: readonly core.Bar[], tfSec: number): core.Bar[] {
  const groups = new Map<number, core.Bar[]>()
  for (const b of bars) {
    const dayStart = b.time - ((b.time - DAY1) % 86_400)
    const key = tfSec >= 86_400 ? dayStart : dayStart + Math.floor((b.time - dayStart) / tfSec) * tfSec
    groups.set(key, [...(groups.get(key) ?? []), b])
  }
  return [...groups.entries()].map(([t, g]) => ({ ...core.mergeBars(g), time: t }))
}

const base1m = sessions(40, 60)
const barsFor = (interval: string) => fold(base1m, core.intervalToSeconds(interval))

applyTimeframeToAll(core)

const chart = core.createChart(document.getElementById('chart') as HTMLElement)
chart.setTimezone('Asia/Kolkata')
const series = chart.addSeries('candlestick')

let requests = 0
chart.setBarsProvider(async (req) => {
  requests++
  await new Promise((r) => setTimeout(r, 50))
  return barsFor(req.interval).filter((b) => b.time >= req.from && b.time <= req.to)
})

function load(interval: string, keepLast = 0) {
  chart.setDataContext({ symbol: 'NIFTY', exchange: 'NSE_INDEX', interval })
  const all = barsFor(interval)
  series.setData(keepLast ? all.slice(-keepLast) : all)
}

Object.assign(window, { core, chart, series, load, barsFor, requests: () => requests })
load('5m', 1500)
;(window as unknown as { ready: boolean }).ready = true
