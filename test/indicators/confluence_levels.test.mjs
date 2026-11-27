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
    gamma: { flip: 24000, flip_far: true, walls: [{ strike: 25900, gex: 5 }], expected_move: { day: 250 } },
  }
  const codes = Object.fromEntries(sourceLevels([], body).map((l) => [l.code, l.price]))
  assert.equal(codes.PW, 25500)
  assert.equal(codes.PW2, 25600)
  assert.equal(codes.CW, 26000)
  assert.equal(codes.GW, 25900)
  assert.equal(codes.EMH, 26130)
  assert.equal(codes.EML, 25630)
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

test('an option record from another day than the chart is not used', () => {
  const ind = indicator()
  const s = defaults(ind)
  const bars = []
  for (let i = 0; i < 30; i++) bars.push(bar(MON + i * 60, 100, 101, 99, 100, 10))
  const body = (date) => ({ status: 'success', date, spot: 100, step: 5, walls: [{ strike: 95, put: 9, call: 0 }], gamma: {} })
  const old = { levels: body('2026-10-27') }
  ind.calc(bars, { ...s, minScore: 1 }, old)
  assert.equal(old.stale, true)
  assert.ok(old.zones.every((z) => !z.codes.includes('PW')))
  const today = { levels: body('2026-11-23') }
  ind.calc(bars, { ...s, minScore: 1 }, today)
  assert.equal(today.stale, false)
  assert.ok(today.zones.some((z) => z.codes.includes('PW')))
})

import { stackLabels } from '../../strategies/indicators/confluence_levels.js'

test('labels keep their gap and stay above the status line', () => {
  assert.deepEqual(stackLabels([100, 104, 300], 15, 8, 280), [100, 115, 280])
  assert.deepEqual(stackLabels([270, 275, 279], 15, 8, 280), [250, 265, 280])
  assert.deepEqual(stackLabels([2], 15, 8, 280), [8])
})

import { dayOutcome, dayZones, isoOf } from '../../strategies/indicators/confluence_levels.js'

function threeDays() {
  // Mon 100-110, Tue 105-115, Wed falls from 112 to 95.
  const bars = []
  const day = (d, prices) => prices.forEach((p, k) => bars.push(bar(MON + d * 86400 + k * 300, p, p + 0.5, p - 0.5, p, 10)))
  day(0, [100, 104, 110, 106, 103])
  day(1, [105, 109, 115, 111, 108])
  day(2, [112, 108, 104, 99, 95])
  return bars
}

test('each day gets its own levels, from what was known at its open', () => {
  const bars = threeDays()
  const s = { ...defaults(indicator()), minScore: 1, perSide: 6 }
  const { days } = dayZones(bars, s, null, {})
  assert.equal(days.length, 3)
  const wed = days[2]
  assert.equal(wed.today, true)
  const tue = days[1]
  assert.equal(tue.today, false)
  assert.equal(tue.end, days[2].start) // a day's lines stop where the next begin
  // Tuesday's previous-day high is Monday's, never Tuesday's own 115.5.
  const codes = tue.zones.flatMap((z) => z.codes.map((c) => [c, z.price]))
  const pdh = codes.find(([c]) => c === 'PDH')
  assert.ok(pdh && Math.abs(pdh[1] - 110.5) < 1e-9)
  assert.ok(tue.zones.every((z) => !z.codes.includes('VWAP')))
})

test("a past day reads the previous session's option record, and says what it did", () => {
  const bars = threeDays()
  const s = { ...defaults(indicator()), minScore: 1, perSide: 6 }
  const monday = isoOf(dayOf(MON))
  const record = { status: 'success', date: monday, spot: 103, step: 5, walls: [{ strike: 100, put: 9, call: 0 }], gamma: {} }
  const { days, wanted } = dayZones(bars, s, null, { [monday]: record })
  assert.ok(wanted.includes(monday))
  const tue = days[1]
  assert.equal(tue.options, true)
  assert.ok(tue.zones.some((z) => z.codes.includes('PW')))
  assert.equal(days[2].options, false) // today waits for the live record
})

