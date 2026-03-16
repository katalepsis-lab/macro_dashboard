# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the Streamlit dashboard
streamlit run dashboard_v1.py

# Download/refresh all data (standalone)
python data_downloader.py
```

No test suite exists. Validate changes by running the dashboard and clicking "Refresh Global Data" in the sidebar.

## Architecture

Three independent entry points:

- **`dashboard_v1.py`** — Streamlit app. All display logic lives here. Imports `data_downloader` for the sidebar refresh button. Never contains download logic.
- **`data_downloader.py`** — All API fetch logic. Single public function: `run_all_updates(progress_callback=None)` — returns a list of failed series names (empty = success). Downloads to `./source_data/`. Can run standalone. **User-coded file — do not modify without explicit confirmation from the user.**

### Data flow in `dashboard_v1.py`

1. `get_processed_data()` (cached) reads all CSVs, aligns frequencies, computes indicators, returns `{"CAN": df, "US": df, "CAN_raw": {...}, "US_raw": {...}}`.
2. Frequency alignment: daily/weekly → `resample('ME').last()`; quarterly → `resample('ME').last().ffill()`. All frames merged via `pd.merge_asof(direction='backward', tolerance=62 days)`.
3. `METRICS` list (9 dicts) drives the 3×3 grid — each dict declares column names, chart type, and raw key lookups.
4. `_render_tab()` iterates `METRICS` in rows of 3, using `raw_series` for charts when available (higher frequency takes precedence over the monthly merged frame).

### Data sources

| Country | Source | Helper |
|---|---|---|
| Canada stats | Statistics Canada (ZIP/CSV by PID) | `get_statscan_data(pid, label)` |
| Canada rates/yields | Bank of Canada Valet API | `get_boc_data(series_id, label)` |
| US | FRED via `pandas_datareader` | `web.DataReader(series, 'fred', ...)` |

### Column naming gotchas

- `CAN_10Y_TBILL` — ALL CAPS (not `CAN_10Y_tbill`)
- `US_CB_BS.csv` column is `3M_Rolling_Avg_Change(%)` (parens), CAN is `3M_Rolling_Avg_Change_%` (underscore)
- `US_INTEREST` stores SAAR annual-rate values — divide by 4 before `rolling(4).sum()` to get TTM
- `CAN_1Y_INF` DATE is stored as `'YYYY-MM'` string (BoC quarterly CES); `pd.to_datetime` parses it as quarter-start
- `US_REV.csv` has both `TTM` and `US_GOVT_REV` columns; dashboard renames to `US_REV_TTM` and `US_REV_QTR`

### Adding a new indicator

1. Add the download block to `run_all_updates()` in `data_downloader.py` with a `_cb(...)` call and increment `_TOTAL_STEPS` in `dashboard_v1.py`.
2. Load and resample the series in `get_processed_data()`.
3. Add a dict to the `METRICS` list.
