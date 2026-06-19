#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any
from zoneinfo import ZoneInfo

import requests


FINRA_BASE = "https://cdn.finra.org/equity/regsho/daily"
SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
STOCKANALYSIS_STATS = "https://stockanalysis.com/stocks/{ticker}/statistics/"

US_EASTERN = ZoneInfo("America/New_York")

ITEM_202_RE = re.compile(
    r"item\s+2\.02|2\.02\s+results\s+of\s+operations\s+and\s+financial\s+condition",
    re.IGNORECASE,
)

BULLISH_WORDS = [
    "raised guidance",
    "raise guidance",
    "guidance raised",
    "reiterated guidance",
    "beat",
    "exceeded",
    "record revenue",
    "record quarterly revenue",
    "record gross margin",
    "margin expansion",
    "improved margin",
    "accelerating",
    "strong demand",
    "strong growth",
    "positive cash flow",
    "free cash flow",
    "profitability",
    "operating leverage",
]

EXCLUDED_TITLE_WORDS = [
    "etf",
    "trust",
    "fund",
    "index",
    "bond",
    "notes",
    "note",
    "treasury",
    "etn",
    "portfolio",
]


@dataclass
class WatchRow:
    ticker: str
    float_m: float | None = None
    borrow_fee_pct: float | None = None
    premarket_gap_pct: float | None = None


@dataclass
class ScanResult:
    ticker: str
    short_volume: int | None
    total_volume: int | None
    short_volume_pct: float | None
    earnings_date: str | None
    earnings_event: bool
    item_202: bool
    bullish_hits: list[str]
    float_m: float | None
    borrow_fee_pct: float | None
    premarket_gap_pct: float | None
    session_gap_pct: float | None
    market_cap_m: float | None
    float_m: float | None
    short_interest_m: float | None
    short_interest_float_pct: float | None
    short_ratio_days: float | None
    score: float
    notes: str