test('a zone closed through that day broke, one reached and kept held', () => {
  const dayBars = [bar(0, 0, 106, 104, 105), bar(1, 0, 104, 99.8, 101), bar(2, 0, 103, 100, 102)]
  assert.equal(dayOutcome(dayBars, { role: 'support', low: 100, high: 100.2 }, 0.2), 'held')
  // One minute through and back is a poke, not a break; fifteen minutes through is.
  const poke = [...dayBars, bar(60, 0, 99, 97, 97), bar(120, 0, 102, 100.5, 101)]
  assert.equal(dayOutcome(poke, { role: 'support', low: 100, high: 100.2 }, 0.2), 'held')
  const stay = [...dayBars]
  for (let m = 1; m <= 16; m++) stay.push(bar(m * 60, 0, 99, 97, 97))
  assert.equal(dayOutcome(stay, { role: 'support', low: 100, high: 100.2 }, 0.2), 'broke')
  assert.equal(dayOutcome(dayBars, { role: 'resistance', low: 120, high: 120 }, 0.2), 'untouched')
})

test('a wall beyond two days of priced-in move does not count', () => {
  const body = {
    status: 'success',
    spot: 7959,
    step: 50,
    walls: [
      { strike: 7500, put: 7455, call: 10 },
      { strike: 7800, put: 3418, call: 10 },
      { strike: 8000, put: 10, call: 5146 },
    ],
    gamma: { walls: [{ strike: 7500, gex: -1 }, { strike: 8100, gex: 1 }], expected_move: { day: 158 } },
  }
  const codes = Object.fromEntries(sourceLevels([], body).map((l) => [l.code, l.price]))
  assert.equal(codes.PW, 7800)
  assert.equal(codes.GW, 8100)
  assert.equal(codes.CW, 8000)
})

test("today's option record is asked for first, then the past days one at a time", async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  const asked = []
  let open = 0
  let most = 0
  globalThis.document = { hidden: false }
  globalThis.fetch = async (url) => {
    asked.push(url.includes('&date=') ? url.split('&date=')[1] : 'live')
    open++
    most = Math.max(most, open)
    await new Promise((r) => setImmediate(r))
    open--
    return { status: 200, json: async () => ({ status: 'success', walls: [] }) }
  }
  const ctx = {
    settings: () => ({ ...defaults(indicator()), underlying: 'NIFTY' }),
    symbol: () => 'NIFTY',
    store: { wanted: ['2026-11-25', '2026-11-26'] },
    requestRecompute() {},
    addPrimitive() {},
    removePrimitive() {},
  }
  const stop = indicator().attach(ctx)
  for (let k = 0; k < 6; k++) await new Promise((r) => setImmediate(r))
  assert.equal(asked[0], 'live')
  assert.deepEqual(asked.slice(1).sort(), ['2026-11-25', '2026-11-26'])
  assert.equal(most, 1) // the server builds one at a time
  stop()
})

test('a server that could not read the record is asked again, and its words are shown', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  globalThis.document = { hidden: false }
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    if (calls === 1) return { status: 200, json: async () => ({ status: 'success', date: '2026-11-27', walls: [] }) }
    return { status: 500, json: async () => ({ status: 'error', message: 'Could not read the option levels. Try again in a moment.' }) }
  }
  const ctx = {
    settings: () => ({ ...defaults(indicator()), underlying: 'NIFTY' }),
    symbol: () => 'NIFTY',
    store: { wanted: ['2026-11-26'] },
    requestRecompute() {},
    addPrimitive(p) { this.layer = p },
    removePrimitive() {},
  }
  const stop = indicator().attach(ctx)
  for (let k = 0; k < 6; k++) await new Promise((r) => setImmediate(r))
  t.mock.timers.tick(60000) // the minute's reload, which the server fails
  for (let k = 0; k < 6; k++) await new Promise((r) => setImmediate(r))
  assert.equal(ctx.store.levels.date, '2026-11-27') // what was drawn is kept
  assert.equal('2026-11-26' in ctx.store.records, false) // asked again later
  const said = []
  const c = new Proxy(
    { fillText: (x) => said.push(x), measureText: () => ({ width: 10 }) },
    { get: (o, k) => o[k] ?? (() => {}), set: () => true },
  )
  ctx.layer.draw(c, { dpr: 1, plotWidth: 800, plotHeight: 400, priceScale: { priceToY: () => null }, timeScale: { indexToX: () => 0 }, dataLayer: { timeToIndexFloat: () => 0 } })
  assert.ok(said.some((x) => x.includes('Try again in a moment')))
  stop()
})
