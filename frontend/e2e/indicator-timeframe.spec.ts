/**
 * The per-indicator Timeframe, end to end in a browser: the real chart, the
 * real built-in indicators and the real wrapper, on synthetic NSE sessions
 * served by e2e/harness/mtf.ts, so it needs no broker login.
 *
 * The reference is the higher chart: a 1h study on a 5m chart must read, on
 * every 5m bar, exactly what the same study reads on the 1h chart for that hour.
 */
import { expect, type Page, test } from '@playwright/test'

test.use({ viewport: { width: 1400, height: 800 } })
test.skip(({ browserName }) => browserName !== 'chromium', 'chart maths is engine independent')

async function open(page: Page) {
  await page.goto('/e2e/harness/mtf.html')
  await page.waitForFunction(() => (window as unknown as { ready?: boolean }).ready === true)
}

/** Add `id` at `tf` on the chart and wait for its higher bars to arrive. */
async function add(page: Page, id: string, settings: Record<string, unknown>) {
  return page.evaluate(
    async ({ id, settings }) => {
      const w = window as any
      const ind = w.chart.addIndicator(id, settings)
      for (let i = 0; i < 100 && ind.dataStatus()?.state === 'loading'; i++) {
        await new Promise((r) => setTimeout(r, 50))
      }
      return ind.id as string
    },
    { id, settings }
  )
}

/** Largest gap between the chart-bar values and the same study on the higher chart. */
async function worstGap(page: Page, instanceId: string, tf: string) {
  return page.evaluate(
    ({ instanceId, tf }) => {
      const w = window as any
      const ind = w.chart.indicators().find((i: any) => i.id === instanceId)
      const s = ind.settings()
      const higher = w.barsFor(tf)
      const ref = w.core.getIndicator(ind.indicatorId).calc(higher, { ...s, tf: '' }, {}, undefined)
      const got = ind.values()
      const bars = w.chart.primaryBars()
      let worst = 0
      let compared = 0
      let mismatchedNulls = 0
      for (const key of Object.keys(ref)) {
        let j = -1
        for (let i = 0; i < bars.length; i++) {
          while (j + 1 < higher.length && higher[j + 1].time <= bars[i].time) j++
          const a = got[key]?.[i]
          const b = ref[key][j]
          if (a == null || b == null) {
            if ((a == null) !== (b == null)) mismatchedNulls++
            continue
          }
          worst = Math.max(worst, Math.abs(a - b))
          compared++
        }
      }
      return { worst, compared, mismatchedNulls }
    },
    { instanceId, tf }
  )
}

