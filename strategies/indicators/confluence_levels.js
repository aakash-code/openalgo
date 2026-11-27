/**
 * Confluence Levels: support and resistance where several independent
 * readings agree, the way a desk builds its levels.
 *
 * Sources, each a price with a weight:
 *  - Options positioning, from the Writer Flow record (/writerflow/api/levels):
 *    the put and call walls and the next ones, the gamma walls (strikes where
 *    dealers hedge hardest), the gamma flip, and the edges of the move the
 *    at-the-money straddle prices in for the session.
 *  - Volume: the previous session's point of control and value area (70% of
 *    its volume; time at price when the symbol has no volume, as an index has
 *    none), and today's VWAP with one and two standard deviation bands.
 *  - Reference prices: the previous session's high, low and close, and the
 *    previous week's high and low.
 *
 * Sources within a small band of each other merge into one zone; a zone's
 * score is the sum of their weights. Each zone carries how often price held it
 * over the bars on the chart, measured, not assumed. It describes where
 * positioning and trading sit; it does not forecast.
 */

const IST = 19800 // seconds east of UTC: sessions are Indian trading days
const DAY = 86400
const GREEN = '#22c55e'
const RED = '#ef4444'
const MUTED = '#94a3b8'
const REFRESH_MS = 60 * 1000
const VALUE_AREA = 0.7
const PROFILE_ROWS = 60
const HOLD_LOOKAHEAD = 20 // bars a touch is followed for
const HOLD_BOUNCE = 0.0015 // a move this far away (share of price) counts as held
const HOLD_BREAK = 0.001 // a close this far through counts as broken
const BREAK_MINUTES = 15 // a day's zone is broken once price stays through it this long

// Short names, shown on the labels and explained in the settings.
export const SOURCES = {
  PW: { name: 'Put wall', weight: 3 },
  CW: { name: 'Call wall', weight: 3 },
  PW2: { name: 'Next put wall', weight: 1.5 },
  CW2: { name: 'Next call wall', weight: 1.5 },
  GW: { name: 'Gamma wall', weight: 2 },
  FLIP: { name: 'Gamma flip', weight: 2 },
  EMH: { name: 'Expected move high', weight: 1.5 },
  EML: { name: 'Expected move low', weight: 1.5 },
  POC: { name: 'Previous point of control', weight: 2 },
  VAH: { name: 'Previous value area high', weight: 1.5 },
  VAL: { name: 'Previous value area low', weight: 1.5 },
  VWAP: { name: 'VWAP', weight: 1.5 },
  'V+1': { name: 'VWAP +1 sd', weight: 1 },
  'V-1': { name: 'VWAP -1 sd', weight: 1 },
  'V+2': { name: 'VWAP +2 sd', weight: 1.5 },
  'V-2': { name: 'VWAP -2 sd', weight: 1.5 },
  PDH: { name: 'Previous day high', weight: 2 },
  PDL: { name: 'Previous day low', weight: 2 },
  PDC: { name: 'Previous close', weight: 1 },
  PWH: { name: 'Previous week high', weight: 2 },
  PWL: { name: 'Previous week low', weight: 2 },
}

export const dayOf = (t) => Math.floor((t + IST) / DAY)
export const weekOf = (t) => Math.floor((dayOf(t) + 3) / 7) // weeks start Monday

/** Bars grouped by session day, oldest first: [{ day, bars }]. */
export function sessions(bars) {
  const out = []
  for (const b of bars) {
    const d = dayOf(b.time)
    if (!out.length || out[out.length - 1].day !== d) out.push({ day: d, bars: [] })
    out[out.length - 1].bars.push(b)
  }
  return out
}

export function isIntraday(bars) {
  if (bars.length < 3) return false
  const gaps = []
  for (let i = 1; i < bars.length; i++) gaps.push(bars[i].time - bars[i - 1].time)
  gaps.sort((a, b) => a - b)
  return gaps[gaps.length >> 1] < DAY
}

const hasVolume = (bars) => bars.some((b) => Number(b.volume) > 0)

