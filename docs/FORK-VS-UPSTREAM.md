# OpenAlgo Fork — Features Not in Upstream

What this fork (`aakash-code/openalgo`) has that upstream (`marketcalls/openalgo`)
does not, so the work can be re-implemented in **OpenAlgo Desktop** and kept
updatable when upstream Desktop ships new versions.

## How this list was built

- Compared against upstream at merge-base `91ced6f7` using the net fork diff
  `git diff upstream/main...main`.
- Scope at time of writing: **326 changed source files** (excluding the generated
  `frontend/dist/` bundle and lockfiles) across **164 fork commits**.
- Regenerate this list after any upstream merge:
  `git fetch upstream && git diff --name-only upstream/main...main -- . ':!frontend/dist'`

## The two kinds of change (this is the whole porting problem)

1. **NEW files (additive)** — files that exist only in the fork. These are
   drop-in: copy the file into Desktop, wire the route/nav, done. An upstream
   Desktop update never touches them, so they never conflict.
2. **MODIFIES upstream files (merge-sensitive)** — files that upstream also ships
   and the fork edited (`App.tsx`, `tools.ts`, order/quote services, broker
   adapters, `app.py`, config). Every upstream Desktop update can change these,
   so each one is a potential merge conflict and must be re-applied/merged. These
   are the only hard part of staying updatable.

Each feature below is tagged NEW / MODIFIES / MIXED on that axis.

## Recommended way to keep Desktop updatable

- **Keep fork code additive wherever possible.** A new page, service, blueprint,
  REST resource, or custom-indicator JS file is a new file -> zero merge cost.
- **Shrink the merge-sensitive surface.** Where the fork edits a shared upstream
  file only to *register* something (a route in `App.tsx`, a tool in `tools.ts`,
  a blueprint in `app.py`), keep that edit to a single, obvious insertion block so
  it re-applies in seconds after an upstream bump.
- **Track upstream as a remote in the Desktop fork too**, and merge upstream in
  underneath (never reset/force), exactly like this backend fork does. Make a
  backup branch+tag before each merge and confirm the fork files survive.
- **This document is the manifest.** When porting, work top-to-bottom; the
  "Desktop port notes" on each feature say drop-in vs merge points and
  dependencies between features.

## Feature index (at a glance)

| # | Feature | Area | Status vs upstream | One line |
|---|---------|------|--------------------|----------|
| 1 | Writer Flow | OI analytics | NEW | Per-minute OI order-flow recorder + strike-by-strike writer analysis page + chart overlay |
| 2 | Option Levels | OI analytics | NEW | Support/resistance, OI walls, gamma flip with measured hold-rates from the recorded OI |
| 3 | OI Profile live-OI | OI analytics | MODIFIES | Adds live WebSocket OI, NSE-bhavcopy prev-session anchor, multi-expiry sums to upstream OI Profile |
| 4 | Market Read | Market view | NEW | Live market-state panel + Telegram alerts, driven by the OI-Flow recorder |
| 5 | Indicator Screener | Scanning | NEW | Bulk-OHLCV server + in-browser Web Worker scan over openalgo-charts indicators (`/screener`) |
| 6 | NSE Corporate Events | Data API | NEW | Day-cached NSE corporate-events REST endpoint (`/api/v1/nseevents`) |
| 7 | Expired F&O download | Historify | NEW | Historical download of expired F&O contracts (Upstox + Dhan) into DuckDB |
| 8 | TradeFinder panel + pulse | TradeFinder | NEW | tradefinder.in session/JWT keep-alive + market-pulse / sector-scope panels & enrichments |
| 9 | TF Boost | TradeFinder | NEW | Boost snapshot recorder + rank-movement engine + chart "boost" badges |
| 10 | Signal distribution API | Signals | NEW | Ingest signals → Telegram → subscriber webhooks (on main SQLAlchemy DB) |
| 11 | /trading terminal UX | Charting | MODIFIES | Per-indicator MTF, `expiries` input + ExpiryPicker, right-rail side panels, watchlist sort + M-Score, toolbar toggles, server-event bus, live OI |
| 12 | Custom indicator library | Charting | NEW | 497-indicator ported library (runtime-loaded, gitignored) + licensing scaffolding |
| 13 | Sandbox SPAN margin | Analyzer | MODIFIES | SEBI SPAN / broker-API margin in the sandbox engine + margin-source audit trail |
| 14 | Streaming/infra hardening | Backend | MIXED | Out-of-process WS proxy + MDS bridge, stale-feed self-healing, live `oi` on adapters, FD/GC/shutdown hardening |

## The merge-sensitive set (this is the whole maintenance cost)

These files exist in upstream and the fork edited them, so **every upstream Desktop
update can conflict** with each. Re-apply the fork changes on top (for the near-rewrite
ones, re-port the behavior rather than git-merging a reflowed file).

**Frontend (shared files):** `App.tsx` (+routes `/writerflow`,`/screener`,`/oiprofile`),
`lib/tools.ts` (registry; home count derives from it), `lib/trading/terminal.ts`,
`components/trading/ChartPane.tsx`, `IndicatorSettingsDialog.tsx`, `WatchlistPanel.tsx`,
`RightRail.tsx`, `pages/Trading.tsx`, `lib/MarketDataManager.ts`,
`lib/trading/customIndicators.ts`, `hooks/useSocket.ts`, `pages/OIProfile.tsx`,
`Login.tsx`, `Footer.tsx`.

**Backend (shared files):** `app.py` (blueprint + scheduler wiring, ProxyFix gating),
`restx_api/__init__.py` + `restx_api/data_schemas.py` (conflict whenever upstream adds a
namespace/schema), `services/oi_profile_service.py`, `blueprints/oiprofile.py`,
`blueprints/historify.py`, `services/historify_service.py`, `database/historify_db.py`,
`websocket_proxy/server.py` (HIGHEST risk, near-rewrite), `websocket_proxy/app_integration.py`,
`base_adapter.py`, `connection_manager.py`, `sandbox/order_manager.py`, `sandbox/fund_manager.py`,
`sandbox/execution_engine.py`, `database/sandbox_db.py`, `services/history_service.py`,
`utils/httpx_client.py`, `utils/shutdown.py`, `database/auth_db.py`,
`database/token_db_enhanced.py`, `upgrade/migrate_all.py`, every `broker/*/plugin.json`
(adds `capabilities`), the live-`oi` streaming adapters, `broker/upstox/{api/data.py,
api/order_api.py,streaming/*,database/master_contract_db.py}`, `broker/shoonya` adapter.

**Config:** `.sample.env` (new keys), `pyproject.toml`, `requirements*.txt`,
`.github/workflows/ci.yml`, `.gitignore`.

## Additive drop-ins (zero merge cost)

Everything else is fork-only files — copy them in, they never conflict. The large blocks:
all `services/tf_*`, `tradefinder_*`, `restx_api/{tf_*,boost_*,signals,nse_events}`,
`database/{tf_boost_db,signal_db,oi_flow_db}.py`, `services/market_read/*`,
`market_read_service.py`, `screener_service.py`, `writer_flow_service.py`,
`oi_flow_*.py`, `services/oi_levels/*`, `nse_oi_bhavcopy.py`, `expired_fno_service.py`,
`broker/{upstox,dhan}/api/expired_data.py`, `services/market_data_bridge.py`,
`websocket_proxy/ws_liveness.py`, `utils/{gc_freeze,exit_after_cleanup,plugin_loader}.py`,
`upgrade/migrate_sandbox_margin_audit.py`, frontend pages (`WriterFlow`, `Screener`,
`OIProfile` body, `Historify/ExpiredFnoTab`), components (`MarketReadPanel`,
`TradeFinderPanel`, `BoostStrikesPanel`, `screener/FilterBuilder`),
`lib/trading/{indicatorTimeframe,tfSymbol}.ts`, `lib/serverEvents.ts`, `api/*` clients,
`strategies/indicators/*.js` + licensing, and the new blueprints
(`writer_flow`, `tradefinder_proxy`, `signals`).

## Suggested porting order (dependency-aware)

Port in this order — later phases lean on earlier ones, and the UI panels self-hide if
their backend isn't present yet.

0. **Shared plumbing first** — the registration edits (`App.tsx`, `tools.ts`,
   `restx_api/__init__.py`, `data_schemas.py`, `app.py` wiring), `plugin_loader.py` +
   plugin.json `capabilities`, live `oi` on adapters + `ws_liveness.py`, and the
   `serverEvents` + `useSocket` + `MarketDataManager` event bus.
1. **OI stack** — `oi_flow_db` + recorder + `writer_flow_service` + `oi_levels` +
   `market_read` (core+service) → Writer Flow page, Option Levels, Market Read panel.
   (Option Levels and the `/read` routes import `market_read/core` — port it with them.)
2. **OI Profile** live-OI extension (merge into upstream OI Profile).
3. **Screener + NSE events.**
4. **Expired F&O** (historify + broker `expired_data`).
5. **TradeFinder + Boost + Signals** (needs a tradefinder.in session/JWT with a headless
   browser, and Telegram bot config; Signals are independent of TradeFinder).
6. **/trading terminal UX** (MTF, panels, watchlist, toggles) — the merge-sensitive frontend.
7. **Sandbox SPAN margin + infra hardening** — only if the Desktop deployment model wants
   them; validate against how Desktop packages the server/proxy.

## Must-carry caveats

- **Ported indicator library (497 files) is gitignored and NOT committed.** It is
  MIT/GPL-3.0/MPL-2.0; the copyleft licence files must travel with any Desktop port and
  must not be relabelled MIT. Only `oi_profile_live.js` and `writer_flow_live.js` ship in-repo.
- **ISI proprietary strategies (`strategies/isi_v56_*`) are gitignored** and must never
  reach the public fork or Desktop build.
- **`broker/wisdom/plugin.json` bug:** uses key `historify_capabilities` instead of
  `capabilities`, so the loader silently reads none/none/none. Fix on port.
- **Deployment model differs:** several infra items assume a self-hosted server with an
  out-of-process proxy and a SEBI static IP for live orders. Confirm how Desktop packages
  the proxy and whether live orders route through a static IP before relying on them.
- **Discount the noise:** many backend `services/*.py` diffs are only `typing`
  import cleanup / ruff reflow with no behavior change — take those from upstream freely.

---

# Feature catalog (by area)

## TradeFinder, Boost & Signals

Every file in scope is **brand-new in the fork** (none exists in `upstream/main`),
so the Python/TS files themselves are drop-in. What is merge-sensitive is the
*wiring* into upstream files that are NOT in this scope list:
`app.py` (blueprint registration + scheduler start-up), `restx_api/__init__.py`
(namespace registration), `restx_api/data_schemas.py` (the `BoostSnapshotsSchema`,
`BoostMovementSchema`, `TfMarketPulseSchema`, `TfSectorScopeSchema`,
`TfJwtKeepAliveSchema` classes these endpoints import), and the frontend
`lib/trading/tfSymbol.ts` (imported by the panels, mirror of `services/tf_symbol_alias.py`).
Those exact merge points are named per-feature below.

Shared external dependency for every TradeFinder feature: a logged-in
**tradefinder.in session** (Google SSO) persisted in `strategies/.tf_browser_profile/`,
producing the JWT in `strategies/tf_jwt.txt`. This is an external third-party
login, not a broker login, and the headless-refresh browser automation needs a
real display or Xvfb. The signal features do NOT need TradeFinder.

---

### TradeFinder session & JWT keep-alive (auth core)
- **What it is (trader-facing):** Keeps the server logged in to tradefinder.in automatically so every other TradeFinder feature works without the user re-pasting a token. A background job refreshes the JWT every ~20 min; when the TF session has truly logged out it opens a visible browser (or on a headless server, accepts a hand-pasted token) to re-establish it.
- **Status vs upstream:** NEW (all-new files, additive).
- **Backend:**
  - `services/tradefinder_service.py` — read-only HTTP client for the three ranked lists + sector scope; TOTP `accesstoken` from `TF_SECRET`, `jwttoken` read fresh from file each call. `fetch_market_pulse`, `fetch_sector_scope`, `has_tf_jwt`.
  - `services/tradefinder_client.py` — thin client for the internal Option Apex money-flow endpoint (`jwttoken`+`accesstoken`), raises `TradefinderTokenError`/`TradefinderExpiryError`.
  - `strategies/tf_auth.py` — JWT refresh via persistent-profile headless Chromium (Playwright); `refresh_tf_jwt`, `ensure_fresh_jwt`, `jwt_expiry_seconds`. Auto-installs missing Playwright chromium.
  - `strategies/tf_login_setup.py` — one-time interactive Google login that creates the persistent profile.
  - `strategies/tf_relogin_oracle.py` — headless-server re-login: paste a `tradefinder_token` from your laptop, it sanitises/validates and writes `tf_jwt.txt`, verifies against sector_scope.
  - `services/tf_jwt_keepalive_service.py` — isolated APScheduler; cheap expiry gate → slow browser refresh on a real thread; headed-login fallback (Xvfb-aware) when headless exhausts.
  - `services/tf_settings.py` — the background on/off switch (stored in tf_boost DuckDB `read_tf_setting`/`write_tf_setting`, falls back to `TF_BOOST_SNAPSHOT_ENABLED`); gates snapshot + enrichment work. Read with a `real_threading.Lock`.
  - `services/tf_symbol_alias.py` — TradeFinder-name ↔ broker-master-name mapping (e.g. TATAMOTORS↔TMPV) so corporate-action renames don't silently drop a stock. Mirror of frontend `lib/trading/tfSymbol.ts`.
  - **REST:** `restx_api/tf_jwt_keepalive.py` → `/api/v1/tfjwtkeepalive` (POST, apikey; cheap expiry check, kicks background refresh).
  - **Blueprint:** `blueprints/tradefinder_proxy.py` → `/trading/tradefinder/moneyflow` (GET) and `/trading/tradefinder/settings` (GET/POST, the background switch). Session-auth, not apikey.
  - **Scheduled jobs:** `init_tf_jwt_keepalive_scheduler()` started in `app.py`.
  - **Env:** `TF_BASE`, `TF_SECRET`, `TF_JWT_TOKEN`, `TF_JWT_FILE`, `TF_PROFILE_DIR`, `TF_EMAIL`, `TF_HEADED_LOGIN`, `TF_LOGIN_WAIT_S`, `TF_BOOST_SNAPSHOT_ENABLED`.
