/**
 * TradeFinder's ranked lists (Intraday Boost / Breakout Beacon / High
 * Powered / Sectors) surfaced in the charting terminal's right sidebar.
 *
 * Both endpoints behind this panel are poll-only -- neither pushes an update
 * over the socket -- and the snapshotter that feeds `/boostsnapshots` for
 * backtesting only samples every 5 minutes, so polling this panel faster
 * than that buys nothing. 30s keeps the panel visibly live without hammering
 * the server-side TradeFinder session.
 *
 * `breakout_beacon`'s param_0/param_1/param_2 are not genuine ltp/prev_close/
 * change_pct (see services/tradefinder_service.py:_map_items) -- one of them
 * is actually a BULL/BEAR sentiment label that the backend coerces to 0.0.
 * Score is the only reliable field on that list, so LTP/Chg% are hidden for
 * it rather than shown as misleading zeros.
 */

import {
  ArrowDown,
  ArrowUp,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import {
  type JwtHealthResponse,
  type MarketPulseData,
  type SectorScopeData,
  type SectorStockItem,
  type TfListItem,
  tradefinderApi,
} from '@/api/tradefinder'
import { watchlistApi } from '@/api/watchlist'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import type { SearchRow } from '@/lib/trading/terminal'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'
import { PANEL_HEADER, PanelShell } from './panelShell'

const PREFS_KEY = 'oa-trading-tradefinder'
const FEATURES_KEY = 'oa-trading-tradefinder-features'
const ACTIVE_WATCHLIST_KEY = 'oa-trading-watchlist'

const POLL_INTERVALS = [5_000, 15_000, 30_000, 60_000] as const
const CPR_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'bullish', label: 'Bullish only' },
  { id: 'bearish', label: 'Bearish only' },
] as const
type CprFilter = (typeof CPR_FILTERS)[number]['id']

/** Every feature here defaults to off. Nothing in this panel changes
 * behaviour until the user opens the settings popover and switches it on
 * themselves -- same rule the Gainers/Losers sort follows. */
interface Features {
  freshness: boolean
  pauseAutoRefresh: boolean
  rankArrows: boolean
  search: boolean
  compactDensity: boolean
  addToWatchlist: boolean
  columns: { ltp: boolean; chg: boolean; score: boolean; cpr: boolean }
  /** Off = the hardcoded 30s default; any other value is the user's choice. */
  pollIntervalMs: number
  jwtHealth: boolean
  retryBackoff: boolean
  /** null = no filter. Only meaningful once a real value is set. */
  scoreThreshold: number | null
  cprFilter: CprFilter
  sectorChips: boolean
  /** Whether the pin icon shows at all -- pinning changes row order, so it
   * stays opt-in like everything else here, not just the pinned list itself. */
  pinning: boolean
  /** Pinned symbols float to the top of whichever list/sector view they
   * appear in, ahead of the active sort. */
  pinnedSymbols: string[]
}

const DEFAULT_FEATURES: Features = {
  freshness: false,
  pauseAutoRefresh: false,
  rankArrows: false,
  search: false,
  compactDensity: false,
  addToWatchlist: false,
  columns: { ltp: true, chg: true, score: true, cpr: true },
  pollIntervalMs: 30_000,
  jwtHealth: false,
  retryBackoff: false,
  scoreThreshold: null,
  cprFilter: 'all',
  sectorChips: false,
  pinning: false,
  pinnedSymbols: [],
}

function readFeatures(): Features {
  try {
    const saved = JSON.parse(localStorage.getItem(FEATURES_KEY) || '{}')
    return {
      ...DEFAULT_FEATURES,
      ...saved,
      columns: { ...DEFAULT_FEATURES.columns, ...saved.columns },
      pinnedSymbols: Array.isArray(saved.pinnedSymbols) ? saved.pinnedSymbols : [],
    }
  } catch {
    return DEFAULT_FEATURES
  }
}

/** Sentinel sector key for "every sector combined", distinct from any real
 * `"<name>_r_factor"` key so it can share the same selection state. */
const ALL_SECTORS = '__all__'

const SORT_MODES = [
  { id: 'gainers', label: 'Gainers' },
  { id: 'losers', label: 'Losers' },
] as const
type SortMode = (typeof SORT_MODES)[number]['id']

const VIEWS = [
  { id: 'intraday_boost', label: 'Intraday Boost' },
  { id: 'breakout_beacon', label: 'Breakout Beacon' },
  { id: 'high_powered_stocks', label: 'High Powered' },
  { id: 'sectors', label: 'Sectors' },
] as const
type ViewId = (typeof VIEWS)[number]['id']
type ListView = Exclude<ViewId, 'sectors'>

function readView(): ViewId {
  const saved = localStorage.getItem(PREFS_KEY)
  return (VIEWS.find((v) => v.id === saved)?.id ?? 'intraday_boost') as ViewId
}

const ROW_GRID = 'grid grid-cols-[22px_1fr_52px_48px_44px] items-center gap-1'

