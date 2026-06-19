# Stock Squeeze & Institutional Sweep Scanner

A toolkit for detecting institutional footprints in US equities during extended hours (04:00–09:30 and 16:00–20:00 ET), when option-dealer gamma hedging is offline and large prints more closely reflect directional intent.

The core scanner connects to Interactive Brokers (IB Gateway / TWS) and pulls full SIP consolidated tick data for a configurable watchlist (defaults to Nasdaq-100 top weights). It surfaces two signal types:

- **Clusters** — same-price repeated prints in a short window (iceberg / VWAP execution)
- **Single sweeps** — individual prints above a configurable notional threshold

Output is two CSVs per run (`clusters` + `trades`) plus a ranked notional summary by ticker. Fallback data sources (Nasdaq public API, Alpaca IEX, Finnhub) are included for environments without an IBKR account.

---

## Components

Two complementary tools for pre/post-market edge detection:

| Script | Purpose |
|--------|---------|
| `scanner.py` | Daily short-squeeze candidate ranking (FINRA short volume + SEC 8-K) |
| `nasdaq100_afterhours_scraper.py` | Institutional sweep detection via tick-level tape (IBKR / Nasdaq API) |
| `premarket_tape_scanner.py` | Cluster detection engine shared by both tape tools |

---

## Setup

```bash
pip3 install requests ib_insync
```

`ib_insync` is only required for the IBKR data source (recommended). The Nasdaq free source has no dependencies beyond `requests`.

---

## 1. Institutional Sweep Scanner (`nasdaq100_afterhours_scraper.py`)

Detects patterns like: **8 prints × 114k–574k shares at $87.02 within 1 second = $174M sweep**. Surfaces clusters in the output with a 🔥 flag when ≥3 prints and ≥500K total volume.

### Data Sources

| Source | Flag | Cost | Coverage | Notes |
|--------|------|------|----------|-------|
| **IBKR** | `--source ibkr` | Free (account required) | Full SIP consolidated tape, all exchanges, extended hours | **Recommended** |
| **Nasdaq API** | `--source nasdaq` | Free, no auth | Nasdaq extended-hours visible table only | Default fallback |
| **Alpaca IEX** | `--source alpaca` | Free account | IEX feed, regular hours only (~25% tape) | Needs `ALPACA_API_KEY` + `ALPACA_API_SECRET` |
| **Finnhub** | `--source finnhub` | Free key, 60 req/min | Multi-exchange, regular hours | Needs `FINNHUB_API_KEY` |

### IBKR Setup (one-time)

1. Open **IB Gateway** (lighter) or **TWS**
2. `Configure → Settings → API → Settings`
   - ✅ Enable ActiveX and Socket Clients
   - Note the port: **4001** (live Gateway) · **4002** (paper Gateway) · **7496** (live TWS) · **7497** (paper TWS)
3. Keep Gateway running while scanning

### Usage

