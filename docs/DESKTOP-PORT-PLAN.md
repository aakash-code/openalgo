# Porting the fork's features into OpenAlgo Desktop

Companion to `docs/FORK-VS-UPSTREAM.md`. That doc catalogs what the fork added;
this one is the plan to put it into **OpenAlgo Desktop** (`marketcalls/openalgo-desktop`)
and keep pulling upstream Desktop updates.

## The architecture reality (this reframes everything)

OpenAlgo Desktop is **not the Flask app packaged** — it is a ground-up rewrite:

- **Shell:** Tauri 2. **Backend:** 100% **Rust** (`src-tauri/src/`: `server/` axum
  routes, `services/`, `brokers/` (356 files), `sandbox/`, `historify/`, `analytics/`,
  `messaging/`, `strategy/`, `mcp/`; SQLite + DuckDB). **No Python.**
- **Frontend:** React 19 + Vite + Tailwind + shadcn/ui + TanStack Query + Zustand —
  **the same stack as the fork**, the same runtime-loaded chart-indicator JS system
  (`.claude/skills/chart-indicator`, openalgo-charts), and the same registration points
  (`src/lib/tools.ts`, `src/config/navigation.ts`). Many pages already exist
  (Trading, Historify, OIProfile, Scalping).
- **Contract:** Desktop holds `/api/v1` + WebSocket compatibility via contract tests.
  Anything added must expose the same endpoint + SocketIO-event shape the React expects.

**Consequence — two very different port paths:**

1. **Frontend (React):** ports with light adaptation. Same stack, same conventions.
   Custom-indicator `.js` files are drop-in (runtime-loaded, never bundled).
2. **Backend (Python services/blueprints/brokers):** **cannot be copied or merged.**
   Each must be **re-implemented in Rust** as a module under `src-tauri/src/services/`
   (or `historify/`, `analytics/`) + an axum route in `server/` + a `socketioxide` event.
   This is the bulk of the work and it crosses a language boundary.

## What Desktop already has (do NOT rebuild)

OI Profile (base page + backend), options analytics (chain / straddle / strike_profile /
surface / greeks / black76), Historify (scheduler / jobs / export / import / source),
and the full brokers / orders / order-router / sandbox / analyzer / pnl / arbitrage layer.

## Feature-by-feature port map

| Feature | In Desktop? | Frontend (React) | Backend to build (Rust) | Effort |
|---|---|---|---|---|
| Custom indicator library (497 + oi_profile_live.js, writer_flow_live.js) | loader present | drop `.js` into the indicators dir; licence files travel | none (runtime-loaded) | **Low** |
| Per-indicator timeframe (MTF) | No | port `lib/trading/indicatorTimeframe.ts` (pure React) + IndicatorSettingsDialog | none | **Low** |
| `expiries` indicator input + ExpiryPicker | No | port input type + picker | needs an expiries endpoint (Desktop may already serve one) | **Low** |
| NSE Corporate Events | No | (API only) | NEW `services/nse_events` + `/api/v1/nseevents` | **Low** |
| Indicator Screener | No | port Screener page + FilterBuilder + scanWorker (runs in browser) | NEW bulk-OHLCV endpoint in Rust | **Med** |
| Writer Flow | No | port WriterFlow page + indicator JS | NEW: per-minute OI recorder (tokio task) + DuckDB tables + `/writerflow/api/*` + `writer_flow_update` event | **High** |
| Option Levels | No | (served data + indicator) | NEW `oi_levels` from the recorded OI (after Writer Flow) | **Med** |
| Market Read | No | port MarketReadPanel + useMarketReadAlerts | NEW market-read core + Telegram alerts (`messaging/`) + `/writerflow/api/read*` (needs the recorder) | **Med** |
| OI Profile: live OI + NSE bhavcopy anchor + multi-expiry | base exists | extend existing `OIProfile.tsx` (merge) | extend Desktop's OI-profile Rust: live OI off the WS feed + bhavcopy fetch + multi-expiry sum | **Med** |
| Expired F&O download | No | port ExpiredFnoTab into Desktop Historify | NEW expired-contract download (Upstox + Dhan) into Historify DuckDB + progress event | **Med-High** |
| TF Boost | No | port BoostStrikesPanel + boostBadge | NEW snapshot recorder + rank-movement engine + `boost_snapshot` event | **High** |
| TradeFinder panel + pulse | No | port TradeFinderPanel + RightRail | NEW tradefinder.in session/JWT + pulse/sector endpoints — **JWT refresh must be redesigned** (no Xvfb in Tauri) | **High + risk** |
| Signal distribution API | No (`strategy/signals.rs` is a different thing) | — | NEW ingest → Telegram → subscriber-webhook + SQLite tables | **Med** |
| /trading terminal UX (side panels, watchlist sort + M-Score, toolbar toggles, server-event bus, live OI) | Trading page exists | merge into Trading.tsx / ChartPane / WatchlistPanel / RightRail; port `serverEvents.ts` | consumes the WS `oi` + the endpoints above | **Med** (depends on the backends) |
| Sandbox SPAN margin | Desktop has its own sandbox | — | Desktop's sandbox differs; defer / revisit separately | n/a |

