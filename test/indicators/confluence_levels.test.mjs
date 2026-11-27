import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildZones,
  dayOf,
  holdRate,
  profile,
  sessions,
  sourceLevels,
  strongest,
  vwapBands,
  weekOf,
} from '../../strategies/indicators/confluence_levels.js'

// 09:15 IST on Mon 23-Nov-2026, in UTC seconds.
const MON = Date.UTC(2026, 10, 23, 3, 45) / 1000
const bar = (t, o, h, l, c, v = 0) => ({ time: t, open: o, high: h, low: l, close: c, volume: v })

test('sessions split on the Indian trading day, weeks start Monday', () => {
  const bars = [bar(MON, 1, 1, 1, 1), bar(MON + 6 * 3600, 1, 1, 1, 1), bar(MON + 86400, 1, 1, 1, 1)]
  assert.equal(sessions(bars).length, 2)
  assert.equal(dayOf(MON) + 1, dayOf(MON + 86400))
  assert.equal(weekOf(MON), weekOf(MON + 4 * 86400)) // Mon and Fri
  assert.equal(weekOf(MON) - 1, weekOf(MON - 3 * 86400)) // the Friday before
})

test('point of control sits where most volume traded, value area holds 70%', () => {
  const bars = []
  for (let i = 0; i < 10; i++) bars.push(bar(MON + i * 60, 100, 101, 99, 100, 1000)) // heavy at 100
  bars.push(bar(MON + 600, 110, 111, 109, 110, 10))
  bars.push(bar(MON + 660, 90, 91, 89, 90, 10))
  const p = profile(bars)
  assert.ok(Math.abs(p.poc - 100) < 1)
  assert.ok(p.val <= 99.5 && p.vah >= 100.5)
  assert.ok(p.vah < 109 && p.val > 91)
  assert.equal(p.byVolume, true)
})

test('without volume the profile counts time at price', () => {
  const bars = []
  for (let i = 0; i < 10; i++) bars.push(bar(MON + i * 60, 100, 101, 99, 100))
  bars.push(bar(MON + 600, 110, 111, 109, 110))
  assert.equal(profile(bars).byVolume, false)
  assert.ok(Math.abs(profile(bars).poc - 100) < 1)
})

test('VWAP is volume weighted and has no value without volume', () => {
  const v = vwapBands([bar(MON, 0, 100, 100, 100, 3), bar(MON + 60, 0, 110, 110, 110, 1)])
  assert.equal(v.vwap, 102.5)
  assert.ok(Math.abs(v.sd - Math.sqrt(18.75)) < 1e-9)
  assert.equal(vwapBands([bar(MON, 0, 1, 1, 1)]), null)
})

test('sources close together merge into one scored zone on the right side', () => {
  const zones = buildZones(
    [
      { price: 25500, code: 'PW' },
      { price: 25510, code: 'PDL' },
      { price: 25495, code: 'VAL' },
      { price: 26000, code: 'CW' },
    ],
    25800,
    30,
  )
  assert.equal(zones.length, 2)
  assert.deepEqual(zones[0].codes.sort(), ['PDL', 'PW', 'VAL'])
  assert.equal(zones[0].score, 6.5)
  assert.equal(zones[0].role, 'support')
  assert.equal(zones[1].role, 'resistance')
  assert.deepEqual(strongest(zones, 3, 4).map((z) => z.price > 25800), [false])
})

