/**
 * OI Profile and Writer Flow on one chart share its right edge.
 *
 *   node --test test/indicators/right_edge_shared.test.mjs
 *
 * Both pin something to the pane's right edge: OI Profile its open interest
 * bars, Writer Flow each strike's current streak. Drawn without knowing of each
 * other, the streaks sat on top of the bars and the newest strips ran under
 * them. OI Profile now holds the strip beside the price axis while it has bars,
 * and Writer Flow draws to the left of whatever is held.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

const oi = await import(new URL('../../strategies/indicators/oi_profile_live.js', import.meta.url))
const wf = await import(new URL('../../strategies/indicators/writer_flow_live.js', import.meta.url))

const PLOT = 800
const ok = (body) => ({ ok: true, status: 200, json: async () => body })
const priceToY = (p) => 400 - (p - 23000) / 2

function canvas() {
  const rects = []
  const noop = () => {}
  return {
    rects,
    c: new Proxy(
      { measureText: (t) => ({ width: t.length * 6 }), fillRect: (x, y, w, h) => rects.push({ x, y, w, h }) },
      { get: (t, k) => (k in t ? t[k] : noop), set: () => true }
    ),
  }
}

const rc = () => ({
  plotWidth: PLOT,
  plotHeight: 400,
  dpr: 1,
  priceScale: { priceToY },
  // One bar per minute, the last minute at the right edge.
  timeScale: { barSpacing: 10, indexToX: (i) => PLOT - (60 - i) * 10 },
  dataLayer: { timeToIndexFloat: (t) => (t - T0) / 60 },
})

const T0 = 1_792_381_500 // 09:15 IST
const STRIKES = [23000, 23050, 23100]
const minutes = 60
const flowBody = {
  status: 'success',
  underlying: 'NIFTY',
  expiry: '19OCT26',
  step_seconds: 60,
  oi_unit: 'quantity',
  spot: 23050,
  strikes: STRIKES,
  times: Array.from({ length: minutes }, (_, i) => `09:${15 + i}`),
  times_epoch: Array.from({ length: minutes }, (_, i) => T0 + 60 * i),
  heat: {
    ce: STRIKES.map(() => Array.from({ length: minutes }, () => 500_000)),
    pe: STRIKES.map(() => Array.from({ length: minutes }, () => -500_000)),
  },
  ladder: STRIKES.map((strike) => ({
    strike,
    atm: strike === 23050,
    observed: true,
    ce: { cls: 'writing', observed: true, streak_minutes: 12, streak_oi_change: 900_000, bias: -1 },
    pe: { cls: 'covering', observed: true, streak_minutes: 9, streak_oi_change: -700_000, bias: -1 },
  })),
  events: [{ time: '10:14', strike: 23050, side: 'CE', kind: 'writing', minutes: 10, oi_change: 1, epoch: T0 + 59 * 60, bias: -1 }],
  totals: [],
}

function writerFlow() {
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  globalThis.fetch = async () => ({ json: async () => flowBody })
  const descriptors = []
  wf.default({ registerIndicator: (d) => descriptors.push(d), nulls: (a) => a })
  const strips = descriptors.find((d) => d.id === 'writer-flow-live')
  const primitives = []
  const stop = strips.attach({
    settings: () => ({}),
    symbol: () => 'NIFTY',
    dataContext: () => ({ symbol: 'NIFTY', exchange: 'NSE_INDEX' }),
    store: {},
    addPrimitive: (p) => primitives.push(p),
    removePrimitive() {},
    requestRecompute() {},
  })
  return { primitives, stop }
}

async function oiProfile() {
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  globalThis.fetch = async (url) => {
    if (String(url).includes('/search/api/expiries')) return ok({ expiries: ['19-OCT-26'] })
    if (String(url).includes('csrf')) return ok({ csrf_token: 't' })
    return ok({
      status: 'success',
      market_open: true,
      oi_chain: STRIKES.map((strike) => ({
        strike, ce_oi: 8_000_000, pe_oi: 6_000_000, ce_oi_change: 900_000, pe_oi_change: -400_000, ce_legs: [], pe_legs: [],
      })),
    })
  }
  oi.forgetAnswers()
  let descriptor
  oi.default({ registerIndicator: (d) => { descriptor = d }, nulls: (a) => a })
  let primitive
  const stop = descriptor.attach({
    settings: () => ({ barWidth: 140, maxPain: false, strikes: 5, expiries: 1 }),
    symbol: () => 'NIFTY',
    timezone: () => 'Asia/Kolkata',
    addPrimitive: (p) => { primitive = p },
    removePrimitive() {},
    requestRecompute() {},
  })
  const until = Date.now() + 3000
  // Until the chain has arrived: a frame then draws bars.
  while (Date.now() < until) {
    const frame = canvas()
    if (primitive) primitive.draw(frame.c, rc())
    if (frame.rects.length > 0) break
    await new Promise((r) => setTimeout(r, 20))
  }
  return { primitive, stop }
}

test('OI Profile holds the strip beside the price axis while it has bars, and lets go when removed', async () => {
  const shared = canvas()
  const { primitive, stop } = await oiProfile()
  try {
    primitive.draw(shared.c, rc())
    const held = wf.rightEdgeHeld(shared.c, rc())
    const widest = Math.max(...shared.rects.map((r) => PLOT - r.x))
    assert.ok(held >= widest, `held ${held}px, but the bars reach ${widest}px in from the edge`)
  } finally {
    stop()
  }
  assert.equal(wf.rightEdgeHeld(shared.c, rc()), 0, 'a removed OI Profile still held the edge')
})

test('Writer Flow draws nothing inside the strip OI Profile holds', async () => {
  const shared = canvas()
  const { primitives, stop } = writerFlow()
  try {
    await new Promise((r) => setTimeout(r, 50))
    wf.holdRightEdge(shared.c, {}, () => 200)
    for (const p of primitives) p.draw(shared.c, rc())
    assert.ok(shared.rects.length > 0, 'Writer Flow drew nothing at all')
    // The legend's 10x8 swatches run along the bottom from the left, and this
    // canvas measures text far wider than a real one, so they are not part of
    // what is pinned to the right edge.
    const legend = (r) => r.w === 10 && r.h === 8
    const intrusions = shared.rects.filter((r) => !legend(r) && r.x + r.w > PLOT - 200 + 0.5)
    assert.deepEqual(intrusions, [], 'Writer Flow drew over the held strip')
  } finally {
    stop()
  }
})

test('Writer Flow alone still uses the whole width', async () => {
  const shared = canvas()
  const { primitives, stop } = writerFlow()
  try {
    await new Promise((r) => setTimeout(r, 50))
    for (const p of primitives) p.draw(shared.c, rc())
    assert.ok(shared.rects.some((r) => r.x + r.w > PLOT - 20), 'without OI Profile the streaks belong at the edge')
  } finally {
    stop()
  }
})