test.describe('indicator Timeframe on /trading charts', () => {
  test.beforeEach(async ({ page }) => open(page))

  test('a 1h EMA on a 5m chart equals the 1h chart, hour for hour', async ({ page }) => {
    const id = await add(page, 'ema', { tf: '1h' })
    const gap = await worstGap(page, id, '1h')
    expect(gap.compared).toBeGreaterThan(1000)
    expect(gap.mismatchedNulls).toBe(0)
    expect(gap.worst).toBeLessThan(1e-6)
  })

  test('the value is a step: one value per hour, starting on the 09:15 bar', async ({ page }) => {
    const id = await add(page, 'ema', { tf: '1h' })
    const steps = await page.evaluate((id) => {
      const w = window as any
      const ind = w.chart.indicators().find((i: any) => i.id === id)
      const v = ind.values().ma as number[]
      const bars = w.chart.primaryBars()
      const changes: string[] = []
      for (let i = 1; i < v.length; i++) {
        if (v[i] !== v[i - 1]) {
          changes.push(new Date((bars[i].time + 19_800) * 1000).toISOString().slice(11, 16))
        }
      }
      return [...new Set(changes)].sort()
    }, id)
    expect(steps).toEqual(['09:15', '10:15', '11:15', '12:15', '13:15', '14:15', '15:15'])
  })

  test('multi-plot and band studies match their higher chart too', async ({ page }) => {
    for (const [id, tf] of [
      ['supertrend', '15m'],
      ['bollinger', '1h'],
      ['macd', '15m'],
      ['rsi', 'D'],
    ] as const) {
      const instance = await add(page, id, { tf })
      const gap = await worstGap(page, instance, tf)
      expect(gap.compared, `${id} at ${tf}`).toBeGreaterThan(100)
      expect(gap.mismatchedNulls, `${id} at ${tf}`).toBe(0)
      expect(gap.worst, `${id} at ${tf}`).toBeLessThan(1e-6)
    }
  })

  test('every registered indicator runs at 1h on a 5m chart without an error', async ({ page }) => {
    const failures = await page.evaluate(async () => {
      const w = window as any
      const out: string[] = []
      for (const d of w.core.registeredIndicators()) {
        if (!d.inputs.some((i: any) => i.key === 'tf')) continue
        const ind = w.chart.addIndicator(d.id, { tf: '1h' })
        for (let i = 0; i < 100 && ind.dataStatus()?.state === 'loading'; i++) {
          await new Promise((r) => setTimeout(r, 20))
        }
        w.chart.indicators()
        const st = ind.dataStatus()
        if (st?.state === 'error') out.push(`${d.id}: ${String(st.error?.message ?? st.error)}`)
        const lengths = Object.values(ind.values()).map((c: any) => c.length)
        if (lengths.some((n: number) => n !== w.chart.primaryBars().length)) out.push(`${d.id}: column length`)
        ind.remove()
      }
      return out
    })
    expect(failures).toEqual([])
  })

  test('the forming hour moves with each 5m tick', async ({ page }) => {
    const id = await add(page, 'ema', { tf: '1h' })
    const [before, after] = await page.evaluate(async (id) => {
      const w = window as any
      const ind = () => w.chart.indicators().find((i: any) => i.id === id)
      const last = () => ind().values().ma.at(-1)
      const a = last()
      const bar = w.chart.primaryBars().at(-1)
      w.series.update({ ...bar, close: bar.close + 200, high: bar.high + 200 })
      await new Promise((r) => setTimeout(r, 50))
      return [a, last()]
    }, id)
    // EMA(9) weights the newest close 2/10: +200 on it moves the hour by 40.
    expect(after - before).toBeCloseTo(40, 6)
  })

  test('a timeframe at or below the chart is refused in a plain sentence', async ({ page }) => {
    const id = await add(page, 'ema', { tf: '1m' })
    const status = await page.evaluate((id) => {
      const w = window as any
      w.chart.indicators()
      const st = w.chart
        .indicators()
        .find((i: any) => i.id === id)
        .dataStatus()
      return { state: st?.state, message: String(st?.error?.message ?? '') }
    }, id)
    expect(status.state).toBe('error')
    expect(status.message).toContain("is not higher than the chart's 5m")
  })

  test('switching back to the chart interval restores the plain study', async ({ page }) => {
    const id = await add(page, 'ema', { tf: '1h' })
    const same = await page.evaluate(async (id) => {
      const w = window as any
      const ind = w.chart.indicators().find((i: any) => i.id === id)
      ind.setSettings({ tf: '' })
      const plain = w.core
        .getIndicator('ema')
        .calc(w.chart.primaryBars(), { ...ind.settings(), tf: '' }, {}, undefined).ma
      return JSON.stringify(w.chart.indicators().find((i: any) => i.id === id).values().ma) === JSON.stringify(plain)
    }, id)
    expect(same).toBe(true)
  })

  test('the Timeframe survives a save and restore of the chart', async ({ page }) => {
    await add(page, 'ema', { tf: '1h', length: 20 })
    const state = await page.evaluate(() => JSON.stringify((window as any).chart.getState()))
    await open(page)
    const restored = await page.evaluate(async (s) => {
      const w = window as any
      for (const i of w.chart.indicators()) i.remove()
      w.chart.restoreState(JSON.parse(s))
      const ind = w.chart.indicators().find((i: any) => i.indicatorId === 'ema')
      for (let i = 0; i < 100 && ind?.dataStatus()?.state === 'loading'; i++) {
        await new Promise((r) => setTimeout(r, 50))
      }
      return ind ? { tf: ind.settings().tf, length: ind.settings().length, id: ind.id } : null
    }, state)
    expect(restored).toMatchObject({ tf: '1h', length: 20 })
    const gap = await worstGap(page, restored!.id, '1h')
    expect(gap.worst).toBeLessThan(1e-6)
  })

  test('renders as a step line over the 5m candles', async ({ page }) => {
    await add(page, 'ema', { tf: '1h' })
    await add(page, 'supertrend', { tf: '15m' })
    await page.waitForTimeout(300)
    await page.screenshot({ path: 'test-results/indicator-timeframe.png' })
  })
})
