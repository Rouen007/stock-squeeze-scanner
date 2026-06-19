#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


US_EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
POLYGON_TRADES = "https://api.polygon.io/v3/trades/{ticker}"


@dataclass(frozen=True)
class Trade:
    ticker: str
    ts: dt.datetime
    price: float
    size: int
    source: str

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class TapeCluster:
    ticker: str
    start: str
    end: str
    session: str
    price: float
    trades: int
    volume: int
    notional: float
    direction: str
    score: float
    source: str


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_trade_time(value: Any, trade_date: dt.date | None) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%I:%M:%S %p",
        "%I:%M %p",
        "%H:%M:%S.%f",
        "%H:%M:%S",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if "%Y" not in fmt and "%y" not in fmt:
                if trade_date is None:
                    trade_date = dt.datetime.now(US_EASTERN).date()
                parsed = dt.datetime.combine(trade_date, parsed.time())
            return parsed.replace(tzinfo=US_EASTERN)
        except ValueError:
            pass

    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=US_EASTERN)
    return parsed.astimezone(US_EASTERN)


def load_csv_trades(path: Path, default_ticker: str | None, trade_date: dt.date | None) -> list[Trade]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        headers = {normalize_header(name): name for name in reader.fieldnames}
        ticker_key = headers.get("ticker") or headers.get("symbol")
        time_key = (
            headers.get("time")
            or headers.get("timestamp")
            or headers.get("datetime")
            or headers.get("tradetime")
        )
        price_key = headers.get("price") or headers.get("tradeprice")
        size_key = (
            headers.get("size")
            or headers.get("volume")
            or headers.get("shares")
            or headers.get("qty")
            or headers.get("quantity")
        )
        if not time_key or not price_key or not size_key:
            raise ValueError(
                f"{path} must include time/timestamp, price, and size/volume columns"
            )

        trades: list[Trade] = []
        for raw in reader:
            ticker = (raw.get(ticker_key) if ticker_key else default_ticker) or default_ticker
            if not ticker:
                ticker = path.stem.split("_")[0]
            ts = parse_trade_time(raw.get(time_key), trade_date)
            price = parse_number(raw.get(price_key))
            size = parse_number(raw.get(size_key))
            if ts is None or price is None or size is None:
                continue
            trades.append(
                Trade(
                    ticker=str(ticker).upper(),
                    ts=ts,
                    price=float(price),
                    size=int(size),
                    source=str(path),
                )
            )
        return trades


def ns_since_epoch(value: dt.datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000_000_000)


def fetch_polygon_trades(
    ticker: str,
    trade_date: dt.date,
    start_time: dt.time,
    end_time: dt.time,
    api_key: str,
    limit: int,
) -> list[Trade]:
    start = dt.datetime.combine(trade_date, start_time, US_EASTERN)
    end = dt.datetime.combine(trade_date, end_time, US_EASTERN)
    params: dict[str, Any] = {
        "timestamp.gte": ns_since_epoch(start),
        "timestamp.lt": ns_since_epoch(end),
        "order": "asc",
        "limit": 50000,
        "apiKey": api_key,
    }
    url = POLYGON_TRADES.format(ticker=ticker.upper())
    trades: list[Trade] = []
    sess = requests.Session()

    while url and len(trades) < limit:
        resp = sess.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("results", []):
            sip_ts = item.get("sip_timestamp") or item.get("participant_timestamp")
            price = item.get("price")
            size = item.get("size")
            if sip_ts is None or price is None or size is None:
                continue
            ts = dt.datetime.fromtimestamp(int(sip_ts) / 1_000_000_000, UTC).astimezone(
                US_EASTERN
            )
            trades.append(
                Trade(
                    ticker=ticker.upper(),
                    ts=ts,
                    price=float(price),
                    size=int(size),
                    source="polygon",
                )
            )
            if len(trades) >= limit:
                break
        next_url = data.get("next_url")
        url = f"{next_url}&apiKey={api_key}" if next_url else ""
        params = {}
    return trades