function ChgCell({ value }: { value: number }) {
  return (
    <span
      className={cn(
        'text-right tabular-nums',
        value > 0 && 'text-emerald-600 dark:text-emerald-400',
        value < 0 && 'text-rose-600 dark:text-rose-400'
      )}
    >
      {value > 0 ? '+' : ''}
      {value.toFixed(2)}%
    </span>
  )
}

/** Score with a direction arrow -- the number alone doesn't say whether the
 * symbol is moving up or down, only how strongly TradeFinder is ranking it. */
function ScoreCell({ score, direction }: { score: number; direction: 'up' | 'down' | null }) {
  return (
    <span className="flex items-center justify-end gap-0.5 tabular-nums">
      {score.toFixed(1)}
      {direction === 'up' && (
        <TrendingUp className="h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
      )}
      {direction === 'down' && (
        <TrendingDown className="h-3 w-3 shrink-0 text-rose-600 dark:text-rose-400" />
      )}
    </span>
  )
}

/** One sector as a horizontal bar: length scaled to its magnitude relative to
 * the strongest sector on screen, so "how hot is this sector" reads at a
 * glance instead of requiring a mental ranking of raw numbers.
 *
 * The "ALL" row is a different kind of value -- a stock count, not an
 * rfactor -- so it gets its own colour and a plain number instead of ChgCell,
 * matching how TradeFinder's own UI sets it apart from the per-sector rows. */
function SectorBarRow({
  label,
  value,
  maxAbs,
  selected,
  disabled,
  onClick,
  isAll,
  countLabel,
}: {
  label: string
  value: number
  maxAbs: number
  selected: boolean
  disabled: boolean
  onClick(): void
  isAll?: boolean
  countLabel?: number
}) {
  const pct = maxAbs > 0 ? Math.min(100, (Math.abs(value) / maxAbs) * 100) : 0
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex w-full items-center gap-2 border-b border-border/40 px-2 py-1.5 text-left text-[12px] transition-colors hover:bg-accent disabled:opacity-50',
        selected && 'bg-accent font-medium ring-1 ring-inset ring-primary/60'
      )}
    >
      <span className={cn('w-[92px] shrink-0 truncate', isAll && 'font-semibold')}>{label}</span>
      <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
        <span
          className={cn(
            'block h-full rounded-full',
            isAll ? 'bg-amber-500' : value >= 0 ? 'bg-emerald-500' : 'bg-rose-500'
          )}
          style={{ width: `${pct}%` }}
        />
      </span>
      {isAll ? (
        <span className="text-right font-semibold tabular-nums text-amber-600 dark:text-amber-400">
          {countLabel ?? 0}
        </span>
      ) : (
        <ChgCell value={value} />
      )}
    </button>
  )
}

/** How long ago a Date was, in the coarse units a freshness caption needs --
 * seconds up to a minute, then minutes. Never "just now": a caption that
 * flips between two strings every second is harder to read at a glance than
 * one that only changes once a real interval has passed. */