/** Point of control and value area of one session: volume at price, or time at price without volume. */
export function profile(bars) {
  if (!bars.length) return null
  const lo = Math.min(...bars.map((b) => b.low))
  const hi = Math.max(...bars.map((b) => b.high))
  if (!(hi > lo)) return null
  const size = (hi - lo) / PROFILE_ROWS
  const rows = new Array(PROFILE_ROWS).fill(0)
  const byVolume = hasVolume(bars)
  for (const b of bars) {
    const a = Math.max(0, Math.min(PROFILE_ROWS - 1, Math.floor((b.low - lo) / size)))
    const z = Math.max(0, Math.min(PROFILE_ROWS - 1, Math.floor((b.high - lo) / size)))
    const w = (byVolume ? Number(b.volume) || 0 : 1) / (z - a + 1)
    for (let r = a; r <= z; r++) rows[r] += w
  }
  const total = rows.reduce((s, v) => s + v, 0)
  if (!(total > 0)) return null
  let poc = 0
  for (let r = 1; r < rows.length; r++) if (rows[r] > rows[poc]) poc = r
  let a = poc
  let z = poc
  let inside = rows[poc]
  while (inside < total * VALUE_AREA && (a > 0 || z < rows.length - 1)) {
    const down = a > 0 ? rows[a - 1] : -1
    const up = z < rows.length - 1 ? rows[z + 1] : -1
    if (up >= down) inside += rows[++z]
    else inside += rows[--a]
  }
  const mid = (r) => lo + (r + 0.5) * size
  return { poc: mid(poc), vah: lo + (z + 1) * size, val: lo + a * size, byVolume }
}

/** Today's VWAP and its standard deviation bands; null without volume. */
export function vwapBands(bars) {
  let pv = 0
  let v = 0
  let p2v = 0
  for (const b of bars) {
    const vol = Number(b.volume) || 0
    const p = (b.high + b.low + b.close) / 3
    pv += p * vol
    p2v += p * p * vol
    v += vol
  }
  if (!(v > 0)) return null
  const vwap = pv / v
  const sd = Math.sqrt(Math.max(0, p2v / v - vwap * vwap))
  return { vwap, sd }
}

/** Every source level the bars and the option record give, as { price, code }. */
export function sourceLevels(bars, body, { vwap = true } = {}) {
  const out = []
  const add = (price, code) => {
    if (Number.isFinite(price) && price > 0) out.push({ price, code })
  }
  const days = sessions(bars)
  if (days.length >= 2) {
    const prev = days[days.length - 2].bars
    add(Math.max(...prev.map((b) => b.high)), 'PDH')
    add(Math.min(...prev.map((b) => b.low)), 'PDL')
    add(prev[prev.length - 1].close, 'PDC')
    if (isIntraday(bars)) {
      const p = profile(prev)
      if (p) {
        add(p.poc, 'POC')
        add(p.vah, 'VAH')
        add(p.val, 'VAL')
      }
    }
  }
  const lastWeek = bars.length ? weekOf(bars[bars.length - 1].time) : null
  const prevWeek = bars.filter((b) => weekOf(b.time) === lastWeek - 1)
  if (prevWeek.length) {
    add(Math.max(...prevWeek.map((b) => b.high)), 'PWH')
    add(Math.min(...prevWeek.map((b) => b.low)), 'PWL')
  }
  if (vwap && days.length && isIntraday(bars)) {
    const vb = vwapBands(days[days.length - 1].bars)
    if (vb) {
      add(vb.vwap, 'VWAP')
      if (vb.sd > 0) {
        add(vb.vwap + vb.sd, 'V+1')
        add(vb.vwap - vb.sd, 'V-1')
        add(vb.vwap + 2 * vb.sd, 'V+2')
        add(vb.vwap - 2 * vb.sd, 'V-2')
      }
    }
  }
  if (body && body.status === 'success') {
    const spot = Number(body.spot)
    const half = (Number(body.step) || 0) / 2
    // The heaviest put wall at or below the price and call wall at or above it, then the next.
    // Only walls within reach count: two days' priced-in move, the same reach
    // the server uses for the gamma flip. A put wall 460 points under the
    // price (CRUDEOIL 7500 on 27-Nov-2026, the day's move 158) took the
    // nearby 7800 wall's place.
    const g0 = body.gamma || {}
    const reach = g0.expected_move && g0.expected_move.day > 0 ? 2 * g0.expected_move.day : Infinity
    const near = (k) => !Number.isFinite(spot) || Math.abs(k - spot) <= reach
    const walls = (body.walls || []).filter((w) => near(w.strike))
    const puts = walls.filter((w) => w.strike <= spot + half).sort((a, b) => b.put - a.put)
    const calls = walls.filter((w) => w.strike >= spot - half).sort((a, b) => b.call - a.call)
    if (puts[0]) add(puts[0].strike, 'PW')
    if (puts[1] && puts[1].put >= puts[0].put * 0.5) add(puts[1].strike, 'PW2')
    if (calls[0]) add(calls[0].strike, 'CW')
    if (calls[1] && calls[1].call >= calls[0].call * 0.5) add(calls[1].strike, 'CW2')
    const g = body.gamma || {}
    for (const w of g.walls || []) if (near(w.strike)) add(w.strike, 'GW')
    if (g.flip != null && !g.flip_far) add(Number(g.flip), 'FLIP')
    const em = g.expected_move
    if (em && em.day > 0 && Number.isFinite(spot)) {
      add(spot + em.day, 'EMH')
      add(spot - em.day, 'EML')
    }
  }
  return out
}