def in_time_window(trade: Trade, start_time: dt.time, end_time: dt.time) -> bool:
    current = trade.ts.astimezone(US_EASTERN).time()
    return start_time <= current < end_time


def session_label(value: dt.datetime) -> str:
    current = value.astimezone(US_EASTERN).time()
    if dt.time(4, 0) <= current < dt.time(9, 30):
        return "premarket"
    if dt.time(9, 30) <= current < dt.time(16, 0):
        return "regular"
    if dt.time(16, 0) <= current < dt.time(20, 0):
        return "after-hours"
    return "overnight"


def direction_for_cluster(all_trades: list[Trade], start_index: int, end_index: int) -> str:
    previous_price = all_trades[start_index - 1].price if start_index > 0 else None
    next_price = all_trades[end_index + 1].price if end_index + 1 < len(all_trades) else None
    cluster_price = all_trades[start_index].price
    if previous_price is not None:
        if cluster_price > previous_price:
            return "up"
        if cluster_price < previous_price:
            return "down"
    if next_price is not None:
        if next_price > cluster_price:
            return "up-follow"
        if next_price < cluster_price:
            return "down-follow"
    return "flat"


def find_clusters(
    trades: Iterable[Trade],
    start_time: dt.time,
    end_time: dt.time,
    window_seconds: float,
    price_tolerance: float,
    min_cluster_volume: int,
    min_single_trade_size: int,
    min_repeats: int,
) -> list[TapeCluster]:
    by_ticker: dict[str, list[Trade]] = {}
    for trade in trades:
        if in_time_window(trade, start_time, end_time):
            by_ticker.setdefault(trade.ticker, []).append(trade)

    clusters: list[TapeCluster] = []
    for ticker, ticker_trades in by_ticker.items():
        ticker_trades.sort(key=lambda item: item.ts)
        for start_index, seed in enumerate(ticker_trades):
            cluster = [seed]
            end_index = start_index
            for idx in range(start_index + 1, len(ticker_trades)):
                candidate = ticker_trades[idx]
                seconds = (candidate.ts - seed.ts).total_seconds()
                if seconds > window_seconds:
                    break
                if abs(candidate.price - seed.price) <= price_tolerance:
                    cluster.append(candidate)
                    end_index = idx

            volume = sum(item.size for item in cluster)
            largest = max(item.size for item in cluster)
            if volume < min_cluster_volume and largest < min_single_trade_size:
                continue
            if len(cluster) < min_repeats and largest < min_single_trade_size:
                continue

            notional = sum(item.notional for item in cluster)
            direction = direction_for_cluster(ticker_trades, start_index, end_index)
            score = (
                volume / max(min_cluster_volume, 1)
                + notional / 1_000_000
                + len(cluster) / max(min_repeats, 1)
            )
            if direction.startswith("up"):
                score += 1.0
            clusters.append(
                TapeCluster(
                    ticker=ticker,
                    start=cluster[0].ts.strftime("%H:%M:%S"),
                    end=cluster[-1].ts.strftime("%H:%M:%S"),
                    session=session_label(cluster[0].ts),
                    price=seed.price,
                    trades=len(cluster),
                    volume=volume,
                    notional=notional,
                    direction=direction,
                    score=score,
                    source=seed.source,
                )
            )

    dedup: dict[tuple[str, str, str, float], TapeCluster] = {}
    for cluster in clusters:
        key = (cluster.ticker, cluster.start, cluster.end, round(cluster.price, 4))
        current = dedup.get(key)
        if current is None or cluster.score > current.score:
            dedup[key] = cluster

    return remove_overlapping_clusters(
        sorted(dedup.values(), key=lambda item: item.score, reverse=True)
    )