@dataclass
class FinraRow:
    ticker: str
    short_volume: int | None
    total_volume: int | None
    short_exempt_volume: int | None
    market: str | None

    @property
    def short_volume_pct(self) -> float | None:
        if self.short_volume is None or not self.total_volume:
            return None
        return self.short_volume / self.total_volume


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return None


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.environ.get(
                "SEC_USER_AGENT",
                "StockSqueezeScanner/1.0 contact=you@example.com",
            ),
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json,text/plain,text/html,*/*",
        }
    )
    return s


def previous_us_business_day(now: dt.datetime | None = None) -> dt.date:
    current = now or dt.datetime.now(US_EASTERN)
    candidate = current.date()
    if current.hour < 18:
        candidate -= dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= dt.timedelta(days=1)
    return candidate


def business_day_back(day: dt.date, back: int) -> dt.date:
    candidate = day
    count = 0
    while count < back:
        candidate -= dt.timedelta(days=1)
        if candidate.weekday() < 5:
            count += 1
    return candidate


def fetch_text(sess: requests.Session, url: str, timeout: int = 30) -> str:
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_json(sess: requests.Session, url: str, timeout: int = 30) -> dict[str, Any]:
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def load_watchlist(path: str | None, tickers: list[str]) -> list[WatchRow]:
    rows: list[WatchRow] = []
    if path:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                ticker = (raw.get("ticker") or raw.get("Ticker") or "").strip().upper()
                if not ticker:
                    continue
                rows.append(
                    WatchRow(
                        ticker=ticker,
                        float_m=to_float(raw.get("float_m") or raw.get("float")),
                        borrow_fee_pct=to_float(
                            raw.get("borrow_fee_pct") or raw.get("borrow_fee")
                        ),
                        premarket_gap_pct=to_float(
                            raw.get("premarket_gap_pct") or raw.get("gap_pct")
                        ),
                    )
                )
    for ticker in tickers:
        rows.append(WatchRow(ticker=ticker.upper()))
    dedup: dict[str, WatchRow] = {}
    for row in rows:
        dedup[row.ticker] = row
    return list(dedup.values())


def load_optional_metrics(path: str | None) -> dict[str, dict[str, float]]:
    if not path:
        return {}
    metric_path = Path(path)
    if not metric_path.exists():
        return {}
    metrics: dict[str, dict[str, float]] = {}
    with metric_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            ticker = (raw.get("ticker") or raw.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            bucket: dict[str, float] = {}
            for key in ("float_m", "borrow_fee_pct", "premarket_gap_pct"):
                value = to_float(raw.get(key))
                if value is not None:
                    bucket[key] = value
            if bucket:
                metrics[ticker] = bucket
    return metrics


def fetch_stockanalysis_snapshot(sess: requests.Session, ticker: str) -> dict[str, Any]:
    url = STOCKANALYSIS_STATS.format(ticker=ticker.lower())
    try:
        resp = sess.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return {}

    html = resp.text
    patterns = [
        r'"id":"marketcap".*?"value":"([0-9.]+)([MBT])"',
        r'"title":"Market Cap".*?"value":"([0-9.]+)([MBT])"',
        r"market cap or net worth of \$([0-9.]+)\s*(billion|million|trillion)",
    ]
    snapshot: dict[str, Any] = {}
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit in ("b", "billion"):
            snapshot["market_cap_m"] = value * 1000.0
            break
        if unit in ("m", "million"):
            snapshot["market_cap_m"] = value
            break
        if unit in ("t", "trillion"):
            snapshot["market_cap_m"] = value * 1_000_000.0
            break

    prev_close_match = re.search(r'"p":(-?[0-9.]+)', html)
    ext_price_match = re.search(r'"ep":(-?[0-9.]+)', html)
    session_label_match = re.search(r'"es":"([^"]+)"', html)
    if prev_close_match and ext_price_match:
        prev_close = float(prev_close_match.group(1))
        ext_price = float(ext_price_match.group(1))
        if prev_close:
            snapshot["session_gap_pct"] = (ext_price - prev_close) / prev_close
    if session_label_match:
        snapshot["session_label"] = session_label_match.group(1)

    float_match = re.search(
        r'"title":"Float","value":"([0-9.]+)([MBT])"',
        html,
        re.IGNORECASE,
    )
    if float_match:
        value = float(float_match.group(1))
        unit = float_match.group(2).lower()
        if unit == "b":
            snapshot["float_m"] = value * 1000.0
        elif unit == "m":
            snapshot["float_m"] = value
        elif unit == "t":
            snapshot["float_m"] = value * 1_000_000.0

    short_interest_match = re.search(
        r'"title":"Short Interest","value":"([0-9.]+)([MBT])"',
        html,
        re.IGNORECASE,
    )
    if short_interest_match:
        value = float(short_interest_match.group(1))
        unit = short_interest_match.group(2).lower()
        if unit == "b":
            snapshot["short_interest_m"] = value * 1000.0
        elif unit == "m":
            snapshot["short_interest_m"] = value
        elif unit == "t":
            snapshot["short_interest_m"] = value * 1_000_000.0

    pct_patterns = {
        "short_interest_float_pct": r'"title":"Short % of Float","value":"(-?[0-9.]+)%"',
        "short_ratio_days": r'"title":"Short Ratio \(days to cover\)","value":"(-?[0-9.]+)"',
    }
    for key, pattern in pct_patterns.items():
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            snapshot[key] = float(match.group(1))
    return snapshot


def load_ticker_map(sess: requests.Session) -> dict[str, dict[str, Any]]:
    data = fetch_json(sess, SEC_COMPANY_TICKERS)
    mapping: dict[str, dict[str, Any]] = {}
    for item in data.values():
        ticker = str(item["ticker"]).upper()
        mapping[ticker] = item
    return mapping


def is_operating_company_title(title: str | None) -> bool:
    if not title:
        return True
    lower = title.lower()
    if any(token in lower for token in EXCLUDED_TITLE_WORDS):
        return False
    if any(lower.startswith(prefix) for prefix in ("ishares ", "vanguard ", "spdr ", "proshares ", "direxion ", "invesco ", "grayscale ", "global x ", "wisdomtree ")):
        return False
    return True


def latest_8k_item_202(
    sess: requests.Session,
    cik10: str,
    lookback_days: int,
    asof: dt.date,
) -> dict[str, Any] | None:
    submissions = fetch_json(sess, SEC_SUBMISSIONS.format(cik10=cik10))
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    for idx, form in enumerate(forms):
        if form != "8-K":
            continue
        filing_date_text = filing_dates[idx] if idx < len(filing_dates) else None
        if not filing_date_text:
            continue
        filing_date = dt.date.fromisoformat(filing_date_text)
        age = (asof - filing_date).days
        if age < 0 or age > lookback_days:
            continue

        accession = accession_numbers[idx]
        primary_doc = primary_docs[idx]
        accession_nodash = accession.replace("-", "")
        filing_dir_url = SEC_ARCHIVES.format(
            cik=str(int(cik10)),
            accession=accession_nodash,
            doc="index.json",
        )
        filing_url = SEC_ARCHIVES.format(
            cik=str(int(cik10)),
            accession=accession_nodash,
            doc=primary_doc,
        )
        try:
            filing_index = fetch_json(sess, filing_dir_url)
        except requests.HTTPError:
            continue

        docs = [primary_doc]
        for item in filing_index.get("directory", {}).get("item", []):
            name = item.get("name")
            if not name:
                continue
            lower = str(name).lower()
            if lower.endswith(".htm") or lower.endswith(".html"):
                docs.append(str(name))

        seen_docs: set[str] = set()
        bullish_hits: list[str] = []
        press_release_like = False
        item_202 = False

        for doc_name in docs:
            if doc_name in seen_docs:
                continue
            seen_docs.add(doc_name)
            doc_url = SEC_ARCHIVES.format(
                cik=str(int(cik10)),
                accession=accession_nodash,
                doc=doc_name,
            )
            try:
                filing_text = fetch_text(sess, doc_url)
            except requests.HTTPError:
                continue

            lower = filing_text.lower()
            if "ex-99" in lower or "press release" in lower:
                press_release_like = True
            if ITEM_202_RE.search(filing_text):
                item_202 = True
            for word in BULLISH_WORDS:
                if word in lower:
                    bullish_hits.append(word)
            if item_202 or bullish_hits:
                break

        return {
            "filing_date": filing_date_text,
            "accession": accession,
            "primary_doc": primary_doc,
            "item_202": item_202,
            "bullish_hits": bullish_hits,
            "press_release_like": press_release_like,
        }
    return None


def fetch_finra_short_volume(sess: requests.Session, trade_date: dt.date) -> dict[str, FinraRow]:
    url = f"{FINRA_BASE}/CNMSshvol{trade_date:%Y%m%d}.txt"
    resp = sess.get(url, timeout=30)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    text = resp.text.strip()
    if not text:
        return {}
    reader = csv.DictReader(text.splitlines(), delimiter="|")
    rows: dict[str, FinraRow] = {}
    for raw in reader:
        normalized = {normalize_key(k): v for k, v in raw.items()}
        symbol = (normalized.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        short_volume = to_int(normalized.get("shortvolume"))
        total_volume = to_int(normalized.get("totalvolume"))
        short_exempt_volume = to_int(normalized.get("shortexemptvolume"))
        rows[symbol] = FinraRow(
            ticker=symbol,
            short_volume=short_volume,
            total_volume=total_volume,
            short_exempt_volume=short_exempt_volume,
            market=(normalized.get("market") or None),
        )
    return rows


def build_auto_watchlist(
    finra_rows: dict[str, FinraRow],
    ticker_map: dict[str, dict[str, Any]],
    min_short_volume_pct: float,
    min_total_volume: int,
    top_n: int,
) -> list[WatchRow]:
    scored: list[tuple[str, float, int]] = []
    for ticker, row in finra_rows.items():
        ticker_info = ticker_map.get(ticker)
        if ticker_info and not is_operating_company_title(str(ticker_info.get("title"))):
            continue
        pct = row.short_volume_pct
        if pct is None:
            continue
        total_volume = row.total_volume or 0
        if pct < min_short_volume_pct:
            continue
        if total_volume < min_total_volume:
            continue
        scored.append((ticker, pct, total_volume))

    scored.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return [WatchRow(ticker=ticker) for ticker, _, _ in scored[:top_n]]


def evaluate_candidate(
    row: WatchRow,
    finra_row: FinraRow | None,
    earnings: dict[str, Any] | None,
    market_cap_m: float | None,
    session_gap_pct: float | None,
    float_m: float | None,
    short_interest_m: float | None,
    short_interest_float_pct: float | None,
    short_ratio_days: float | None,
    min_short_pct: float,
    min_market_cap_m: float,
) -> ScanResult:
    short_volume = finra_row.short_volume if finra_row else None
    total_volume = finra_row.total_volume if finra_row else None
    short_volume_pct = finra_row.short_volume_pct if finra_row else None

    earnings_date = earnings["filing_date"] if earnings else None
    item_202 = bool(earnings and earnings.get("item_202"))
    bullish_hits = list(earnings.get("bullish_hits", [])) if earnings else []
    press_release_like = bool(earnings and earnings.get("press_release_like"))
    earnings_event = bool(earnings and (item_202 or press_release_like))

    score = 0.0
    notes: list[str] = []

    if short_volume_pct is not None:
        if short_volume_pct >= min_short_pct:
            score += 3
            notes.append(f"short_volume_pct>={min_short_pct:.0%}")
        if short_volume_pct >= 0.20:
            score += 1
        if short_volume_pct >= 0.30:
            score += 1

    if earnings_event:
        score += 3
        notes.append("recent_earnings_release")
    if item_202:
        score += 1
    if bullish_hits:
        score += min(3, len(set(bullish_hits)))
        notes.append(f"bullish_hits={len(set(bullish_hits))}")

    if float_m is not None and float_m <= 100:
        score += 1
        notes.append("float<=100m")
    if short_interest_float_pct is not None:
        if short_interest_float_pct >= 10:
            score += 1
            notes.append("si/float>=10%")
        if short_interest_float_pct >= 20:
            score += 1
    if short_ratio_days is not None and short_ratio_days >= 3:
        score += 1
        notes.append("days_to_cover>=3")
    if row.borrow_fee_pct is not None and row.borrow_fee_pct >= 10:
        score += 1
        notes.append("borrow_fee_high")
    if row.premarket_gap_pct is not None and row.premarket_gap_pct >= 0.03:
        score += 1
        notes.append("premarket_gap>=3%")
    if session_gap_pct is not None and session_gap_pct >= 0.03:
        score += 1
        notes.append("session_gap>=3%")

    if market_cap_m is not None:
        if market_cap_m >= min_market_cap_m:
            score += 1
            notes.append(f"market_cap>={min_market_cap_m/1000:.1f}B")
        else:
            notes.append(f"market_cap<{min_market_cap_m/1000:.1f}B")

    qualifies = (
        short_volume_pct is not None
        and short_volume_pct >= min_short_pct
        and earnings_event
    )
    if qualifies:
        score += 2
        notes.append("core_setup")

    return ScanResult(
        ticker=row.ticker,
        short_volume=short_volume,
        total_volume=total_volume,
        short_volume_pct=short_volume_pct,
        earnings_date=earnings_date,
        earnings_event=earnings_event,
        item_202=item_202,
        bullish_hits=sorted(set(bullish_hits)),
        float_m=float_m,
        borrow_fee_pct=row.borrow_fee_pct,
        premarket_gap_pct=row.premarket_gap_pct,
        session_gap_pct=session_gap_pct,
        market_cap_m=market_cap_m,
        short_interest_m=short_interest_m,
        short_interest_float_pct=short_interest_float_pct,
        short_ratio_days=short_ratio_days,
        score=score,
        notes=", ".join(notes) if notes else "",
    )


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:5.1f}%"


def fmt_num(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def fmt_market_cap(value_m: float | None) -> str:
    if value_m is None:
        return "-"
    if value_m >= 1000:
        return f"{value_m / 1000:.2f}B"
    return f"{value_m:.1f}M"


def print_table(results: list[ScanResult], trade_date: dt.date, min_short_pct: float) -> None:
    headers = [
        "ticker",
        "score",
        "short-volume%",
        "short vol",
        "total vol",
        "earnings",
        "item2.02",
        "bullish hits",
        "borrow%",
        "premkt%",
        "session%",
        "mkt cap",
        "float",
        "short int",
        "si/float",
        "days",
        "basis",
    ]
    rows = []
    for r in results:
        rows.append(
            [
                r.ticker,
                f"{r.score:.1f}",
                fmt_pct(r.short_volume_pct),
                fmt_num(r.short_volume),
                fmt_num(r.total_volume),
                r.earnings_date or "-",
                "yes" if r.item_202 else "no",
                ",".join(r.bullish_hits) if r.bullish_hits else "-",
                "-" if r.borrow_fee_pct is None else f"{r.borrow_fee_pct:.1f}",
                "-" if r.premarket_gap_pct is None else f"{r.premarket_gap_pct * 100:.1f}%",
                "-" if r.session_gap_pct is None else f"{r.session_gap_pct * 100:.1f}%",
                fmt_market_cap(r.market_cap_m),
                "-" if r.float_m is None else f"{r.float_m:.1f}M",
                "-" if r.short_interest_m is None else f"{r.short_interest_m:.1f}M",
                "-" if r.short_interest_float_pct is None else f"{r.short_interest_float_pct:.2f}%",
                "-" if r.short_ratio_days is None else f"{r.short_ratio_days:.2f}",
                r.notes or "-",
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))

    print(f"trade_date={trade_date.isoformat()} min_short_volume_pct={min_short_pct:.0%}")
    print(
        "data: FINRA short volume = short vol / total vol; SEC 8-K for earnings; "
        "market cap/float/short interest from StockAnalysis when available"
    )
    print(
        "rules: short-volume>=threshold, recent earnings-related 8-K, market cap>=min, "
        "exclude funds/ETFs/trusts, score boosts for float, short interest, days-to-cover, "
        "borrow, and positive session/premarket moves"
    )
    print(
        "score: +3 if short-volume>=threshold, +1 at 20%, +1 at 30%, +3 if recent earnings release, "
        "+1 if Item 2.02, +1 to +3 for bullish wording, +1 if float<=100M, +1 if si/float>=10%, "
        "+1 more if si/float>=20%, +1 if days-to-cover>=3, +1 if borrow>=10%, +1 if premkt>=3%, "
        "+1 if session>=3%, +1 if market cap>=min, +2 if core setup is complete"
    )
    print(" ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print(" ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print(" ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan for short-volume + earnings squeeze setups."
    )
    parser.add_argument(
        "--watchlist",
        help="CSV file with columns ticker,float_m,borrow_fee_pct,premarket_gap_pct",
    )
    parser.add_argument(
        "--metrics",
        help="Optional CSV with columns ticker,float_m,borrow_fee_pct,premarket_gap_pct to enrich auto-scan results.",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Tickers to scan if no watchlist CSV is provided.",
    )
    parser.add_argument(
        "--trade-date",
        help="FINRA trade date to scan (YYYY-MM-DD). Defaults to the latest completed U.S. business day.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=2,
        help="How many calendar days back to accept an earnings filing.",
    )
    parser.add_argument(
        "--min-short-volume-pct",
        type=float,
        default=0.15,
        help="Minimum short volume ratio to flag.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a table.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="Maximum number of auto-selected candidates to keep when scanning the full FINRA universe.",
    )
    parser.add_argument(
        "--min-total-volume",
        type=int,
        default=1_000_000,
        help="Minimum total FINRA volume to consider in auto mode.",
    )
    parser.add_argument(
        "--auto-require-earnings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="In auto mode, keep only names with a recent earnings-related 8-K when possible.",
    )
    parser.add_argument(
        "--min-market-cap",
        type=float,
        default=5000.0,
        help="Minimum market cap in millions to keep a name in the scan.",
    )
    args = parser.parse_args()

    sess = session()
    current_et = dt.datetime.now(US_EASTERN)
    trade_date = (
        dt.date.fromisoformat(args.trade_date)
        if args.trade_date
        else previous_us_business_day()
    )
    earnings_asof = current_et.date()
    finra_rows = fetch_finra_short_volume(sess, trade_date)
    ticker_map = load_ticker_map(sess)
    optional_metrics = load_optional_metrics(args.metrics)

    if args.watchlist or args.tickers:
        rows = load_watchlist(args.watchlist, args.tickers)
    else:
        rows = build_auto_watchlist(
            finra_rows,
            ticker_map,
            args.min_short_volume_pct,
            args.min_total_volume,
            args.top_n,
        )

    results: list[ScanResult] = []
    for row in rows:
        if row.float_m is None or row.borrow_fee_pct is None or row.premarket_gap_pct is None:
            extra = optional_metrics.get(row.ticker, {})
            row = WatchRow(
                ticker=row.ticker,
                float_m=row.float_m if row.float_m is not None else extra.get("float_m"),
                borrow_fee_pct=(
                    row.borrow_fee_pct
                    if row.borrow_fee_pct is not None
                    else extra.get("borrow_fee_pct")
                ),
                premarket_gap_pct=(
                    row.premarket_gap_pct
                    if row.premarket_gap_pct is not None
                    else extra.get("premarket_gap_pct")
                ),
            )
        finra_row = finra_rows.get(row.ticker)
        earnings = None
        ticker_info = ticker_map.get(row.ticker)
        if ticker_info:
            cik10 = str(ticker_info["cik_str"]).zfill(10)
            earnings = latest_8k_item_202(sess, cik10, args.lookback_days, earnings_asof)
        stockanalysis_snapshot = fetch_stockanalysis_snapshot(sess, row.ticker)
        market_cap_m = stockanalysis_snapshot.get("market_cap_m")
        session_gap_pct = stockanalysis_snapshot.get("session_gap_pct")
        float_m = stockanalysis_snapshot.get("float_m") or row.float_m
        short_interest_m = stockanalysis_snapshot.get("short_interest_m")
        short_interest_float_pct = stockanalysis_snapshot.get("short_interest_float_pct")
        short_ratio_days = stockanalysis_snapshot.get("short_ratio_days")
        if market_cap_m is not None and market_cap_m < args.min_market_cap:
            continue
        result = evaluate_candidate(
            row,
            finra_row,
            earnings,
            market_cap_m,
            session_gap_pct,
            float_m,
            short_interest_m,
            short_interest_float_pct,
            short_ratio_days,
            args.min_short_volume_pct,
            args.min_market_cap,
        )
        results.append(result)

    if not (args.watchlist or args.tickers) and args.auto_require_earnings:
        earnings_results = [r for r in results if r.earnings_event]
        if earnings_results:
            results = earnings_results

    results.sort(key=lambda r: (r.score, r.short_volume_pct or 0.0), reverse=True)

    if args.json:
        print(
            json.dumps(
                {
                    "trade_date": trade_date.isoformat(),
                    "min_short_volume_pct": args.min_short_volume_pct,
                    "results": [asdict(r) for r in results],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print_table(results, trade_date, args.min_short_volume_pct)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
