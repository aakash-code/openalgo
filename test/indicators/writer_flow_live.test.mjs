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

function harness(symbol, { serverEvents = true } = {}) {
  const asked = []
  // The loader's onServerEvent, as lib/serverEvents.ts hands it in.
  const heard = new Map()
  const onServerEvent = (name, fn) => {
    if (!heard.has(name)) heard.set(name, new Set())
    heard.get(name).add(fn)
    return () => heard.get(name).delete(fn)
  }
  const announce = (underlying) => {
    for (const fn of [...(heard.get('writer_flow_update') ?? [])]) fn({ underlying })
  }
  const shown = []
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  globalThis.fetch = async (url) => {
    asked.push(url)
    return { json: async () => empty }
  }
  const descriptors = []
  indicator.default({
    registerIndicator: (d) => descriptors.push(d),
    nulls: (a) => a,
    ...(serverEvents ? { onServerEvent } : {}),
  })
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
  const listeners = () => heard.get('writer_flow_update')?.size ?? 0
  return { asked, chart, stop, announce, listeners, starts: () => asked.filter((u) => u.includes('&exchange=')) }
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

test('a new minute announced for this chart is fetched at once, not at the next poll', async () => {
  const h = harness('NIFTY27OCT26FUT')
  await settle()
  assert.equal(h.asked.length, 1)
  await advance(5_000)
  h.announce('NIFTY')
  await settle()
  assert.equal(h.asked.length, 2, 'the announcement did not fetch')
  // The poll is pushed back a full beat: no second read of the same minute
  // 55 s later, where the old beat would have fallen. (The one ask at 15 s
  // that starts a recording is a different request, and is left out.)
  await advance(56_000)
  assert.equal(h.asked.filter((u) => !u.includes('&exchange=')).length, 2)
  h.stop()
})

test('an announcement for another underlying is ignored', async () => {
  const h = harness('NIFTY27OCT26FUT')
  await settle()
  h.announce('BANKNIFTY')
  await settle()
  assert.equal(h.asked.length, 1)
  h.stop()
})

test('a closed chart stops listening, and an older OpenAlgo still polls', async () => {
  const h = harness('NIFTY27OCT26FUT')
  await settle()
  assert.equal(h.listeners(), 1)
  h.stop()
  assert.equal(h.listeners(), 0)

  const old = harness('NIFTY27OCT26FUT', { serverEvents: false })
  await settle()
  await advance(61_000)
  assert.equal(old.asked.length, 2, 'without server events the 60 s poll still runs')
  old.stop()
})
