/**
 * The "?" on the Writer Flow Totals pane, and the note it opens.
 *
 *   node --test test/indicators/writer_flow_totals_help.test.mjs
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

const wf = await import(new URL('../../strategies/indicators/writer_flow_live.js', import.meta.url))

function canvas() {
  const texts = []
  const boxes = []
  const noop = () => {}
  const c = new Proxy(
    {
      measureText: (t) => ({ width: String(t).length * 5.5 }),
      fillText: (t, x, y) => texts.push({ t: String(t), x, y }),
      roundRect: (x, y, w, h) => boxes.push({ x, y, w, h }),
    },
    { get: (t, k) => (k in t ? t[k] : noop), set: () => true }
  )
  return { c, texts, boxes }
}

const rc = (plotHeight = 107) => ({ plotWidth: 1180, plotHeight, dpr: 1 })

test('closed, only the mark is drawn', () => {
  const { c, texts, boxes } = canvas()
  wf.drawTotalsHelp(c, rc(), false, 'quantity')
  assert.deepEqual(texts.map((t) => t.t), ['?'])
  assert.equal(boxes.length, 0)
})

test('open, the note says what each line and the bars mean, and fits the pane', () => {
  const { c, texts, boxes } = canvas()
  wf.drawTotalsHelp(c, rc(), true, 'quantity')
  const words = texts.map((t) => t.t).join(' ')
  for (const phrase of ['Red line', 'Green line', 'Bars', 'lakh', 'not who added it']) {
    assert.ok(words.includes(phrase), `the note does not mention "${phrase}"`)
  }
  const [box] = boxes
  assert.ok(box.y >= 0 && box.y + box.h <= 107, `the note runs past the 107 px pane (${box.y}..${box.y + box.h})`)
  assert.ok(texts.every((t) => t.t === '?' || (t.y >= box.y && t.y < box.y + box.h)), 'a line sits outside the box')
})

test('MCX quotes lots, and the note says so', () => {
  const { c, texts } = canvas()
  wf.drawTotalsHelp(c, rc(), true, 'lots')
  assert.ok(texts.some((t) => t.t.includes('(lots)')))
})
