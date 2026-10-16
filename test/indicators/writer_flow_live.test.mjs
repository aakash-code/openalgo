/**
 * Writer Flow on the chart: when a symbol that is not recorded starts recording.
 *
 *   node --test test/indicators/writer_flow_live.test.mjs
 *
 * Opening a chart of an unrecorded symbol starts a recording, and its first fill
 * is some 60 history calls to the broker. Flicking through ten stocks one after
 * another started ten of them. Now a chart only reads what is recorded until it
 * has stayed on a symbol for START_AFTER_MS, and then asks once, at that moment.
 */
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'

const indicator = await import(new URL('../../strategies/indicators/writer_flow_live.js', import.meta.url))

const empty = { status: 'empty', recorder_enabled: true, message: 'Nothing recorded for this yet.' }

beforeEach(() => {
  indicator.forgetFlowAnswers()
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] })
})
afterEach(() => mock.timers.reset())

const settle = () => new Promise((resolve) => setImmediate(resolve))

async function advance(ms) {
  // One second at a time, so the settings watcher and the fetch replies
  // interleave the way they do in a browser.
  for (let left = ms; left > 0; left -= 1000) {
    mock.timers.tick(Math.min(1000, left))
    await settle()
  }
}

function harness(symbol) {
  const asked = []
  const shown = []
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  globalThis.fetch = async (url) => {
    asked.push(url)
    return { json: async () => empty }
  }
  const descriptors = []
  indicator.default({ registerIndicator: (d) => descriptors.push(d), nulls: (a) => a })
  const totals = descriptors.find((d) => d.id === 'writer-flow-totals')
  const chart = { symbol }
  const store = {}
  const stop = totals.attach({
    settings: () => ({}),
    symbol: () => chart.symbol,
    dataContext: () => ({ symbol: chart.symbol, exchange: 'NFO' }),
    store,
    requestRecompute: () => shown.push(store.writerFlowTotals),
  })
  return { asked, chart, stop, starts: () => asked.filter((u) => u.includes('&exchange=')) }
}

test('a chart starts recording only after staying on the symbol', async () => {
  const h = harness('TECHM27OCT26FUT')
  await settle()
  assert.equal(h.asked.length, 1, 'the chart reads what is recorded straight away')
  assert.equal(h.starts().length, 0, 'nothing is started the moment the chart opens')
  await advance(14_000)
  assert.equal(h.starts().length, 0, 'nothing is started before the wait is over')
  await advance(2_000)
  assert.equal(h.starts().length, 1, 'the recording is asked for once the wait is over, not a minute later')
  assert.match(h.starts()[0], /underlying=TECHM&.*exchange=NFO/)
  h.stop()
})

test('flicking through symbols starts no recording at all', async () => {
  const h = harness('TECHM27OCT26FUT')
  await settle()
  for (const symbol of ['INFY27OCT26FUT', 'TCS27OCT26FUT', 'SBIN27OCT26FUT', 'RELIANCE27OCT26FUT', 'HDFCBANK27OCT26FUT']) {
    await advance(3_000)
    h.chart.symbol = symbol
  }
  await advance(3_000)
  assert.equal(h.starts().length, 0, `started: ${h.starts().join(', ')}`)
  // Each symbol was still read, so a recorded one shows at once.
  for (const name of ['INFY', 'TCS', 'SBIN', 'RELIANCE', 'HDFCBANK']) {
    assert.ok(h.asked.some((u) => u.includes(`underlying=${name}&`)), `${name} was never read`)
  }
  // The last one is stayed on: it alone starts.
  await advance(14_000)
  assert.deepEqual(
    h.starts().map((u) => new URL(u, 'http://x').searchParams.get('underlying')),
    ['HDFCBANK'],
  )
  h.stop()
})

test('a closed chart asks for nothing more', async () => {
  const h = harness('TECHM27OCT26FUT')
  await settle()
  h.stop()
  await advance(20_000)
  assert.equal(h.asked.length, 1)
})
