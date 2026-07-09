# Graph Report - /Users/bond7/Desktop/Project/openalgo/graph-target  (2026-04-18)

## Corpus Check
- 4 files · ~25,014 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 162 nodes · 254 edges · 29 communities detected
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 5 edges (avg confidence: 0.8)
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

## God Nodes (most connected - your core abstractions)
1. `get_connection()` - 65 edges
2. `get_ohlcv()` - 9 edges
3. `export_to_zip()` - 9 edges
4. `parse_interval()` - 7 edges
5. `_get_aggregated_ohlcv()` - 6 edges
6. `_get_daily_aggregated_ohlcv()` - 6 edges
7. `_clean_schedule_record()` - 6 edges
8. `run_production_scan()` - 6 edges
9. `get_db_path()` - 5 edges
10. `ensure_db_directory()` - 5 edges

## Surprising Connections (you probably didn't know these)
- `run_production_scan()` --calls--> `train_nnls_model()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/graph-target/intraday_setup_scanner.py → /Users/bond7/Desktop/Project/openalgo/graph-target/ml_volatility.py
- `run_production_scan()` --calls--> `get_ohlcv()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/graph-target/intraday_setup_scanner.py → /Users/bond7/Desktop/Project/openalgo/graph-target/historify_db.py
- `run_production_scan()` --calls--> `compute_lwma()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/graph-target/intraday_setup_scanner.py → /Users/bond7/Desktop/Project/openalgo/graph-target/ml_volatility.py
- `run_production_scan()` --calls--> `prepare_ml_dataset()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/graph-target/intraday_setup_scanner.py → /Users/bond7/Desktop/Project/openalgo/graph-target/ml_volatility.py
- `run_production_scan()` --calls--> `compute_ohlc_volatility()`  [INFERRED]
  /Users/bond7/Desktop/Project/openalgo/graph-target/intraday_setup_scanner.py → /Users/bond7/Desktop/Project/openalgo/graph-target/volatility.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (34): add_to_watchlist(), bulk_delete_market_data(), create_download_job(), create_schedule(), export_to_txt(), get_connection(), get_data_catalog(), get_symbol_metadata() (+26 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (27): clear_watchlist(), create_schedule_execution(), delete_market_data(), export_to_parquet(), get_all_expired_fno_jobs(), get_data_range(), get_expired_fno_job(), get_last_candle_timestamp() (+19 more)

### Community 2 - "Community 2"
Cohesion: 0.14
Nodes (20): export_to_dataframe(), export_to_zip(), _get_aggregated_ohlcv(), _get_daily_aggregated_ohlcv(), _get_market_open_seconds(), get_ohlcv(), is_custom_interval(), is_daily_aggregated_interval() (+12 more)

### Community 3 - "Community 3"
Cohesion: 0.22
Nodes (9): _clean_schedule_record(), get_active_schedules(), get_all_schedules(), get_schedule(), get_schedule_executions(), Clean a schedule record for JSON serialization.     Converts pandas NaT/NaN valu, Get a schedule by ID., Get execution history for a schedule. (+1 more)

### Community 4 - "Community 4"
Cohesion: 0.31
Nodes (6): run_production_scan(), compute_lwma(), prepare_ml_dataset(), train_nnls_model(), compute_ohlc_volatility(), Computes various OHLC-based historical volatility measures.          Args:

### Community 5 - "Community 5"
Cohesion: 0.25
Nodes (8): ensure_db_directory(), get_database_stats(), get_db_path(), init_database(), Get database statistics.      Returns:         Dictionary with database size, re, Get absolute path to the DuckDB database file., Ensure the database directory exists., Initialize the Historify database schema.     Creates all required tables if the

### Community 6 - "Community 6"
Cohesion: 0.33
Nodes (6): import_from_csv(), import_from_parquet(), Import OHLCV data from a CSV file into the database.      Expected CSV format (o, Import OHLCV data from a Parquet file into the database.      Expected Parquet f, Insert or update OHLCV data from a pandas DataFrame.      Args:         df: Data, upsert_market_data()

### Community 7 - "Community 7"
Cohesion: 0.5
Nodes (4): get_download_job(), Convert timestamp to ISO string, handling NaT/None values., Get a download job by ID., _safe_timestamp()

### Community 8 - "Community 8"
Cohesion: 0.5
Nodes (4): get_catalog_grouped(), get_catalog_with_metadata(), Get data catalog enriched with symbol metadata.      Returns:         List of ca, Get data catalog grouped by underlying or exchange.      Args:         group_by:

### Community 9 - "Community 9"
Cohesion: 1.0
Nodes (2): get_watchlist(), Get all symbols in the watchlist.

### Community 10 - "Community 10"
Cohesion: 1.0
Nodes (2): bulk_remove_from_watchlist(), Remove multiple symbols from the watchlist in a single transaction.      Args:

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (2): get_available_symbols(), Get list of unique symbol-exchange combinations with data.      Returns:

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (2): Vacuum the database to reclaim space and optimize performance., vacuum_database()

### Community 13 - "Community 13"
Cohesion: 1.0
Nodes (2): get_all_download_jobs(), Get all download jobs, optionally filtered by status.

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (2): delete_download_job(), Delete a download job and its items.

### Community 15 - "Community 15"
Cohesion: 1.0
Nodes (2): get_export_preview(), Get a preview of what will be exported (record count, date range, etc.)      Arg

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (2): get_expired_fno_expiry_stats(), Per-expiry contract counts keyed by expiry_date string (YYYY-MM-DD).

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (2): get_expired_fno_contracts(), Get cached contracts for an instrument + expiry combination.      Args:

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (2): bulk_add_to_watchlist(), Add multiple symbols to the watchlist in a single transaction.      Args:

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (2): get_existing_dates(), Get the set of dates (as date objects) that have data in market_data     for a g

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (2): export_to_csv(), Export market data to CSV file.      Args:         output_path: Path to save the

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (2): get_job_items(), Get all items for a job, optionally filtered by status.

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (2): Update the status of a job item., update_job_item_status()

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (2): export_bulk_csv(), Export multiple symbols to a single CSV file.      Args:         output_path: Pa

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (2): delete_schedule(), Delete a schedule and its execution history.

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (2): Update an execution record., update_schedule_execution()

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (2): get_expired_fno_expiries(), Get cached expiry dates for an instrument.      Args:         upstox_key: Upstox

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (2): create_expired_fno_job(), Create a new expired F&O download job record.      Args:         job: Dict with

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (2): get_expired_fno_stats(), Get summary statistics for expired F&O data.

## Knowledge Gaps
- **77 isolated node(s):** `Get absolute path to the DuckDB database file.`, `Ensure the database directory exists.`, `Get a DuckDB connection with proper resource management and retry logic.      Du`, `Initialize the Historify database schema.     Creates all required tables if the`, `Get all symbols in the watchlist.` (+72 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 9`** (2 nodes): `get_watchlist()`, `Get all symbols in the watchlist.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 10`** (2 nodes): `bulk_remove_from_watchlist()`, `Remove multiple symbols from the watchlist in a single transaction.      Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 11`** (2 nodes): `get_available_symbols()`, `Get list of unique symbol-exchange combinations with data.      Returns:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 12`** (2 nodes): `Vacuum the database to reclaim space and optimize performance.`, `vacuum_database()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 13`** (2 nodes): `get_all_download_jobs()`, `Get all download jobs, optionally filtered by status.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 14`** (2 nodes): `delete_download_job()`, `Delete a download job and its items.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 15`** (2 nodes): `get_export_preview()`, `Get a preview of what will be exported (record count, date range, etc.)      Arg`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `get_expired_fno_expiry_stats()`, `Per-expiry contract counts keyed by expiry_date string (YYYY-MM-DD).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (2 nodes): `get_expired_fno_contracts()`, `Get cached contracts for an instrument + expiry combination.      Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (2 nodes): `bulk_add_to_watchlist()`, `Add multiple symbols to the watchlist in a single transaction.      Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (2 nodes): `get_existing_dates()`, `Get the set of dates (as date objects) that have data in market_data     for a g`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (2 nodes): `export_to_csv()`, `Export market data to CSV file.      Args:         output_path: Path to save the`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (2 nodes): `get_job_items()`, `Get all items for a job, optionally filtered by status.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (2 nodes): `Update the status of a job item.`, `update_job_item_status()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (2 nodes): `export_bulk_csv()`, `Export multiple symbols to a single CSV file.      Args:         output_path: Pa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (2 nodes): `delete_schedule()`, `Delete a schedule and its execution history.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (2 nodes): `Update an execution record.`, `update_schedule_execution()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (2 nodes): `get_expired_fno_expiries()`, `Get cached expiry dates for an instrument.      Args:         upstox_key: Upstox`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (2 nodes): `create_expired_fno_job()`, `Create a new expired F&O download job record.      Args:         job: Dict with`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (2 nodes): `get_expired_fno_stats()`, `Get summary statistics for expired F&O data.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_connection()` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 12`, `Community 13`, `Community 14`, `Community 15`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 20`, `Community 21`, `Community 22`, `Community 23`, `Community 24`, `Community 25`, `Community 26`, `Community 27`, `Community 28`?**
  _High betweenness centrality (0.351) - this node is a cross-community bridge._
- **Why does `get_ohlcv()` connect `Community 2` to `Community 0`, `Community 1`, `Community 4`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `run_production_scan()` connect `Community 4` to `Community 2`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **What connects `Get absolute path to the DuckDB database file.`, `Ensure the database directory exists.`, `Get a DuckDB connection with proper resource management and retry logic.      Du` to the rest of the system?**
  _77 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.06 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.07 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.14 - nodes in this community are weakly interconnected._