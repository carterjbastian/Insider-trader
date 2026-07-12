"""Options-amplification backtest (Black-Scholes-modeled, no options data needed).

Keeps the locked $100 EQUITY bet on every Phase-1 candidate unchanged, and adds a leveraged
CALL-option sleeve sized by the Phase-2 signal_strength — leaning leverage into exactly the
trades the signal eval showed pay off most. Premiums are modeled with Black-Scholes from the
equity prices we already have (trailing realized vol × an IV premium), since our Polygon plan
lacks historical option prices. Approximate (no skew / early exercise / bid-ask) — a directional
read on whether options amplification helps, to be confirmed on real premiums later if promising.

  DATABASE_URL=... uv run python -m insider_trader.options_backtest
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from math import erf, exp, log, sqrt

from . import store
from .phase1_backtest import TODAY, _last, _on_after, _prices
from .phase12_backtest import _load

_OUT = (
    "/home/carter/vault/Projects/Active/Black Box/Insider Trader/Backtests/"
    "2026-06-25 Phase 1+2 Options Amplification.md"
)
LOCKED_RUNUP = 0.05
EQUITY_HOLD = 360  # the locked 1-year equity sleeve
IV_PREMIUM = 1.2  # options trade richer than realized vol
RFR = 0.03


# --- Black-Scholes + realized vol -------------------------------------------


def _ncdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


def bs_call(s: float, k: float, t: float, sigma: float, r: float = RFR) -> float:
    if t <= 0 or sigma <= 0 or s <= 0:
        return max(0.0, s - k * exp(-r * max(t, 0)))
    d1 = (log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt(t))
    d2 = d1 - sigma * sqrt(t)
    return s * _ncdf(d1) - k * exp(-r * t) * _ncdf(d2)


def realized_vol(ser: dict, as_of, lookback: int = 90, fallback: float = 0.5) -> float:
    days = sorted(d for d in ser if d < as_of)[-(lookback + 1) :]
    rets = [
        log(ser[days[i]] / ser[days[i - 1]])
        for i in range(1, len(days))
        if ser[days[i - 1]] > 0 and ser[days[i]] > 0
    ]
    if len(rets) < 10:
        return fallback
    return min(2.0, statistics.pstdev(rets) * sqrt(252))  # cap at 200% annualized


# --- bet construction -------------------------------------------------------


def _build_bets(sigs, prices):
    bets = []
    for s in sigs:
        ser = prices.get(s["ticker"])
        buy = _on_after(ser, s["disc"]) if ser else None
        spy0 = _on_after(prices["SPY"], buy[0]) if buy else None
        txn = _on_after(ser, s["txn_date"]) if ser else None
        if not buy or not spy0 or not txn or txn[1] <= 0:
            continue
        if buy[1] / txn[1] - 1.0 < LOCKED_RUNUP:  # locked run-up leg
            continue
        bets.append(
            {
                "ticker": s["ticker"],
                "buy_date": buy[0],
                "buy_close": buy[1],
                "spy_buy": spy0[1],
                "signal": s["signal"] or 0,
            }
        )
    return bets


# --- the two sleeves --------------------------------------------------------


def _equity_leg(ser, spy, b):
    """$100 equity, held EQUITY_HOLD days (1-yr), marked-to-market if not yet elapsed."""
    target = b["buy_date"] + timedelta(days=EQUITY_HOLD)
    hit = _on_after(ser, target)
    sell = hit if (hit and hit[0] <= TODAY) else _last(ser)
    sd, sc = sell
    spy_sell = _on_after(spy, sd) or _last(spy)
    end = 100.0 * (sc / b["buy_close"])
    spy_alt = 100.0 * (spy_sell[1] / b["spy_buy"])
    return 100.0, end, spy_alt


def _option_leg(ser, spy, b, dollars, otm, dte):
    """A modeled call sleeve. Returns (invested, end_value, spy_alt)."""
    if dollars <= 0:
        return 0.0, 0.0, 0.0
    s0 = b["buy_close"]
    k = s0 * (1 + otm)
    sigma = realized_vol(ser, b["buy_date"]) * IV_PREMIUM
    prem = bs_call(s0, k, dte / 365, sigma)
    if prem <= 0:
        return 0.0, 0.0, 0.0
    expiry = b["buy_date"] + timedelta(days=dte)
    if expiry <= TODAY:  # realized payoff at expiry (intrinsic)
        hit = _on_after(ser, expiry) or _last(ser)
        opt_val, exit_date = max(0.0, hit[1] - k), hit[0]
    else:  # not yet expired -> mark-to-model at today's price
        s_now = _last(ser)[1]
        t_rem = (expiry - TODAY).days / 365
        opt_val, exit_date = bs_call(s_now, k, t_rem, sigma), TODAY
    end = dollars * (opt_val / prem)
    spy_sell = _on_after(spy, exit_date) or _last(spy)
    spy_alt = dollars * (spy_sell[1] / b["spy_buy"])
    return dollars, end, spy_alt


def _size(signal, tiers):
    d = 0.0
    for thresh, amt in tiers:
        if signal >= thresh:
            d = amt
    return d


def _sim(bets, prices, tiers, otm, dte):
    spy = prices["SPY"]
    inv = end = spy_end = 0.0
    n_opt = 0
    for b in bets:
        ser = prices[b["ticker"]]
        ei, ee, es = _equity_leg(ser, spy, b)
        dollars = _size(b["signal"], tiers)
        oi, oe, os = _option_leg(ser, spy, b, dollars, otm, dte)
        n_opt += oi > 0
        inv += ei + oi
        end += ee + oe
        spy_end += es + os
    return {
        "n": len(bets),
        "n_opt": n_opt,
        "invested": inv,
        "end": end,
        "roi": end / inv - 1 if inv else 0,
        "spy_roi": spy_end / inv - 1 if inv else 0,
    }


# tiers = [(signal_threshold, option_dollars)] ; equity is always $100
VARIANTS = [
    ("equity only (1-yr, $100)", [], 0.0, 360),
    ("+ calls ATM 1yr  ($50 / $100)", [(50, 50), (60, 100)], 0.0, 360),
    ("+ calls ATM 1yr  ($100 / $200)", [(50, 100), (60, 200)], 0.0, 360),
    ("+ calls 10% OTM 180d  ($50 / $100)", [(50, 50), (60, 100)], 0.10, 180),
    ("+ calls ATM 1yr on signal>=50 only ($100)", [(50, 100)], 0.0, 360),
]


def _pct(x):
    return f"{x * 100:+.1f}%"


def run() -> None:
    conn = store.connect()
    sigs, _sales = _load(conn)
    conn.close()
    print(f"[options] {len(sigs)} growth+proven; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])
    bets = _build_bets(sigs, prices)
    print(f"[options] {len(bets)} locked bets", flush=True)

    results = {name: _sim(bets, prices, tiers, otm, dte) for name, tiers, otm, dte in VARIANTS}
    base = results["equity only (1-yr, $100)"]
    base_edge = base["roi"] - base["spy_roi"]

    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1+2 Options Amplification (BS-modeled)",
        "",
        "> Locked set (House+Senate). The **$100 equity bet is unchanged** on every trade; a "
        "leveraged **call-option sleeve** is added, sized by Phase-2 signal_strength. Premiums "
        "are **Black-Scholes-modeled** (trailing realized vol × 1.2 IV premium) because our "
        "Polygon plan lacks historical option prices — approximate, a directional read. Each "
        f"sleeve's SPY-alt = same dollars in SPY over the same window. As of {TODAY}.",
        "",
        f"**Equity-only baseline:** {base['n']} trades, ROI {_pct(base['roi'])} vs SPY "
        f"{_pct(base['spy_roi'])} = **{_pct(base_edge)} edge**.",
        "",
        "## Variants (equity sleeve identical; options sleeve varies)",
        "| variant | trades w/ options | invested | total ROI | SPY-alt ROI | **edge** | Δ edge |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for name, *_ in VARIANTS:
        r = results[name]
        edge = r["roi"] - r["spy_roi"]
        d = "" if name.startswith("equity only") else f"{_pct(edge - base_edge)}"
        L.append(
            f"| {name} | {r['n_opt']} | ${r['invested']:,.0f} | {_pct(r['roi'])} | "
            f"{_pct(r['spy_roi'])} | **{_pct(edge)}** | {d} |"
        )

    L += [
        "",
        "## Read",
        "- The options sleeve only touches high-signal trades (signal≥50); everything else is "
        "equity-only, so the baseline edge is preserved and options add asymmetric upside.",
        "- A positive Δ edge means the modeled calls amplified returns net of premium burn; a "
        "negative Δ means premiums (and total-loss expiries) outweighed the leverage.",
        "- **Heavy caveat:** premiums are modeled, not real. BS with realized×1.2 vol likely "
        "UNDER-prices true premiums (so this may be optimistic); it ignores skew, early "
        "exercise, liquidity, and assignment. Treat as a go/no-go on whether to buy real Polygon "
        "options history (~$79/mo) and do this properly. Equity sleeve marked-to-market; "
        "not-yet-expired options marked-to-model.",
    ]
    md = "\n".join(L)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"\nwrote {_OUT}\n")
    print(md)


if __name__ == "__main__":
    run()