```bash
# After-hours sweep scan — run at 16:00 ET (IBKR live Gateway)
python3 nasdaq100_afterhours_scraper.py \
  --source ibkr --ibkr-port 4001 \
  --fallback-universe --markettype post \
  --min-cluster-volume 50000 --min-single-trade-size 100000 --min-repeats 2

# Premarket sweep scan — run at 04:00 ET
python3 nasdaq100_afterhours_scraper.py \
  --source ibkr --ibkr-port 4001 \
  --fallback-universe --markettype pre \
  --min-cluster-volume 50000 --min-single-trade-size 100000 --min-repeats 2

# Specific tickers only
python3 nasdaq100_afterhours_scraper.py \
  --source ibkr --ibkr-port 4001 \
  --tickers NFLX NVDA TSLA --date 2026-05-18 --markettype pre \
  --min-cluster-volume 10000 --min-single-trade-size 10000 --min-repeats 2

# Dry-run: verify API connectivity and response shape (no clustering)
python3 nasdaq100_afterhours_scraper.py --dry-run --ibkr-port 4001

# Combine sources (Nasdaq API for extended hours + IBKR)
python3 nasdaq100_afterhours_scraper.py \
  --source nasdaq ibkr --ibkr-port 4001 \
  --fallback-universe --markettype pre

# No IBKR — free Nasdaq API only (extended hours, limited depth)
python3 nasdaq100_afterhours_scraper.py --fallback-universe --markettype post
```

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--source` | `nasdaq` | Data source(s): `nasdaq` / `ibkr` / `alpaca` / `finnhub` |
| `--markettype` | `post` | `post` = after-hours 16:00–20:00 ET; `pre` = premarket 04:00–09:30 ET |
| `--tickers` | — | Override universe; otherwise top QQQ/Nasdaq-100 holdings |
| `--fallback-universe` | off | Use embedded top-50 QQQ list (skip live holdings fetch) |
| `--limit` | 50 | Number of top QQQ holdings to scan |
| `--min-cluster-volume` | 100,000 | Total shares in cluster to report |
| `--min-single-trade-size` | 100,000 | Report even a single print this large |
| `--min-repeats` | 2 | Minimum prints at same price in window |
| `--cluster-window-sec` | 3.0 | Time window for grouping prints (seconds) |
| `--price-tolerance` | 0.0 | Allow prints within this many dollars of cluster price |
| `--ibkr-port` | 7497 | Gateway/TWS API port |
| `--ibkr-client-id` | 1 | IBKR client ID (use different IDs for parallel connections) |
| `--save-raw` | off | Save raw Nasdaq API JSON to `outputs/nasdaq_extended/raw/` |
| `--dry-run` | off | Fetch one ticker, print API response shape, exit |

### Output

```
🔥NFLX   379.66 premarket   08:16:12-08:16:13  87.0200  8     2.00M  $174.33M  up
  AAPL   106.57 premarket   08:49:25           300.2300  2   326.5K   $98.04M  up-follow
  TSLA    80.81 premarket   08:49:25           422.2400  1   179.3K   $75.72M  up
```

🔥 = sweep flag: ≥3 prints AND ≥500K total volume  
Output CSVs written to `outputs/nasdaq_extended/`:
- `*_trades.csv` — every individual tick normalized to `ticker/time/price/size/source`
- `*_clusters.csv` — detected clusters with score/direction/notional

---

## 2. Short Squeeze Scanner (`scanner.py`)

Daily ranking for premarket squeeze setups. Uses FINRA short-sale volume + SEC 8-K filings.

### Setup

```bash
export SEC_USER_AGENT='Your Name your.email@example.com'
```

### Usage

```bash
# Auto-scan (FINRA universe for today)
python3 scanner.py

# From watchlist
python3 scanner.py --watchlist watchlist.csv

# Specific tickers
python3 scanner.py --tickers RKLB FLNC

# JSON output
python3 scanner.py --watchlist watchlist.csv --json

# Historical date
python3 scanner.py --trade-date 2026-05-08
```

Copy `watchlist.example.csv` → `watchlist.csv`. Optional columns: `float_m`, `borrow_fee_pct`, `premarket_gap_pct`.

### Score Components

| Signal | Points |
|--------|--------|
| Short volume ≥ threshold | +3 |
| Short volume ≥ 20% / 30% | +1 / +1 |
| Recent earnings 8-K | +3 |
| Item 2.02 | +1 |
| Bullish language in filing | +1 to +3 |
| Float ≤ 100M | +1 |
| Short interest ≥ 10% / 20% of float | +1 / +1 |
| Days-to-cover ≥ 3 | +1 |
| Borrow fee ≥ 10% | +1 |
| Premarket / session move ≥ 3% | +1 |
| Core setup complete (short vol + earnings) | +2 |

---

## 3. Tape Cluster Engine (`premarket_tape_scanner.py`)

Standalone cluster detector. Accepts CSV files or Polygon.io live tick data.

```bash
# From CSV export
python3 premarket_tape_scanner.py --csv tape.csv

# From directory of CSVs
python3 premarket_tape_scanner.py --csv-dir ./tapes --min-cluster-volume 100000

# Polygon.io (paid key required)
export POLYGON_API_KEY='your-key'
python3 premarket_tape_scanner.py --tickers NFLX TSLA --date 2026-05-19
```

CSV column names accepted: `ticker`/`symbol`, `time`/`timestamp`/`datetime`/`trade_time`, `price`, `size`/`volume`/`shares`/`qty`/`quantity`.

---

## Workflow

```
Pre-market (04:00 ET)
  → nasdaq100_afterhours_scraper.py --markettype pre --source ibkr
  → 🔥 clusters flagged → manual review

After market close (16:00 ET)
  → nasdaq100_afterhours_scraper.py --markettype post --source ibkr
  → 🔥 clusters flagged → cross-check with scanner.py short setup
  → scanner.py for next-day squeeze candidates
```