test('option sources: walls on their side, a far gamma flip is left out', () => {
  const body = {
    status: 'success',
    spot: 25880,
    step: 50,
    walls: [
      { strike: 25500, put: 900, call: 10 },
      { strike: 25600, put: 700, call: 10 },
      { strike: 26000, put: 10, call: 1000 },
      { strike: 25700, put: 10, call: 2000 }, // in the money call below price: not resistance
      { strike: 26100, put: 5000, call: 10 }, // in the money put above price: not support
    ],
    gamma: { flip: 24000, flip_far: true, walls: [{ strike: 25900, gex: 5 }], expected_move: { day: 120 } },
  }
  const codes = Object.fromEntries(sourceLevels([], body).map((l) => [l.code, l.price]))
  assert.equal(codes.PW, 25500)
  assert.equal(codes.PW2, 25600)
  assert.equal(codes.CW, 26000)
  assert.equal(codes.GW, 25900)
  assert.equal(codes.EMH, 26000)
  assert.equal(codes.EML, 25760)
  assert.equal(codes.FLIP, undefined)
})

test('a level price bounced from counts as held, one closed through as broken', () => {
  const bars = [bar(0, 0, 0, 0, 105)]
  // Twice down to 100 and back up: held. Then down and through: broken.
  for (const p of [101, 100.2, 103, 106, 104, 100.1, 102, 106, 103, 100, 98, 97]) bars.push(bar(bars.length, p, p + 0.5, p - 0.5, p))
  const r = holdRate(bars, 100, 'support', 0.3)
  assert.equal(r.n, 3)
  assert.equal(r.held, 2)
})

test('neighbours do not chain into one wide zone, and a zone at the price takes no side', () => {
  const levels = [0, 50, 100, 150, 200].map((d) => ({ price: 84100 + d, code: 'GW' }))
  const zones = buildZones(levels, 84200, 100)
  assert.ok(zones.every((z) => z.high - z.low <= 100))
  assert.ok(zones.length >= 2)
  assert.equal(buildZones([{ price: 84230, code: 'PW' }], 84200, 100)[0].role, 'at price')
})

import mod from '../../strategies/indicators/confluence_levels.js'

function indicator() {
  let ind
  mod({ registerIndicator: (d) => (ind = d), nulls: (a) => a })
  return ind
}
const defaults = (ind) => Object.fromEntries(ind.inputs.map((i) => [i.key, i.default]))

test('calc survives no bars, one bar, daily bars and a failed option answer', () => {
  const ind = indicator()
  const s = defaults(ind)
  for (const bars of [[], [bar(MON, 1, 2, 0, 1)]]) {
    const store = {}
    ind.calc(bars, s, store)
    assert.deepEqual(store.zones, [])
  }
  const daily = []
  for (let i = 0; i < 15; i++) daily.push(bar(MON - (14 - i) * 86400, 100, 102 + (i % 3), 98 - (i % 2), 100 + (i % 4)))
  const store = { levels: { status: 'error', message: 'nothing recorded' } }
  ind.calc(daily, s, store)
  assert.ok(Array.isArray(store.zones))
  assert.ok(store.zones.every((z) => !z.codes.some((c) => ['POC', 'VWAP', 'PW'].includes(c))))
})

test('changing the underlying reloads within seconds, not at the next minute', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  const asked = []
  globalThis.document = { hidden: false }
  globalThis.fetch = async (url) => {
    asked.push(decodeURIComponent(url.split('underlying=')[1]))
    return { json: async () => ({ status: 'success', underlying: 'X', walls: [] }) }
  }
  const settings = { ...defaults(indicator()), underlying: 'NIFTY' }
  const ctx = {
    settings: () => settings,
    symbol: () => 'NIFTY',
    store: {},
    requestRecompute() {},
    addPrimitive() {},
    removePrimitive() {},
  }
  const stop = indicator().attach(ctx)
  await new Promise((r) => setImmediate(r))
  assert.deepEqual(asked, ['NIFTY'])
  settings.underlying = 'BANKNIFTY'
  t.mock.timers.tick(5000)
  await new Promise((r) => setImmediate(r))
  assert.deepEqual(asked, ['NIFTY', 'BANKNIFTY'])
  t.mock.timers.tick(5000) // nothing changed and under a minute: no new ask
  await new Promise((r) => setImmediate(r))
  assert.equal(asked.length, 2)
  stop()
})
