# Graph Report - /Users/bond7/Desktop/Project/openalgo/database  (2026-04-18)

## Corpus Check
- 27 files · ~39,595 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 718 nodes · 1030 edges · 39 communities detected
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 85 edges (avg confidence: 0.69)
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
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]

## God Nodes (most connected - your core abstractions)
1. `get_connection()` - 66 edges
2. `InvalidAPIKeyTracker` - 32 edges
3. `init_db_with_logging()` - 21 edges
4. `Auth` - 9 edges
5. `export_to_zip()` - 9 edges
6. `update_workflow()` - 8 edges
7. `verify_api_key()` - 8 edges
8. `_get_samco_auth()` - 8 edges
9. `get_ohlcv()` - 8 edges
10. `Settings` - 8 edges

## Surprising Connections (you probably didn't know these)
- `init_db()` --calls--> `init_db_with_logging()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/database/user_db.py → /Users/bond7/Desktop/Project/openalgo/database/db_init_helper.py
- `init_health_db()` --calls--> `init_db_with_logging()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/database/health_db.py → /Users/bond7/Desktop/Project/openalgo/database/db_init_helper.py
- `init_db()` --calls--> `init_db_with_logging()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/database/sandbox_db.py → /Users/bond7/Desktop/Project/openalgo/database/db_init_helper.py
- `init_db()` --calls--> `init_db_with_logging()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/database/telegram_db.py → /Users/bond7/Desktop/Project/openalgo/database/db_init_helper.py
- `get_auth_token_by_username()` --calls--> `get_auth_token()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/database/telegram_db.py → /Users/bond7/Desktop/Project/openalgo/database/auth_db.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (153): Convert log entry to dictionary, add_to_watchlist(), bulk_add_to_watchlist(), bulk_delete_market_data(), bulk_remove_from_watchlist(), _clean_schedule_record(), clear_watchlist(), create_download_job() (+145 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (79): Base, get_all_configs(), get_config(), init_db(), init_default_config(), _migrate_margin_audit_columns(), Sandbox trades table - executed trades, Sandbox positions table - open positions (+71 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (69): ApiKeys, Auth, decrypt_token(), encrypt_token(), get_api_key(), get_api_key_for_tradingview(), get_auth_token(), get_auth_token_broker() (+61 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (52): activate_workflow(), add_execution_log(), clear_workflow_cache(), create_execution(), create_workflow(), deactivate_workflow(), delete_workflow(), disable_webhook() (+44 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (44): check_and_update_holidays(), clear_market_calendar_cache(), ensure_market_calendar_tables_exists(), get_all_market_timings(), get_holidays_by_year(), get_market_hours_status(), get_market_timing(), get_market_timings_for_date() (+36 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (34): init_db(), Initialize database tables, AnalyzerLog, async_log_analyzer(), init_db(), Initialize the analyzer table, Asynchronously log analyzer request, async_log_order() (+26 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (29): add_symbol_mapping(), bulk_add_symbol_mappings(), clear_strategy_cache(), create_strategy(), delete_strategy(), delete_symbol_mapping(), get_strategy(), get_strategy_by_webhook_id() (+21 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (27): ensure_qty_freeze_tables_exists(), get_all_freeze_qty(), get_freeze_qty(), get_freeze_qty_for_option(), init_db(), load_freeze_qty_cache(), load_freeze_qty_from_csv(), QtyFreeze (+19 more)

### Community 8 - "Community 8"
Cohesion: 0.09
Nodes (22): add_symbol_mapping(), bulk_add_symbol_mappings(), ChartinkStrategy, ChartinkSymbolMapping, create_strategy(), delete_symbol_mapping(), get_strategy_by_webhook_id(), get_symbol_mappings() (+14 more)

### Community 9 - "Community 9"
Cohesion: 0.13
Nodes (21): clear_settings_cache(), _decrypt_password(), _encrypt_password(), get_analyze_mode(), _get_encryption_key(), get_smtp_settings(), init_db(), Set analyze mode setting (+13 more)

### Community 10 - "Community 10"
Cohesion: 0.13
Nodes (20): Close ZMQ connections, check_if_ready(), get_exchange_stats_from_db(), get_last_download_time(), get_last_downloaded_broker(), get_status(), init_broker_status(), mark_status_ready_without_download() (+12 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (12): create_alert(), HealthAlert, HealthMetric, init_health_db(), log_metrics(), purge_old_metrics(), Health Monitoring Database  Tracks infrastructure-level health metrics: - File d, Model for tracking health alerts (+4 more)

### Community 12 - "Community 12"
Cohesion: 0.16
Nodes (13): LogBase, get_security_settings(), Get security configuration (cached for 1 hour), ban_ip(), Error404Tracker, IPBan, is_ip_banned(), log_request() (+5 more)

### Community 13 - "Community 13"
Cohesion: 0.12
Nodes (19): approve_pending_order(), create_pending_order(), delete_pending_order(), get_ist_timestamp(), get_pending_count(), get_pending_order_by_id(), get_pending_orders(), PendingOrder (+11 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (12): add_user(), authenticate_user(), find_user_by_email(), init_db(), Authenticate user with Argon2 hashed password, Find user by email for password reset, Utility function to rehash all existing passwords with Argon2.     This should b, Hash password using Argon2 with pepper (+4 more)

### Community 15 - "Community 15"
Cohesion: 0.19
Nodes (12): CacheInvalidationPublisher, get_cache_invalidation_publisher(), publish_all_cache_invalidation(), publish_auth_cache_invalidation(), publish_feed_cache_invalidation(), Get the singleton cache invalidation publisher instance.      Returns:         C, Convenience function to publish auth cache invalidation.      Args:         user, Convenience function to publish feed cache invalidation.      Args:         user (+4 more)

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (13): clear_cache_on_logout(), get_cache_health(), _get_health_recommendations(), hook_into_master_contract_download(), load_symbols_to_cache(), Master Contract Cache Hook Automatically loads symbols into memory cache after s, Clear the cache when user logs out or session expires     This helps free memory, Check if cache needs refresh and reload if necessary     Called periodically or (+5 more)

### Community 17 - "Community 17"
Cohesion: 0.24
Nodes (9): ChartPreferences, ensure_chart_prefs_tables_exists(), get_chart_prefs(), init_db(), Ensure tables exist (alias for init_db to match app.py pattern), Initialize the chart preferences database, Get all chart preferences for the user associated with the API key.     Returns, Update chart preferences for the user associated with the API key.     'data' sh (+1 more)

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (0): 

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Log health metrics (background thread only - zero API latency impact)

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Get most recent metrics

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Get recent metrics ordered by timestamp

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Get metrics for the specified number of hours

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Get aggregated statistics for the specified time period

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Get all active (not resolved) alerts

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Automatically resolve alerts when metrics return to healthy range

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): Log order execution latency

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): Get recent latency logs ordered by timestamp

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): Get latency statistics - optimized with minimal database queries

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): Log a request to the database

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (1): Get recent traffic logs ordered by timestamp

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (1): Get basic traffic statistics

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (1): Check if an IP is currently banned

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (1): Get all current IP bans

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (1): Track a 404 error for an IP

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (1): Get IPs with suspicious 404 activity

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (1): Track an invalid API key attempt

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): Get IPs with suspicious API key activity

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **296 isolated node(s):** `Health Monitoring Database  Tracks infrastructure-level health metrics: - File d`, `Model for tracking infrastructure health metrics`, `Log health metrics (background thread only - zero API latency impact)`, `Get most recent metrics`, `Get recent metrics ordered by timestamp` (+291 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 18`** (2 nodes): `search_symbols()`, `tv_search.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `Log health metrics (background thread only - zero API latency impact)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Get most recent metrics`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Get recent metrics ordered by timestamp`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Get metrics for the specified number of hours`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Get aggregated statistics for the specified time period`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `Get all active (not resolved) alerts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `Automatically resolve alerts when metrics return to healthy range`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `Log order execution latency`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `Get recent latency logs ordered by timestamp`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `Get latency statistics - optimized with minimal database queries`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `Log a request to the database`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `Get recent traffic logs ordered by timestamp`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `Get basic traffic statistics`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `Check if an IP is currently banned`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `Get all current IP bans`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `Track a 404 error for an IP`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `Get IPs with suspicious 404 activity`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `Track an invalid API key attempt`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `Get IPs with suspicious API key activity`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AnalyzerLog` connect `Community 5` to `Community 0`, `Community 1`?**
  _High betweenness centrality (0.233) - this node is a cross-community bridge._
- **Why does `init_db_with_logging()` connect `Community 5` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 9`, `Community 11`, `Community 14`, `Community 17`?**
  _High betweenness centrality (0.194) - this node is a cross-community bridge._
- **Why does `get_connection()` connect `Community 0` to `Community 10`?**
  _High betweenness centrality (0.118) - this node is a cross-community bridge._
- **Are the 28 inferred relationships involving `InvalidAPIKeyTracker` (e.g. with `Auth` and `ApiKeys`) actually correct?**
  _`InvalidAPIKeyTracker` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `init_db_with_logging()` (e.g. with `init_health_db()` and `init_db()`) actually correct?**
  _`init_db_with_logging()` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Auth` (e.g. with `InvalidAPIKeyTracker` and `Restore symbol cache from database on startup.      Loads all symbols from the s`) actually correct?**
  _`Auth` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Health Monitoring Database  Tracks infrastructure-level health metrics: - File d`, `Model for tracking infrastructure health metrics`, `Log health metrics (background thread only - zero API latency impact)` to the rest of the system?**
  _296 weakly-connected nodes found - possible documentation gaps or missing edges._