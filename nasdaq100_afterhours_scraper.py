#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any

import requests

from zoneinfo import ZoneInfo

from premarket_tape_scanner import Trade, find_clusters, parse_trade_time

US_EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

HOLDINGS_URL = "https://stockanalysis.com/etf/qqq/holdings/"
NASDAQ_EXTENDED_URL = (
    "https://api.nasdaq.com/api/quote/{ticker}/extended-trading"
    "?assetclass=stocks&markettype={markettype}"
)
ALPACA_TRADES_URL = "https://data.alpaca.markets/v2/stocks/{ticker}/trades"
FINNHUB_TICK_URL = "https://finnhub.io/api/v1/stock/tick"

FALLBACK_QQQ_TOP_50 = [
    "NVDA",
    "MSFT",
    "AAPL",
    "AVGO",
    "AMZN",
    "META",
    "GOOGL",
    "GOOG",
    "TSLA",
    "COST",
    "NFLX",
    "PLTR",
    "ASML",
    "AMD",
    "TMUS",
    "CSCO",
    "AZN",
    "LIN",
    "INTU",
    "PEP",
    "ISRG",
    "QCOM",
    "TXN",
    "AMGN",
    "BKNG",
    "AMAT",
    "ARM",
    "ADBE",
    "PDD",
    "GILD",
    "HON",
    "MU",
    "PANW",
    "CMCSA",
    "ADP",
    "MELI",
    "LRCX",
    "SBUX",
    "VRTX",
    "ADI",
    "KLAC",
    "APP",
    "CRWD",
    "CEG",
    "MSTR",
    "DASH",
    "CDNS",
    "MDLZ",
    "SNPS",
    "ORLY",
]


@dataclass
class FetchResult:
    ticker: str
    ok: bool
    trades: int
    error: str = ""


def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        }
    )
    return sess


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def to_float(value: Any) -> float | None:
    text = clean_text(value).replace("$", "").replace(",", "")
    if not text or text in {"--", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    return int(number)


def extract_holdings_from_stockanalysis(html: str, limit: int) -> list[str]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL)
    holdings: list[tuple[float, str]] = []
    for row in rows:
        symbol_match = re.search(
            r"/stocks/([a-z0-9.-]+)/", row, flags=re.IGNORECASE
        )
        if not symbol_match:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        weight = None
        for cell in cells:
            text = clean_text(cell)
            if text.endswith("%"):
                weight = to_float(text.replace("%", ""))
                break
        if weight is None:
            continue
        holdings.append((weight, symbol_match.group(1).upper().replace(".", "-")))

    holdings.sort(reverse=True)
    tickers: list[str] = []
    for _, ticker in holdings:
        if ticker not in tickers:
            tickers.append(ticker)
        if len(tickers) >= limit:
            break
    return tickers


def load_top_tickers(sess: requests.Session, limit: int, use_fallback: bool) -> tuple[list[str], str]:
    if not use_fallback:
        try:
            resp = sess.get(HOLDINGS_URL, timeout=20)
            resp.raise_for_status()
            tickers = extract_holdings_from_stockanalysis(resp.text, limit)
            if tickers:
                return tickers, HOLDINGS_URL
        except requests.RequestException:
            pass
    return FALLBACK_QQQ_TOP_50[:limit], "embedded fallback QQQ top 50"


def rows_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    tables = [
        data.get("tradeDetailTable"),
        data.get("tradesTable"),
        data.get("timeSalesTable"),
    ]
    rows: list[dict[str, Any]] = []
    for table in tables:
        if isinstance(table, dict) and isinstance(table.get("rows"), list):
            rows.extend(row for row in table["rows"] if isinstance(row, dict))
    return rows