function formatAgo(date: Date): string {
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.round(seconds / 60)}m ago`
}

/** Rank movement since the previous poll -- a small arrow only, no number,
 * since "moved up 3 places" matters far less than the direction. */
function RankDeltaIcon({ delta }: { delta: number | undefined }) {
  if (!delta) return null
  return delta > 0 ? (
    <ArrowUp className="h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
  ) : (
    <ArrowDown className="h-3 w-3 shrink-0 text-rose-600 dark:text-rose-400" />
  )
}

/** The server-side TF token's health -- unknown (grey) until the first poll
 * answers, then green/amber/red by how much runway is left. Refreshing
 * shows as amber regardless of the remaining time, since a refresh in
 * flight means the answer is about to change anyway. */
function JwtHealthBadge({ health }: { health: JwtHealthResponse | null }) {
  const label = !health
    ? 'TradeFinder token: checking…'
    : health.refreshing
      ? 'TradeFinder token: refreshing…'
      : !health.hasToken
        ? 'TradeFinder token: missing'
        : `TradeFinder token: ${Math.round((health.expiresInSeconds ?? 0) / 60)}m left`

  const color = !health
    ? 'bg-muted-foreground/40'
    : health.refreshing
      ? 'bg-amber-500'
      : !health.hasToken || (health.expiresInSeconds ?? 0) < 300
        ? 'bg-rose-500'
        : (health.expiresInSeconds ?? 0) < 1800
          ? 'bg-amber-500'
          : 'bg-emerald-500'

  return (
    <span
      className={cn('h-2 w-2 shrink-0 rounded-full', color)}
      title={label}
      aria-label={label}
      role="status"
    />
  )
}

/** A small dot beside the symbol, coloured by CPR bias -- only intraday_boost
 * ever carries this. Title text is the only place cpr_width_pct/
 * first_candle_range_pct show, since there is no room for two more columns. */
function CprDot({ item }: { item: TfListItem }) {
  if (item.cpr_bias == null && item.cpr_width_pct == null) return null
  const parts: string[] = []
  if (item.cpr_width_pct != null) parts.push(`CPR width ${item.cpr_width_pct.toFixed(2)}%`)
  if (item.first_candle_range_pct != null) {
    parts.push(`1st candle range ${item.first_candle_range_pct.toFixed(2)}%`)
  }
  return (
    <span
      className={cn(
        'inline-block h-1.5 w-1.5 shrink-0 rounded-full',
        item.cpr_bias === 'bullish' && 'bg-emerald-500',
        item.cpr_bias === 'bearish' && 'bg-rose-500',
        item.cpr_bias == null && 'bg-muted-foreground/40'
      )}
      title={parts.join(' · ') || undefined}
      aria-hidden="true"
    />
  )
}

/** One row: a label and a Switch, the same shape WatchlistPanel already uses
 * for its column toggles. Kept generic so both top-level features and the
 * nested column checkboxes render identically. */
function FeatureRow({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange(next: boolean): void
}) {
  return (
    <label className="flex items-center justify-between gap-3 py-1 text-[12px]">
      <span className="text-foreground">{label}</span>
      <Switch checked={checked} onCheckedChange={onChange} className="scale-90" />
    </label>
  )
}

/** All 20-feature-audit toggles, off by default, persisted by the caller.
 * Grouped to match the plan's categories so the popover reads as a menu,
 * not a flat wall of switches. */
function FeatureSettings({
  features,
  onChange,
}: {
  features: Features
  /** A React state updater, not a plain setter -- reading `features` off
   * this component's own props would race two switches flipped in the same
   * render tick (each computing "next" from the same stale snapshot, so
   * only the last one's change survives). The functional form always reads
   * the true latest state. */
  onChange(updater: (prev: Features) => Features): void
}) {
  const set = <K extends keyof Features>(key: K, value: Features[K]) =>
    onChange((prev) => ({ ...prev, [key]: value }))
  const setColumn = <K extends keyof Features['columns']>(key: K, value: boolean) =>
    onChange((prev) => ({ ...prev, columns: { ...prev.columns, [key]: value } }))

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Data
      </p>
      <FeatureRow
        label="Freshness caption"
        checked={features.freshness}
        onChange={(v) => set('freshness', v)}
      />
      <FeatureRow
        label="Pause auto-refresh"
        checked={features.pauseAutoRefresh}
        onChange={(v) => set('pauseAutoRefresh', v)}
      />
      <label className="flex items-center justify-between gap-3 py-1 text-[12px]">
        <span className="text-foreground">Poll interval</span>
        <Select
          value={String(features.pollIntervalMs)}
          onValueChange={(v) => set('pollIntervalMs', Number(v))}
        >
          <SelectTrigger className="h-6 w-20 text-[11px]" aria-label="Poll interval">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {POLL_INTERVALS.map((ms) => (
              <SelectItem key={ms} value={String(ms)} className="text-[11px]">
                {ms / 1000}s
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>
      <FeatureRow
        label="JWT health badge"
        checked={features.jwtHealth}
        onChange={(v) => set('jwtHealth', v)}
      />
      <FeatureRow
        label="Retry backoff on failure"
        checked={features.retryBackoff}
        onChange={(v) => set('retryBackoff', v)}
      />

      <p className="mt-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Filters
      </p>
      <label className="flex items-center justify-between gap-3 py-1 text-[12px]">
        <span className="text-foreground">Min score</span>
        <input
          type="number"
          min={0}
          step={0.5}
          value={features.scoreThreshold ?? ''}
          onChange={(e) =>
            set('scoreThreshold', e.target.value === '' ? null : Number(e.target.value))
          }
          placeholder="Off"
          className="h-6 w-16 rounded border bg-background px-1.5 text-right text-[11px]"
        />
      </label>
      <label className="flex items-center justify-between gap-3 py-1 text-[12px]">
        <span className="text-foreground">CPR bias</span>
        <Select value={features.cprFilter} onValueChange={(v) => set('cprFilter', v as CprFilter)}>
          <SelectTrigger className="h-6 w-28 text-[11px]" aria-label="CPR bias filter">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {CPR_FILTERS.map((f) => (
              <SelectItem key={f.id} value={f.id} className="text-[11px]">
                {f.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>

      <p className="mt-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Display
      </p>
      <FeatureRow
        label="Rank change arrows"
        checked={features.rankArrows}
        onChange={(v) => set('rankArrows', v)}
      />
      <FeatureRow
        label="Symbol search"
        checked={features.search}
        onChange={(v) => set('search', v)}
      />
      <FeatureRow
        label="Compact rows"
        checked={features.compactDensity}
        onChange={(v) => set('compactDensity', v)}
      />
      <FeatureRow
        label="Add-to-watchlist button"
        checked={features.addToWatchlist}
        onChange={(v) => set('addToWatchlist', v)}
      />
      <FeatureRow
        label="Pin/favorite symbols"
        checked={features.pinning}
        onChange={(v) => set('pinning', v)}
      />
      <FeatureRow
        label="Sector quick-filter chips"
        checked={features.sectorChips}
        onChange={(v) => set('sectorChips', v)}
      />

      <p className="mt-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Columns
      </p>
      <FeatureRow
        label="LTP"
        checked={features.columns.ltp}
        onChange={(v) => setColumn('ltp', v)}
      />
      <FeatureRow
        label="Change %"
        checked={features.columns.chg}
        onChange={(v) => setColumn('chg', v)}
      />
      <FeatureRow
        label="Score"
        checked={features.columns.score}
        onChange={(v) => setColumn('score', v)}
      />
      <FeatureRow
        label="CPR indicator"
        checked={features.columns.cpr}
        onChange={(v) => setColumn('cpr', v)}
      />
    </div>
  )
}

interface Props {
  apiKey: string
  onPick(row: SearchRow): void
  activeSymbol?: string | null
}

export function TradeFinderPanel({ apiKey, onPick, activeSymbol }: Props) {
  const [view, setView] = useState<ViewId>(readView)
  const [pulse, setPulse] = useState<MarketPulseData | null>(null)
  const [pulseError, setPulseError] = useState<string | null>(null)
  const [pulseLoading, setPulseLoading] = useState(false)

  const [sectorData, setSectorData] = useState<SectorScopeData | null>(null)
  const [sectorError, setSectorError] = useState<string | null>(null)
  const [sectorLoading, setSectorLoading] = useState(false)
  const [selectedSector, setSelectedSector] = useState<string | null>(null)
  /** Off (null) until the user picks one from the panel -- the stock list
   * keeps its default ranking by |rfactor| unless Gainers/Losers is chosen. */
  const [sortMode, setSortMode] = useState<SortMode | null>(null)

  /** Bumped by the refresh button to force an immediate reload. */
  const [attempt, setAttempt] = useState(0)
  const attemptRef = useRef(0)

  const [features, setFeatures] = useState<Features>(readFeatures)
  /** Read inside the poll interval's closure instead of as an effect
   * dependency -- restarting the interval on every toggle would also reset
   * its timing, so a pause flipped on right before a tick would still let
   * that tick through. A ref always reads the latest value without that. */
  const pauseRef = useRef(features.pauseAutoRefresh)
  useEffect(() => {
    pauseRef.current = features.pauseAutoRefresh
  }, [features.pauseAutoRefresh])
  /** Same reasoning as pauseRef, for retryBackoff -- read fresh inside the
   * poll effect's closure without making it an effect dependency. */
  const featuresRef = useRef(features)
  useEffect(() => {
    featuresRef.current = features
  }, [features])

  /** This poll's rank per symbol, so the next poll can tell whether each
   * symbol moved up or down the list -- compared, then overwritten, once
   * per successful fetch. Null until a symbol has been seen twice. */
  const prevRanksRef = useRef<Map<string, number>>(new Map())
  const [rankDeltas, setRankDeltas] = useState<Map<string, number>>(new Map())

  const [pulseUpdatedAt, setPulseUpdatedAt] = useState<Date | null>(null)
  const [sectorUpdatedAt, setSectorUpdatedAt] = useState<Date | null>(null)
  const [search, setSearch] = useState('')
  const [jwtHealth, setJwtHealth] = useState<JwtHealthResponse | null>(null)

  useEffect(() => {
    localStorage.setItem(PREFS_KEY, view)
  }, [view])

  useEffect(() => {
    localStorage.setItem(FEATURES_KEY, JSON.stringify(features))
  }, [features])

  /* ── market_pulse: the three ranked lists, polled regardless of which one
     is on screen so switching views never shows a stale list mid-load ── */
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is a deliberate re-run trigger, not a value this effect reads; the refresh button bumps it to refetch without changing the contract
  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setInterval> | null = null
    let backoffTimer: ReturnType<typeof setTimeout> | null = null
    let backoffStep = 0

    const load = async () => {
      if (!alive || document.hidden) return
      setPulseLoading(true)
      try {
        const res = await tradefinderApi.getMarketPulse(apiKey)
        if (!alive) return
        if (res.status === 'success' && res.data) {
          setPulse(res.data)
          setPulseError(null)
          setPulseUpdatedAt(new Date())
          backoffStep = 0

          // Rank delta: this poll's position minus the last poll's, per
          // (list, symbol). Keyed by list too, since the same symbol can
          // hold a different rank on each of the three lists at once.
          const nextRanks = new Map<string, number>()
          const deltas = new Map<string, number>()
          for (const listKey of [
            'intraday_boost',
            'breakout_beacon',
            'high_powered_stocks',
          ] as const) {
            res.data[listKey].forEach((item, i) => {
              const key = `${listKey}:${item.symbol}`
              const rank = i + 1
              nextRanks.set(key, rank)
              const prevRank = prevRanksRef.current.get(key)
              if (prevRank != null && prevRank !== rank) deltas.set(key, prevRank - rank)
            })
          }
          prevRanksRef.current = nextRanks
          setRankDeltas(deltas)
        } else {
          setPulseError(res.message ?? 'Failed to load TradeFinder data')
          scheduleBackoffRetry()
        }
      } catch {
        if (!alive) return
        setPulseError('Failed to load TradeFinder data')
        scheduleBackoffRetry()
      } finally {
        if (alive) setPulseLoading(false)
      }
    }

    /** Off by default -- on, a failed fetch retries at 2s/5s/15s instead of
     * waiting out the rest of the normal poll interval. Caps at 15s and
     * resets the moment a fetch succeeds, so a real outage doesn't spin. */
    const scheduleBackoffRetry = () => {
      if (!featuresRef.current.retryBackoff || !alive || pauseRef.current) return
      const delays = [2_000, 5_000, 15_000]
      const delay = delays[Math.min(backoffStep, delays.length - 1)]
      backoffStep += 1
      backoffTimer = setTimeout(load, delay)
    }

    load()
    timer = setInterval(
      () => {
        if (!pauseRef.current) load()
      },
      Math.max(5_000, features.pollIntervalMs)
    )
    const onVisible = () => {
      if (!document.hidden && !pauseRef.current) load()
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      alive = false
      if (timer) clearInterval(timer)
      if (backoffTimer) clearTimeout(backoffTimer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [apiKey, attempt, features.pollIntervalMs])

  /* ── sector_scope: only fetched while that view is actually selected ── */
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is a deliberate re-run trigger, not a value this effect reads; the refresh button bumps it to refetch without changing the contract
  useEffect(() => {
    if (view !== 'sectors') return
    let alive = true
    let timer: ReturnType<typeof setInterval> | null = null
    let backoffTimer: ReturnType<typeof setTimeout> | null = null
    let backoffStep = 0

    const load = async () => {
      if (!alive || document.hidden) return
      setSectorLoading(true)
      try {
        const res = await tradefinderApi.getSectorScope(apiKey)
        if (!alive) return
        if (res.status === 'success' && res.data) {
          setSectorData(res.data)
          setSectorError(null)
          setSectorUpdatedAt(new Date())
          backoffStep = 0
        } else {
          setSectorError(res.message ?? 'Failed to load sector data')
          scheduleBackoffRetry()
        }
      } catch {
        if (!alive) return
        setSectorError('Failed to load sector data')
        scheduleBackoffRetry()
      } finally {
        if (alive) setSectorLoading(false)
      }
    }

    const scheduleBackoffRetry = () => {
      if (!featuresRef.current.retryBackoff || !alive || pauseRef.current) return
      const delays = [2_000, 5_000, 15_000]
      const delay = delays[Math.min(backoffStep, delays.length - 1)]
      backoffStep += 1
      backoffTimer = setTimeout(load, delay)
    }

    load()
    timer = setInterval(
      () => {
        if (!pauseRef.current) load()
      },
      Math.max(5_000, features.pollIntervalMs)
    )
    return () => {
      alive = false
      if (timer) clearInterval(timer)
      if (backoffTimer) clearTimeout(backoffTimer)
    }
  }, [apiKey, view, attempt, features.pollIntervalMs])

  /* ── JWT health: off by default, a slow 60s poll -- this only checks the
     server-side token's expiry and kicks off a refresh if it's close, it
     never blocks on the actual (slow, browser-based) refresh itself. ── */
  useEffect(() => {
    if (!features.jwtHealth) return
    let alive = true
    const load = async () => {
      if (!alive || document.hidden) return
      try {
        const res = await tradefinderApi.getJwtHealth(apiKey)
        if (alive) setJwtHealth(res)
      } catch {
        // Silent: this is a secondary status indicator, not a fetch the
        // rest of the panel depends on -- an error here just means the
        // badge goes back to unknown until the next tick.
      }
    }
    load()
    const timer = setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [apiKey, features.jwtHealth])

  const refresh = () => {
    attemptRef.current += 1
    setAttempt(attemptRef.current)
  }

  const chartSymbol = (symbol: string) => {
    if (!symbol) return
    onPick({ symbol, exchange: 'NSE' })
  }

  /** Adds to whichever list WatchlistPanel currently has open -- reads the
   * same localStorage key it writes, rather than asking the user to pick a
   * list twice. Falls back to the first list if none is marked active. */
  const addToWatchlist = async (symbol: string) => {
    try {
      const savedId = Number(localStorage.getItem(ACTIVE_WATCHLIST_KEY))
      let listId = Number.isFinite(savedId) && savedId > 0 ? savedId : null
      if (listId == null) {
        const lists = await watchlistApi.list()
        listId = lists[0]?.id ?? null
      }
      if (listId == null) {
        showToast.error('No watchlist to add to -- create one first')
        return
      }
      await watchlistApi.addItem(listId, symbol, 'NSE')
      showToast.success(`Added ${symbol} to watchlist`)
    } catch {
      showToast.error(`Could not add ${symbol} to watchlist`)
    }
  }

  const loading = view === 'sectors' ? sectorLoading : pulseLoading

  const pinnedSet = new Set(features.pinnedSymbols)
  const togglePin = (symbol: string) => {
    setFeatures((prev) => ({
      ...prev,
      pinnedSymbols: prev.pinnedSymbols.includes(symbol)
        ? prev.pinnedSymbols.filter((s) => s !== symbol)
        : [...prev.pinnedSymbols, symbol],
    }))
  }

  const listRowsAll: TfListItem[] = view === 'sectors' ? [] : (pulse?.[view as ListView] ?? [])
  let listRows = listRowsAll
  if (features.search && search.trim()) {
    const q = search.trim().toLowerCase()
    listRows = listRows.filter((r) => r.symbol.toLowerCase().includes(q))
  }
  if (features.scoreThreshold != null) {
    listRows = listRows.filter((r) => r.score >= (features.scoreThreshold as number))
  }
  if (features.cprFilter !== 'all') {
    listRows = listRows.filter((r) => r.cpr_bias === features.cprFilter)
  }
  if (features.pinning && pinnedSet.size > 0) {
    // Stable partition, not a re-sort: everything keeps the rank order the
    // backend gave it, pinned rows just move as a block to the front.
    listRows = [
      ...listRows.filter((r) => pinnedSet.has(r.symbol)),
      ...listRows.filter((r) => !pinnedSet.has(r.symbol)),
    ]
  }
  const showQuoteColumns = view !== 'breakout_beacon'

  const sectorEntries = sectorData
    ? Object.entries(sectorData.sectors).map(([key, stocks]) => ({
        name: key.replace(/_r_factor$/, ''),
        key,
        stocks,
      }))
    : []
  const sectorIndex = sectorData?.index ?? []

  /** Every sector's stocks combined, deduped by symbol -- a stock can sit in
   * more than one sector group, and "ALL" means one row per stock, not one
   * per (sector, stock) pair. */
  const allStocksMap = new Map<string, SectorStockItem>()
  for (const entry of sectorEntries) {
    for (const stock of Object.values(entry.stocks)) {
      if (!allStocksMap.has(stock.symbol)) allStocksMap.set(stock.symbol, stock)
    }
  }

  const selectedStocksRaw: SectorStockItem[] =
    selectedSector === ALL_SECTORS
      ? Array.from(allStocksMap.values())
      : selectedSector
        ? Object.values(sectorEntries.find((s) => s.key === selectedSector)?.stocks ?? {})
        : []

  const selectedStocks = selectedStocksRaw.slice().sort((a, b) => {
    if (sortMode === 'gainers') return b.param_3 - a.param_3
    if (sortMode === 'losers') return a.param_3 - b.param_3
    return Math.abs(b.param_3) - Math.abs(a.param_3)
  })

  return (
    <PanelShell
      id="oa-panel-tradefinder"
      label="TradeFinder"
      storageKey="oa-trading-tradefinder-width"
      defaultWidth={340}
      minWidth={300}
    >
      <div className={PANEL_HEADER}>
        <Select
          value={view}
          onValueChange={(value) => {
            setView(value as ViewId)
            setSelectedSector(null)
          }}
        >
          <SelectTrigger className="h-8 min-w-0 flex-1 text-[12px]" aria-label="TradeFinder list">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {VIEWS.map((v) => (
              <SelectItem key={v.id} value={v.id} className="text-[12px]">
                {v.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {features.jwtHealth && <JwtHealthBadge health={jwtHealth} />}

        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 shrink-0"
          onClick={refresh}
          title="Refresh"
          aria-label="Refresh TradeFinder data"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
        </Button>

        <Popover>
          <PopoverTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0"
              title="TradeFinder settings"
              aria-label="TradeFinder settings"
            >
              <Settings2 className="h-3.5 w-3.5" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="max-h-[80vh] w-64 overflow-y-auto p-3">
            <FeatureSettings features={features} onChange={setFeatures} />
          </PopoverContent>
        </Popover>
      </div>

      {/* Off by default, like every feature here -- filters the active list
          by typed text once turned on in settings. */}
      {features.search && view !== 'sectors' && (
        <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
          <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter symbols…"
            className="h-7 text-[12px]"
          />
        </div>
      )}

      {view !== 'sectors' && (
        <>
          <div
            className={cn(
              ROW_GRID,
              'shrink-0 border-b px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70'
            )}
          >
            <span>#</span>
            <span>Symbol</span>
            <span className="text-right">
              {showQuoteColumns && features.columns.ltp ? 'LTP' : ''}
            </span>
            <span className="text-right">
              {showQuoteColumns && features.columns.chg ? 'Chg' : ''}
            </span>
            <span className="text-right">{features.columns.score ? 'Score' : ''}</span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {pulseError && listRows.length === 0 ? (
              <div className="flex flex-col items-center gap-2 p-6 text-center">
                <p className="text-[12px] text-muted-foreground">{pulseError}</p>
                <Button variant="outline" size="sm" className="h-7 gap-1.5" onClick={refresh}>
                  <RefreshCw className="h-3.5 w-3.5" />
                  Retry
                </Button>
              </div>
            ) : pulseLoading && listRows.length === 0 ? (
              <p className="p-3 text-[12px] text-muted-foreground">Loading TradeFinder list…</p>
            ) : listRows.length === 0 ? (
              <p className="p-3 text-[12px] text-muted-foreground">
                {search.trim() ? 'No symbols match.' : 'No symbols in this list right now.'}
              </p>
            ) : (
              listRows.map((item, i) => (
                <button
                  key={item.symbol}
                  type="button"
                  onClick={() => chartSymbol(item.symbol)}
                  className={cn(
                    ROW_GRID,
                    'w-full border-b border-border/40 px-2 text-left text-[12px] transition-colors hover:bg-accent',
                    features.compactDensity ? 'py-0.5' : 'py-1',
                    activeSymbol === `NSE:${item.symbol}` &&
                      'font-medium ring-1 ring-inset ring-primary/60'
                  )}
                  title={`Chart ${item.symbol}`}
                >
                  <span className="text-muted-foreground tabular-nums">{i + 1}</span>
                  <span className="flex min-w-0 items-center justify-between gap-1">
                    <span className="flex min-w-0 items-center gap-1 truncate">
                      {features.columns.cpr && <CprDot item={item} />}
                      <span className="truncate">{item.symbol}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-1">
                      {features.rankArrows && (
                        <RankDeltaIcon delta={rankDeltas.get(`${view}:${item.symbol}`)} />
                      )}
                      {features.pinning && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            togglePin(item.symbol)
                          }}
                          className={cn(
                            'rounded p-0.5 hover:bg-primary/10',
                            pinnedSet.has(item.symbol)
                              ? 'text-primary'
                              : 'text-muted-foreground hover:text-primary'
                          )}
                          title={
                            pinnedSet.has(item.symbol)
                              ? `Unpin ${item.symbol}`
                              : `Pin ${item.symbol}`
                          }
                          aria-label={
                            pinnedSet.has(item.symbol)
                              ? `Unpin ${item.symbol}`
                              : `Pin ${item.symbol}`
                          }
                        >
                          {pinnedSet.has(item.symbol) ? (
                            <PinOff className="h-3 w-3" />
                          ) : (
                            <Pin className="h-3 w-3" />
                          )}
                        </button>
                      )}
                      {features.addToWatchlist && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            addToWatchlist(item.symbol)
                          }}
                          className="rounded p-0.5 text-muted-foreground hover:bg-primary/10 hover:text-primary"
                          title={`Add ${item.symbol} to watchlist`}
                          aria-label={`Add ${item.symbol} to watchlist`}
                        >
                          <Plus className="h-3 w-3" />
                        </button>
                      )}
                    </span>
                  </span>
                  <span className="text-right tabular-nums">
                    {showQuoteColumns && features.columns.ltp && item.ltp
                      ? item.ltp.toFixed(2)
                      : ''}
                  </span>
                  {showQuoteColumns && features.columns.chg ? (
                    <ChgCell value={item.change_pct} />
                  ) : (
                    <span />
                  )}
                  {features.columns.score ? (
                    <ScoreCell
                      score={item.score}
                      direction={showQuoteColumns ? (item.change_pct >= 0 ? 'up' : 'down') : null}
                    />
                  ) : (
                    <span />
                  )}
                </button>
              ))
            )}
          </div>

          {/* Off by default -- when on, a silent caption unless the feed has
              actually gone stale, matching OptionChainPanel's pattern. */}
          {features.freshness && pulseUpdatedAt && (
            <p
              className={cn(
                'shrink-0 border-t px-2 py-1 text-[10px]',
                pulseError ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
              )}
            >
              {pulseError ? 'Not updating. ' : 'Updated '}
              {formatAgo(pulseUpdatedAt)}
            </p>
          )}
        </>
      )}

      {/* Sectors: both halves stay on screen at once -- picking a sector
          filled the whole panel with its stocks and hid every other sector's
          progress, so comparing two sectors meant bouncing back and forth.
          A fixed 50/50 split keeps the full index visible while drilling in. */}
      {view === 'sectors' && (
        <div className="flex min-h-0 flex-1 flex-col">
          {/* Off by default -- a faster way to jump straight to one of the
              strongest sectors without scrolling the full bar list first. */}
          {features.sectorChips && sectorIndex.length > 0 && (
            <div className="flex shrink-0 flex-wrap gap-1 border-b px-2 py-1.5">
              {sectorIndex
                .slice()
                .sort((a, b) => Math.abs(b.param_3) - Math.abs(a.param_3))
                .slice(0, 6)
                .map((sector) => {
                  const key = `${sector.Symbol}_r_factor`
                  return (
                    <button
                      key={sector.Symbol}
                      type="button"
                      onClick={() => setSelectedSector(key)}
                      disabled={!sectorEntries.some((s) => s.key === key)}
                      className={cn(
                        'rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors disabled:opacity-50',
                        selectedSector === key
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'border-border bg-background text-muted-foreground hover:bg-accent'
                      )}
                    >
                      {sector.Symbol.replace(/^NIFTY\s+/, '')}
                    </button>
                  )
                })}
            </div>
          )}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {sectorError && sectorIndex.length === 0 ? (
              <div className="flex flex-col items-center gap-2 p-6 text-center">
                <p className="text-[12px] text-muted-foreground">{sectorError}</p>
                <Button variant="outline" size="sm" className="h-7 gap-1.5" onClick={refresh}>
                  <RefreshCw className="h-3.5 w-3.5" />
                  Retry
                </Button>
              </div>
            ) : sectorLoading && sectorIndex.length === 0 ? (
              <p className="p-3 text-[12px] text-muted-foreground">Loading sector data…</p>
            ) : sectorIndex.length === 0 ? (
              <p className="p-3 text-[12px] text-muted-foreground">No sector data right now.</p>
            ) : (
              (() => {
                const sorted = sectorIndex
                  .slice()
                  .sort((a, b) => Math.abs(b.param_3) - Math.abs(a.param_3))
                const maxAbs = Math.max(1e-6, ...sorted.map((s) => Math.abs(s.param_3)))
                return (
                  <>
                    {/* Every stock across every sector, ranked by highest
                        rfactor -- the one row that isn't a sector at all. */}
                    <SectorBarRow
                      label="ALL"
                      value={maxAbs}
                      maxAbs={maxAbs}
                      selected={selectedSector === ALL_SECTORS}
                      disabled={allStocksMap.size === 0}
                      onClick={() => setSelectedSector(ALL_SECTORS)}
                      isAll
                      countLabel={allStocksMap.size}
                    />
                    {sorted.map((sector) => {
                      const key = `${sector.Symbol}_r_factor`
                      return (
                        <SectorBarRow
                          key={sector.Symbol}
                          label={sector.Symbol.replace(/^NIFTY\s+/, '')}
                          value={sector.param_3}
                          maxAbs={maxAbs}
                          selected={selectedSector === key}
                          disabled={!sectorEntries.some((s) => s.key === key)}
                          onClick={() => setSelectedSector(key)}
                        />
                      )
                    })}
                  </>
                )
              })()
            )}
          </div>

          {/* Bottom half: the selected sector's stocks. A fixed height
              (not flex-1) so it holds its 50% share even while empty --
              otherwise the top half's list would jump to fill the panel
              every time the selection cleared. */}
          <div className="flex min-h-0 flex-1 flex-col border-t">
            <div className="flex shrink-0 items-center justify-between gap-2 border-b bg-muted/30 px-2 py-1">
              <span className="truncate text-[11px] font-medium">
                {selectedSector === ALL_SECTORS
                  ? 'ALL'
                  : selectedSector
                    ? selectedSector.replace(/_r_factor$/, '')
                    : 'Select a sector'}
              </span>
              {/* Off by default -- the list keeps its |rfactor| ranking until
                  the user explicitly asks to see gainers or losers first.
                  Clicking the active mode again turns sorting back off. */}
              {selectedSector && (
                <div className="flex shrink-0 gap-1">
                  {SORT_MODES.map((mode) => (
                    <button
                      key={mode.id}
                      type="button"
                      onClick={() =>
                        setSortMode((current) => (current === mode.id ? null : mode.id))
                      }
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors',
                        sortMode === mode.id
                          ? 'bg-primary text-primary-foreground'
                          : 'bg-background text-muted-foreground hover:bg-accent'
                      )}
                    >
                      {mode.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {!selectedSector ? (
                <p className="p-3 text-[12px] text-muted-foreground">
                  Pick a sector above to see its stocks here.
                </p>
              ) : selectedStocks.length === 0 ? (
                <p className="p-3 text-[12px] text-muted-foreground">No stocks in this sector.</p>
              ) : (
                selectedStocks.map((stock) => (
                  <button
                    key={stock.symbol}
                    type="button"
                    onClick={() => chartSymbol(stock.symbol)}
                    className={cn(
                      'grid w-full grid-cols-[1fr_52px_48px_44px] items-center gap-1 border-b border-border/40 px-2 text-left text-[12px] transition-colors hover:bg-accent',
                      features.compactDensity ? 'py-0.5' : 'py-1',
                      activeSymbol === `NSE:${stock.symbol}` &&
                        'font-medium ring-1 ring-inset ring-primary/60'
                    )}
                    title={`Chart ${stock.symbol}`}
                  >
                    <span className="flex min-w-0 items-center justify-between gap-1">
                      <span className="truncate">{stock.symbol}</span>
                      {features.addToWatchlist && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            addToWatchlist(stock.symbol)
                          }}
                          className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-primary/10 hover:text-primary"
                          title={`Add ${stock.symbol} to watchlist`}
                          aria-label={`Add ${stock.symbol} to watchlist`}
                        >
                          <Plus className="h-3 w-3" />
                        </button>
                      )}
                    </span>
                    <span className="text-right tabular-nums">{stock.param_0.toFixed(2)}</span>
                    <ChgCell value={stock.param_2} />
                    <span className="text-right tabular-nums">{stock.param_3.toFixed(2)}</span>
                  </button>
                ))
              )}
            </div>
            {features.freshness && sectorUpdatedAt && (
              <p
                className={cn(
                  'shrink-0 border-t px-2 py-1 text-[10px]',
                  sectorError ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
                )}
              >
                {sectorError ? 'Not updating. ' : 'Updated '}
                {formatAgo(sectorUpdatedAt)}
              </p>
            )}
          </div>
        </div>
      )}
    </PanelShell>
  )
}