/**
 * Merge sources within `band` of each other into zones and score them.
 * A zone's price is the weight-averaged price of its sources; its role is
 * support below the price and resistance above it.
 */
export function buildZones(levels, spot, band) {
  const sorted = [...levels].sort((a, b) => a.price - b.price)
  const zones = []
  for (const lv of sorted) {
    const w = SOURCES[lv.code]?.weight ?? 1
    const z = zones[zones.length - 1]
    // Measured from the zone's lowest source, not its last one: chained
    // neighbours otherwise grew one SENSEX zone to 352 points and ten sources
    // across the price (26-Nov-2026).
    if (z && lv.price - z.low <= band) {
      z.high = lv.price
      z.sum += lv.price * w
      z.score += w
      if (!z.codes.includes(lv.code)) z.codes.push(lv.code)
    } else {
      zones.push({ low: lv.price, high: lv.price, sum: lv.price * w, score: w, codes: [lv.code] })
    }
  }
  return zones.map((z) => {
    const price = z.sum / z.score
    return {
      price,
      low: z.low,
      high: z.high,
      score: Math.round(z.score * 10) / 10,
      codes: z.codes,
      // Within the band of the price a zone is where price is, neither side yet.
      role: !Number.isFinite(spot)
        ? 'support'
        : Math.abs(price - spot) <= band
          ? 'at price'
          : price < spot
            ? 'support'
            : 'resistance',
    }
  })
}

/**
 * How often price held a level on the bars given: a touch is a bar reaching
 * within `band` of it from its side; held if price then moved HOLD_BOUNCE
 * away before closing HOLD_BREAK through it, within HOLD_LOOKAHEAD bars.
 */
export function holdRate(bars, price, role, band) {
  let n = 0
  let held = 0
  const up = role === 'support'
  for (let i = 1; i < bars.length - 1; i++) {
    const prev = bars[i - 1].close
    const b = bars[i]
    const touched = up ? prev > price + band && b.low <= price + band : prev < price - band && b.high >= price - band
    if (!touched) continue
    let outcome = null
    for (let j = i; j < Math.min(bars.length, i + HOLD_LOOKAHEAD + 1); j++) {
      const c = bars[j]
      if (up ? c.close < price * (1 - HOLD_BREAK) : c.close > price * (1 + HOLD_BREAK)) {
        outcome = 'broke'
        break
      }
      if (up ? c.high >= price * (1 + HOLD_BOUNCE) && j > i : c.low <= price * (1 - HOLD_BOUNCE) && j > i) {
        outcome = 'held'
        break
      }
    }
    if (outcome) {
      n++
      if (outcome === 'held') held++
      i += 3 // one approach is one touch
    }
  }
  return { n, held }
}

