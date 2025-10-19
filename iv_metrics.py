#!/usr/bin/env python3
"""
Compute current IV, IV Rank, and IV Percentile for a given equity ticker
using Polygon.io's Options API (Starter plan compatible), with a resilient
approach to extract ATM option IV and an optional local-history fallback to
derive rank/percentile.

Usage examples:
  - python iv_metrics.py --ticker AAPL
  - python iv_metrics.py --ticker TSLA --lookback 252 --target-dte 30

Requirements:
  - Set POLYGON_API_KEY in your environment or pass --api-key
  - Python 3.9+
  - pip install requests pytz (if not already available)

Notes:
  - Current IV is computed from the nearest-to-30D (configurable) ATM call/put
    option implied volatility, averaged when both available.
  - IV Rank = (current IV - lookback IV min) / (lookback IV max - lookback IV min)
  - IV Percentile = fraction of lookback IV observations that are <= current IV
  - By default, the script maintains local history at .iv_cache/{TICKER}.csv
    so you can build up historical IV with daily runs. You can disable writing
    with --no-write-history. If insufficient history exists, rank/percentile
    are computed against the available data and a note is printed.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import math
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:
    import pytz  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pytz = None  # Fallback to naive local time if pytz not available


POLYGON_BASE = "https://api.polygon.io"
DEFAULT_LOOKBACK = 252  # trading days in ~1y
DEFAULT_TARGET_DTE = 30  # target days to expiration
HISTORY_DIR = ".iv_cache"


def _us_eastern_today() -> dt.date:
    if pytz is None:
        return dt.date.today()
    tz = pytz.timezone("US/Eastern")
    return dt.datetime.now(tz).date()


def _parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def _safe_get(d: Dict[str, Any], path: Iterable[str], default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _abs_days(a: dt.date, b: dt.date) -> int:
    return abs((a - b).days)


@dataclasses.dataclass
class OptionPick:
    ticker: str
    strike: float
    contract_type: str  # "call" or "put"
    expiration_date: dt.date
    iv: Optional[float]


class PolygonClient:
    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.sess = requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{POLYGON_BASE}{path}"
        params = dict(params or {})
        params.setdefault("apiKey", self.api_key)
        r = self.sess.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # --- Stocks price helpers ---
    def get_last_price(self, ticker: str) -> float:
        # Try v3 last trade first
        try:
            data = self._get(f"/v3/last_trade/stocks/{ticker}")
            price = _safe_get(data, ["results", "price"]) or _safe_get(data, ["results", "p"])  # type: ignore
            if price:
                return float(price)
        except Exception:
            pass

        # Fallback: v2 last trade
        data = self._get(f"/v2/last/trade/{ticker}")
        price = _safe_get(data, ["results", "price"]) or _safe_get(data, ["results", "p"])  # type: ignore
        if not price:
            raise RuntimeError(f"Unable to fetch last price for {ticker}")
        return float(price)

    # --- Options reference + snapshot helpers ---
    def list_option_expirations(
        self,
        underlying: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> List[dt.date]:
        params = {
            "underlying_ticker": underlying,
            "contract_type": "call",  # only to collect expirations; calls and puts share expirations
            "expiration_date.gte": start_date.isoformat(),
            "expiration_date.lte": end_date.isoformat(),
            "sort": "expiration_date",
            "order": "asc",
            "limit": 1000,
        }
        data = self._get("/v3/reference/options/contracts", params)
        results = data.get("results") or []
        expirations: List[dt.date] = []
        seen: set[str] = set()
        for item in results:
            exp = item.get("expiration_date") or item.get("expires_at")
            if not exp:
                continue
            try:
                d = _parse_date(exp[:10])
            except Exception:
                continue
            key = d.isoformat()
            if key not in seen:
                seen.add(key)
                expirations.append(d)
        return expirations

    def snapshot_options(
        self,
        underlying: str,
        expiration: dt.date,
        contract_type: str,
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        params = {
            "expiration_date": expiration.isoformat(),
            "contract_type": contract_type,
            "limit": limit,
            "order": "asc",
            "sort": "strike",
        }
        data = self._get(f"/v3/snapshot/options/{underlying}", params)
        return data.get("results") or []


def _extract_option_ticker(item: Dict[str, Any]) -> Optional[str]:
    return (
        item.get("ticker")
        or _safe_get(item, ["details", "ticker"])  # snapshot
        or _safe_get(item, ["symbol"])  # possible alias
    )


def _extract_strike(item: Dict[str, Any]) -> Optional[float]:
    strike = (
        item.get("strike_price")
        or item.get("strike")
        or _safe_get(item, ["details", "strike_price"])
        or _safe_get(item, ["details", "strike"])
    )
    try:
        return float(strike) if strike is not None else None
    except Exception:
        return None


def _extract_expiration(item: Dict[str, Any]) -> Optional[dt.date]:
    exp = (
        item.get("expiration_date")
        or item.get("expires_at")
        or _safe_get(item, ["details", "expiration_date"])
    )
    if not exp:
        return None
    try:
        return _parse_date(str(exp)[:10])
    except Exception:
        return None


def _extract_iv(item: Dict[str, Any]) -> Optional[float]:
    # Try a few likely locations/keys
    candidates = [
        (item.get("implied_volatility")),
        (item.get("iv")),
        _safe_get(item, ["greeks", "implied_volatility"]),
        _safe_get(item, ["day", "implied_volatility"]),
        _safe_get(item, ["last_quote", "implied_volatility"]),
        _safe_get(item, ["details", "implied_volatility"]),
    ]
    for v in candidates:
        if v is None:
            continue
        try:
            f = float(v)
            if math.isfinite(f):
                return f
        except Exception:
            continue
    return None


def pick_atm_option(
    snapshot_items: List[Dict[str, Any]],
    contract_type: str,
    expiration: dt.date,
    underlying_price: float,
) -> Optional[OptionPick]:
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for it in snapshot_items:
        strike = _extract_strike(it)
        if strike is None:
            continue
        delta = abs(strike - underlying_price)
        if best is None or delta < best[0]:
            best = (delta, it)
    if best is None:
        return None
    it = best[1]
    ticker = _extract_option_ticker(it)
    if not ticker:
        return None
    strike = _extract_strike(it)
    iv = _extract_iv(it)
    return OptionPick(
        ticker=ticker,
        strike=float(strike) if strike is not None else float("nan"),
        contract_type=contract_type,
        expiration_date=expiration,
        iv=iv,
    )


def compute_iv_rank_and_percentile(history: List[Tuple[dt.date, float]], current_iv: float) -> Tuple[Optional[float], Optional[float]]:
    if not history:
        return None, None
    ivs = [iv for _, iv in history]
    lo = min(ivs)
    hi = max(ivs)
    rank = None
    if hi > lo:
        rank = (current_iv - lo) / (hi - lo)
    # Percentile: fraction of observations <= current
    count_le = sum(1 for v in ivs if v <= current_iv)
    pct = count_le / len(ivs)
    return rank, pct


def load_local_history(ticker: str, lookback: int) -> List[Tuple[dt.date, float]]:
    path = os.path.join(HISTORY_DIR, f"{ticker.upper()}.csv")
    if not os.path.exists(path):
        return []
    rows: List[Tuple[dt.date, float]] = []
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                d = _parse_date(row["date"])  # type: ignore
                iv = float(row["iv"])  # type: ignore
                rows.append((d, iv))
            except Exception:
                continue
    rows.sort(key=lambda x: x[0])
    # take most recent lookback entries
    return rows[-lookback:]


def append_local_history(ticker: str, date: dt.date, iv: float) -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{ticker.upper()}.csv")
    # Load existing to dedupe by date
    existing: Dict[str, float] = {}
    if os.path.exists(path):
        with open(path, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    existing[row["date"]] = float(row["iv"])  # type: ignore
                except Exception:
                    pass
    existing[date.isoformat()] = iv
    # Write back
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "iv"])  # header
        for d_str in sorted(existing.keys()):
            w.writerow([d_str, existing[d_str]])


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Compute IV, IV Rank, IV Percentile via Polygon.io")
    p.add_argument("--ticker", required=True, help="Underlying ticker, e.g., AAPL")
    p.add_argument("--api-key", default=os.environ.get("POLYGON_API_KEY"), help="Polygon API key or set POLYGON_API_KEY env var")
    p.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, help="History length (days) for rank/percentile")
    p.add_argument("--target-dte", type=int, default=DEFAULT_TARGET_DTE, help="Target days to expiration for ATM IV")
    p.add_argument("--no-write-history", action="store_true", help="Do not persist today's IV to local history cache")
    p.add_argument("--verbose", action="store_true")

    args = p.parse_args(argv)
    if not args.api_key:
        print("Error: Provide --api-key or set POLYGON_API_KEY", file=sys.stderr)
        return 2

    ticker = args.ticker.upper()
    client = PolygonClient(args.api_key)

    # 1) Get last underlying price
    if args.verbose:
        print(f"Fetching last price for {ticker}...")
    price = client.get_last_price(ticker)

    # 2) Choose target expiration near target-dte
    today = _us_eastern_today()
    target = today + dt.timedelta(days=args.target_dte)
    # Search a reasonable window around target
    start = today + dt.timedelta(days=max(7, args.target_dte - 30))
    end = today + dt.timedelta(days=args.target_dte + 60)
    if args.verbose:
        print(f"Listing expirations between {start} and {end}...")
    expirations = client.list_option_expirations(ticker, start, end)
    if not expirations:
        print("No option expirations found in the target window.", file=sys.stderr)
        return 1
    expiration = min(expirations, key=lambda d: _abs_days(d, target))

    # 3) Pull snapshots for calls/puts on that expiration and pick ATM
    if args.verbose:
        print(f"Fetching snapshots for {ticker} {expiration} calls/puts...")
    calls = client.snapshot_options(ticker, expiration, "call")
    puts = client.snapshot_options(ticker, expiration, "put")
    if not calls and not puts:
        print("No snapshot data returned for selected expiration.", file=sys.stderr)
        return 1

    call_pick = pick_atm_option(calls, "call", expiration, price)
    put_pick = pick_atm_option(puts, "put", expiration, price)

    iv_values: List[float] = []
    if call_pick and call_pick.iv is not None:
        iv_values.append(call_pick.iv)
    if put_pick and put_pick.iv is not None:
        iv_values.append(put_pick.iv)
    if not iv_values:
        print("Unable to extract implied volatility from snapshots.", file=sys.stderr)
        return 1
    current_iv = sum(iv_values) / len(iv_values)

    # 4) Load local history and compute metrics
    history = load_local_history(ticker, args.lookback)
    if not args.no_write_history:
        try:
            append_local_history(ticker, today, float(current_iv))
        except Exception as e:
            if args.verbose:
                print(f"Warning: failed to write history: {e}")

    rank, pct = compute_iv_rank_and_percentile(history, float(current_iv))

    # 5) Output
    print(f"Ticker: {ticker}")
    print(f"Last Price: {price:.2f}")
    print(f"Selected Expiration: {expiration.isoformat()} (target DTE ~{args.target_dte}d)")
    if call_pick:
        print(f"ATM Call: {call_pick.ticker} strike {call_pick.strike:.2f} IV={call_pick.iv if call_pick.iv is not None else 'NA'}")
    if put_pick:
        print(f"ATM Put:  {put_pick.ticker} strike {put_pick.strike:.2f} IV={put_pick.iv if put_pick.iv is not None else 'NA'}")
    print(f"Current IV (ATM avg): {current_iv:.4f}")

    if not history:
        print("IV Rank: NA (no history)")
        print("IV Percentile: NA (no history)")
    else:
        if rank is None:
            print("IV Rank: NA (flat history)")
        else:
            print(f"IV Rank: {rank*100:.2f}%")
        if pct is None:
            print("IV Percentile: NA")
        else:
            print(f"IV Percentile: {pct*100:.2f}%")

    if history and len(history) < args.lookback:
        print(f"Note: history has only {len(history)} entries (requested {args.lookback}).")
    if not args.no_write_history:
        print(f"History updated at {os.path.join(HISTORY_DIR, f'{ticker}.csv')}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        raise SystemExit(1)