def first_present(row: dict[str, Any], names: list[str]) -> Any:
    normalized = {re.sub(r"[^a-z0-9]+", "", key.lower()): value for key, value in row.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if key in normalized:
            return normalized[key]
    return None


def trades_from_rows(
    ticker: str,
    rows: list[dict[str, Any]],
    trade_date: dt.date,
    source: str,
) -> list[Trade]:
    trades: list[Trade] = []
    for row in rows:
        time_value = first_present(row, ["time", "timestamp", "lastSaleTime", "lastTradeTime"])
        price_value = first_present(row, ["price", "lastSalePrice", "lastsale"])
        size_value = first_present(row, ["shareVolume", "volume", "size", "shares"])
        ts = parse_trade_time(clean_text(time_value), trade_date)
        price = to_float(price_value)
        size = to_int(size_value)
        if ts is None or price is None or size is None:
            continue
        trades.append(
            Trade(
                ticker=ticker.upper(),
                ts=ts,
                price=price,
                size=size,
                source=source,
            )
        )
    return trades


def fetch_nasdaq_extended(
    sess: requests.Session,
    ticker: str,
    markettype: str,
    trade_date: dt.date,
    raw_dir: Path | None,
) -> tuple[list[Trade], str]:
    base_url = NASDAQ_EXTENDED_URL.format(ticker=ticker.upper(), markettype=markettype)

    # First request: no filter — gets filterList + last 100 trades
    payload = request_json(sess, base_url)

    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{ticker.upper()}_{markettype}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    status = payload.get("status") or {}
    code = status.get("rCode") or status.get("statusCode")
    if code not in (None, 200):
        return [], json.dumps(status, ensure_ascii=False)

    # Collect rows from default response (last 100 trades)
    all_rows = rows_from_payload(payload)

    # Discover time-window filters (value >= 1; value=0 is "Last 100 Trades")
    filter_list = (payload.get("data") or {}).get("filterList") or []
    window_filters = [f["value"] for f in filter_list if isinstance(f.get("value"), int) and f["value"] >= 1]

    # Fetch each 30-min window to get full session coverage
    for fval in window_filters:
        try:
            time.sleep(0.1)  # avoid rate-limit / connection-pool exhaustion
            window_rows = rows_from_payload(request_json(sess, f"{base_url}&filter={fval}"))
            all_rows.extend(window_rows)
        except requests.RequestException:
            pass

    # Deduplicate by (time, price, shareVolume) — windows may overlap with last-100
    seen: set[tuple] = set()
    unique_rows: list[dict] = []
    for row in all_rows:
        key = (
            clean_text(first_present(row, ["time", "timestamp", "lastSaleTime", "lastTradeTime"])),
            clean_text(first_present(row, ["price", "lastSalePrice", "lastsale"])),
            clean_text(first_present(row, ["shareVolume", "volume", "size", "shares"])),
        )
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)

    return trades_from_rows(ticker, unique_rows, trade_date, "nasdaq"), ""


def load_nasdaq_raw(
    ticker: str,
    markettype: str,
    trade_date: dt.date,
    raw_dir: Path,
) -> tuple[list[Trade], str]:
    path = raw_dir / f"{ticker.upper()}_{markettype}.json"
    if not path.exists():
        return [], f"missing raw file: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = payload.get("status") or {}
    code = status.get("rCode") or status.get("statusCode")
    if code not in (None, 200):
        return [], json.dumps(status, ensure_ascii=False)
    return trades_from_rows(ticker, rows_from_payload(payload), trade_date, "nasdaq-raw"), ""


def request_json(sess: requests.Session, url: str) -> dict[str, Any]:
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return request_json_with_curl(url)


