# trading

Utilities for options implied volatility research. The repo currently includes a script to compute current IV, IV Rank, and IV Percentile for an equity ticker using Polygon.io’s Options API.

## iv_metrics.py

Compute current ATM implied volatility and derive IV Rank and IV Percentile over a lookback window. Works with Polygon’s Starter plan endpoints.

### Features

- Pulls latest stock price to find ATM strike near a target DTE (default 30 days).
- Reads option snapshots for both calls and puts; averages IV if both are available.
- Maintains a lightweight local history cache at `.iv_cache/{TICKER}.csv` to build rank/percentile over time.
- Robust parsing across Polygon snapshot variants (handles multiple possible JSON fields for IV/strike/ticker).
- Starter-plan compatible endpoints; no Greeks subscription required.

### Requirements

- Python 3.9+
- Dependencies: `requests`, `pytz` (optional; improves date handling)
- Polygon API key exposed as `POLYGON_API_KEY` or passed via `--api-key`

Install dependencies:

```bash
pip install requests pytz
```

Or via requirements.txt:

```bash
pip install -r requirements.txt
```

Set your Polygon key (or pass `--api-key` each run):

```bash
export POLYGON_API_KEY=YOUR_KEY_HERE
```

### Usage

Basic run (uses default 252-day lookback and 30 DTE target):

```bash
python iv_metrics.py --ticker AAPL
```

Custom lookback and target DTE:

```bash
python iv_metrics.py --ticker TSLA --lookback 252 --target-dte 45
```

Disable local history write (no cache update):

```bash
python iv_metrics.py --ticker MSFT --no-write-history
```

Verbose mode:

```bash
python iv_metrics.py --ticker AMZN --verbose
```

Explicit API key flag:

```bash
python iv_metrics.py --ticker NVDA --api-key YOUR_KEY
```

Exit codes:

- `0` success
- `1` data/API error (e.g., no expirations or IV found)
- `2` missing API key

### What It Does

1) Gets the last underlying stock price to determine ATM strike.

2) Chooses an expiration date closest to the target DTE: today + `--target-dte` days. It searches a window around that target to find real listed expirations.

3) Calls Polygon snapshots for that expiration for both calls and puts, then selects the option with strike closest to the underlying price for each side (ATM). It extracts implied volatility from several possible JSON locations to be resilient. If both sides provide IV, it averages them to produce the current ATM IV.

4) Loads local IV history from `.iv_cache/{TICKER}.csv` (if present) and computes:

- IV Rank = `(current IV - min(IV)) / (max(IV) - min(IV))` over the lookback window.
- IV Percentile = fraction of lookback observations that are `<= current IV`.

5) Optionally appends today’s IV to the local cache unless `--no-write-history` is provided.

### Local History Cache

- Location: `.iv_cache/{TICKER}.csv`
- CSV schema: `date,iv` with ISO date, numeric IV per line.
- On each run (unless disabled), the script deduplicates by date and writes the latest IV.
- Rank/percentile use up to `--lookback` most recent entries (default 252 trading days ~1 year).

Example file:

```csv
date,iv
2024-10-01,0.3221
2024-10-02,0.3187
...
```

To reset history for a ticker, delete the corresponding CSV in `.iv_cache/`.

### Sample Output

```
Ticker: AAPL
Last Price: 186.32
Selected Expiration: 2024-11-15 (target DTE ~30d)
ATM Call: AAPL241115C00185000 strike 185.00 IV=0.3142
ATM Put:  AAPL241115P00185000 strike 185.00 IV=0.3170
Current IV (ATM avg): 0.3156
IV Rank: 42.18%
IV Percentile: 55.95%
History updated at .iv_cache/AAPL.csv
```

Notes:

- If there is no local history yet, the script prints IV but reports rank/percentile as NA.
- If the historical IV range is flat (max == min), IV Rank is NA.

### Endpoints Used (Polygon)

- Last trade price: `/v3/last_trade/stocks/{ticker}` with fallback to `/v2/last/trade/{ticker}`
- List expirations: `/v3/reference/options/contracts` (filtered by underlying, date range)
- Option snapshots: `/v3/snapshot/options/{underlying}` (per expiration and contract type)

These are compatible with Polygon’s Starter tier as of this script’s design.

### Options And Selection Logic

- Target DTE: default `30` days; adjust via `--target-dte`.
- Expiration window: searches a reasonable range around the target to find the closest listed expiration.
- ATM selection: picks the contract whose strike is closest to the underlying last price.
- IV extraction: checks multiple potential fields such as `implied_volatility`, `greeks.implied_volatility`, `day.implied_volatility`, `last_quote.implied_volatility`, etc., to be robust against response variants.

### Troubleshooting

- Missing API key: set `POLYGON_API_KEY` or pass `--api-key`.
- No option expirations found: broaden the target window by increasing `--target-dte` or try different tickers; ensure the market is open/recent data is available.
- Unable to extract implied volatility: sometimes snapshots lack IV; try during market hours, different expirations, or rerun later.
- Flat/insufficient history: rank may be NA or less stable until more days accumulate. You can supply `--lookback` to adjust the window.
- HTTP error: check your network, API key, and Polygon plan limits; the script surfaces HTTP errors and exits with code 1.

### Development Notes

- Optional timezone support via `pytz`; if unavailable, the script falls back to naive dates.
- Code is organized to keep Polygon access in `PolygonClient` and the ATM/metric logic separate for clarity.

## Convenience

### Makefile Target

Quick run via Make:

```bash
make iv TICKER=AAPL
```

Optional variables:

- `LOOKBACK=252`
- `TARGET_DTE=30`
- `VERBOSE=1` (adds `--verbose`)
- `NOWRITE=1` (adds `--no-write-history`)
- `API_KEY=...` (otherwise uses `POLYGON_API_KEY`)

Example:

```bash
make iv TICKER=TSLA LOOKBACK=126 TARGET_DTE=45 VERBOSE=1
```

### Wrapper Script

The helper script `scripts/iv` lets you pass the ticker as the first argument:

```bash
./scripts/iv AAPL --verbose
```

It forwards all flags to `iv_metrics.py`. Run `./scripts/iv --help` for usage.
