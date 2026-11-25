// A level's side follows where it sits against the price, not only which side holds more.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { levelSide } from '../../strategies/indicators/writer_flow_live.js'

test('in-the-money calls below the price are not drawn as resistance', () => {
  // BANKNIFTY 20-Nov-2026: 64,500 held more calls than puts, price 64,635.
  assert.equal(levelSide({ strike: 64500, put: 100, call: 900 }, 64635, 50), true)
})

test('in-the-money puts above the price are not drawn as support', () => {
  assert.equal(levelSide({ strike: 64800, put: 900, call: 100 }, 64635, 50), false)
})

test('within half a strike the bigger side holds it', () => {
  assert.equal(levelSide({ strike: 25500, put: 100, call: 900 }, 25514, 25), false)
  assert.equal(levelSide({ strike: 25500, put: 900, call: 100 }, 25514, 25), true)
})

test('without a price the bigger side decides, as before', () => {
  assert.equal(levelSide({ strike: 1, put: 2, call: 1 }, Number.NaN, 25), true)
})
