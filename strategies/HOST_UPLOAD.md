# Upload Guide: breakout_intraday_strategy

## On http://127.0.0.1:5000/python

### Step 1: Click "Add Strategy"

| Form field | Value |
|---|---|
| Name | breakout_intraday |
| File | /Users/Shared/Project/openalgo/strategies/breakout_intraday_strategy.py |
| Exchange | NSE |

### Step 2: Parameters (key=value, one per row)

| Key | Value | Purpose |
|---|---|---|
| MODE | live | live execution (sandbox/real per UI analyzer toggle) |
| SECTOR_ONLY_MODE | true | use TF Sector Scope as watchlist (no IntradayBoost JWT needed at startup) |
| SECTOR_TOP_N_LONG | 2 | top 2 gaining sectors for LONG candidates |
| SECTOR_TOP_N_SHORT | 2 | top 2 losing sectors for SHORT candidates |
| SECTOR_TOP_STOCKS_N | 4 | top 4 stocks by r_factor per active sector |
| SECTOR_MIN_BREADTH | 65 | skip incoherent sectors (< 65% stocks aligned) |
| SECTOR_STOCK_ALIGN | true | skip stocks moving against their sector |
| TARGET_RR | 2.0 | fixed 2R profit target |
| TRAIL_MODE | after_1R | trail SL after reaching +1R |
| TRAIL_STEPS_ENABLED | false | disable step-lock trail (fixed 2R is robust default) |
| BREAKEVEN_START_R | 0 | breakeven off (conflicts with fixed 2R target) |
| CAPITAL_PER_TRADE | 50000 | notional per trade (Rs. 50k) |
| LEVERAGE | 5 | MIS leverage (Rs. 10k margin per trade) |
| MAX_OPEN_POSITIONS | 15 | max concurrent positions |
| EXIT_TIME | 1510 | square-off all at 15:10 HHMM |
| ADX_THRESHOLD | 20 | minimum ADX for signal (20 = relaxed, 25 = strict) |
| USE_VWAP | false | VWAP filter off (sector filter replaces it) |
| DRY_RUN | false | real orders to OpenAlgo (sandbox if analyzer ON) |
| REQUIRE_ANALYZER | true | abort if OpenAlgo analyzer/sandbox mode is OFF |
| LOG_LEVEL | INFO | logging verbosity |
| SECTOR_POLL_SEC | 180 | sector data refresh interval (seconds) |
| SECTOR_TRUST_AFTER | 0930 | ignore noisy pre-9:30 sector data |

### Step 3: Schedule

| Field | Value |
|---|---|
| Start time | 09:10 IST |
| Stop time | 15:35 IST |
| Days | Mon, Tue, Wed, Thu, Fri |

Start at 09:10 so the TF JWT auto-refresh and sector poller are ready before 09:15 open.

### Step 4: Click Upload, then Start

Logs stream to the strategy log panel in the UI. The strategy self-captures sector
snapshots to logs/breakout/sector_snapshots_YYYY-MM-DD.jsonl for replay.

To stop gracefully: click Stop in the UI — sends SIGTERM, waits up to 15s for clean shutdown.

## Safety checklist before starting

- [ ] OpenAlgo running (`uv run app.py`)
- [ ] Upstox broker logged in (token refreshed — expires ~3 AM IST daily)
- [ ] Analyzer/sandbox mode ON in OpenAlgo UI (REQUIRE_ANALYZER=true will abort if not)
- [ ] Mac stays awake 09:10-15:35

## Notes

- Live vs sandbox is decided by the OpenAlgo UI analyzer toggle, not this strategy
- STRATEGY_ID injected by host disables local file logging (stdout only under host)
- TF JWT is auto-refreshed by tf_auth.py using the persistent Google OAuth browser profile
  (.tf_browser_profile/) — no manual token paste needed during the session
- Run replay after market: `OPENALGO_API_KEY=... uv run python strategies/sector_replay.py --date YYYY-MM-DD`
- Run 15-day report: `uv run python strategies/sector_report.py --capital 300000`