## Staying updatable (the fork discipline — same as the backend fork)

- Fork `marketcalls/openalgo-desktop`; add `upstream` remote; **merge upstream in
  underneath, never reset/force**; make a backup branch + tag before each merge and
  verify your files survive (see memory `fork-sync-workflow`).
- **Keep every feature additive.** New Rust feature = new module file(s) under
  `services/` + a one-line `mod` + router registration; new React feature = new files +
  one-line entries in `src/lib/tools.ts` and `src/config/navigation.ts`. Confine edits to
  shared files (`Trading.tsx`, `OIProfile.tsx`, the axum router, `services/mod.rs`) to
  tight, obvious insertion blocks so they re-apply fast after an upstream bump.
- Custom indicators stay runtime-loaded JS — upgrade-proof by design.

## Phased roadmap (cheap, visible wins first; dependency-ordered)

- **Phase 0 — stand up the fork.** Clone your Desktop fork, add `upstream`, install the
  toolchain (Rust 1.99.1 per `rust-toolchain.toml` + Node), run `npm run tauri:dev`.
  Port the custom-indicator JS library (zero backend) for an immediate visible win.
- **Phase 1 — frontend-only wins.** Per-indicator MTF, `expiries` input + ExpiryPicker.
  (No Rust.)
- **Phase 2 — learn the Rust service pattern on small ones.** NSE events (smallest), then
  Screener (bulk-OHLCV in Rust; the scan itself runs in the browser worker you already have).
- **Phase 3 — the OI stack.** Writer Flow recorder (Rust) → Option Levels → Market Read.
  This is the biggest value block and the hardest new Rust (a persistent recorder task).
- **Phase 4 — OI Profile live extension** (extend Desktop's existing OI Profile).
- **Phase 5 — Expired F&O download** (into Desktop's Historify).
- **Phase 6 — TradeFinder + TF Boost + Signals.** Hardest: external sessions, recorders,
  and the TradeFinder JWT-refresh redesign. Signals are independent of TradeFinder.

## Decisions needed before execution

1. **Scope of Rust work.** Full backend reimplementation in Rust, or frontend-first
   (port only what runs against Desktop's existing API) and defer the Rust services?
   Do you have / want Rust capability, or should this lean on the chart-indicator JS +
   contract-compatible endpoints as far as possible?
2. **Pilot feature.** Recommend starting with the custom-indicator library + per-indicator
   MTF (Phase 0-1, no Rust) to validate the fork/build/update loop, then NSE events as the
   first Rust service.
3. **Repo.** Your Desktop fork isn't on this machine and I don't have its URL. I need it
   (or I can fork `marketcalls/openalgo-desktop` for you) to start Phase 0.

## Known hard spots

- **TradeFinder JWT refresh** relies on a headless browser + Xvfb on the server. A Tauri
  desktop has no Xvfb; this mechanism must be redesigned (e.g. a Tauri webview login, or
  defer TradeFinder). Flag early.
- **Ported indicator library** is gitignored and copyleft (MIT/GPL-3.0/MPL-2.0) — licence
  files must travel; do not relabel MIT. ISI proprietary strategies must never ship.
- **Rust is a real cost.** The 356-file broker layer and 107 services show Desktop is a
  serious codebase; matching its quality bar for new services is non-trivial.