def request_json_with_curl(url: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "curl",
            "-L",
            "-sS",
            "--retry",
            "4",
            "--retry-delay",
            "1",
            "--connect-timeout",
            "10",
            "-A",
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "-H",
            "Accept: application/json, text/plain, */*",
            "-H",
            "Origin: https://www.nasdaq.com",
            "-H",
            "Referer: https://www.nasdaq.com/",
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def fetch_alpaca_trades(
    ticker: str,
    trade_date: dt.date,
    start_time: dt.time,
    end_time: dt.time,
    api_key: str,
    api_secret: str,
    feed: str = "iex",
) -> tuple[list[Trade], str]:
    """Fetch tick-level trades from Alpaca Markets (free tier: IEX feed, regular hours only)."""
    start = dt.datetime.combine(trade_date, start_time).replace(tzinfo=US_EASTERN)
    end = dt.datetime.combine(trade_date, end_time).replace(tzinfo=US_EASTERN)
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 10_000,
        "feed": feed,
        "sort": "asc",
    }
    sess = requests.Session()
    sess.headers.update(headers)
    trades: list[Trade] = []
    url: str = ALPACA_TRADES_URL.format(ticker=ticker.upper())
    while url:
        try:
            resp = sess.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            return trades, str(exc)
        data = resp.json()
        for item in data.get("trades", []):
            ts = parse_trade_time(item.get("t"), trade_date)
            price = item.get("p")
            size = item.get("s")
            if ts is None or price is None or size is None:
                continue
            trades.append(
                Trade(
                    ticker=ticker.upper(),
                    ts=ts,
                    price=float(price),
                    size=int(size),
                    source=f"alpaca-{feed}",
                )
            )
        next_token = data.get("next_page_token")
        if next_token:
            url = ALPACA_TRADES_URL.format(ticker=ticker.upper())
            params = {"page_token": next_token, "limit": 10_000, "feed": feed}
        else:
            url = ""
    return trades, ""


def connect_ibkr(host: str, port: int, client_id: int) -> tuple[Any, str]:
    """Create and return a connected IB instance, or (None, error_msg) on failure."""
    try:
        from ib_insync import IB  # type: ignore[import]
    except ImportError:
        return None, "ib_insync not installed: pip install ib_insync"
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=True)
        return ib, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"IBKR connect failed (is TWS/Gateway running on port {port}?): {exc}"


def fetch_ibkr_trades(
    ib: Any,
    ticker: str,
    trade_date: dt.date,
    start_time: dt.time,
    end_time: dt.time,
) -> tuple[list[Trade], str]:
    """Fetch consolidated-tape tick data for one ticker using an already-connected IB instance.

    Full SIP tape — all exchanges, extended hours supported.
    Paginates in 1000-tick batches across the full session window.
    """
    try:
        from ib_insync import Stock  # type: ignore[import]
    except ImportError:
        return [], "ib_insync not installed"

    try:
        contract = Stock(ticker.upper(), "SMART", "USD")
        ib.qualifyContracts(contract)

        session_start = dt.datetime.combine(trade_date, start_time).replace(tzinfo=US_EASTERN)
        session_end = dt.datetime.combine(trade_date, end_time).replace(tzinfo=US_EASTERN)

        trades: list[Trade] = []
        seen: set[tuple] = set()
        current_start = session_start

        while current_start < session_end:
            start_str = current_start.strftime("%Y%m%d %H:%M:%S") + " US/Eastern"
            ticks = ib.reqHistoricalTicks(
                contract,
                startDateTime=start_str,
                endDateTime="",
                numberOfTicks=1000,
                whatToShow="TRADES",
                useRth=False,
                ignoreSize=False,
                miscOptions=[],
            )
            if not ticks:
                break

            added = 0
            last_ts: dt.datetime | None = None
            for tick in ticks:
                raw_ts = tick.time
                if raw_ts.tzinfo is None:
                    raw_ts = raw_ts.replace(tzinfo=US_EASTERN)
                ts = raw_ts.astimezone(US_EASTERN)
                if ts >= session_end:
                    break
                key = (int(ts.timestamp()), float(tick.price), int(tick.size))
                if key not in seen:
                    seen.add(key)
                    trades.append(
                        Trade(
                            ticker=ticker.upper(),
                            ts=ts,
                            price=float(tick.price),
                            size=int(tick.size),
                            source="ibkr",
                        )
                    )
                    added += 1
                last_ts = ts

            if len(ticks) < 1000 or added == 0 or last_ts is None:
                break
            current_start = last_ts + dt.timedelta(seconds=1)

        return trades, ""

    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def fetch_finnhub_trades(
    ticker: str,
    trade_date: dt.date,
    api_key: str,
) -> tuple[list[Trade], str]:
    """Fetch tick-level trades from Finnhub (free: 60 req/min, regular hours, multi-exchange)."""
    sess = requests.Session()
    trades: list[Trade] = []
    skip = 0
    limit = 500
    while True:
        params: dict[str, Any] = {
            "symbol": ticker.upper(),
            "date": trade_date.isoformat(),
            "limit": limit,
            "skip": skip,
            "token": api_key,
        }
        try:
            resp = sess.get(FINNHUB_TICK_URL, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            return trades, str(exc)
        data = resp.json()
        if data.get("s") != "ok":
            return trades, json.dumps(data)
        prices = data.get("p") or []
        volumes = data.get("v") or []
        timestamps = data.get("t") or []  # Unix milliseconds
        for price, size, ts_ms in zip(prices, volumes, timestamps):
            ts = dt.datetime.fromtimestamp(int(ts_ms) / 1000, UTC).astimezone(US_EASTERN)
            trades.append(
                Trade(
                    ticker=ticker.upper(),
                    ts=ts,
                    price=float(price),
                    size=int(size),
                    source="finnhub",
                )
            )
        if len(prices) < limit:
            break
        skip += limit
        time.sleep(1.0)  # 60 calls/min = max 1/sec
    return trades, ""


def write_trades_csv(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "time", "price", "size", "source"])
        for trade in sorted(trades, key=lambda item: (item.ticker, item.ts)):
            writer.writerow(
                [
                    trade.ticker,
                    trade.ts.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{trade.price:.4f}",
                    trade.size,
                    trade.source,
                ]
            )


