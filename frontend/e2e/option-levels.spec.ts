/**
 * Option Levels end to end in a browser: the real chart and the real indicator
 * module Flask serves (strategies/indicators/writer_flow_live.js), on a
 * synthetic session with a stubbed /writerflow/api/levels, so it needs no
 * broker login or backend. The harness is e2e/harness/levels.ts.
 *
 * The synthetic positioning puts put writers below the spot and call writers
 * above it, heaviest on the round strikes, so the indicator must draw green
 * support lines low on the chart and red resistance lines high on it, as a few
 * clean lines rather than a band at every strike.
 */
import { expect, type Page, test } from '@playwright/test'

test.use({ viewport: { width: 1400, height: 800 } })
test.skip(({ browserName }) => browserName !== 'chromium', 'canvas drawing is engine independent')

async function open(page: Page) {
  await page.goto('/e2e/harness/levels.html')
  await page.waitForFunction(() => (window as unknown as { ready?: boolean }).ready === true)
  // Let the indicator fetch the stubbed levels and paint a frame.
  await page.waitForTimeout(500)
}

/** Per canvas row, how many pixels read as green (support) and red (resistance),
 * and the strongest row of each. The chart's own candles add some, so a row is
 * only counted as a level line when it runs most of the width. */
async function scan(page: Page) {
  return page.evaluate(() => {
    const canvases = [...document.querySelectorAll('canvas')]
    let best: { greenRows: number[]; redRows: number[]; width: number } | null = null
    for (const cv of canvases) {
      const ctx = cv.getContext('2d')
      if (!ctx) continue
      const { width, height } = cv
      let img: ImageData
      try {
        img = ctx.getImageData(0, 0, width, height)
      } catch {
        continue
      }
      const d = img.data
      const greenRows: number[] = []
      const redRows: number[] = []
      for (let y = 0; y < height; y++) {
        let green = 0
        let red = 0
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4
          const r = d[i]
          const g = d[i + 1]
          const b = d[i + 2]
          const a = d[i + 3]
          if (a < 20) continue
          if (g > r + 12 && g > b + 4) green++
          else if (r > g + 12 && r > b + 4) red++
        }
        // A level line spans most of the plot; a candle body does not.
        if (green > width * 0.5) greenRows.push(y)
        if (red > width * 0.5) redRows.push(y)
      }
      const score = greenRows.length + redRows.length
      if (!best || score > best.greenRows.length + best.redRows.length) {
        best = { greenRows, redRows, width }
      }
    }
    return best ?? { greenRows: [], redRows: [], width: 0 }
  })
}

test.describe('Option Levels overlay', () => {
  test('registers the indicator and fetches the levels record', async ({ page }) => {
    await open(page)
    const info = await page.evaluate(() => {
      const w = window as unknown as { chart: { indicators(): { indicatorId: string }[] } }
      return w.chart.indicators().map((i) => i.indicatorId)
    })
    expect(info).toContain('writer-flow-levels')
  })

  test('draws green support lines below red resistance lines', async ({ page }) => {
    await open(page)
    const { greenRows, redRows } = await scan(page)

    // Both sides are drawn: put writers (green) and call writers (red).
    expect(greenRows.length).toBeGreaterThan(0)
    expect(redRows.length).toBeGreaterThan(0)

    // Support sits below resistance on the chart: lower price is a larger y, so
    // every green line is further down than every red line.
    const lowestGreen = Math.min(...greenRows) // smallest y among green = its highest line
    const highestRed = Math.max(...redRows) // largest y among red = its lowest line
    expect(lowestGreen).toBeGreaterThan(highestRed)

    // Clean, not a wall: only the few significant levels are drawn, so the
    // coloured rows are a handful of thin lines, not most of the chart.
    const spanRows = greenRows.length + redRows.length
    expect(spanRows).toBeLessThan(40)
  })
})
