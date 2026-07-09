# Graph Report - /Users/bond7/Desktop/Project/openalgo/blueprints  (2026-04-18)

## Corpus Check
- 42 files · ~58,219 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 929 nodes · 1205 edges · 37 communities detected
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 11 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]

## God Nodes (most connected - your core abstractions)
1. `serve_react_app()` - 76 edges
2. `save_configs()` - 19 edges
3. `stop_strategy_process()` - 17 edges
4. `get_username_from_session()` - 17 edges
5. `start_strategy_process()` - 13 edges
6. `verify_strategy_ownership()` - 12 edges
7. `get_workflow()` - 10 edges
8. `market_hours_enforcer()` - 9 edges
9. `schedule_strategy()` - 9 edges
10. `get_ist_time()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `delete_strategy_route()` --calls--> `delete_strategy()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/blueprints/strategy.py → /Users/bond7/Desktop/Project/openalgo/blueprints/python_strategy.py
- `delete_strategy_route()` --calls--> `delete_strategy()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/blueprints/chartink.py → /Users/bond7/Desktop/Project/openalgo/blueprints/python_strategy.py
- `parse_bru_file()` --calls--> `search()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/blueprints/playground.py → /Users/bond7/Desktop/Project/openalgo/blueprints/search.py
- `get_broker_config()` --calls--> `search()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/blueprints/auth.py → /Users/bond7/Desktop/Project/openalgo/blueprints/search.py
- `get_health_stats()` --calls--> `get_stats()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/blueprints/health.py → /Users/bond7/Desktop/Project/openalgo/blueprints/historify.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (108): get_health_stats(), Get aggregated statistics, add_watchlist(), broker_historify_capabilities(), bulk_add_watchlist(), bulk_delete_data(), bulk_export(), bulk_remove_watchlist() (+100 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (111): api_get_log_content(), api_get_log_files(), api_get_strategies(), api_get_strategy(), api_get_strategy_content(), api_strategy_events(), broadcast_status_update(), check_and_start_pending_strategies() (+103 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (90): is_react_frontend_available(), React Frontend Serving Blueprint Serves the pre-built React app for migrated rou, Check if the React frontend build exists., Serve the React app's index.html., Serve static assets with long cache headers., Serve Apple touch icon., Serve images from React dist., Serve sounds from React dist. (+82 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (43): api_get_websocket_apikey(), api_get_websocket_config(), api_trade_management_safe(), api_websocket_health(), api_websocket_market_data(), api_websocket_metrics(), api_websocket_status(), api_websocket_subscribe() (+35 more)

### Community 4 - "Community 4"
Cohesion: 0.05
Nodes (32): change_password_api(), check_setup_required(), get_analyzer_mode_status(), get_app_info(), get_broker_config(), get_csrf_token(), get_dashboard_data(), get_profile_data() (+24 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (40): activate_workflow(), create_workflow(), deactivate_workflow(), delete_workflow(), disable_webhook(), enable_webhook(), _execute_webhook(), execute_workflow_now() (+32 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (36): action_center(), action_center_api_data(), action_center_count(), approve_all_pending_orders(), approve_pending_order_route(), cancel_all_orders_ui(), cancel_order_ui(), close_all_positions() (+28 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (38): api_create_strategy(), api_get_strategies(), api_get_strategy(), api_toggle_strategy(), configure_symbols(), delete_strategy_route(), delete_symbol(), ensure_order_processor() (+30 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (38): api_get_configs(), api_my_pnl_data(), export_daily_pnl(), export_holdings(), export_positions(), export_trades(), generate_daily_pnl_csv(), generate_holdings_csv() (+30 more)

### Community 9 - "Community 9"
Cohesion: 0.07
Nodes (36): api_create_strategy(), api_get_strategies(), api_get_strategy(), api_toggle_strategy(), configure_symbols(), delete_strategy_route(), delete_symbol(), ensure_order_processor() (+28 more)

### Community 10 - "Community 10"
Cohesion: 0.07
Nodes (26): custom_straddle_intervals(), get_lotsize(), Custom Straddle Blueprint Serves simulated intraday ATM straddle PnL with automa, Get broker-supported intervals., Run intraday straddle simulation with adjustments., Get lot size for a given underlying and exchange from the symbol database., simulate(), get_intervals() (+18 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (24): api_analytics(), api_config(), api_index(), api_users(), broadcast(), configuration(), _format_stats_for_react(), Stop the telegram bot (+16 more)

### Community 12 - "Community 12"
Cohesion: 0.08
Nodes (22): api_freeze_add(), api_freeze_delete(), api_freeze_edit(), api_freeze_list(), api_freeze_upload(), api_holidays_list(), api_stats(), api_timings_check() (+14 more)

### Community 13 - "Community 13"
Cohesion: 0.15
Nodes (18): convert_to_ist(), export_logs(), format_ist_time(), generate_csv(), get_broker_stats(), get_histogram_data(), get_logs(), get_stats() (+10 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (17): convert_to_ist(), detailed_health_check(), export_metrics(), format_ist_time(), get_alerts(), get_current_metrics(), get_metrics_history(), Health Monitoring Blueprint  Industry-standard health check endpoints: - GET /he (+9 more)

### Community 15 - "Community 15"
Cohesion: 0.12
Nodes (17): ban_host(), ban_ip(), clear_404_tracker(), Manually ban an IP address, Validate that a string is a valid IPv4 or IPv6 address., Validate and sanitize a hostname for safe use in queries., Clear 404 tracker for a specific IP, API endpoint to get all security dashboard data as JSON (+9 more)

### Community 16 - "Community 16"
Cohesion: 0.14
Nodes (18): analyzer(), api_get_data(), export_requests(), format_request(), generate_csv(), get_filtered_requests(), get_recent_requests(), get_requests() (+10 more)

### Community 17 - "Community 17"
Cohesion: 0.19
Nodes (18): check_permission(), fix_permissions(), format_permission(), format_permission_rwx(), get_base_path(), get_permission_checks(), get_permissions(), get_unix_permissions() (+10 more)

### Community 18 - "Community 18"
Cohesion: 0.13
Nodes (14): convert_timestamp_to_ist(), get_pnl_data(), parse_trade_timestamp(), pnltracker(), RateLimiter, Thread-safe rate limiter for API calls, Wait if necessary to respect rate limit, Convert timestamp to IST with robust handling for different formats.     Returns (+6 more)

### Community 19 - "Community 19"
Cohesion: 0.12
Nodes (16): check_master_contract_ready(), clear_cache(), force_master_contract_download(), get_cache_health(), get_cache_status(), get_master_contract_status(), get_smart_download_status(), Manually trigger cache reload (+8 more)

### Community 20 - "Community 20"
Cohesion: 0.17
Nodes (14): convert_to_ist(), export_logs(), format_ist_time(), generate_csv(), get_logs(), get_stats(), API endpoint to get traffic logs, API endpoint to get traffic statistics (+6 more)

### Community 21 - "Community 21"
Cohesion: 0.16
Nodes (14): categorize_endpoint(), get_api_key(), get_collections(), get_endpoints(), index(), load_bruno_endpoints(), parse_bru_file(), Categorize an endpoint based on its path (+6 more)

### Community 22 - "Community 22"
Cohesion: 0.13
Nodes (12): dhan_initiate_oauth(), Save the secret API key received via email, Get IP registration status, Register or update IP addresses, Handle Dhan OAuth initiation, Generate OTP for Samco 2FA setup, Generate Secret API Key using OTP, samco_generate_otp() (+4 more)

### Community 23 - "Community 23"
Cohesion: 0.27
Nodes (10): export_logs(), format_log_entry(), generate_csv(), get_filtered_logs(), Generate CSV file from logs, Remove sensitive information from request data, Format a single log entry, Get filtered logs with pagination (+2 more)

### Community 24 - "Community 24"
Cohesion: 0.29
Nodes (6): generate_api_key(), manage_api_key(), Update order mode (auto/semi_auto) for a user, Generate a secure random API key, update_api_key_mode(), setup()

### Community 25 - "Community 25"
Cohesion: 0.33
Nodes (5): maxpain(), oi_data(), OI Tracker Blueprint  Serves Open Interest and Max Pain data for option chains., Get Open Interest data for all strikes., Calculate Max Pain for an underlying/expiry.

### Community 26 - "Community 26"
Cohesion: 0.4
Nodes (4): get_mode(), Get current analyze mode setting, Set analyze mode setting and manage execution engine thread, set_mode()

### Community 27 - "Community 27"
Cohesion: 0.4
Nodes (4): get_current(), Get the current common leverage setting., Set common leverage for all crypto futures orders.     Expects JSON: {"leverage", update_leverage()

### Community 28 - "Community 28"
Cohesion: 0.5
Nodes (3): iv_smile_data(), IV Smile Blueprint  Serves Implied Volatility Smile data. Endpoints:     POST /i, Get IV Smile data for all strikes.

### Community 29 - "Community 29"
Cohesion: 0.5
Nodes (3): Volatility Surface Blueprint Serves 3D implied volatility surface data for index, Get 3D volatility surface data across strikes and expiries., surface_data()

### Community 30 - "Community 30"
Cohesion: 0.5
Nodes (3): gex_data(), GEX Blueprint  Serves Gamma Exposure and OI Walls data. Endpoints:     POST /gex, Get GEX data for all strikes.

### Community 31 - "Community 31"
Cohesion: 0.67
Nodes (2): logging_dashboard(), Consolidated logging dashboard page.     Provides access to all logging and moni

### Community 32 - "Community 32"
Cohesion: 0.67
Nodes (2): index(), Display all trading platforms

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (0): 

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (0): 

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (0): 

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **378 isolated node(s):** `React Frontend Serving Blueprint Serves the pre-built React app for migrated rou`, `Check if the React frontend build exists.`, `Serve the React app's index.html.`, `Serve static assets with long cache headers.`, `Serve Apple touch icon.` (+373 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 33`** (2 nodes): `tradingview_json()`, `tv_json.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (2 nodes): `gocharting_json()`, `gc_json.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (2 nodes): `dashboard()`, `dashboard.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `delete_strategy()` connect `Community 1` to `Community 9`, `Community 7`?**
  _High betweenness centrality (0.027) - this node is a cross-community bridge._
- **Why does `delete_strategy_route()` connect `Community 7` to `Community 1`?**
  _High betweenness centrality (0.015) - this node is a cross-community bridge._
- **Why does `delete_strategy_route()` connect `Community 9` to `Community 1`?**
  _High betweenness centrality (0.015) - this node is a cross-community bridge._
- **What connects `React Frontend Serving Blueprint Serves the pre-built React app for migrated rou`, `Check if the React frontend build exists.`, `Serve the React app's index.html.` to the rest of the system?**
  _378 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.02 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.03 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.04 - nodes in this community are weakly interconnected._