/** The zones worth drawing: the best `count` on each side of the price, scored at least `min`. */
export function strongest(zones, count, min) {
  const pick = (role) =>
    zones
      .filter((z) => z.role === role && z.score >= min)
      .sort((a, b) => b.score - a.score)
      .slice(0, count)
  return [...pick('support'), ...pick('at price').slice(0, 1), ...pick('resistance')]
}

/**
 * Label centres, sorted top to bottom, spaced at least `gap` apart and kept
 * within [top, bottom]: pushed down past each other, then back up from the
 * bottom so the last one never runs off the floor.
 */
export function stackLabels(ys, gap, top, bottom) {
  const out = []
  for (const y of ys) out.push(Math.max(top, out.length ? Math.max(y, out[out.length - 1] + gap) : y))
  let cap = bottom
  for (let i = out.length - 1; i >= 0; i--) {
    out[i] = Math.min(out[i], cap)
    cap = out[i] - gap
  }
  return out
}

/** The ISO date (YYYY-MM-DD) of a session day number. */
export const isoOf = (day) => new Date(day * DAY * 1000).toISOString().slice(0, 10)

const OPTION_CODES = ['PW', 'CW', 'PW2', 'CW2', 'GW', 'FLIP', 'EMH', 'EML']
const VOLUME_CODES = ['POC', 'VAH', 'VAL', 'VWAP', 'V+1', 'V-1', 'V+2', 'V-2']

/**
 * What a zone did on its own day: broke if a bar closed through it, held if
 * price reached it and stayed on its side, untouched otherwise.
 */
export function dayOutcome(dayBars, z, band) {
  // Broken only once price stays through it for BREAK_MINUTES, as the server's
  // option levels judge a break: one minute's close through it read 25 of 37
  // reached zones as broken on 18-27 Nov 2026.
  let touched = false
  let since = null
  for (const b of dayBars) {
    let through = false
    if (z.role === 'support') {
      through = b.close < z.low * (1 - HOLD_BREAK)
      if (b.low <= z.high + band) touched = true
    } else if (z.role === 'resistance') {
      through = b.close > z.high * (1 + HOLD_BREAK)
      if (b.high >= z.low - band) touched = true
    }
    if (!through) since = null
    else {
      if (since == null) since = b.time
      if (b.time - since >= BREAK_MINUTES * 60) return 'broke'
    }
  }
  return touched ? 'held' : 'untouched'
}

/**
 * Each session's own levels, oldest first, for the last `days` sessions.
 * A past day gets only what was known at its open: the previous day and week,
 * the previous session's profile and the previous session's closing option
 * record (`records[iso date]`). No VWAP and no hindsight, so whether it held
 * that day is a fair test. Today adds the live option record and VWAP.
 * Hold rates are read from the bars before each day only.
 */
export function dayZones(bars, settings, live, records = {}) {
  const ss = sessions(bars)
  const count = Math.max(1, Math.min(10, Number(settings.days) || 7))
  const keep = (code) => {
    if (OPTION_CODES.includes(code)) return settings.options !== false
    if (VOLUME_CODES.includes(code)) return settings.volume !== false
    return settings.reference !== false
  }
  const out = []
  const wanted = []
  // From the first session too: a chart holding only today still gets today's levels.
  for (let i = Math.max(0, ss.length - count); i < ss.length; i++) {
    const today = i === ss.length - 1
    const day = ss[i]
    const start = day.bars[0].time
    // The previous day, week and profile are read from the sessions before
    // this one; VWAP, the one same-day source, is today's alone.
    const upTo = bars.filter((b) => b.time <= day.bars[day.bars.length - 1].time)
    const before = bars.filter((b) => b.time < start)
    let body = null
    if (settings.options !== false) {
      if (today && live && live.date === isoOf(day.day)) body = live
      else {
        const prior = i > 0 ? isoOf(ss[i - 1].day) : null
        if (!today && prior) wanted.push(prior)
        const r = prior ? records[prior] : null
        if (r && r.status === 'success' && r.date === prior) body = r
      }
    }
    const spot = today ? bars[bars.length - 1].close : day.bars[0].open
    const band = (spot * (Number(settings.band) || 0.12)) / 100
    const levels = sourceLevels(upTo, body, { vwap: today }).filter((l) => keep(l.code))
    const zones = strongest(buildZones(levels, spot, band), Number(settings.perSide) || 3, Number(settings.minScore) || 3)
    for (const z of zones) {
      if (z.role !== 'at price') z.hold = holdRate(before, z.price, z.role, band)
      else {
        const a = holdRate(before, z.price, 'support', band)
        const b = holdRate(before, z.price, 'resistance', band)
        z.hold = { n: a.n + b.n, held: a.held + b.held }
      }
      if (!today) z.outcome = dayOutcome(day.bars, z, band)
    }
    out.push({ day: day.day, start, end: today ? null : ss[i + 1].bars[0].time, today, zones, options: Boolean(body) })
  }
  return { days: out, wanted }
}

