"""Real historical option prices from Massive/Polygon (Options Developer tier, history ≥ 2022).

For a congressional trade we model a call bought at the disclosure date: pick the contract whose
expiry is closest to ~1 year out and whose strike is closest to at-the-money, then read its real
daily OHLC bars. Entry premium = first close on/after the buy date; exit = real close near expiry
(or the latest close if not yet expired). Replaces the Black-Scholes model in options_backtest.

  POLYGON_API_KEY=... uv run python -m insider_trader.polygon_options <TICKER> <YYYY-MM-DD>
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = "https://api.polygon.io"
DATA_START = date(2022, 1, 1)  # Developer-tier options history floor (probed 2026-06-25)
# NOTE: on the ~$30 Starter tier the floor is instead a ROLLING ~2 years, so dates older than
# ~today-2yr return empty bars (pick_call still succeeds, option_premium_path returns None).
# Backtests needing 2022+ depend on Developer or on the local export (see export_option_history).

BACKTEST_END = date(2026, 6, 24)  # frozen data cutoff of the LOCKED backtests — do not change:
# every backtest report in the vault was produced with it. Backtest callers pass end=BACKTEST_END
# for reproducibility; the live/paper path must use the default (today).


def _key() -> str:
    k = os.environ.get("POLYGON_API_KEY")
    if not k:
        raise RuntimeError("POLYGON_API_KEY not set")
    return k


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    q = dict(params or {})
    q["apiKey"] = _key()
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "blackbox-insider/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:  # rate limited
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"_error": e.code, "_msg": e.read().decode("utf-8", "replace")[:120]}
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def _d(x) -> date:
    return x if isinstance(x, date) else datetime.strptime(x, "%Y-%m-%d").date()


def unadjusted_spot(ticker: str, d, adj_close: float | None = None, splits=None) -> float | None:
    """UNADJUSTED underlying price on/after `d` — must match the era's option strikes (not
    split-adjusted). Our plan has no stock data, so derive it from yfinance's split-adjusted close
    times the cumulative ratio of splits that happened AFTER `d` (for non-splitters this is the
    adjusted close itself). Pass adj_close/splits to avoid re-fetching in a loop."""
    import yfinance as yf

    d = _d(d)
    t = yf.Ticker(ticker)
    if adj_close is None:
        hist = t.history(start=d.isoformat(), period="7d", auto_adjust=True)
        if hist.empty:
            return None
        adj_close = float(hist["Close"].iloc[0])
    splits = t.splits if splits is None else splits
    factor = 1.0
    for sdate, ratio in splits.items():
        if sdate.date() > d and ratio:
            factor *= float(ratio)
    return adj_close * factor


def pick_call(ticker: str, as_of, spot: float, target_dte: int = 365) -> dict | None:
    """Choose the ~1-year, ~ATM call that existed as of `as_of`. Returns contract metadata."""
    as_of = _d(as_of)
    if as_of < DATA_START:
        return None
    target = as_of + timedelta(days=target_dte)
    d = _get(
        "/v3/reference/options/contracts",
        {
            "underlying_ticker": ticker,
            "as_of": as_of.isoformat(),
            "contract_type": "call",
            "expiration_date.gte": (target - timedelta(days=75)).isoformat(),
            "expiration_date.lte": (target + timedelta(days=90)).isoformat(),
            "limit": 1000,
        },
    )
    res = (d or {}).get("results") or []
    if not res:
        return None
    # expiry closest to target, then strike closest to spot
    best_exp = min({r["expiration_date"] for r in res}, key=lambda e: abs((_d(e) - target).days))
    same_exp = [r for r in res if r["expiration_date"] == best_exp]
    best = min(same_exp, key=lambda r: abs(r["strike_price"] - spot))
    return {
        "contract": best["ticker"],
        "strike": best["strike_price"],
        "expiration": best_exp,
        "dte": (_d(best_exp) - as_of).days,
    }


def daily_bars(contract: str, start, end) -> list[dict]:
    d = _get(
        f"/v2/aggs/ticker/{contract}/range/1/day/{_d(start).isoformat()}/{_d(end).isoformat()}"
    )
    return (d or {}).get("results") or []


def _bar_on_after(bars: list[dict], d: date) -> float | None:
    ms = int(datetime(d.year, d.month, d.day).timestamp() * 1000)
    cand = [b for b in bars if b.get("t", 0) >= ms and b.get("c")]
    return min(cand, key=lambda b: b["t"])["c"] if cand else None


def option_premium_path(
    ticker: str, buy_date, spot: float, target_dte: int = 365, end=None
) -> dict | None:
    """Pick the call, fetch its bars, return entry premium + exit value (real if expired-and-traded,
    else None for exit so the caller can fall back to intrinsic from the underlying).

    `end` caps the bar range. Defaults to TODAY — correct for the live/paper path. Backtests must
    pass end=BACKTEST_END to stay reproducible against the locked reports.
    """
    pick = pick_call(ticker, buy_date, spot, target_dte)
    if not pick:
        return None
    buy_date = _d(buy_date)
    exp = _d(pick["expiration"])
    bars = daily_bars(pick["contract"], buy_date, min(exp, _d(end) if end else date.today()))
    entry = _bar_on_after(bars, buy_date)
    if not entry:
        return None
    exit_close = bars[-1]["c"] if bars else None  # latest real close (near expiry or today)
    return {**pick, "entry_premium": entry, "last_close": exit_close, "n_bars": len(bars)}


if __name__ == "__main__":
    import sys

    tk, dstr = sys.argv[1], sys.argv[2]
    spot = unadjusted_spot(tk, dstr)
    print(f"{tk} unadjusted spot {spot} on/after {dstr}")
    res = option_premium_path(tk, dstr, spot) if spot else None
    if res:
        ret = res["last_close"] / res["entry_premium"] - 1 if res.get("last_close") else None
        res["option_return"] = f"{ret * 100:+.0f}%" if ret is not None else None
    print(json.dumps(res, indent=2, default=str))
