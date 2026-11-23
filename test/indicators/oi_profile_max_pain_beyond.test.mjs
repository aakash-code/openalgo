/**
 * Max pain over the whole chain, not just the strikes fetched.
 *
 *   node --test test/indicators/oi_profile_max_pain_beyond.test.mjs
 *
 * Over the window alone max pain leans to the window's middle (BANKNIFTY sat
 * up to 2000 points from NSE's whole-chain figure in Nov 2026). The server
 * sends the strikes beyond the window at the previous close; they are added.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

const oi = await import(new URL('../../strategies/indicators/oi_profile_live.js', import.meta.url))

// A window 100-140 with light, even OI, and heavy old put OI far below it.
const window = [100, 110, 120, 130, 140].map((k) => ({ strike: k, ce_oi: 10, pe_oi: 10 }))
const beyond = [
  { strike: 60, ce_oi: 0, pe_oi: 5000 },
  { strike: 70, ce_oi: 9000, pe_oi: 0 },
]

test('the window alone puts max pain in its middle', () => {
  assert.equal(oi.maxPainStrike(window), 120)
})

test('with the strikes beyond, max pain follows the whole chain', () => {
  const whole = oi.withBeyond(window, beyond)
  assert.equal(whole.length, 7)
  // Heavy call OI at 70 makes any settle above it costly: max pain moves down.
  assert.equal(oi.maxPainStrike(whole), 70)
})

test('a strike the window holds is never taken from beyond', () => {
  const clash = [{ strike: 120, ce_oi: 1e9, pe_oi: 1e9 }]
  assert.equal(oi.withBeyond(window, clash), window)
})

test('nothing beyond leaves the window as it is', () => {
  assert.equal(oi.withBeyond(window, null), window)
  assert.equal(oi.withBeyond(window, []), window)
})

// Rows 10 px apart, as NIFTY's are on a 5-minute chart.
const rows = [{ strike: 25400, y: 340 }, { strike: 25350, y: 350 }, { strike: 25300, y: 360 }]

test('a wall one strike up takes the space above: max pain goes below', () => {
  // Rows 20 px apart (zoomed in): resistance's label (322-336) fills the
  // space above 340 (324-338) and leaves below (342-356) clear.
  const wide = [{ strike: 25400, y: 320 }, { strike: 25350, y: 340 }]
  const ys = oi.wallYs(wide, { support: null, resistance: 25400 })
  assert.deepEqual(ys, [320])
  assert.equal(oi.maxPainLabelPlace(340, ys), 'below')
})

test('rows 10 px apart, one wall above: its label covers both sides, so beside', () => {
  assert.equal(oi.maxPainLabelPlace(350, oi.wallYs(rows, { resistance: 25400 })), 'beside')
})

test('walls one strike either side, as live on 23-Nov: max pain goes beside them', () => {
  const ys = oi.wallYs(rows, { support: 25300, resistance: 25400 })
  assert.deepEqual(ys, [360, 340])
  assert.equal(oi.maxPainLabelPlace(350, ys), 'beside')
})

test('with no wall near, max pain keeps its label above the line', () => {
  assert.equal(oi.maxPainLabelPlace(350, [200, 500]), 'above')
})

test('max pain on a wall strike keeps its label above, the wall label is below', () => {
  assert.equal(oi.maxPainLabelPlace(340, [340]), 'above')
})