function resolveUnderlying(settings, symbol) {
  const typed = String(settings.underlying ?? '').trim().toUpperCase()
  if (typed) return typed.replace(/[^A-Z0-9&-]/g, '')
  return String(symbol ?? '')
    .toUpperCase()
    .replace(/\d{2}[A-Z]{3}\d{2}FUT$/, '')
    .replace(/\d{2}[A-Z]{3}\d{2}[\d.]*(?:CE|PE)$/, '')
    .replace(/[^A-Z0-9&-]/g, '')
}

const fmt = (p) => Number(p).toLocaleString('en-IN', { maximumFractionDigits: 1 })

export default function ({ registerIndicator, nulls }) {
  registerIndicator({
    id: 'confluence-levels',
    name: 'Confluence Levels',
    category: 'Custom',
    placement: 'onchart',

    inputs: [
      {
        key: 'underlying', type: 'text', label: 'Underlying', default: '', group: 'Instrument',
        tooltip: "Left blank, the chart's own symbol is used: NIFTY futures read NIFTY. Option sources need Writer Flow recording it.",
      },
      {
        key: 'days', type: 'number', label: 'Days Shown', default: 7, min: 1, max: 10, step: 1, group: 'Levels',
        tooltip: "Each day draws its own levels over its own bars. A past day uses only what was known at its open (previous day and week, the previous session's profile and closing option walls), and its label says whether it held, broke or was not reached.",
      },
      { key: 'perSide', type: 'number', label: 'Zones Each Side', default: 3, min: 1, max: 6, step: 1, group: 'Levels' },
      {
        key: 'minScore', type: 'number', label: 'Minimum Score', default: 3, min: 1, max: 10, step: 0.5, group: 'Levels',
        tooltip: 'A zone shows only if its sources add up to this. Put and call walls count 3; gamma walls, the flip, point of control, previous day and week high and low 2; the rest 1 to 1.5.',
      },
      {
        key: 'band', type: 'number', label: 'Merge Band (%)', default: 0.12, min: 0.02, max: 0.5, step: 0.01, group: 'Levels',
        tooltip: 'Sources closer than this share of the price join one zone.',
      },
      { key: 'options', type: 'boolean', label: 'Option Sources', default: true, group: 'Sources' },
      { key: 'volume', type: 'boolean', label: 'Volume And VWAP Sources', default: true, group: 'Sources' },
      { key: 'reference', type: 'boolean', label: 'Previous Day And Week', default: true, group: 'Sources' },
      {
        key: 'codes', type: 'boolean', label: 'Show Sources On Labels', default: true, group: 'Display',
        tooltip: 'PW/CW put and call wall, PW2/CW2 the next, GW gamma wall, FLIP gamma flip, EMH/EML expected move, POC/VAH/VAL previous session profile, VWAP and V±1/V±2 its bands, PDH/PDL/PDC previous day, PWH/PWL previous week.',
      },
    ],

    plots: [{ key: 'cl', type: 'line', title: 'Confluence Levels', style: { visible: false } }],

    calc(bars, settings, store) {
      const out = { cl: nulls(new Array(bars.length).fill(Number.NaN)) }
      if (!store || bars.length < 2) {
        if (store) store.zones = []
        return out
      }
      // The option record must be the chart's own last session: an old one
      // (BHEL's 27OCT26 expiry, a month gone, on 27-Nov-2026) names walls that
      // no longer exist.
      const lastIso = isoOf(dayOf(bars[bars.length - 1].time))
      store.stale = Boolean(store.levels && settings.options !== false && store.levels.date !== lastIso)
      const { days, wanted } = dayZones(bars, settings, store.levels, store.records || {})
      store.days = days
      store.wanted = wanted
      const zones = days.length && days[days.length - 1].today ? days[days.length - 1].zones : []
      store.zones = zones
      return out
    },

    attach(ctx) {
      const state = { problem: null, timer: null, key: '' }
      const yOf = (rc, p) => {
        const y = rc.priceScale.priceToY(p)
        return y == null || Number.isNaN(y) ? null : y
      }

      const load = async () => {
        const settings = ctx.settings()
        if (settings.options === false) return
        const u = resolveUnderlying(settings, ctx.symbol?.())
        if (!u) return
        if (u !== state.key) {
          state.key = u
          ctx.store.levels = null
          ctx.store.records = {}
        }
        try {
          const res = await fetch(`/writerflow/api/levels?underlying=${encodeURIComponent(u)}`, {
            credentials: 'same-origin',
          })
          const body = await res.json().catch(() => null)
          if (state.key !== u) return
          if (body && body.status === 'success') {
            ctx.store.levels = body
            state.problem = null
          } else {
            ctx.store.levels = null
            state.problem = 'no option record for this symbol, chart sources only'
          }
        } catch {
          state.problem = 'option record unreachable, chart sources only'
        }
        ctx.requestRecompute()
        // Past days' closing option records, after today's so the live levels
        // come first, and together: one by one they held the first draw for
        // about 20 seconds on the server (27-Nov-2026). Each is asked for
        // once: a finished day never changes, and one with none recorded is
        // remembered as none.
        const records = (ctx.store.records ||= {})
        const missing = (ctx.store.wanted || []).filter((d) => !(d in records))
        if (!missing.length) return
        for (const d of missing) records[d] = null
        await Promise.all(
          missing.map(async (d) => {
            try {
              const r = await fetch(`/writerflow/api/levels?underlying=${encodeURIComponent(u)}&date=${d}`, {
                credentials: 'same-origin',
              })
              const b = await r.json().catch(() => null)
              if (state.key === u) records[d] = b && b.status === 'success' ? b : null
            } catch {
              delete records[d] // unreachable: ask again next time
            }
          }),
        )
        if (state.key === u) ctx.requestRecompute()
      }

      const layer = {
        zOrder() {
          return 'top'
        },
        draw(c, rc) {
          const settings = ctx.settings()
          const days = ctx.store.days || []
          const dpr = rc.dpr || 1
          const width = rc.plotWidth
          const all = days.flatMap((d) => d.zones)
          const top = Math.max(...all.map((z) => z.score), 1)
          const xOf = (t) => {
            const x = rc.timeScale.indexToX(rc.dataLayer.timeToIndexFloat(t))
            return Number.isFinite(x) ? x : null
          }
          c.save()
          const labels = []
          for (const d of days) {
            // Each day's levels over its own bars only, so a day's line stops
            // where the next day's begin.
            const x0 = Math.max(0, xOf(d.start) ?? 0)
            const x1 = d.end == null ? width : Math.min(width, xOf(d.end) ?? width)
            if (!(x1 > x0)) continue
            for (const z of d.zones) {
              const y = yOf(rc, z.price)
              if (y == null || y < 0 || y > rc.plotHeight) continue
              const color =
                z.role === 'support'
                  ? rc.theme?.upColor || GREEN
                  : z.role === 'resistance'
                    ? rc.theme?.downColor || RED
                    : rc.theme?.axisText || MUTED
              const strength = z.score / top
              const y1 = yOf(rc, z.high)
              const y2 = yOf(rc, z.low)
              if (y1 != null && y2 != null && Math.abs(y2 - y1) >= 1) {
                c.globalAlpha = 0.08 + 0.1 * strength
                c.fillStyle = color
                c.fillRect(x0 * dpr, Math.min(y1, y2) * dpr, (x1 - x0) * dpr, Math.abs(y2 - y1) * dpr)
              }
              c.globalAlpha = (0.45 + 0.5 * strength) * (z.outcome === 'broke' ? 0.6 : 1)
              c.strokeStyle = color
              c.lineWidth = (1 + 2.5 * strength) * dpr
              c.setLineDash(z.score >= 5 ? [] : [6 * dpr, 4 * dpr])
              c.beginPath()
              c.moveTo(x0 * dpr, y * dpr)
              c.lineTo(x1 * dpr, y * dpr)
              c.stroke()
              const codes = settings.codes === false ? '' : `  ${z.codes.join('+')}`
              if (d.today) {
                const held = z.hold && z.hold.n ? `  held ${z.hold.held}/${z.hold.n}` : ''
                labels.push({ y, color, text: `${fmt(z.price)}  [${z.score}]${codes}${held}` })
              } else if (x1 - x0 > 70) {
                // A past day: its price and what it did that day, at the day's start.
                c.setLineDash([])
                c.globalAlpha = 0.9
                c.font = `${9 * dpr}px sans-serif`
                c.textBaseline = 'bottom'
                c.fillStyle = color
                const text = `${fmt(z.price)} ${z.outcome}`
                c.fillText(text, (x0 + 3) * dpr, (y - 2) * dpr)
              }
            }
          }
          c.setLineDash([])
          // Spaced 15 px apart and kept above the status line at the bottom:
          // a put wall near the bottom printed over it (CRUDEOIL, 27-Nov-2026).
          labels.sort((a, b) => a.y - b.y)
          const ys = stackLabels(
            labels.map((l) => l.y),
            15,
            8,
            rc.plotHeight - 26,
          )
          c.font = `${10 * dpr}px sans-serif`
          c.textBaseline = 'middle'
          labels.forEach((l, k) => {
            l.y = ys[k]
          })
          for (const l of labels) {
            const y = l.y
            const w = c.measureText(l.text).width / dpr + 10
            // Right edge, by the price axis: the top left holds the legend.
            const x = width - 6 - w
            c.globalAlpha = 0.9
            c.fillStyle = rc.theme?.background || '#131722'
            c.beginPath()
            c.roundRect(x * dpr, (y - 8) * dpr, w * dpr, 16 * dpr, 3 * dpr)
            c.fill()
            c.strokeStyle = l.color
            c.lineWidth = dpr
            c.stroke()
            c.globalAlpha = 1
            c.fillStyle = l.color
            c.fillText(l.text, (x + 5) * dpr, y * dpr)
          }
          const lv = ctx.store.levels
          const note = state.problem
            ? `Confluence levels: ${state.problem}`
            : lv && ctx.store.stale
              ? `Confluence levels: option record is from ${lv.date}, not the chart's day, so not used`
              : lv
                ? `Confluence levels ${lv.underlying} ${lv.expiry}, options at ${lv.minute}`
                : ''
          if (note) {
            // Bottom right, where the option levels note sits: the top left
            // is the symbol's legend.
            c.globalAlpha = 0.75
            c.fillStyle = rc.theme?.axisText || MUTED
            c.textBaseline = 'bottom'
            c.font = `${11 * dpr}px sans-serif`
            const tw = c.measureText(note).width / dpr
            c.fillText(note, (width - 8 - tw) * dpr, (rc.plotHeight - 6) * dpr)
          }
          c.restore()
        },
      }
      ctx.addPrimitive(layer)
      load()
      // Every few seconds: reload at once when the underlying changes, else each minute.
      let lastLoad = Date.now()
      state.timer = setInterval(() => {
        if (document.hidden) return
        const settings = ctx.settings()
        const changed = resolveUnderlying(settings, ctx.symbol?.()) !== state.key
        const records = ctx.store.records || {}
        const pending = (ctx.store.wanted || []).some((d) => !(d in records))
        if (changed || pending || Date.now() - lastLoad >= REFRESH_MS) {
          lastLoad = Date.now()
          load()
        }
      }, 5000)
      return () => {
        clearInterval(state.timer)
        ctx.removePrimitive(layer)
      }
    },
  })
}
