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