def hms_to_seconds(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def remove_overlapping_clusters(clusters: list[TapeCluster]) -> list[TapeCluster]:
    kept: list[TapeCluster] = []
    occupied: dict[str, list[tuple[int, int]]] = {}
    for cluster in clusters:
        start = hms_to_seconds(cluster.start)
        end = hms_to_seconds(cluster.end)
        ranges = occupied.setdefault(cluster.ticker, [])
        if any(start <= used_end and end >= used_start for used_start, used_end in ranges):
            continue
        ranges.append((start, end))
        kept.append(cluster)
    return kept


def fmt_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def fmt_num(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def print_table(clusters: list[TapeCluster], args: argparse.Namespace) -> None:
    print(
        "extended-hours tape clusters "
        f"range={args.start}-{args.end} "
        f"window={args.cluster_window_sec:g}s min_volume={args.min_cluster_volume} "
        f"min_single={args.min_single_trade_size} min_repeats={args.min_repeats}"
    )
    headers = [
        "ticker",
        "score",
        "session",
        "time",
        "price",
        "trades",
        "volume",
        "notional",
        "dir",
        "source",
    ]
    rows = [
        [
            c.ticker,
            f"{c.score:.2f}",
            c.session,
            c.start if c.start == c.end else f"{c.start}-{c.end}",
            f"{c.price:.4f}",
            str(c.trades),
            fmt_num(c.volume),
            fmt_money(c.notional),
            c.direction,
            "csv" if c.source != "polygon" else c.source,
        ]
        for c in clusters[: args.top_n]
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    print(" ".join(h.ljust(widths[idx]) for idx, h in enumerate(headers)))
    print(" ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in rows:
        print(" ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))


def parse_time_arg(value: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use HH:MM or HH:MM:SS") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan time-and-sales for repeated large prints at the same price. "
            "Defaults to the full U.S. extended session, 04:00-20:00 ET."
        )
    )
    parser.add_argument("--tickers", nargs="*", default=[], help="Tickers to fetch from Polygon.")
    parser.add_argument("--csv", nargs="*", default=[], help="Time-and-sales CSV file(s) to scan.")
    parser.add_argument("--csv-dir", help="Directory of CSV files to scan.")
    parser.add_argument("--date", help="Trade date in U.S. Eastern time, YYYY-MM-DD.")
    parser.add_argument("--start", type=parse_time_arg, default=dt.time(4, 0))
    parser.add_argument("--end", type=parse_time_arg, default=dt.time(20, 0))
    parser.add_argument("--cluster-window-sec", type=float, default=3.0)
    parser.add_argument("--price-tolerance", type=float, default=0.0)
    parser.add_argument("--min-cluster-volume", type=int, default=100_000)
    parser.add_argument("--min-single-trade-size", type=int, default=100_000)
    parser.add_argument("--min-repeats", type=int, default=3)
    parser.add_argument("--polygon-api-key", default=os.environ.get("POLYGON_API_KEY"))
    parser.add_argument("--fetch-limit", type=int, default=250_000)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    trade_date = (
        dt.date.fromisoformat(args.date)
        if args.date
        else dt.datetime.now(US_EASTERN).date()
    )

    trades: list[Trade] = []
    csv_paths = [Path(path) for path in args.csv]
    if args.csv_dir:
        csv_paths.extend(sorted(Path(args.csv_dir).glob("*.csv")))
    for path in csv_paths:
        trades.extend(load_csv_trades(path, None, trade_date))

    if args.tickers:
        if not args.polygon_api_key:
            raise SystemExit("Set POLYGON_API_KEY or pass --polygon-api-key to fetch live tape.")
        for ticker in args.tickers:
            trades.extend(
                fetch_polygon_trades(
                    ticker=ticker,
                    trade_date=trade_date,
                    start_time=args.start,
                    end_time=args.end,
                    api_key=args.polygon_api_key,
                    limit=args.fetch_limit,
                )
            )

    if not trades:
        raise SystemExit("No trades loaded. Pass --csv/--csv-dir or --tickers with POLYGON_API_KEY.")

    clusters = find_clusters(
        trades,
        start_time=args.start,
        end_time=args.end,
        window_seconds=args.cluster_window_sec,
        price_tolerance=args.price_tolerance,
        min_cluster_volume=args.min_cluster_volume,
        min_single_trade_size=args.min_single_trade_size,
        min_repeats=args.min_repeats,
    )

    if args.json:
        print(json.dumps([asdict(item) for item in clusters[: args.top_n]], indent=2))
    else:
        print_table(clusters, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