- **Frontend:** JWT-health surfaced in the TradeFinder panel (see panel feature). No own page.
- **Desktop port notes:** Files additive. Merge points: `app.py` (import `tradefinder_proxy_bp`, `register_blueprint(tradefinder_proxy_bp)`, start `init_tf_jwt_keepalive_scheduler`), `restx_api/__init__.py` (add `tf_jwt_keepalive_ns`), `restx_api/data_schemas.py` (`TfJwtKeepAliveSchema`). **External login dependency:** needs the tradefinder.in session + a browser/Xvfb for refresh; on a desktop app a visible-browser refresh is actually easier than on the OCI box. All other TF features depend on this one.
- **Commits:** 75b0b4ffa, 2dfe5fee0, b2787a4c6, a2db9a5e4, 2852bc521, e41f23c95, 11fc9a709.

### TradeFinder market-pulse panel & enrichments (/tfmarketpulse, /tfsectorscope)
- **What it is (trader-facing):** TradeFinder's ranked lists (Intraday Boost / Breakout Beacon / High Powered / Sectors) in the charting terminal's right sidebar, served through the server's own JWT so no token is pasted in the browser. The Intraday Boost list is enriched server-side with signals TF doesn't give: narrow-CPR width/bias, opening-candle shock range, Kaufman directional-steadiness score, current-month future liquidity (spread/turnover), and a Momentum-Setup (Vijay Thakare) buy/sell scalping trigger.
- **Status vs upstream:** NEW (all-new files, additive).
- **Backend:**
  - `services/tradefinder_service.py` `fetch_market_pulse`/`fetch_sector_scope` (shared with auth core).
  - Enrichment services, all non-blocking `ensure_*_cache` (background thread) + `attach_*` (serves whatever's cached, `None` = unknown), TTL-bounded caches:
    - `services/tf_cpr_service.py` — narrow CPR width/bias, once-per-day.
    - `services/tf_first_candle_service.py` — 09:15–09:20 opening-candle range %, once-per-day.
    - `services/tf_directional_score_service.py` — Kaufman efficiency ratio since open, refreshed intraday.
    - `services/tf_future_liquidity_service.py` — live current-month future spread/turnover tier (reads live book, not bhavcopy; Spearman 0.46 rationale in docstring).
    - `services/tf_momentum_setup_service.py` + `services/tf_momentum_setup_math.py` — pure bar-for-bar port of the `oa-momentum-setup` chart indicator (supertrend/stoch/atr/ema transcribed from openalgo-charts@2.5.1); candle-close-aligned staleness.
  - **REST:** `restx_api/tf_market_pulse.py` → `/api/v1/tfmarketpulse` (POST, apikey); `restx_api/tf_sector_scope.py` → `/api/v1/tfsectorscope` (POST, apikey). Both return `{status:'error'}` at HTTP 200 when the server JWT is unavailable so the client can fall back to a pasted token. Both gated on `tf_settings.background_enabled()`.
  - **Env:** inherits TF_* above; enrichment gated by the background switch.
- **Frontend:**
  - `frontend/src/api/tradefinder.ts` — typed client (`TfListItem`, `MarketPulseData`, `SectorScopeData`, `tradefinderApi`, etc.).
  - `frontend/src/components/trading/TradeFinderPanel.tsx` — the right-sidebar panel: four lists, graphical sector view, poll interval (30s), JWT health, filters, pinning, sparkline/rank-timeline, rank-movement badges & optional alerts.
  - Nav: it is a /trading terminal panel, not a routed page — no `App.tsx`/`react_app.py`/`navigation.ts` entry.
- **Desktop port notes:** Files additive. Merge points: `restx_api/__init__.py` (`tf_market_pulse_ns`, `tf_sector_scope_ns`), `restx_api/data_schemas.py` (`TfMarketPulseSchema`, `TfSectorScopeSchema`), frontend wiring of `TradeFinderPanel` into the /trading sidebar + `lib/trading/tfSymbol.ts`. Depends on the auth core; Momentum-Setup depends on the `oa-momentum-setup` indicator being present. Uses `history_service` (rate-limited 3 req/s) heavily — this is why the background switch exists.
- **Commits:** e41f23c95, 8725c83f9, fc5bdf90b, 645cd91e8, 7986c6644, 23f6327c2, 145b05160, 7eea07cd1, a2db9a5e4, f22bb799d.

### TF Boost snapshot recorder & backtest universe (/boostsnapshots)
- **What it is (trader-facing):** Records TradeFinder's three ranked lists (+ sector index) into an isolated DuckDB every 30s–1min during market hours, with the Intraday-Boost enrichments attached, so a backtest can replay the *whole day's* boost universe (every stock that was on the list at any minute), not just the live list now.
- **Status vs upstream:** NEW (all-new files, additive).
- **Backend:**
  - `database/tf_boost_db.py` — isolated DuckDB (`db/tf_boost_snapshots.duckdb`, `TF_BOOST_DATABASE_PATH`); tables `tf_boost_snapshots`, `tf_boost_heartbeat`, backfill table, settings (`read/write_tf_setting`). Insert-and-ignore idempotency.
  - `services/tf_boost_snapshot_service.py` — isolated in-memory APScheduler, weekday 09:15–15:30 IST, `start_background()`/`stop_background()`, watchdog, latency self-check at 09:35; **SocketIO emit `boost_snapshot`** `{snapshot_time, rows}` the moment a tick lands.
  - `services/tf_boost_query_service.py` — `boost_universe_service`: day's full boost universe (union over lookback window), optional ranks/prices.
  - `services/tf_boost_backfill_service.py` — reconstructs the price path the recorder missed (late start) from 1-min broker candles into its own table; never writes snapshot rows.
  - **REST:** `restx_api/boost_snapshots.py` → `/api/v1/boostsnapshots` (POST, apikey; union universe for backtest).
  - **Scheduled jobs:** started in `app.py` via `tf_settings.background_enabled()` → `start_background()`.
  - **Env:** `TF_BOOST_DATABASE_PATH`, `TF_BOOST_SNAPSHOT_ENABLED`.
- **Frontend:** consumed by the TradeFinder panel (`/boostsnapshots` type in `tradefinder.ts`); listens for `boost_snapshot` socket event to refresh immediately.
- **Desktop port notes:** Additive files + a new DuckDB file (no migration — DuckDB schema self-creates). Merge points: `app.py` scheduler block, `restx_api/__init__.py` (`boost_snapshots_ns`), `restx_api/data_schemas.py` (`BoostSnapshotsSchema`), register the `boost_snapshot` SocketIO event on the frontend. Depends on auth core + enrichment services. Watch FD/history-rate-limit load (the whole reason for the on/off switch).
- **Commits:** b0ab133cc, dfe937401, 0ffcedfcf, db8e60e66, a8b72cf60, 200bfd4d5, 510053844, a9c8f5fce, 8ab1032d5, 888e69606, dd314c67a, 8b471b2b7.

### TF Boost rank-movement engine & badges (/boostmovement)
- **What it is (trader-facing):** Turns the recorded rank timeline into live "how a stock got to its rank" state — delta, velocity, acceleration, Top-N entry/exit/re-entry, sustained zone, and a classified event (JUMP/FAST/IN T5…) — plus a directional RUN badge (move measured against what it gave back). Drawn as word-badges in the panel and on the chart. No extra TradeFinder polling: reconstructed from the snapshot DuckDB.
- **Status vs upstream:** NEW (all-new files, additive).
- **Backend:**
  - `services/tf_rank_movement_service.py` — PURE core (`compute_rank_state(s)`, like `services/risk/`) + thin DuckDB I/O wrapper (`rank_movement_service`, `movement_snapshot`, `run_episodes_for`).
  - **REST:** `restx_api/boost_movement.py` → `/api/v1/boostmovement` (POST, apikey; per-symbol current-day movement rows).
  - **Env:** none of its own (reads tf_boost DuckDB).
- **Frontend:**
  - `frontend/src/lib/trading/boostBadge.ts` (+`.test.ts`) — single source of badge text/colour (`MOVEMENT_BADGE`, `badgeFor`), shared by both panels so they can't disagree. Words not glyphs.
  - `frontend/src/components/trading/BoostStrikesPanel.tsx` — second /trading panel: badged stocks + the liquid option strikes to trade them; clicking a strike charts the contract. Fetches option chain one stock at a time.
  - Rank-movement events + optional alerts also render inside `TradeFinderPanel.tsx`.
- **Desktop port notes:** Additive. Merge points: `restx_api/__init__.py` (`boost_movement_ns`), `restx_api/data_schemas.py` (`BoostMovementSchema`), wire `BoostStrikesPanel` into the /trading sidebar, `lib/trading/tfSymbol.ts`. Depends on the snapshot recorder (no snapshots → empty, not error).
- **Commits:** d4abcb0f4, 3904f4c68, 4eefdaca3, 25263cd3a, 337381429, ac407db51, 8d67d74d9, 3be64f290, 2b1091655, b0056bb22, 09c67276d, dc3c2cd33, a1345ba56, b400ce0c9, e41f61573, d6a7a341e, 806ac0e75, 33075a831, ec3dac7f3, a883161df, be9f2e7c4, 1469cfd65, 52581d189, a212c2490, 5c5a6074a, b5d53b502.

### Signal distribution API (ingest → Telegram → subscriber webhooks)
- **What it is (trader-facing):** A separate surface that ingests trading-signal edges from a headless producer, stores them idempotently, instantly posts them to one Telegram channel (plus a periodic grouped-by-sector summary), and feeds B2B subscribers over HMAC-signed webhooks with cursor backfill. Deliberately NOT under /api/v1 — its own `/signals/v1` surface so subscribers never hold the owner's apikey.
- **Status vs upstream:** NEW (all-new files, additive).
- **Backend:**
  - `database/signal_db.py` — on the MAIN SQLAlchemy DB (NullPool for SQLite). Tables `signal_events` (autoincrement id = backfill cursor, UNIQUE event_id idempotency) and `signal_subscribers` (argon2-hashed api key, Fernet-encrypted HMAC secret, webhook_url). Helpers: `insert_signal_event`, `get_active_signals`, `get_events_since`, `create/verify/revoke_subscriber`, `get_webhook_subscribers`, `decrypt_hmac_secret`.
  - `services/signal_ingest_service.py` — validate→persist→broadcast (storage before broadcast = idempotent, no double Telegram post on retry).
  - `services/signal_broadcast_service.py` — single-worker-thread Telegram poster (coalesces bursts, ~20 msg/min), reuses `telegram_alert_service.send_alert_sync`.
  - `services/signal_summary_service.py` — isolated APScheduler, IST market-hours, grouped summary; opt-in `SIGNAL_SUMMARY_ENABLED`.
  - `services/signal_webhook_service.py` — best-effort HMAC-signed webhook push (timestamp inside the MAC); recovery is the backfill endpoint, no retry queue.
  - **REST:** `restx_api/signals.py` → **own blueprint `signals_bp` at `/signals/v1`** (CSRF-exempt): `/signals/v1/ingest` (POST, producer apikey, stage 2) and subscriber-facing `/signals/v1/events` backfill + webhook management (stage 4). `restx_api/signal_schemas.py` — marshmallow ingest schema (rejects quantity & upstream-provider vocabulary).
  - `scripts/manage_signal_subscribers.py` — CLI (no admin UI) to create/list/revoke subscribers.
  - **Scheduled jobs:** `init_signal_summary_scheduler()` in `app.py`.
  - **Env:** `SIGNAL_RATE_LIMIT`, `SIGNAL_CHANNEL_ALL`, `SIGNAL_MIN_SEND_INTERVAL`, `SIGNAL_SUMMARY_ENABLED`, `SIGNAL_SUMMARY_EVERY_MINUTES`.
- **Frontend:** none (producer is the external headless chart repo; subscribers are external).
- **Desktop port notes:** Additive. Merge points: `app.py` (`from restx_api.signals import signals_bp`, `register_blueprint(signals_bp)`, `csrf.exempt(signals_bp)`, `init_signal_summary_scheduler`). Uses the main DB — if the desktop app ships schema migrations, add a migration for `signal_events`/`signal_subscribers` (currently created by `init_db()`-style create_all; verify it runs on existing DBs). Needs Telegram bot config (shared with the platform's existing Telegram alert service). **No TradeFinder dependency.**
- **Commits:** 1bcddb1af, 21d6809e3, 4857700d6.

### Infra / support (ops scripts, docs, tests)
- **Analysis/ops scripts (all additive, standalone CLIs):** `scripts/boost_continuation_analysis.py` (entry→continuation micro-analysis), `scripts/tf_boost_behaviour.py` (post-mortem of a day's list, writes a stored study), `scripts/tf_boost_gate_compare.py` (badge rating-gate comparison across days), `scripts/tf_boost_health.py` (pre-open readiness check), `scripts/tf_boost_latency_check.py` (snapshot→client latency vs 40s budget; also run at 09:35 by the scheduler), `scripts/tf_boost_option_audit.py` (what the badge said vs what stock/option did), `scripts/tf_boost_verify.py` (every badge raised on a day + outcome). None wired into app.py; run via `uv run python`.
- **Docs:** `docs/tf-boost-monday-runbook.md` — operator runbook. (Should be linked from `docs/INDEX.md` in a port.)
- **Tests (all additive):** `test/test_tf_settings.py`, `test/test_tf_jwt_keepalive_retry.py`, `test/test_tf_future_liquidity.py`, `test/test_tf_momentum_setup.py`, `test/test_tf_rank_movement.py`, `test/test_tf_boost_rank_as_of.py`, `test/test_tf_boost_snapshot_notify.py`, `test/test_tf_boost_snapshot_window.py`, `test/test_signal_ingest.py`, `test/test_signal_broadcast.py`, `test/test_signal_summary.py`, `test/test_signal_subscribers.py`, `test/test_isi_v56_tf_jwt_update.py`, `test/test_isi_v56_tf_snapshot_logger.py` (the last two tie TF JWT/snapshot logging to the external `isi_v56` strategy scripts — note a dependency on `strategies/isi_v56_*` which are outside this scope).
- **Frontend test:** `frontend/src/components/trading/TradeFinderBackgroundSwitch.test.tsx`, `frontend/src/lib/trading/boostBadge.test.ts`.

---

## Writer Flow, OI Profile & OI Analytics

Scope note: all these features share one DuckDB file (`db/oi_flow.duckdb`, via `database/oi_flow_db.py`) and one build thread (the Writer Flow builder in `services/writer_flow_service.py`). Two referenced modules are **out of scope** for this section (catalogued elsewhere): `services/market_read_service.py` / `services/market_read/core.py` (the "market read" panel and the `_book`/`_level_events` primitives that Option Levels imports). They are hard dependencies of the Option Levels and the `/writerflow/api/read*` endpoints below.

### Writer Flow (OI Flow per-minute recorder + strike-by-strike writer analysis)
- **What it is (trader-facing):** A minute-by-minute record of what option writers did at every strike around ATM through the day (writing / covering / buying / unwinding), shown as a strike-by-time heatmap and a current-streak ladder on a dedicated page and overlaid on the `/trading` chart. Tells a trader where writers have been adding, covering or holding, and for how long. Reads only its own recorded database, so opening it costs the broker nothing.
- **Status vs upstream:** NEW (whole feature; the recorder, DB, analysis, page and chart indicator do not exist upstream).
- **Backend:**
  - `services/oi_flow_recorder_service.py` — APScheduler `BackgroundScheduler` (IST). Jobs: `oi_flow_tick` (every minute :05, one `/api/v1/optionchain`-equivalent call per underlying), `oi_flow_backfill` (startup fill from 1m history), `oi_flow_gaps` (`*/5`) and `oi_flow_gaps_close` (23:58 for MCX). Opt-in via `OI_FLOW_RECORDER_ENABLED=true`, wired in `app.py` (startup) and `shutdown_oi_flow_recorder()` (teardown). On-demand recording of any charted F&O underlying via `request_recording()` / `_start_on_demand` / `on_demand_state`. Single-writer guarantee via `claim_writer()` file lock (`<db>.writer`). Gap-fill paced behind history gate; honors `stream_registry.should_stop()`.
  - `services/oi_flow_analysis.py` — **pure** (no I/O, services/risk/ style): classifies each strike-side step and streak from OI+premium together; `analyse()`, `classify()`, `bias()`, `oi_lot()` (reads lot unit from data — quantity on NFO/BFO, lots on MCX).
  - `services/writer_flow_service.py` — builds + caches the JSON answer once per recorded write (`generation()` keyed), gzipped; runs every build on one real OS thread (`_on_builder`, `real_threading`) so it never holds the eventlet hub; rebuilds only views watched in last 150s.
  - `database/oi_flow_db.py` — DuckDB store (`db/oi_flow.duckdb`, overridable `OI_FLOW_DATABASE_PATH`). Tables: `oi_flow_samples` (the record), `oi_flow_settings`, plus market-read tables (`market_read_quotes/events/minutes`, out-of-scope consumer). No `upgrade/` migration — tables are `CREATE TABLE IF NOT EXISTS` in `init_oi_flow_database()` (isolated DB, no openalgo.db schema impact). In-process `generation` counter + write-serialized single-process lock.
  - `blueprints/writer_flow.py` (`writer_flow_bp`, url_prefix `/`): REST `GET /writerflow/api/sessions`, `GET /writerflow/api/flow` (gzipped), `GET /writerflow/api/levels` (Option Levels, see below), `GET /writerflow/api/read` + `GET/POST /writerflow/api/read/settings` (market read — out-of-scope service). All `@check_session_validity` (session-cookie, not `/api/v1/` apikey).
  - SocketIO: emits `writer_flow_update` (`UPDATE_EVENT`) per underlying when a new minute lands, via `emit_from_any_thread`, so open charts fetch immediately.
  - Env vars: `OI_FLOW_RECORDER_ENABLED`, `OI_FLOW_UNDERLYINGS` (default `NIFTY:NSE_INDEX,BANKNIFTY:NSE_INDEX,SENSEX:BSE_INDEX`), `OI_FLOW_STRIKES_AROUND_ATM` (15), `OI_FLOW_ON_DEMAND_MAX` (5, cap 20), `OI_FLOW_DATABASE_PATH`.
- **Frontend:**
  - Page `frontend/src/pages/WriterFlow.tsx` → React route `/writerflow` (`App.tsx` lazy import + Route), Flask route `blueprints/react_app.py:/writerflow`, registry entry in `frontend/src/lib/tools.ts` ("Writer Flow", href `/writerflow`). (NOT in `navigation.ts`; surfaced via Tools registry.)
  - Plot helpers `frontend/src/lib/PlotFlow.tsx` + `frontend/src/lib/plotly-flow.ts` (dedicated Plotly build: scatter+bar+heatmap, kept separate so other tools don't load heatmap). Units: `frontend/src/lib/writerFlowUnits.ts` (`oiScale` — lakh qty vs lots; tests `writerFlowUnits.test.ts`).
  - Chart indicator `strategies/indicators/writer_flow_live.js` — runtime-loaded custom indicator for `/trading`; draws strike bands behind candles + right-edge streak bars + event diamonds + a Totals pane; fetches `/writerflow/api/flow`, listens for `writer_flow_update`. Loaded at runtime by `blueprints/custom_indicators.py` (not bundled); committed as a reference indicator (whitelisted in `strategies/indicators/.gitignore`).
  - Tests: `test/indicators/writer_flow_live.test.mjs`, `test/indicators/right_edge_shared.test.mjs`, `frontend/.../writerFlowUnits.test.ts`.
- **Desktop port notes:** Mostly additive — isolated DB, own blueprint, own scheduler, no openalgo.db schema change. Port the recorder + DB + analysis + writer_flow_service + blueprint together; they are one unit. **Dependency:** `/writerflow/api/read*` endpoints require the out-of-scope `market_read_service`; if that section isn't ported, drop those two routes (the sessions/flow/levels routes stand alone). On-demand recording touches `token_db`, `expiry_service`, `option_chain_service`, `history_service`, `option_symbol_service`, `market_calendar_db`. Data retention: unbounded DuckDB growth by design (per-minute rows); no purge job.
- **Commits:** 670372ef2 (core strike-by-strike record), 2cc1682db, 164966a11, 7eea07cd1, fdd487411, a46eb6a4c, 47ea31ffb, 2a486072e, c54db6f58, 4093ff214.

### Option Levels (support/resistance + gamma from the Writer Flow record)
- **What it is (trader-facing):** Support/resistance zones, strength heatmap (put/call walls), gamma concentration + flip, and measured hold rates — all derived minute-by-minute from the recorded option OI, drawn as a `/trading` chart overlay. Honest about itself: each zone shows its measured hold rate next to chance.
- **Status vs upstream:** NEW.
- **Backend:**
  - `services/oi_levels/core.py` + `services/oi_levels/__init__.py` — **pure** core (`build_levels`, `LevelInputs`): zones, anchor-takeover hysteresis, touches/outcomes, hold rates, gamma (via injected greeks/flip callables). Imports `_book`, `_level_events`, `_near`, `HOLD_MINUTES`, `SIGNIFICANT_SHARE`, `WEAKEN_SHARE`, `LEVEL_STEPS` from `services/market_read/core` (out-of-scope dependency).
  - `services/oi_levels_service.py` — reads recorded day from `oi_flow_db`, runs core on the Writer Flow builder thread, caches per `generation`; adds rolling-month "persistence" strength (`_cycle_strength`, 30-day half-life-weighted). Injects `calculate_chain_greeks` (option_greeks_service) and `find_gamma_flip` (strike_profile_service).
  - REST: `GET /writerflow/api/levels` (in `blueprints/writer_flow.py`, gzipped, `@check_session_validity`). No new DB/table (reads `oi_flow_samples`). Rebuilt on `writer_flow_update` via `rebuild_watched`.
- **Frontend:** No dedicated page; consumed by the `/trading` chart as part of the Writer Flow chart story. (Overlay drawing lives in the chart indicator layer / openalgo-charts; `writer_flow_live.js` is the primary custom indicator.)
- **Desktop port notes:** Additive, but **hard-depends on `services/market_read/core`** (out of scope) — port that first. Also depends on option_greeks_service + strike_profile_service. Shares the builder thread and DB with Writer Flow; port after it.
- **Commits:** 1fc8d95c1, 5ad622d9c (market read panel, overlaps).

### OI Profile — live OI, NSE bhavcopy anchor, multi-expiry sums (upstream feature, heavily extended)
- **What it is (trader-facing):** Futures candles alongside a current-OI butterfly and a daily/intraday OI-change butterfly per strike. Fork adds: live OI laid over the polled profile from the WebSocket feed, "Change in OI" anchored on NSE's published previous-session close (no per-leg broker history), summing OI across several ticked expiries (Sensibull-style), and an arbitrary intraday window delta.
- **Status vs upstream:** MIXED — MODIFIES upstream `blueprints/oiprofile.py` (+82), `services/oi_profile_service.py` (+837, the bulk), `frontend/src/pages/OIProfile.tsx` (+391); plus NEW support files (bhavcopy service, live-OI overlay lib, chart indicator). `services/oi_tracker_service.py` change is a trivial 1-line lint fix (f-string with no placeholder) — not a feature.
- **Backend:**
  - `services/oi_profile_service.py` (modified) — adds: multi-expiry per-strike summing (`expiry_dates`), intraday windowed delta (`window_start`/`window_end`, unix secs), bhavcopy-anchored change, and a `base` per leg in the payload so a client holding a live tick recomputes change itself (`live - base`). Shared TTL cache (`OI_PROFILE_CACHE_TTL` 60s, `OI_PROFILE_CACHE_MAXSIZE` 64).
  - `services/nse_oi_bhavcopy.py` (NEW) — downloads NSE F&O bhavcopy (one ~1.1MB zipped CSV per session) to supply previous-session close OI for the whole market in one fetch (`previous_session_oi`, `cached_previous_session_oi`, `underlying_of`); TTLCache + lock; via shared `get_httpx_client`. NSE-only; BSE/crypto keep the per-leg fallback in `oi_profile_service`.
  - `blueprints/oiprofile.py` (modified, `oiprofile_bp`): `POST /oiprofile/api/profile-data` (extended with expiries/window), `GET /oiprofile/api/intervals`.
  - No new DB table/migration (bhavcopy is cached in-memory, not persisted).
  - Env: `OI_PROFILE_CACHE_TTL`, `OI_PROFILE_CACHE_MAXSIZE`.
- **Frontend:**
  - `frontend/src/pages/OIProfile.tsx` (modified) → React route `/oiprofile` (exists upstream), Flask `blueprints/react_app.py:/oiprofile`, registry `tools.ts` ("OI Profile").
  - `frontend/src/lib/oiProfileLive.ts` (NEW) — lays WebSocket-feed `oi` ticks over the polled profile (`liveOiKey`, `live - base`); keyed by exchange:symbol. Test `oiProfileLive.test.ts`.
  - Chart indicator `strategies/indicators/oi_profile_live.js` (NEW) — runtime-loaded `/trading` indicator drawing live OI bars pinned to the pane's right edge via `ctx.addPrimitive()` (screen-space, not data-space); sums expiries, polls, overlays live ticks. Loaded by `blueprints/custom_indicators.py`; committed reference (whitelisted in `.gitignore`).
  - Tests: `test/indicators/oi_profile_live*.mjs` (`.test`, `_both`, `_fixes`, `_overlay`), `right_edge_shared.test.mjs`.
- **Desktop port notes:** MERGE POINTS — `blueprints/oiprofile.py`, `services/oi_profile_service.py`, `frontend/src/pages/OIProfile.tsx` all exist upstream; take the fork diffs on top of upstream's version, not whole-file. `nse_oi_bhavcopy.py`, `oiProfileLive.ts`, `oi_profile_live.js` are additive. Live overlay depends on the WebSocket proxy (port 8765) carrying `oi` on ticks (broker-dependent). Depends on `history_service`, `option_chain_service`, `strategy_chart_service`, `token_db_enhanced`, `market_calendar_db`. No data retention concern (caches only).
- **Commits:** bhavcopy/anchor + multi-expiry: 53572ab64/344277945, a3ae80e97, 62d6f1777, 603f552e6, 5e7368479, 58bca03b5, f43a67c74, b61bcbfd8, f4d3c4015, 1783feffb, e5f1ff6ef; live OI: 26d62553b, d9744f116, 44ac4ff98, 6a5137de7, 66734ea36, 69e503e84, 6c4f672d2, 1334a952f.

### Custom chart-indicator library (ported indicators + licensing docs)
- **What it is (trader-facing):** The reference/licensing scaffolding for OpenAlgo's runtime-loaded `/trading` custom indicators. Documents a 497-strong ported Pine Script indicator library (MIT/GPL-3.0/MPL-2.0) that users drop into `strategies/indicators/` and the chart `import()`s at runtime.
- **Status vs upstream:** NEW (fork-only). The 497 ported `.js` files themselves are **gitignored / not committed** (user-installed); only the two reference indicators (`oi_profile_live.js`, `writer_flow_live.js`, catalogued above) + `open_range_breakout.js` (out of scope) ship. These files are the licensing/attribution record.
- **Backend:** none. Served at runtime by `blueprints/custom_indicators.py` (out of scope); chart fetches index and `import()`s each per `frontend/src/lib/trading/customIndicators.ts`.
- **Frontend:** `strategies/indicators/.gitignore` (whitelists the 3 reference `.js` + README, ignores the rest), `ATTRIBUTION.md`, `LICENSING.md`, `LICENSE` (MIT), `LICENSE-GPL-3.0.txt`, `LICENSE-MPL-2.0.txt`. Governed by the `chart-indicator` skill.
- **Desktop port notes:** Additive docs/config. If Desktop ships the ported library, carry these licence files with it (GPL-3.0/MPL-2.0 ports are copyleft — must not be relabelled MIT). The `.gitignore` whitelist pattern must be preserved so the reference indicators survive `git pull` while user indicators stay local.
- **Commits:** 81dcba74a.

---

## Market Read, Screener & NSE Events

All files in this section are **NEW in the fork** (none exist in `upstream/main`). They form three loosely-coupled features. Note a shared dependency: the Market Read feature leans on `database/oi_flow_db.py` (new market-quote / read-event / settings tables) and `services/oi_flow_recorder_service.py` (the Writer Flow OI recorder that drives it) — those files belong to the Writer Flow / OI-Flow domain (another section), so they are called out as port dependencies, not re-catalogued here.

### Market Read (live market panel + alerts)
- **What it is (trader-facing):** A once-a-minute plain-English read of what option writers, futures traders and volatility are doing across the whole NIFTY chain (plus BANKNIFTY and SENSEX for agreement) — one headline, per-reading state (flow lead, futures vs OI, support/resistance levels, India VIX / ATM IV), and a timeline of what changed. Shown as a panel inside the `/trading` and Writer Flow surfaces; major turns pop a toast and sound a tone, and optionally fire a Telegram message. It describes, it does not forecast (the code even documents measured correlations).
- **Status vs upstream:** NEW.
- **Backend:**
  - `services/market_read/__init__.py`, `services/market_read/core.py` — pure, no-I/O computation core (same discipline as `services/risk/`): `build_read(ReadInputs)` recomputes the whole day's events deterministically with stable ids, so restarts/backfills never change what a minute says. Four readings (flow, futures, levels, volatility) with tunable module constants.
  - `services/market_read_service.py` — the I/O + delivery layer. `on_minute(underlying)` is called by the OI-Flow recorder (`services/oi_flow_recorder_service.py:485`) after each stored minute; it only queues work onto a dedicated real thread named `market-read` (`stop_worker()` on recorder shutdown). Per minute it pulls one batch quote (current+next-month NIFTY futures, India VIX, index prev-close via `services/quotes_service.get_multiquotes`), backfills missed minutes every 15 min from `services/history_service.get_history`, computes the read, stores events/minute, and announces. **Eventlet-critical detail** (commit `1e95baf1e`): the read reaches green-managed DB/broker/quotes, so the real worker thread runs it via `real_threading.run_on_hub(..., timeout=300)` — called inline directly it hung the worker forever under eventlet.
  - Routes (in `blueprints/writer_flow.py`, not in scope but the wiring): `GET /writerflow/api/read` (serves cached `latest(day)` or computes on the Writer Flow builder thread), `GET|POST /writerflow/api/read/settings` (the three switches: read / Telegram / sound).
  - DB (dependency, `database/oi_flow_db.py`): `insert_market_quotes`, `load_market_quotes`, `store_read_events`, `store_read_minute`, `load_day`, `expiry_for_day`, `read_setting`/`write_setting`. Settings keys `market_read_enabled|telegram|sound`.
  - SocketIO: emits `market_read_update` via `extensions.emit_from_any_thread`.
  - Telegram: batches major events (`TELEGRAM_EVERY=10min`, `TELEGRAM_FRESH=5min`) through `services/telegram_alert_service`.
  - Env: `MARKET_READ_UNDERLYING` (default `NIFTY`).
  - Tests: `test/test_market_read_core.py`, `test/test_market_read_service.py`, fixture `test/data/market_read_21oct2026.json.gz` (recorded NIFTY morning 21-Oct-2026).
- **Frontend:**
  - `frontend/src/components/trading/MarketReadPanel.tsx` (+ `.test.tsx`) — the panel; reads via `writerFlowApi` (`@/api/writer-flow`, not in scope), subscribes to `market_read_update` through `onServerEvent`, has a settings popover (read/Telegram/sound switches).
  - `frontend/src/hooks/useMarketReadAlerts.ts` — mounted by the page (not the panel) so a major event sounds a tone + 15s toast even with the panel closed; handles browser audio-unlock gesture.
- **Desktop port notes:** Core is self-contained and pure (easy, additive). Service layer is additive but depends on: the OI-Flow recorder calling `on_minute`, the new `oi_flow_db` tables/functions, `quotes_service`, `history_service`, `option_symbol_service.near_month_futures`, `option_greeks_service.calculate_chain_greeks`, `telegram_alert_service`, and `extensions.emit_from_any_thread`. Frontend panel wiring lives in the not-in-scope `@/api/writer-flow` client and the `/trading` + Writer Flow pages — those two routes/clients must come across together. The `run_on_hub` eventlet fix is mandatory for correctness on the default worker.
- **Commits:** `5ad622d9c` (feat, panel + writer/futures/level/volatility read), `1e95baf1e` (eventlet hub fix), `e989ff928` (ruff cleanup).

### Indicator Screener
- **What it is (trader-facing):** A TradingView-Pine-Screener-shaped page: pick a symbol universe, pick ONE chart indicator (`openalgo-charts` descriptor — built-in or a user file in `strategies/indicators/`), filter on its plot outputs or its declared alert conditions, and scan hundreds of symbols. The key design point: the same `calc(bars, settings)` that draws the study on `/trading` runs in the browser worker, so a screener value equals the chart value by construction.
- **Status vs upstream:** NEW.
- **Backend:**
  - `services/screener_service.py` — bar delivery only (does NOT evaluate indicators/filters; the browser does). `scan_bars(pairs, interval, bars, live, api_key)` reads history in bulk from Historify DuckDB (`database.historify_db.get_bulk_ohlcv`, one query per exchange) and optionally stitches the forming candle from a batch quote (`get_multiquotes`). Returns columnar arrays keyed `"EXCHANGE:SYMBOL"`. Bounds: `MAX_SYMBOLS=500`, `MIN_BARS=50`/`MAX_BARS=1000`/`DEFAULT_BARS=300`, `QUOTE_CHUNK=50`. Live-candle stitcher (`_apply_quote`) carefully refuses to invent candles outside market hours / across stale gaps.
  - Route (dependency, `blueprints/historify.py:237`): `POST /historify/api/bulk-ohlcv` — session-authenticated (not an API-key surface); resolves the user's API key only when `live=true`.
  - Test: `test/test_screener_bars.py` (asserts SQL aggregation boundaries equal `bucket_start`, and live-stitch never produces ragged columns — both silent-corruption risks).
- **Frontend:**
  - `frontend/src/pages/Screener.tsx` — the page (React route `/screener` in `App.tsx`; Flask route `/screener` in `blueprints/react_app.py:378`; tool-registry entry `frontend/src/lib/tools.ts:150`).
  - `frontend/src/components/screener/FilterBuilder.tsx` — value-filters (plot column vs number/column) and alert-filters (indicator's own `alertcondition` analogue).
  - `frontend/src/lib/screener/model.ts` (+ `model.test.ts`) — React/DOM-free shared vocabulary (filters, wire shapes, `toBars`), imported by both page and worker.
  - `frontend/src/lib/screener/scanWorker.ts` — Web Worker that does the fetching (`POST /historify/api/bulk-ohlcv`) AND runs the indicator `calc` over each symbol off the main thread, reporting real progress.
  - Also consumes `tradefinderApi` (TradeFinder lists as universes) and `watchlistApi` — both out of scope (other sections).
- **Desktop port notes:** Service is additive but hard-depends on the Historify DuckDB store (`historify_db.get_bulk_ohlcv`, intervals/bucket helpers) and `quotes_service`. Frontend needs the `openalgo-charts` indicator descriptor plumbing (`IndicatorPickerDialog`, `IndicatorSettingsDialog`, `lib/trading/terminal`) plus a Web Worker build step. The three page-registrations (App.tsx route, react_app.py route, tools.ts entry) must all come across or the page 404s / is undiscoverable.
- **Commits:** `11fc9a709` (feat: screener page, tradefinder proxy, boost snapshot), `e989ff928` (ruff cleanup).

### NSE Corporate Events
- **What it is (trader-facing):** A per-symbol flag for upcoming NSE board meetings, results and corporate actions, so a scanner/chart can warn before entry that a stock is in a news window (trades on news, not structure). Returns the single soonest event per symbol inside a day window.
- **Status vs upstream:** NEW.
- **Backend:**
  - `services/nse_events_service.py` — server-side NSE client (browser can't warm nseindia.com cookies cross-origin; also one fetch serves all clients). `fetch_events(force)` warms a cookie'd `requests.Session`, pulls `/api/event-calendar` and `/api/corporates-corporateActions`, caches for the IST trading day, degrades quietly on NSE 401s/partials (never raises). `events_for(symbols, within_days)` returns one soonest event per symbol with `daysAway`.
  - REST (API-key surface): `restx_api/nse_events.py` → namespace at `POST /api/v1/nseevents` (registered `restx_api/__init__.py:122`). Schema `NseEventsSchema` (`restx_api/data_schemas.py:101`): `apikey`, optional `symbols` (empty = whole calendar), `within_days` (0–30, default 2). Rate-limited via `API_RATE_LIMIT`. Returns `{status:success, data:{symbol:{kind,date,purpose,daysAway}}}`; errors return HTTP 200 with an empty/"unavailable" payload so a caller's flow never breaks.
  - DB/schedulers: none — pure in-memory day cache (module `threading.Lock`).
- **Frontend:** No in-repo frontend consumer found. `/api/v1/nseevents` is an API-only surface (external scanners / the downstream openalgo-chart app). The Screener page itself does not call it (its worker only hits `/historify/api/bulk-ohlcv`).
- **Desktop port notes:** Fully self-contained and additive — one service file + one restx namespace + one marshmallow schema entry + the namespace registration line. Only dependency is `database.auth_db.get_auth_token_broker` for apikey validation and the shared `limiter`. No DB migration needed.
- **Commits:** `11fc9a709` (feat). (Schema entry `NseEventsSchema` lives in `restx_api/data_schemas.py`, which is out of scope here but is the one required merge-point line.)

---

## Expired F&O, Options & Order Services

Scope = 34 files. One large net-new feature (Expired F&O historical download),
one documentation/test-only entry (Option Levels, core lives in another
section), a small set of real options/order service modifications, and a large
tail of trivial `from typing import ...` cleanups that are merge-noise, not
features.

### Expired F&O historical download (for Historify)
- **What it is (trader-facing):** A new "Expired F&O" tab in Historify that
  downloads historical 1-minute OHLCV for *already-expired* options and futures
  contracts (which brokers normally drop from their live symbol master) and
  stores them in the local DuckDB historify store. Trader picks an underlying,
  pulls the list of past expiries, picks expiries + contract types (CE/PE/FUT),
  and starts a background download job with a live Socket.IO progress bar. Only
  Upstox and Dhan expose the needed broker APIs. Also adds a bulk NSE F&O stock
  list importer into the Historify watchlist and a bulk-OHLCV endpoint.
- **Status vs upstream:** MIXED — new broker adapters + new service, with
  additive routes/functions bolted onto existing Historify files.
  - NEW files: `broker/dhan/api/expired_data.py`, `broker/upstox/api/expired_data.py`,
    `services/expired_fno_service.py`, `frontend/src/api/expiredFno.ts`,
    `frontend/src/pages/historify/ExpiredFnoTab.tsx`, `frontend/e2e/expired-fno.spec.ts`.
  - MODIFIES upstream: `blueprints/historify.py` (+347, adds the routes below),
    `database/historify_db.py` (+1288, adds expired tables + bulk/repair helpers),
    `services/historify_service.py` (+213, adds `get_fno_stock_list()` and gap
    detection `_find_middle_gaps`), `frontend/src/pages/Historify.tsx` (+12, the
    tab trigger + `<ExpiredFnoTab>` panel).
- **Backend:**
  - Blueprint `historify_bp` (`url_prefix="/historify"`), all session-auth'd
    (`@check_session_validity`). New routes (session, not `/api/v1`):
    `POST /historify/api/bulk-ohlcv`, `GET /historify/api/broker/capabilities`,
    `GET /historify/api/expired/capability`,
    `POST|GET /historify/api/expired/expiries`,
    `POST|GET /historify/api/expired/contracts`,
    `GET|POST /historify/api/expired/jobs`,
    `POST /historify/api/expired/harvest`,
    `GET /historify/api/expired/jobs/<job_id>`,
    `POST /historify/api/expired/jobs/<job_id>/cancel`,
    `GET /historify/api/expired/stats`, `GET /historify/api/fno/stocks`.
  - `services/expired_fno_service.py`: capability gate
    `EXPIRED_FNO_CAPABLE_BROKERS = {"upstox","dhan"}`; `fetch_expiries`,
    `fetch_contracts_for_expiry`, `start_expired_fno_download` /
    `start_dhan_harvest` (parallel `ThreadPoolExecutor(max_workers=4)` download
    workers), job tracking (`get_job_status`, `list_jobs`, `cancel_job`,
    `get_stats`). Jobs run on a module-level singleton
    `ThreadPoolExecutor(max_workers=1, thread_name_prefix="expired_fno_job")`.
    Emits `historify_progress` / `historify_job_complete` Socket.IO events.
  - Broker adapters: `UpstoxExpiredDataClient` (resolve underlying key, sync
    rate limiter), `DhanExpiredDataClient` (resolve underlying, strike offsets,
    rate limiter, candle/rolling normalization, contract-key encode/decode).
    Brokers touched: **upstox, dhan** only.
  - DB/migrations: `database/historify_db.py` adds DuckDB tables
    `expired_fno_expiries`, `expired_fno_contracts`, `expired_fno_jobs`,
    `expired_options_rolling`, created `IF NOT EXISTS` at connect time (no
    `upgrade/` migration script — DuckDB historify store, created lazily). New
    helpers: `upsert_/get_expired_fno_expiries`, `..._contracts`,
    `..._jobs`, `resolve_expired_fno_contract`, `mark_..._done`,
    `increment_..._retry`, `get_pending_expired_fno_contracts`,
    `upsert_rolling_options`, plus unrelated additive helpers reused here:
    `get_bulk_ohlcv`/`_bulk_frame`/`_columnarise` (bulk endpoint),
    `get_existing_dates`, `get_last_candle_timestamp`,
    `delete_market_data_symbols`, `repair_market_data_quality`,
    `bucket_start`. Store: `historify.duckdb` (note the comment about DuckDB's
    single-writer lock — a process-wide lock serializes connects).
  - Env vars: none new dedicated; capability is broker-derived. Worker counts
    (`NUM_WORKERS=4`, job executor `max_workers=1`) are hardcoded.
- **Frontend:**
  - No new React route — Historify page already exists upstream; this adds one
    tab inside it (`frontend/src/pages/Historify.tsx`), rendering
    `frontend/src/pages/historify/ExpiredFnoTab.tsx` (+1130, the whole UI:
    underlying picker, expiry/contract selection, job list, progress).
  - API client `frontend/src/api/expiredFno.ts`: `getExpiredFnoCapability`,
    `fetchExpiries`/`getExpiries`, `fetchContracts`/`getContracts`,
    `startDownloadJob`, `getJobStatus`, `cancelJob`, `getBrokerCapabilities`,
    `listExpiredFnoJobs`, `getExpiredFnoStats`, `getFnoStocks` — all hit the
    `/historify/api/...` routes above via the session-auth `webClient`.
  - E2E: `frontend/e2e/expired-fno.spec.ts`.
- **Desktop port notes:** Mostly additive. Merge points to watch on upstream
  rebase: `blueprints/historify.py`, `database/historify_db.py`,
  `services/historify_service.py`, `frontend/src/pages/Historify.tsx` — these
  are upstream files with fork insertions. The two broker adapters and the
  service are self-contained new files. Depends on: Upstox/Dhan auth tokens,
  the DuckDB historify store, Socket.IO (`historify_progress`), and the shared
  `webClient` session auth (not `/api/v1` apikey).
- **Commits:** 805fc33dd (feat), 7cf6dd10d (review fixes),
  a3a6817ca (NSE F&O stock list bulk import), c4cda7af4 (WIP snapshot).

### Option Levels (docs + e2e only in this scope)
- **What it is (trader-facing):** A `/trading` chart overlay drawing
  support/resistance lines at the strikes where option writers sit (green = put
  writers/support, red = call writers/resistance), read from the Writer Flow OI
  recorder. Updates every minute during market hours. Distinct from the
  third-party "Meridian - Options Levels" price-action study.
- **Status vs upstream:** NEW — but only the documentation and one e2e test
  fall in this file set. The feature's implementation (chart indicator,
  `/writerflow/api/levels` endpoint, Writer Flow recorder) lives in another
  section's files, so catalog the core there.
- **Backend:** endpoint consumed is `/writerflow/api/levels` (stubbed in the
  test); recorder gated by `OI_FLOW_RECORDER_ENABLED=true` in `.env`. Not in
  this scope beyond the reference.
- **Frontend:** `frontend/e2e/option-levels.spec.ts` drives a synthetic harness
  (`/e2e/harness/levels.html`) with a stubbed levels API.
- **Desktop port notes:** Port alongside Writer Flow; this scope only carries
  `docs/option-levels.md` and the e2e spec. Dependency: Writer Flow recorder.
- **Commits:** 1fc8d95c1.

### Options / multi-leg order service modifications
- **What it is (trader-facing):** Supports the signal/options order path —
  multi-leg option baskets now respect sandbox/analyzer mode (margin precheck),
  and futures resolution can return several near-month contracts.
- **Status vs upstream:** MODIFIES upstream (all these files pre-exist
  upstream).
  - `services/options_multiorder_service.py` (+62): when `get_analyze_mode()`,
    resolves each leg's symbol, builds a precheck basket, runs
    `OrderManager(user_id).precheck_orders(...)` and
    `fund_manager.check_margin_available(...)` against the sandbox before
    accepting the multi-leg order. Merge-sensitive (touches sandbox + option
    symbol resolution).
  - `services/option_symbol_service.py` (+25): adds
    `near_month_futures(base, exchange, count=1)` returning up to `count`
    contracts (nearest first); `find_near_month_futures` now delegates to it.
    Return type changed from `None` to `[]` on no-match — minor but a
    behavior/signature change.
  - `services/place_options_order_service.py`, `services/custom_straddle_service.py`,
    `services/straddle_chart_service.py`: **trivial** only — unused-import
    (`typing`) cleanup and a set-comprehension/var-rename lint fix. Not
    features. (Note: custom straddle and straddle chart themselves are upstream
    features, untouched functionally by the fork.)
- **Backend:** no new routes/tables/env; rides existing `/api/v1` options order
  endpoints.
- **Desktop port notes:** `options_multiorder` and `option_symbol` carry real
  logic — port the diffs. The other three are drop-on-merge lint noise.
- **Commits:** 5ad622d9c, c4cda7af4 (and ruff-sweep e989ff928 for the lint-only
  bits).

### Order/execution service modifications (generic)
- **`services/place_order_service.py` (+39, MODIFIES, merge-sensitive):** adds
  `broker_response_status(res)` helper that reads `.status` then falls back to
  `.status_code` and tolerates `None`, so a rejected order surfaces the broker's
  real HTTP status (e.g. 403 SEBI static-IP block Upstox UDAPI1154, 400 bad
  order) instead of a blanket 500 that masked the broker's message. Infra-safety
  fix; worth porting. Commit c4cda7af4 (and surrounding).
- **Trivial typing-only cleanups (NOT features — drop on merge, pure
  `from typing import ...` unused-import removal, 1 line each):**
  `basket_order_service.py`, `cancel_all_order_service.py`,
  `cancel_gtt_order_service.py`, `cancel_order_service.py`,
  `close_position_service.py`, `gtt_orderbook_service.py`,
  `historify_scheduler_service.py`, `instruments_service.py`,
  `intervals_service.py`, `modify_gtt_order_service.py`,
  `modify_order_service.py`, `openposition_service.py`,
  `orderbook_service.py`, `orderstatus_service.py`,
  `positionbook_service.py`, `split_order_service.py`. All from the ruff sweep
  (e989ff928) / merge churn. No trader-facing behavior.

---

## /trading Terminal, Chart Indicators & Market-Data Frontend

Scope note: these a5 files are the `/trading` frontend *wiring*. Several features
here mount or depend on components, hooks, API clients, backend blueprints and
runtime indicator JS that live in other sections (noted per feature). All React
routes added to `App.tsx` are listed under "New routes in App.tsx".

---

### Per-indicator Timeframe (MTF) on every chart study
- **What it is (trader-facing):** Every chart indicator (built-ins, the user's own JS modules, OpenScript studies) gains a "Timeframe" setting, like pro terminals: e.g. a 1h EMA on a 5m chart plots the 1h value as a step, the forming 1h bar updates live, drawings land at their real time. Plus a "Wait for timeframe closes" option to show only completed periods. Only coarser-than-chart timeframes are offered.
- **Status vs upstream:** MIXED. `indicatorTimeframe.ts` is NEW (506 lines). `terminal.ts` MODIFIES upstream to wrap every registered descriptor (`applyTimeframeToAll`) after each indicator tier loads, filter the Timeframe dropdown to only coarser codes, and surface `indicator:data-status` errors as toasts.
- **Backend it depends on:** None new — folds higher-timeframe bars from existing history/feed. Uses `openalgo-charts` core (`mergeBars`, `zonedDayIndex`, `bucketStartOf`, etc.).
- **Frontend:** `lib/trading/indicatorTimeframe.ts` (exports `TF_KEY`, `TF_WAIT_KEY`, `withTimeframe`, `applyTimeframeToAll`, `buildHigherBars`, `mapToChart`, `hasOwnAttach`, `safeIntervalSeconds`); wired in `lib/trading/terminal.ts` (`applyIndicatorTimeframe`). Tests: `indicatorTimeframe.test.ts`, `terminalScriptStudies.test.ts`, `terminalChartHistory.test.ts`, `terminalHistory.test.ts`, `terminalAlerts.test.ts`, `terminal.test.ts`.
- **Desktop port notes:** `indicatorTimeframe.ts` is additive (new file). `terminal.ts` is a heavily-merged upstream file — merge-sensitive; the key integration points are the `applyIndicatorTimeframe()` calls in the indicator-loading path, the `onIndicatorsChange` signature change (added `indicatorId`), and the TF_KEY dropdown filtering. Depends on `openalgo-charts` exports being present.
- **Commits:** b31d29907, 0278e5cbd, 9d93e7624, e5f1ff6ef, f1c7d7209, 4093ff214.

### Indicator "nothing to plot / starvation" warning refinements
- **What it is (trader-facing):** When an added indicator produces no plottable data, the terminal warns once. Fork refines it so it does NOT false-warn on indicators that draw via a primitive with hidden plots (OI Profile) or that fetch their own data in `attach` (Writer Flow Totals), and asks the underlying study rather than the Timeframe wrapper.
- **Status vs upstream:** MODIFIES upstream `terminal.ts` (`warnIfStarved` now takes a descriptor; early-returns on all-hidden plots and on `hasOwnAttach`).
- **Backend it depends on:** None.
- **Frontend:** `lib/trading/terminal.ts`. Test: `indicatorStarvationWarning.test.ts`.
- **Desktop port notes:** Merge-sensitive (same upstream `terminal.ts`). Ties to the Timeframe and OI Profile/Writer Flow features.
- **Commits:** part of the indicatorTimeframe + OI Profile + Writer Flow work (4093ff214, 1fc8d95c1, b31d29907).

### Default-indicator-on-first-load (Volume)
- **What it is (trader-facing):** A brand-new browser with no saved chart layout opens with Volume on; a user who deliberately removed every indicator (persisted `[]`) does not get it back.
- **Status vs upstream:** MODIFIES upstream `terminal.ts` (`DEFAULT_INDICATORS`, distinguishes `raw === null` from explicit clear).
- **Backend it depends on:** None (localStorage `indicators`).
- **Frontend:** `lib/trading/terminal.ts`.
- **Desktop port notes:** Small additive block inside merge-sensitive upstream file.
- **Commits:** within terminal.ts fork changes (9c15e5082 range).

### OI Profile overlay toolbar toggle
- **What it is (trader-facing):** A one-click toolbar button (with a settings gear) toggling the "OI Profile" overlay — open interest and its change per strike, drawn on the price. Button only appears when the `oi_profile_live.js` custom indicator is installed on the server.
- **Status vs upstream:** MODIFIES upstream `ChartPane.tsx` (adds OI Profile toggle, install-detection fetch of `/custom-indicators/index.json`, instance lookup by `indicatorId`). Also MODIFIES `IndicatorSettingsDialog.tsx` (passes `symbol`, renders `expiries` input).
- **Backend it depends on:** `/custom-indicators/index.json` (served by `blueprints/custom_indicators.py`); the runtime indicator `strategies/indicators/oi_profile_live.js`; `blueprints/oiprofile.py` (profile endpoint, MAX_EXPIRIES=6); `/search/api/expiries` for the expiry picker; live OI over the WebSocket feed (see MarketDataManager feature). These are fork features in other sections.
- **Frontend:** `components/trading/ChartPane.tsx` (`toggleOiProfile`, `OI_PROFILE_ID`); `components/trading/IndicatorSettingsDialog.tsx` (`ExpiryPicker`, `underlyingOf`, `exchangesOf`). Tests: `ChartPane.actions.test.tsx`, `IndicatorSettingsDialog.expiries.test.tsx`.
- **Desktop port notes:** Merge-sensitive (shared upstream `ChartPane.tsx`/`IndicatorSettingsDialog.tsx`). Also requires the OI Profile indicator JS + backend blueprint + live-OI plumbing to be ported together, or the button self-hides.
- **Commits:** 62d6f1777, 6c4f672d2, f43a67c74, 544e2e04d, 4093ff214.

### Writer Flow + Option Levels toolbar toggles
- **What it is (trader-facing):** Two more toolbar buttons. "Writer Flow": where option writers are adding/covering/holding strike-by-strike through the day (price-strips + a totals pane, toggled together). "Option Levels": support/resistance zones derived from the Writer Flow record, moving minute by minute. Both only appear when `writer_flow_live.js` is installed.
- **Status vs upstream:** MODIFIES upstream `ChartPane.tsx` (`toggleWriterFlow`, `toggleLevels`, constants `WRITER_FLOW_ID`, `WRITER_FLOW_TOTALS_ID`, `WRITER_FLOW_FILE`, `LEVELS_ID`).
- **Backend it depends on:** `strategies/indicators/writer_flow_live.js` runtime indicator; `writer_flow_update` SocketIO event (via serverEvents); Writer Flow backend/page (`/writerflow`, tools.ts entry); `/custom-indicators/index.json`. Other-section fork features.
- **Frontend:** `components/trading/ChartPane.tsx`.
- **Desktop port notes:** Merge-sensitive. Depends on the Writer Flow indicator JS + its server event plumbing.
- **Commits:** 670372ef2 (writer-flow), 1fc8d95c1 (option levels).

### `expiries` indicator input type + ExpiryPicker
- **What it is (trader-facing):** A new indicator-setting control: a tick-list of the underlying's nearest option expiries (up to 6 picked of 8 shown), stored as a comma-separated string. Used by OI Profile. "Chart interval"/Auto-exchange aware; shows loading/failed/none states correctly.
- **Status vs upstream:** MIXED. `customIndicators.ts` MODIFIES upstream to add `'expiries'` to `INPUT_TYPES` and to pass `subscribeQuotes` + `onServerEvent` into every registered indicator. `IndicatorSettingsDialog.tsx` MODIFIES upstream to render the picker.
- **Backend it depends on:** `/search/api/expiries?exchange=&underlying=&instrumenttype=options`.
- **Frontend:** `lib/trading/customIndicators.ts`; `components/trading/IndicatorSettingsDialog.tsx` (`ExpiryPicker`, `underlyingOf`, `exchangesOf`, EXPIRY_CHOICES/MAX_PICKED_EXPIRIES). Tests: `customIndicators.test.ts`, `IndicatorSettingsDialog.expiries.test.tsx`.
- **Desktop port notes:** Merge-sensitive upstream files; the `INPUT_TYPES` + `register({...subscribeQuotes, onServerEvent})` additions are the integration seam for runtime indicators.
- **Commits:** 26d62553b, 164966a11, 6c4f672d2.

### Server-event bus for runtime indicators (serverEvents + useSocket wiring)
- **What it is (trader-facing):** Lets runtime chart indicators react to live broadcasts (new Writer Flow minute, new market read) without opening their own socket — so an open chart repaints when fresh data arrives.
- **Status vs upstream:** MIXED. `serverEvents.ts` is NEW. `useSocket.ts` MODIFIES upstream to hand its Socket.IO connection to `setServerEventSocket` and clear it on teardown/reconnect.
- **Backend it depends on:** SocketIO events `writer_flow_update`, `market_read_update` (the only allow-listed names). Emitted by fork backend (other sections).
- **Frontend:** `lib/serverEvents.ts` (`onServerEvent`, `setServerEventSocket`, `serverEventSocket`, `SERVER_EVENTS`); `hooks/useSocket.ts`. Passed to indicators via `customIndicators.ts`. Test: `serverEvents.test.ts`.
- **Desktop port notes:** `serverEvents.ts` additive. `useSocket.ts` is shared upstream — small merge (2 inserts). Allow-list is deliberately broadcast-only (no account/session events) for third-party-indicator safety.
- **Commits:** 164966a11, 5ad622d9c.

### Live open interest in MarketDataManager + `subscribeQuotes`
- **What it is (trader-facing):** The shared market-data cache now carries `oi` and `oi_updated_at` per instrument. Adds `subscribeQuotes(symbols, onTick)` — a plain-callback quote subscription over the one shared WebSocket for consumers that sample on their own beat or live outside React (OI Profile page, runtime indicators) without opening a second socket.
- **Status vs upstream:** MODIFIES upstream `MarketDataManager.ts` (OI fields merged on both WS tick and REST snapshot paths; new exported `subscribeQuotes` + `HostQuoteTick`).
- **Backend it depends on:** WebSocket proxy quote feed carrying `oi`; broker streaming adapters that emit OI.
- **Frontend:** `lib/MarketDataManager.ts`. Test: `MarketDataManager.test.ts`.
- **Desktop port notes:** Merge-sensitive upstream file; additive fields + one exported function. Underpins OI Profile overlay and `onServerEvent`-driven indicators.
- **Commits:** 26d62553b, d9744f116, 44ac4ff98.

### Right-rail side panels: TradeFinder, Market read, Boost strikes
- **What it is (trader-facing):** Three new right-rail panels on `/trading`: **TradeFinder** (Intraday Boost / Breakout Beacon / High Powered / Sectors lists), **Market read** (live sentences on who leads option writing, futures, levels, volatility), **Boost strikes** (badged names + liquid contracts to trade them). Plus market-read audible/visual alerts.
- **Status vs upstream:** MODIFIES upstream `RightRail.tsx` (3 new PANELS entries + icons) and `Trading.tsx` (lazy-mounts `BoostStrikesPanel`, `MarketReadPanel`, static `TradeFinderPanel`, calls `useMarketReadAlerts`).
- **Backend it depends on:** TradeFinder proxy endpoints (`tradefinderApi`), market-read backend + `market_read_update` SocketIO event, boost snapshot endpoints — all fork features in other sections. The panel components themselves (`BoostStrikesPanel`, `TradeFinderPanel`, `MarketReadPanel`), hook `useMarketReadAlerts`, and `api/tradefinder` live outside a5.
- **Frontend:** `components/trading/RightRail.tsx` (panel registry; exports `PanelId`, `isPanelId`); `pages/Trading.tsx` (mount points).
- **Desktop port notes:** Both are shared upstream files — merge-sensitive. The panel components/hooks/api clients are dependencies that must be ported alongside; `RightRail`/`Trading` only register and mount them.
- **Commits:** ec3dac7f3 (boost), 5ad622d9c (market read), 11fc9a709 (tradefinder/screener).

### Watchlist column sorting + TradeFinder "M Score" column + keyboard nav
- **What it is (trader-facing):** TradingView-style click-to-sort column headers (asc → desc → back to drag order, persisted per browser, unknown values sort last, drag disabled while sorted). New "M Score" column = TradeFinder R-factor per NSE cash symbol (merged from intraday_boost + sector_scope). Arrow-key row navigation.
- **Status vs upstream:** MODIFIES upstream `WatchlistPanel.tsx`.
- **Backend it depends on:** `tradefinderApi.getMarketPulse` + `getSectorScope` (fork TradeFinder proxy, other section).
- **Frontend:** `components/trading/WatchlistPanel.tsx` (`Sort`, `SortHeader`, `SORT_VALUE`, `readSort`, rFactor column, `rowRefs`). Test: `WatchlistPanel.test.tsx`.
- **Desktop port notes:** Merge-sensitive upstream file. The sort/keyboard-nav parts are self-contained; the M Score column depends on `api/tradefinder`.
- **Commits:** 970969c5f, 026828e1d, b6f5fefaa, c6b937604, f9a695e4e, 11fc9a709, 4093ff214.

### TradeFinder symbol alias (corporate-action remap)
- **What it is (trader-facing):** Maps TradeFinder's stale symbol name to the broker master's current one after a corporate action (e.g. TATAMOTORS → TMPV), so ranked-list symbols still resolve for history/option-chain calls.
- **Status vs upstream:** NEW (`tfSymbol.ts`). Python twin is `services/tf_symbol_alias.py` (other section).
- **Backend it depends on:** Parallels `services/tf_symbol_alias.py`.
- **Frontend:** `lib/trading/tfSymbol.ts` (`tradableSymbol`).
- **Desktop port notes:** Additive, trivial. Keep the two-copy (JS + Python) rule; table is hand-maintained.
- **Commits:** ec3dac7f3.

### New routes in App.tsx + tools.ts registry entries
- **What it is (trader-facing):** Two new tool pages reachable from `/tools` and routing: **Writer Flow** (`/writerflow`) and **Screener** (`/screener`, scan a watchlist/TradeFinder list with any chart indicator and filter/sort on its plots).
- **Status vs upstream:** MODIFIES upstream `App.tsx` (2 lazy imports + 2 `<Route>`s under `Layout`) and `tools.ts` (2 registry entries — the home page derives its tool count from this).
- **Backend it depends on:** Writer Flow + Screener page components (`pages/WriterFlow`, `pages/Screener`) and their backends (other sections).
- **Frontend:** `App.tsx`, `lib/tools.ts`.
- **Desktop port notes:** Both are the canonical merge-sensitive shared files (CLAUDE.md flags App.tsx + tools.ts). Per the "three registrations" rule each page also needs a `blueprints/react_app.py` route (check other section) — a5 only covers registration #1 (route) and the tools registry. Routes added: `/writerflow`, `/screener`.
- **Commits:** 670372ef2 (writerflow), 11fc9a709 (screener).

### chart_service.py typing cleanup
- **What it is:** Not a feature — a 1-line ruff/typing cleanup (`Dict, Tuple` → `Any`).
- **Status vs upstream:** MODIFIES upstream `services/chart_service.py`, cosmetic only.
- **Desktop port notes:** Ignore for porting; absorbed by any upstream merge.
- **Commits:** e989ff928.

---

**File accounting (all 28 in scope):**
- Per-indicator Timeframe: indicatorTimeframe.ts, indicatorTimeframe.test.ts, terminal.ts, terminal.test.ts, terminalScriptStudies.test.ts, terminalChartHistory.test.ts, terminalHistory.test.ts, terminalAlerts.test.ts
- Starvation warning: indicatorStarvationWarning.test.ts (+ terminal.ts)
- OI Profile toggle: ChartPane.tsx, ChartPane.actions.test.tsx, IndicatorSettingsDialog.tsx, IndicatorSettingsDialog.expiries.test.tsx
- expiries input: customIndicators.ts, customIndicators.test.ts (+ IndicatorSettingsDialog.tsx)
- Server-event bus: serverEvents.ts, serverEvents.test.ts, useSocket.ts (+ customIndicators.ts)
- Live OI / subscribeQuotes: MarketDataManager.ts, MarketDataManager.test.ts
- Right-rail panels: RightRail.tsx, Trading.tsx
- Watchlist sorting/M-Score: WatchlistPanel.tsx, WatchlistPanel.test.tsx
- TF alias: tfSymbol.ts
- Routes/registry: App.tsx, tools.ts
- Cleanup: services/chart_service.py

Unclassified files: none.

---

## Brokers, Config, Docs & App Chrome

Scope: all 62 files in `a6.txt`. Net fork changes measured with `git diff upstream/main...main`.

Two cross-cutting fork features drive almost every change here:
1. **Live open interest (`oi`) on the market-data feed** — every Noren/XTS/proto broker adapter now forwards per-contract OI, feeding the OI Profile live-OI overlay and Writer Flow.
2. **Historify / expired-F&O + historical-data capability model** — each `plugin.json` gains a `capabilities` block declaring how much history the broker can serve.

---

### Broker plugin.json changes

Every `plugin.json` in scope got the **same additive change**: a `capabilities` block (`futures_historical`, `options_historical`, `expired_contracts`) consumed by the fork-only `utils/plugin_loader.py` (cached at startup) and exposed via `GET /historify/api/broker/capabilities` (`blueprints/historify.py`) and the React `ExpiredFnoTab`. Several files were also whitespace-reformatted (single-line arrays expanded, missing trailing newline added, non-ASCII em-dash escaped) — cosmetic, no behaviour.

Default when the block is absent is `none/none/none`, so the block is purely declarative. Capability values per broker:

| Broker | futures_hist / options_hist / expired | What changed | Merge-sensitive? |
| --- | --- | --- | --- |
| upstox | full / full / **full** | capabilities added (only broker with full expired) | Low — additive key |
| zerodha | full / full / none | capabilities added | Low |
| fyers | full / full / none | capabilities added | Low |
| angel | full / limited / none | capabilities added + array reformat | Low |
| dhan | full / limited / none | capabilities added | Low |
| dhan_sandbox | full / limited / none | capabilities added | Low |
| kotak | full / limited / none | capabilities added | Low |
| deltaexchange | limited / limited / none | capabilities added + array reformat + em-dash escape | Low |
| aliceblue, compositedge, definedge, firstock, fivepaisa, fivepaisaxts, flattrade, ibulls, iifl, jainamxts, motilal, mstock, pocketful, rmoney, samco, shoonya, tradejini, zebu | limited / none / none | capabilities added (+ reformat on most) | Low |
| groww, indmoney, paytm | none / none / none | capabilities added | Low |
| **wisdom** | limited / none / none | **Uses key `historify_capabilities`, NOT `capabilities`** — plugin_loader reads `"capabilities"`, so wisdom silently falls back to the `none/none/none` default. Likely a fork typo. | Low, but **note the bug** when porting |

**Desktop port notes:** Purely additive JSON keys — port by copying the `capabilities` block into each plugin.json. The only logic that reads them is `utils/plugin_loader.py` + the historify blueprint/frontend (out of this scope but must be ported for the keys to matter). Fix wisdom's key name to `capabilities` during the port. Merges are safe: upstream never touches these keys, so no conflict expected on upstream pulls.

---

### Broker streaming-adapter changes

All MODIFY existing upstream adapters. Unifying theme: **publish per-contract open interest as `oi`, distinguishing "field not sent" (omit → client keeps last value) from a real `0` (forward it).** This feeds the OI Profile live-OI feature (`docs/oi-profile-live-oi.md`).

- `broker/aliceblue/streaming/aliceblue_mapping.py` — MODIFIES. Depth-frame parser emits `oi` (from Noren `oi`, the contract's own OI; `toi` is the underlying total and is not used). Present `0` forwarded, absent/empty skipped.
- `broker/aliceblue/streaming/aliceblue_adapter.py` — MODIFIES. Adds `oi` to the cache-merge field list and to depth publish payload.
- `broker/flattrade/streaming/flattrade_adapter.py` — MODIFIES. New `_oi_field()` helper; merged into Quote + Depth normalizers via `**_oi_field(data)`. Deliberately not `safe_int` (which would turn a parse failure into a fake 0).
- `broker/shoonya/streaming/shoonya_adapter.py` — MODIFIES. Same `_oi_field()` pattern on Quote + Depth; **removes** the old `"open_interest": safe_int(data.get("toi"))` (toi was the wrong/underlying value).
- `broker/zebu/streaming/zebu_adapter.py` — MODIFIES. Identical `_oi_field()` pattern (Quote + Depth). (flattrade/shoonya/zebu share the same Noren helper verbatim.)
- `broker/groww/streaming/groww_protobuf.py` — MODIFIES. Adds proto field 14 `openInterest` (fixed64) to the mini protobuf parser.
- `broker/groww/streaming/groww_adapter.py` — MODIFIES. Adds `oi` to the LTP cache field tuple; publishes `oi` from `open_interest` (proto3 omits zero, so absent stays absent).
- `broker/samco/streaming/samcoWebSocket.py` — MODIFIES. `open_interest` becomes `None` when no frame has carried `oI` yet (vs a real 0), so the adapter can tell "not sent" from zero.
- `broker/samco/streaming/samco_adapter.py` — MODIFIES. New static `_oi_field()`; merged into Quote (mode 2) and Snap-Quote (mode 3) results.
- `broker/upstox/streaming/upstox_adapter.py` — MODIFIES, **largest streaming change**. Four distinct fixes:
  1. **O(1) tick routing** — new `_subs_by_instrument` / `_subs_by_token` indexes replace a linear scan over all subscriptions on every tick (was ~100k dict comparisons/sec under the lock at 240+ symbols).
  2. **Multi-mode unsubscribe fix (14-Oct-2026 incident)** — Upstox keeps one mode per instrument key; an unsub for one mode used to silence other subscribers of the same key. Now unsubs upstream only when nobody is left, else downgrades to the richest mode still needed (`_effective_upstox_mode`, `full` > `ltpc`).
  3. **`oi` extraction** — new `_extract_oi()` merged into quote + depth payloads.
  4. **prev_close / LTP hardening** — LTP-mode now skips publishing when no `ltpc` or LTP ≤ 0 (kills the "Missing LTP value" warning storm); adds `prev_close` (from `ltpc.cp`, never the live `ohlc.close`).
- `broker/upstox/streaming/upstox_client.py` — MODIFIES. (a) Health-check/ping constants now sourced from a new shared module `websocket_proxy/ws_liveness.py` (**out of this scope but a hard port dependency**), replacing hard-coded 30s/90s. (b) Subscribe now uses Upstox `change_mode` for a key already held in another mode (vs `sub`), and fixes the per-mode key-cap accounting so a key *moving* modes isn't double-counted against the cap.

**Desktop port notes:** All additive except shoonya (removed a wrong `open_interest` line — carry that removal over) and the upstox unsubscribe rewrite (replaces upstream's unsub body — a real merge point, re-apply by hand). The Noren `_oi_field()` helper is duplicated verbatim across flattrade/shoonya/zebu; port as-is. **upstox_client.py requires `websocket_proxy/ws_liveness.py`** to exist first.

---

### Non-streaming broker API changes (upstox)

- `broker/upstox/api/data.py` — MODIFIES, large (+252). Adds a module-level **history chunk cache** (`_CHUNK_CACHE`, OrderedDict+Lock, LRU 512, 15s TTL for today-chunks / 6h for finalized past chunks) to stay under Upstox's 500 req/min & 2000 req/30min quotas (chart+scanner were issuing ~7k/hr at open). `get_history()` splits chunks so none spans past+today. Also: `_previous_close()` helper (trust-ordered fallback for null `prev_close_price` around the open), one safe GET retry on connection reset, 3-attempt per-chunk retry with backoff, `BACKGROUND_HISTORY_PER_MINUTE=15` throttle constant. `prev_close` now never falls back to the live `ohlc.close`.
- `broker/upstox/api/order_api.py` — MODIFIES, tiny. `logger.error`→`logger.exception` and keeps the `.status` contract on HTTPStatusError so `place_order_service` surfaces the broker's real rejection (e.g. UDAPI1154 "no static IP") instead of a generic 500.
- `broker/upstox/database/master_contract_db.py` — MODIFIES, small. Dedups the contract master on `(symbol, exchange)` preferring `instrumenttype='EQ'`, so a rights/NCD tranche sharing a display symbol (MOTHERSON, CHOLAFIN, …) can't shadow the real equity and silently subscribe the WS feed to a dead instrument.

**Desktop port notes:** All three are self-contained within upstox and additive to method bodies; port directly. `expired_data.py` files (`broker/upstox/api/expired_data.py` +332, `broker/dhan/api/expired_data.py` +691) are NEW and part of the same expired-F&O feature but **were not in this scope's file list** — flag for whoever owns the historify/expired section.

---

### Config & build

- **`.sample.env`** — adds (all opt-in, commented, default-off) blocks for the fork features:
  - `TF_BOOST_SNAPSHOT_ENABLED`, `TF_BOOST_DATABASE_PATH` — TradeFinder/Intraday Boost snapshot recorder (separate DuckDB).
  - `OI_FLOW_RECORDER_ENABLED`, `OI_FLOW_UNDERLYINGS`, `OI_FLOW_STRIKES_AROUND_ATM`, `OI_FLOW_DATABASE_PATH`, `OI_FLOW_ON_DEMAND_MAX`, `MARKET_READ_UNDERLYING` — Writer Flow / Market-read recorder.
  - `WS_STALE_TICK_WARN_SECONDS`, `WS_STALE_TICK_RECOVER_SECONDS`, `WS_SYMBOL_STALE_SECONDS`, `WS_SYMBOL_STALE_FRACTION`, `WS_SYMBOL_STALE_MIN_SYMBOLS` — WebSocket-proxy stale-feed detection and self-heal (rebuild+replay at the ~3AM rollover failure; majority-stale detection from the 2026-07-22 incident).
  - Version-stamp bump 2026-05-03 → 2026-05-09.
- **`pyproject.toml`** / **`requirements.txt`** / **`requirements-nginx.txt`** — new deps: `redis==5.2.1`, `openpyxl==3.1.5/>=`, `playwright==1.63.0/>=1.60.0`, `tqdm==4.70.0/>=4.67.3`. playwright = TF JWT keep-alive browser login; openpyxl/tqdm = security-audit skill xlsx export + progress bars; redis = (dep only). nginx variant keeps the eventlet-pin prose.
- **`.github/workflows/ci.yml`** — adds to the CI-safe pytest subset: `test_stream_oi.py`, `test_oi_profile_multi_expiry.py`, `test_upstox_get_reset_retry.py`. Adds two frontend-CI steps that validate + `node --test` the three shipped runtime indicators (`oi_profile_live.js`, `open_range_breakout.js`, `writer_flow_live.js`) against the pinned openalgo-charts (nothing else runs them, since they're runtime-loaded not bundled).
- **`.gitignore`** — adds ignores for: Writer Flow DuckDB writer-lock (`*.duckdb.writer`), TradeFinder Playwright profile + `tf_jwt.txt`, **proprietary ISI strategy source** (`strategies/isi_v56_*`, `test_isi_v56_*` — must never hit the public fork), `graphify-out/`, `backtesting/`, `SURVIVOR_PRO_PACKAGE/`, survivor state/results, `log/*.out|*.log|errors.jsonl|tf_boost_*`, `.october/`, `.playwright-mcp/`, `worktrees/`, assorted venv/pem/key.
- **`app.py`** — encodes the fork's runtime wiring (+116):
  - New blueprints registered: `writer_flow_bp`, `tradefinder_proxy_bp`, and `signals_bp` (CSRF-exempt, separate subscriber-auth surface).
  - **ProxyFix** gated on `TRUST_PROXY_HEADERS` (`x_for=1, x_proto=1`; `x_host` deliberately not trusted) — the per-IP login-lockout fix.
  - New DB init: Signal DB.
  - New schedulers/warm-ups in `setup_environment`: TF Boost snapshot, OI Flow recorder (`OI_FLOW_RECORDER_ENABLED`), signal summary, TF JWT keep-alive; plus NSE OI bhavcopy warm-up in the background cache thread.
  - `import utils.exit_after_cleanup` first (atexit ordering), `utils.gc_freeze.freeze_long_lived("startup")` after cache restore, and `allow_unsafe_werkzeug=True` on the dev `socketio.run`.
- **`CLAUDE.md`** — adds only the "Downstream consumer: openalgo-chart" section (points at the separate chart UI + the `additionalDirectories` setting). Nothing else.

**Desktop port notes:** `.sample.env` keys are additive/opt-in — port the blocks. app.py changes are new-blueprint registrations + gated middleware + scheduler starts, each wrapped in try/except, so additive; **but each references a fork-only module** (`utils.exit_after_cleanup`, `utils.gc_freeze`, `utils.ip_helper._trust_proxy_headers`, `services.tf_settings`, `services.oi_flow_recorder_service`, `services.signal_summary_service`, `services.tf_jwt_keepalive_service`, `services.nse_oi_bhavcopy`, `database.signal_db`, the three blueprints) that must be ported alongside. app.py is a frequent upstream-merge conflict file — re-apply by hand. The ISI `.gitignore` entries protect proprietary source; keep them. CI steps depend on the three indicator files + `chart-indicator/validate.mjs` existing.

---

### Docs added

All NEW markdown documenting fork features (plus one 1-line edit and an INDEX update). No upstream equivalent, so zero merge risk — pure additions.

| Doc file | Documents fork feature |
| --- | --- |
| `docs/oi-profile-anchors.md` (+208) | OI Profile: where "Change in OI" is anchored (NSE F&O bhavcopy / `nse_oi_bhavcopy.py`, fallbacks) |
| `docs/oi-profile-live-oi.md` (+96) | OI Profile: live OI from the broker feed + per-broker capability layering (the streaming-adapter `oi` work above) |
| `docs/oi-profile-chart.png` (bin) | Image asset for the OI Profile docs |
| `docs/writer-flow.md` (+325) | Writer Flow feature: per-strike per-minute option-writer OI/premium record |
| `docs/writer-flow-guide.md` (+168) | Writer Flow: how to read it on the chart (colours, streak bars, markers) |
| `docs/market-read.md` (+76) | Market-read `/trading` panel (reads writers/futures/levels/vol each minute, alerts) |
| `docs/custom-indicators.md` (+181) | Writing runtime custom chart indicators in `strategies/indicators/` |
| `docs/CHANGELOG.md` (+461) | Accumulated fork feature/fix stanzas (login-lockout proxy fix, Market read, Writer Flow, live OI, etc.) under `[Unreleased]` |
| `docs/INDEX.md` (+7) | Index rows linking the new docs (also references `tf-boost-monday-runbook.md` + `option-levels.md` — NOT in this scope) |
| `docs/prompt/services_documentation.md` (±1 line) | Expands the `oi_profile_service.py` row describing the bhavcopy anchor + fallbacks |
| `research/trademonkey.in.md` (+524) | Research notes on trademonkey.in V2 (captured via Playwright 2026-09-10) — competitor/feature research, not product code |

**Desktop port notes:** Port the docs that correspond to features being ported; `INDEX.md` links to two docs outside this scope (`tf-boost-monday-runbook.md`, `option-levels.md`) — ensure those are brought in or the links dangle. `research/trademonkey.in.md` is pure notes, port only if desired.

---

### Login / Footer / chrome

- `frontend/src/components/icons/GithubIcon.tsx` — NEW. Small local inline-SVG GitHub mark (lucide-react dropped brand/logo icons in its v1 major with no replacement). Styled to lucide's `className`/`currentColor` conventions.
- `frontend/src/pages/Login.tsx` — MODIFIES. Swap `Github` (lucide) → local `GithubIcon`.
- `frontend/src/components/layout/Footer.tsx` — MODIFIES. Same swap.

**Desktop port notes:** Trivial, self-contained, additive (one new file + two import swaps). Low merge sensitivity. This is a lucide-version workaround, not a product feature — only needed if the fork's lucide-react major differs from upstream's; otherwise skip.

---

### Unclassified files

None — all 62 files in `a6.txt` accounted for.

---

## Backend Infrastructure & Shared Services

Scope: 73 files. Almost all are upstream files the fork MODIFIED; a handful are new files the fork ADDED. The merge-sensitive ones are the modified upstream files (they collide on every Desktop upstream update); the new files are additive and only need re-copying.

Recurring theme across this section: the fork makes the websocket proxy run **out-of-process by default even on the dev server**, then adds a second ZMQ "bridge" so in-Flask consumers still see ticks; it adds **aggressive stale-feed self-healing** to the proxy; it adds **SEBI SPAN / broker-API margin** to the sandbox (analyzer) engine with an audit trail; and it adds assorted **FD/GC/shutdown hardening**. A large fraction of the line-count in `server.py`, `data_schemas.py`, `telegram_bot_service.py` and the `services/*.py` one-liners is pure ruff reformatting / `typing` import cleanup (merge noise, not behavior).

### Market-data & quotes/history services

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `services/market_data_bridge.py` | Whole new module. Binds a 2nd ZMQ SUB on `ZMQ_MDS_PORT` (default 5557) in the Flask process; every broker publisher additionally connects its PUB there so the sandbox/analyzer engine + MTM get ticks even though the proxy runs as a subprocess. Topic parsing mirrors `server.py:zmq_listener`. Started only in subprocess mode (else double-delivery). | NEW | No (additive), but tightly coupled to `base_adapter`/`app_integration`/`server.py` topic format | Out-of-process proxy default; sandbox tick feed |
| `services/history_service.py` | Background-vs-foreground history pacing rewrite: a background caller (TradeFinder enrichment, Writer Flow fill, Historify download) "gives way" to chart requests for up to `_BACKGROUND_YIELD_MAX` (10s), plus a per-broker per-minute budget (`BACKGROUND_HISTORY_PER_MINUTE` read off the broker data module) to stop background work exhausting the broker's request allowance. Honors `stream_registry.should_stop()` so a stuck wait can't hold shutdown for minutes. | MODIFIES | Yes (core request-pacing logic; high collision risk) | TradeFinder/Boost, Writer Flow, Historify (all fork features) |
| `services/quotes_service.py` | `get_multiquotes_with_auth` now distinguishes broker throttling (429/"too many request"/"rate limit") from a 500, returning HTTP 429 + `error_type: rate_limit` so clients back off instead of retrying into the limit. | MODIFIES | Low (isolated except-branch) | Feed/rate-limit stability (helps chart app + scanners) |
| `services/market_data_service.py` | Only `typing` import cleanup (`Dict/List/Set/Tuple` -> builtins). | MODIFIES | Trivial (merge noise) | none (plumbing) |
| `services/ping_service.py`, `services/search_service.py`, `services/symbol_service.py`, `services/telegram_alert_service.py`, `services/websocket_service.py` | Each: one-line `typing` import cleanup only, no behavior change. | MODIFIES | Trivial (merge noise) | none (plumbing) |
| `services/telegram_bot_service.py` | `TYPE_CHECKING` guard for lazily-imported `telegram`/`openalgo` SDK, `zip(..., strict=False)` and quote-style fixes. Mostly lint; the TYPE_CHECKING block is a small genuine change. | MODIFIES | Low | none (plumbing / lint) |

### websocket_proxy & streaming infra

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `websocket_proxy/server.py` | **The biggest change in this section (+593/-81).** Adds full stale-feed SELF-HEALING: per-user last-tick tracking, born-dead detection (`_subscription_started_at`), disconnected-grace timer, **majority-stale** per-symbol detection (`_check_symbol_level_staleness`), and `_recover_stale_adapter()` which tears down + rebuilds a connected-but-silent adapter with fresh tokens and replays every subscription, with exponential backoff. Tighter thresholds (warn 30s, heal 60s vs upstream 120s warn-only). Adds **client linger**: `cleanup_client` defers teardown by `WS_CLIENT_LINGER_SECONDS` (20s) so a page reload doesn't churn the broker feed; client ids changed from `id(websocket)` to a monotonic counter (reuse bug with deferred teardown). Unsubscribe-without-mode now unsubscribes every held mode. authenticate_client won't evict a shared adapter when other clients are active. Rest is ruff reformatting. | MODIFIES | **Very high** — largest, most-rewritten file here; many interleaved real changes + reformatting make merges painful | Feed reliability for chart app; out-of-process proxy |
| `websocket_proxy/base_adapter.py` | Adds `get_mds_bridge_endpoint()` + `_connect_to_mds_bridge()`: publishers additionally connect their PUB to the Flask-process MDS bridge (best-effort, never fails adapter startup). Avoids legacy 5556 fallback port. | MODIFIES | Medium (inserts into `_connect_to_zmq_bus`) | MDS bridge / sandbox ticks |
| `websocket_proxy/connection_manager.py` | `SharedZmqPublisher.connect` also calls `_connect_to_mds_bridge`; debug->info log. | MODIFIES | Low | MDS bridge |
| `websocket_proxy/app_integration.py` | `resolve_proxy_mode()` now returns `subprocess` by default even on the dev server (`WEBSOCKET_PROXY_SUBPROCESS=false` opts out) for GIL isolation from pandas-heavy /history threads; starts the MDS bridge after spawning the subprocess (skipped under eventlet). | MODIFIES | Medium (changes default runtime topology) | Out-of-process proxy default; MDS bridge |
| `websocket_proxy/ws_liveness.py` | New module: shared broker-WS liveness constants (ping 5s/4s, health 5s, data-timeout 30s) replacing per-adapter hardcoded 30/10/90, with import-time `_sanity()` asserts. Intended to be imported by broker streaming adapters. | NEW | No (additive), but adapters must opt in | Faster dead-socket detection (feed reliability) |
| `websocket_proxy/__init__.py` | Removes top-level `from .server import WebSocketProxy/main` and the `__all__` entries (so importing the package no longer imports server — needed because server now runs as a subprocess). | MODIFIES | Low-Medium (import-surface change) | Out-of-process proxy |

### Sandbox engine

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `sandbox/fund_manager.py` | **Standalone behavior change.** `calculate_margin_required` for naked option SELL now uses a priority chain: real broker SPAN API (`_try_broker_margin_api` via `services.margin_service`) -> SEBI SPAN approximation (`_calculate_span_approx_margin`, using `span/min/exposure_margin_pct` config + live underlying price) -> legacy leverage formula. Legacy formula unchanged for everything else. | MODIFIES | Medium-High (rewrites a core sandbox method) | Analyzer SEBI-compliant margin |
| `sandbox/order_manager.py` | **Standalone behavior change (+454/-42).** Adds broker-basket margin for derivatives in analyzer mode: `precheck_orders`, projected-basket margin diff (`_project_margin_positions`, `_calculate_broker_margin_for_positions`), `analyzer_broker_margin_mode` (strict vs fallback), and writes a `margin_source` + JSON `margin_snapshot` audit trail onto every order. The old inline reduce/reverse margin logic is preserved in the `internal` branch. | MODIFIES | High | Analyzer broker margin + audit |
| `sandbox/execution_engine.py` | Copies `margin_source`/`margin_snapshot` from order onto the position on every open/reopen/add/reverse path (`hasattr` guarded). | MODIFIES | Medium | Analyzer margin audit |
| `sandbox/websocket_execution_engine.py` | Adds refused-subscription retry: `_refused_users`/`_mark_refused`/`_retry_refused_subscriptions`, driven by the health monitor every `RESUBSCRIBE_SECONDS` (15s) — handles OpenAlgo starting before broker login. | MODIFIES | Medium | Sandbox feed resubscribe (depends on MDS bridge) |

### Database & migrations

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `database/sandbox_db.py` | Adds `margin_source`/`margin_snapshot` columns to `SandboxOrders` + `SandboxPositions`, a `_migrate_margin_audit_columns()` startup migration, and 5 new config rows (`analyzer_broker_margin_mode`, `option_sell_margin_mode`, `span/min/exposure_margin_pct`) + reworded `option_sell_leverage`. | MODIFIES | High (schema + seed config; collides with upstream sandbox config changes) | Analyzer broker/SPAN margin |
| `database/auth_db.py` | Adds `_migrate_auth_columns()` (ALTER-adds feed_token/user_id/secret_api_key/primary_ip/.../aux_param1-4 on existing DBs); `log_login_attempt` now JSON-serializes a non-str `failure_reason` (SQLite binding fix). | MODIFIES | Medium | Broker-session/IP plumbing + login audit robustness |
| `database/token_db_enhanced.py` | Symbol lookup now prefers `instrumenttype='EQ'` when multiple rows share `(symbol, exchange)` — both the cache snapshot index and the DB-fallback queries (`_query_symtoken_preferring_eq`). Fixes equities silently resolving to rights/NCD tranches (MOTHERSON, CHOLAFIN...). | MODIFIES | Medium (edits hot lookup path) | Correct quotes/feed for affected equities (helps all surfaces) |
| `database/traffic_db.py` | `log_request` JSON-serializes a non-str `error` (same SQLite binding fix as auth_db). | MODIFIES | Low | Traffic-log robustness |
| `database/master_contract_cache_hook.py` | After master-contract load, calls `utils.gc_freeze.freeze_long_lived()` to exclude the long-lived contract objects from full GC. | MODIFIES | Low (small insert) | GC pause reduction |
| `upgrade/migrate_sandbox_margin_audit.py` | New standalone migration (upgrade/ path) adding the margin audit columns to sandbox_orders/positions, `--status` supported. | NEW | No | Analyzer margin audit |
| `upgrade/migrate_all.py` | Registers the new migration in `MIGRATIONS`. | MODIFIES | Low (one-line list insert, easy conflict) | Analyzer margin audit |

### utils & app plumbing

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `utils/gc_freeze.py` | New: `freeze_long_lived()` — `gc.unfreeze()/collect()/freeze()` under a real lock to keep full-GC pauses short in a process holding ~600k long-lived objects. | NEW | No | Latency/GC hardening |
| `utils/exit_after_cleanup.py` | New: registers a last-of-all `atexit` hook that `os._exit(code)` after all other hooks, to dodge a DuckDB C++ static-destructor abort on Ctrl+C. Imported first in app.py. | NEW | No (but app.py must import it first) | Clean dev-server shutdown (Historify/DuckDB) |
| `utils/shutdown.py` | Adds `_stop_oi_flow_recorder` to the shutdown sequence; `_handle_signal` calls `exit_after_cleanup(128+signum)`; registers **SIGHUP** (orphaned-proxy-port fix for closed terminals). | MODIFIES | Medium | OI Flow recorder, clean shutdown, out-of-process proxy |
| `utils/httpx_client.py` | HTTP/2 now OFF by default in all modes (`HTTPX_HTTP2=1` to re-enable) due to httpcore sync-h2 ReadError storms under eventlet; adds `_request_with_retry` (one replay of idempotent GET/HEAD/OPTIONS on transport errors). | MODIFIES | Medium (edits shared client factory; affects all brokers) | Feed/history reliability (chart app depends on this) |
| `utils/plugin_loader.py` | Adds a `capabilities` block (futures/options_historical, expired_contracts) to each broker's loaded plugin data + `get_all_broker_historify_capabilities()` for the `/historify/api/broker/capabilities` endpoint. | MODIFIES | Low | Historify / expired-F&O UI |
| `utils/db_sessions.py` | Registers `database.signal_db` scoped session for teardown (one-line). | MODIFIES | Low | Signal strategy DB (fork feature) + FD hygiene |

### blueprints & restx_api base

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `restx_api/__init__.py` | Registers 6 new namespaces: `boost_snapshots`, `boost_movement`, `tf_jwt_keepalive`, `tf_market_pulse`, `tf_sector_scope`, `nse_events`; also reorders `sip` import (alpha). | MODIFIES | Medium (import list + add_namespace; conflicts whenever upstream adds a namespace) | TradeFinder/Boost, NSE events (fork features) |
| `restx_api/data_schemas.py` | Adds schemas `BoostSnapshotsSchema`, `BoostMovementSchema`, `TfMarketPulseSchema`, `TfSectorScopeSchema`, `NseEventsSchema`, `TfJwtKeepAliveSchema`. Rest is ruff reflow of existing schemas (merge noise). | MODIFIES | Medium (real adds + heavy reformat) | TradeFinder/Boost, NSE events |
| `blueprints/react_app.py` | Adds `/writerflow` and `/screener` React catch-all routes (the "2nd registration" from CLAUDE.md). | MODIFIES | Low | Writer Flow, Screener pages |
| `blueprints/sandbox.py` | Exposes `analyzer_broker_margin_mode` in the config UI API (new "Risk Engine Settings" group), defaults, reset, and `validate_config` for it. | MODIFIES | Medium | Analyzer broker margin (UI side) |

### Frontend (in-scope)

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `frontend/src/pages/Sandbox.tsx` | Adds a select renderer for `analyzer_broker_margin_mode` (strict / fallback). | MODIFIES | Low | Analyzer broker margin UI |
| `frontend/src/api/writer-flow.ts` | New API client + types for the Writer Flow OI-recorder endpoints. | NEW | No | Writer Flow |
| `frontend/src/api/oi-profile.ts` | Adds per-leg OI (`OIProfileLeg`), multi-expiry summing, window/strike-count params, `market_open`, `oi_change_pending`. | MODIFIES | Low-Medium | OI Profile live-feed (fork feature) |
| `frontend/src/types/plotly.d.ts` | Adds `onSelected`/`onDeselect` to the react-plotly typing. | MODIFIES | Trivial | Plotly-based tools |
| `frontend/src/test/setup.ts` | Adds a Node-25+ Web Storage polyfill for jsdom tests. | MODIFIES | Low | Test infra |
| `frontend/vite.config.ts` | Dev-server proxies `/historify` and `/watchlist` to :5000. | MODIFIES | Low | Screener / Historify dev workflow |
| `frontend/package.json` | Bumps html2canvas-pro, lucide-react, @types/node, jsdom, cssnano, jest-axe, testing-library/jest-dom; adds `plotly.js` devDep. | MODIFIES | Medium (lockfile/dep conflicts on merge) | Tools (plotly), general |

### chart-indicator skill & gthread review lists

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? | Supports which fork feature |
| --- | --- | --- | --- | --- |
| `.claude/skills/chart-indicator/SKILL.md` | Documents the per-study Timeframe wrapper on `/trading`. | MODIFIES | Low | Chart indicators / timeframe |
| `.claude/skills/chart-indicator/validate.mjs` | Adds OpenAlgo-only `expiries` input type to the validator's allow-sets. | MODIFIES | Low | `expiries` input (OI tools) |
| `.claude/skills/chart-indicator/coverage.mjs` | Adds `input 'expiries'` capability row. | MODIFIES | Low | same |
| `.claude/skills/chart-indicator/reference/cookbook.md` | Rewrites input-types + fills sections to reflect new semantic input types (session/timeframe/symbol/price/time/expiries) and real `gradient` fills. | MODIFIES | Low | Chart indicators |
| `scripts/gthread_check_then_act_reviewed.txt` | Adds `tf_jwt_keepalive_service` lazy-init entry (reviewed-benign allowlist). | MODIFIES | Low | TF JWT keepalive |
| `scripts/gthread_sleep_reviewed.txt` | Adds reviewed-benign sleep entries for signal broadcast, history pacing, background budget, historify_db connect. | MODIFIES | Low | Signal strategy, history pacing, historify |

### Tests (all pin fork behavior above)

| File | What the fork changed | NEW/MODIFIES | Merge-sensitive? |
| --- | --- | --- | --- |
| `test/sandbox/test_broker_margin_path.py` | NEW — sandbox broker/SPAN margin path | NEW | No |
| `test/test_sandbox_feed_resubscribe.py` | NEW — refused-subscription retry | NEW | No |
| `test/test_history_background_budget.py` | NEW — background history budget/give-way | NEW | No |
| `test/test_gthread_rest_history_limiter.py` | MODIFIES — history pacing under gthread | MODIFIES | Low |
| `test/test_httpx_retry.py` | NEW — idempotent GET retry | NEW | No |
| `test/test_ws_stale_feed_recovery.py`, `test/test_ws_client_linger.py`, `test/test_websocket_unsubscribe_contract.py` | stale-recovery / linger / unsubscribe-mode contract | NEW (2) + MODIFIES (1) | Low |
| `test/test_stream_oi.py` | NEW — OI stream | NEW | No |
| `test/test_upstox_get_reset_retry.py`, `test/test_upstox_prev_close.py`, `test/test_upstox_stream_modes.py` | NEW — Upstox adapter behavior | NEW | No |
| `test/test_gc_freeze.py`, `test/test_exit_after_cleanup.py`, `test/test_graceful_shutdown.py` | GC freeze / exit-after-cleanup / shutdown | NEW (2) + MODIFIES (1) | Low |
| `test/test_eventlet_cross_thread_locks.py` | MODIFIES — eventlet cross-thread lock suite (extended) | MODIFIES | Low |
| `test/test_gthread_core_proxy_mode.py` | MODIFIES — proxy-mode resolution default | MODIFIES | Low |
| `test/test_isi_v56_per_symbol_locks.py` | NEW — ISI strategy per-symbol locks (fork strategy) | NEW | No |
| `frontend/e2e/indicator-timeframe.spec.ts` + `frontend/e2e/harness/{levels,mtf}.{html,ts}` | NEW — Playwright e2e + harness pages for the per-study Timeframe wrapper on /trading | NEW | No |

### Desktop port notes

**Must be re-applied carefully on every upstream Desktop update (merge-sensitive modified upstream files):** `websocket_proxy/server.py` is by far the hardest — it's a near-rewrite mixing real stale-recovery/linger logic with wholesale ruff reformatting, so expect conflicts every update; treat it as "re-port the behaviors" not "git merge". Next hardest: `sandbox/order_manager.py`, `sandbox/fund_manager.py`, `database/sandbox_db.py` (schema + seed config), and `services/history_service.py` (request-pacing core). Medium: `websocket_proxy/app_integration.py`/`base_adapter.py`/`connection_manager.py` (proxy-topology triad — keep them consistent with `services/market_data_bridge.py`), `utils/httpx_client.py`, `utils/shutdown.py`, `database/auth_db.py`, `database/token_db_enhanced.py`, `sandbox/execution_engine.py`, `blueprints/sandbox.py`, `restx_api/__init__.py` + `data_schemas.py` (conflict whenever upstream adds a namespace/schema), `upgrade/migrate_all.py` (one-line list). Low/trivial but still collision-prone: `frontend/package.json` (dep bumps), and the many `services/*.py` files whose only change is `typing` import cleanup — those can simply be re-skipped/accepted from upstream since they carry no fork behavior.

**Additive new files (just re-copy, no merge):** `services/market_data_bridge.py`, `websocket_proxy/ws_liveness.py`, `utils/gc_freeze.py`, `utils/exit_after_cleanup.py`, `upgrade/migrate_sandbox_margin_audit.py`, `frontend/src/api/writer-flow.ts`, all the NEW `test/*` files, and the e2e harness files. These depend on the modified files above (e.g. the bridge is useless without the proxy-topology + base_adapter changes; gc_freeze/exit_after_cleanup must be wired from master_contract_cache_hook/shutdown/app.py), so copy the file AND verify its one or two call-sites survived the merge.

---