def write_clusters_csv(path: Path, clusters: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticker",
                "score",
                "session",
                "start",
                "end",
                "price",
                "trades",
                "volume",
                "notional",
                "direction",
                "source",
            ]
        )
        for cluster in clusters:
            writer.writerow(
                [
                    cluster.ticker,
                    f"{cluster.score:.4f}",
                    cluster.session,
                    cluster.start,
                    cluster.end,
                    f"{cluster.price:.4f}",
                    cluster.trades,
                    cluster.volume,
                    f"{cluster.notional:.2f}",
                    cluster.direction,
                    cluster.source,
                ]
            )


def print_summary(
    clusters: list[Any],
    fetch_results: list[FetchResult],
    holdings_source: str,
    trades_csv: Path,
    clusters_csv: Path,
) -> None:
    ok_count = sum(1 for item in fetch_results if item.ok)
    print(f"universe_source={holdings_source}")
    print(f"fetched={ok_count}/{len(fetch_results)} trades_csv={trades_csv}")
    print(f"clusters_csv={clusters_csv}")
    print("ticker score session     time              price   trades volume notional direction")
    print("------ ----- ----------- ----------------- ------- ------ ------ -------- ---------")
    for cluster in clusters[:25]:
        time_range = cluster.start if cluster.start == cluster.end else f"{cluster.start}-{cluster.end}"
        volume = f"{cluster.volume / 1_000_000:.2f}M" if cluster.volume >= 1_000_000 else f"{cluster.volume / 1_000:.1f}K"
        notional = f"${cluster.notional / 1_000_000:.2f}M" if cluster.notional >= 1_000_000 else f"${cluster.notional / 1_000:.1f}K"
        # Flag high-conviction sweeps: ≥3 repeats AND ≥500K total volume
        sweep_flag = "🔥" if cluster.trades >= 3 and cluster.volume >= 500_000 else "  "
        print(
            f"{sweep_flag}{cluster.ticker:<6} {cluster.score:<5.2f} {cluster.session:<11} "
            f"{time_range:<17} {cluster.price:<7.4f} {cluster.trades:<6} "
            f"{volume:<6} {notional:<8} {cluster.direction}"
        )

    failures = [item for item in fetch_results if not item.ok]
    if failures:
        print("\nfailed:")
        for item in failures[:20]:
            print(f"{item.ticker}: {item.error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch visible Nasdaq extended-hours trades for the top QQQ/Nasdaq-100 names."
    )
    parser.add_argument("--limit", type=int, default=50, help="Number of top QQQ holdings to scan.")
    parser.add_argument("--tickers", nargs="*", default=[], help="Override the top-50 universe.")
    parser.add_argument("--date", help="Trade date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--markettype",
        choices=["post", "pre"],
        default="post",
        help="Nasdaq extended endpoint market type: post=after-hours, pre=premarket.",
    )
    parser.add_argument("--fallback-universe", action="store_true", help="Skip holdings refresh.")
    parser.add_argument("--sleep", type=float, default=0.35, help="Delay between Nasdaq requests.")
    parser.add_argument("--min-cluster-volume", type=int, default=100_000)
    parser.add_argument("--min-single-trade-size", type=int, default=100_000)
    parser.add_argument("--min-repeats", type=int, default=2)
    parser.add_argument("--cluster-window-sec", type=float, default=3.0)
    parser.add_argument("--price-tolerance", type=float, default=0.0)
    parser.add_argument("--out-dir", default="outputs/nasdaq_extended")
    parser.add_argument("--save-raw", action="store_true")
    parser.add_argument(
        "--input-raw-dir",
        help="Parse downloaded Nasdaq JSON files named TICKER_pre.json or TICKER_post.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch one ticker (NVDA) and dump raw API response, skip clustering.",
    )
    parser.add_argument(
        "--source",
        nargs="+",
        choices=["nasdaq", "alpaca", "finnhub", "ibkr"],
        default=["nasdaq"],
        help=(
            "Data source(s). nasdaq=free extended hours (no auth); "
            "alpaca=IEX regular hours (free account); "
            "finnhub=multi-exchange regular hours (free key); "
            "ibkr=full SIP consolidated tape via TWS API (free with IBKR account, best coverage)."
        ),
    )
    parser.add_argument("--alpaca-api-key", default=os.environ.get("ALPACA_API_KEY"))
    parser.add_argument("--alpaca-api-secret", default=os.environ.get("ALPACA_API_SECRET"))
    parser.add_argument(
        "--alpaca-feed",
        choices=["iex", "sip"],
        default="iex",
        help="iex=free (regular hours only), sip=$9/mo (full tape + extended hours).",
    )
    parser.add_argument("--finnhub-api-key", default=os.environ.get("FINNHUB_API_KEY"))
    parser.add_argument("--ibkr-host", default="127.0.0.1", help="TWS/Gateway host.")
    parser.add_argument("--ibkr-port", type=int, default=7497, help="7497=paper TWS, 7496=live TWS, 4002=IB Gateway.")
    parser.add_argument("--ibkr-client-id", type=int, default=1)
    args = parser.parse_args()

    trade_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw" if args.save_raw else None
    sess = build_session()

    # --dry-run: fetch one ticker, dump raw JSON, exit early
    if args.dry_run:
        test_ticker = (args.tickers or ["NVDA"])[0].upper()
        print(f"[dry-run] fetching {test_ticker} {args.markettype} {trade_date} …")
        base_url = NASDAQ_EXTENDED_URL.format(ticker=test_ticker, markettype=args.markettype)
        resp = sess.get(base_url, timeout=20)
        payload = resp.json()
        filter_list = (payload.get("data") or {}).get("filterList") or []
        rows = rows_from_payload(payload)
        print(f"  status={resp.status_code}  filterList={[f.get('value') for f in filter_list]}  rows={len(rows)}")
        if rows:
            print(f"  first row keys: {list(rows[0].keys())}")
            print(f"  first row: {json.dumps(rows[0], ensure_ascii=False)}")
        else:
            print("  (no rows — market likely closed)")
        return 0

    if args.tickers:
        tickers = [ticker.upper() for ticker in args.tickers]
        holdings_source = "manual tickers"
    else:
        tickers, holdings_source = load_top_tickers(
            sess, args.limit, args.fallback_universe
        )

    # Validate source credentials early
    if "alpaca" in args.source and not (args.alpaca_api_key and args.alpaca_api_secret):
        raise SystemExit(
            "Alpaca source requires ALPACA_API_KEY and ALPACA_API_SECRET env vars "
            "(or --alpaca-api-key / --alpaca-api-secret)."
        )
    if "finnhub" in args.source and not args.finnhub_api_key:
        raise SystemExit(
            "Finnhub source requires FINNHUB_API_KEY env var (or --finnhub-api-key)."
        )

    # Connect IBKR once before the ticker loop — avoid per-ticker connect/disconnect
    ibkr_conn: Any = None
    if "ibkr" in args.source:
        ibkr_conn, ibkr_err = connect_ibkr(args.ibkr_host, args.ibkr_port, args.ibkr_client_id)
        if ibkr_conn is None:
            print(f"[ibkr] {ibkr_err}")
            args.source = [s for s in args.source if s != "ibkr"]

    ibkr_start_t = dt.time(16, 0) if args.markettype == "post" else dt.time(4, 0)
    ibkr_end_t = dt.time(20, 0) if args.markettype == "post" else dt.time(9, 30)

    all_trades: list[Trade] = []
    fetch_results: list[FetchResult] = []
    try:
        for ticker in tickers:
            ticker_trades: list[Trade] = []
            errors: list[str] = []

            if "nasdaq" in args.source:
                try:
                    if args.input_raw_dir:
                        trades, error = load_nasdaq_raw(
                            ticker, args.markettype, trade_date, Path(args.input_raw_dir)
                        )
                    else:
                        trades, error = fetch_nasdaq_extended(
                            sess, ticker, args.markettype, trade_date, raw_dir
                        )
                    ticker_trades.extend(trades)
                    if error:
                        errors.append(f"nasdaq:{error}")
                except (
                    requests.RequestException,
                    subprocess.SubprocessError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    errors.append(f"nasdaq:{exc}")

            if "alpaca" in args.source:
                try:
                    trades, error = fetch_alpaca_trades(
                        ticker, trade_date, ibkr_start_t, ibkr_end_t,
                        args.alpaca_api_key, args.alpaca_api_secret, args.alpaca_feed,
                    )
                    ticker_trades.extend(trades)
                    if error:
                        errors.append(f"alpaca:{error}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"alpaca:{exc}")

            if "finnhub" in args.source:
                try:
                    trades, error = fetch_finnhub_trades(
                        ticker, trade_date, args.finnhub_api_key,
                    )
                    ticker_trades.extend(trades)
                    if error:
                        errors.append(f"finnhub:{error}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"finnhub:{exc}")

            if "ibkr" in args.source and ibkr_conn is not None:
                try:
                    trades, error = fetch_ibkr_trades(
                        ibkr_conn, ticker, trade_date, ibkr_start_t, ibkr_end_t,
                    )
                    ticker_trades.extend(trades)
                    if error:
                        errors.append(f"ibkr:{error}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"ibkr:{exc}")

            all_trades.extend(ticker_trades)
            fetch_results.append(
                FetchResult(
                    ticker=ticker,
                    ok=len(ticker_trades) > 0 or not errors,
                    trades=len(ticker_trades),
                    error="; ".join(errors),
                )
            )
            time.sleep(args.sleep)
    finally:
        if ibkr_conn is not None and ibkr_conn.isConnected():
            ibkr_conn.disconnect()

    start_time = dt.time(16, 0) if args.markettype == "post" else dt.time(4, 0)
    end_time = dt.time(20, 0) if args.markettype == "post" else dt.time(9, 30)
    clusters = find_clusters(
        all_trades,
        start_time=start_time,
        end_time=end_time,
        window_seconds=args.cluster_window_sec,
        price_tolerance=args.price_tolerance,
        min_cluster_volume=args.min_cluster_volume,
        min_single_trade_size=args.min_single_trade_size,
        min_repeats=args.min_repeats,
    )

    suffix = f"{trade_date}_{args.markettype}"
    trades_csv = out_dir / f"nasdaq100_top{len(tickers)}_{suffix}_trades.csv"
    clusters_csv = out_dir / f"nasdaq100_top{len(tickers)}_{suffix}_clusters.csv"
    write_trades_csv(trades_csv, all_trades)
    write_clusters_csv(clusters_csv, clusters)
    print_summary(clusters, fetch_results, holdings_source, trades_csv, clusters_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
