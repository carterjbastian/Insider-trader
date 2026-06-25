"""Options-amplification backtest on REAL historical premiums (Massive/Polygon Developer tier).

Same design as options_backtest, but the call sleeve uses real option prices instead of the
Black-Scholes model: for each high-signal trade we pick the real ~1-year ATM call (split-safe),
read its entry premium and exit value from Polygon, and the sleeve return = exit/entry − 1. The
$100 equity bet is unchanged. Options data starts 2022, so pre-2022 high-signal trades get equity
only (reported). Validates whether the BS-modeled +30–40% edge survives on real premiums.

  POLYGON_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.real_options_backtest
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from . import polygon_options as po
from . import store
from .phase1_backtest import TODAY, _last, _on_after, _prices
from .phase12_backtest import _load

_OUT = (
    "/home/carter/vault/Projects/Black Box/Insider Trader/Backtests/"
    "2026-06-25 Phase 1+2 Options Amplification (Real Premiums).md"
)
LOCKED_RUNUP = 0.05
EQUITY_HOLD = 360
OPT_FLOOR = date(2022, 1, 1)
STRUCTURES = {"atm1yr": (0.0, 365), "otm180": (0.10, 180)}


def _build(sigs, prices):
    bets = []
    for s in sigs:
        ser = prices.get(s["ticker"])
        buy = _on_after(ser, s["disc"]) if ser else None
        spy0 = _on_after(prices["SPY"], buy[0]) if buy else None
        txn = _on_after(ser, s["txn_date"]) if ser else None
        if not buy or not spy0 or not txn or txn[1] <= 0:
            continue
        if buy[1] / txn[1] - 1.0 < LOCKED_RUNUP:
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


def _equity_leg(ser, spy, b):
    target = b["buy_date"] + timedelta(days=EQUITY_HOLD)
    hit = _on_after(ser, target)
    sell = hit if (hit and hit[0] <= TODAY) else _last(ser)
    spy_sell = _on_after(spy, sell[0]) or _last(spy)
    return 100.0, 100.0 * (sell[1] / b["buy_close"]), 100.0 * (spy_sell[1] / b["spy_buy"])


def _fetch_options(bets, min_signal=50):
    """For each eligible (signal>=min, 2022+) bet, fetch real ATM-1yr + OTM-180d call paths."""
    import yfinance as yf

    splits_cache: dict[str, object] = {}
    elig = [b for b in bets if b["signal"] >= min_signal and b["buy_date"] >= OPT_FLOOR]
    print(f"[real-opt] fetching real premiums for {len(elig)} eligible trades...", flush=True)
    got = 0
    for i, b in enumerate(elig, 1):
        if b["ticker"] not in splits_cache:
            try:
                splits_cache[b["ticker"]] = yf.Ticker(b["ticker"]).splits
            except Exception:  # noqa: BLE001
                splits_cache[b["ticker"]] = None
        spot = po.unadjusted_spot(
            b["ticker"], b["buy_date"], adj_close=b["buy_close"], splits=splits_cache[b["ticker"]]
        )
        b["opt"] = {}
        if not spot:
            continue
        for label, (otm, dte) in STRUCTURES.items():
            res = po.option_premium_path(
                b["ticker"], b["buy_date"], spot * (1 + otm), target_dte=dte
            )
            if res and res.get("last_close") and res.get("entry_premium"):
                exp = datetime.strptime(res["expiration"], "%Y-%m-%d").date()
                b["opt"][label] = {
                    "ret": res["last_close"] / res["entry_premium"] - 1.0,
                    "exit": min(exp, TODAY),
                }
            time.sleep(0.15)
        got += bool(b["opt"])
        if i % 15 == 0:
            print(f"[real-opt] {i}/{len(elig)}", flush=True)
    print(f"[real-opt] {got}/{len(elig)} eligible trades got real option paths", flush=True)


def _size(signal, tiers):
    d = 0.0
    for thresh, amt in tiers:
        if signal >= thresh:
            d = amt
    return d


def _sim(bets, prices, structure, tiers):
    spy = prices["SPY"]
    inv = end = spy_end = 0.0
    n_opt = 0
    for b in bets:
        ser = prices[b["ticker"]]
        ei, ee, es = _equity_leg(ser, spy, b)
        inv += ei
        end += ee
        spy_end += es
        dollars = _size(b["signal"], tiers)
        opt = b.get("opt", {}).get(structure) if structure else None
        if dollars > 0 and opt and opt["ret"] is not None:
            spy_exit = _on_after(spy, opt["exit"]) or _last(spy)
            inv += dollars
            end += dollars * (1 + opt["ret"])
            spy_end += dollars * (spy_exit[1] / b["spy_buy"])
            n_opt += 1
    return {
        "n": len(bets),
        "n_opt": n_opt,
        "invested": inv,
        "roi": end / inv - 1 if inv else 0,
        "spy_roi": spy_end / inv - 1 if inv else 0,
    }


VARIANTS = [
    ("equity only (1-yr, $100)", None, []),
    ("+ REAL ATM 1yr  ($50 / $100)", "atm1yr", [(50, 50), (60, 100)]),
    ("+ REAL ATM 1yr  ($100 / $200)", "atm1yr", [(50, 100), (60, 200)]),
    ("+ REAL 10% OTM 180d  ($50 / $100)", "otm180", [(50, 50), (60, 100)]),
    ("+ REAL ATM 1yr on signal>=50 ($100)", "atm1yr", [(50, 100)]),
]


def _pct(x):
    return f"{x * 100:+.1f}%"


def run() -> None:
    conn = store.connect()
    sigs, _sales = _load(conn)
    conn.close()
    print(f"[real-opt] {len(sigs)} growth+proven; fetching equity prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])
    bets = _build(sigs, prices)
    print(f"[real-opt] {len(bets)} locked bets", flush=True)
    _fetch_options(bets)

    results = {name: _sim(bets, prices, st, tiers) for name, st, tiers in VARIANTS}
    base = results["equity only (1-yr, $100)"]
    base_edge = base["roi"] - base["spy_roi"]
    elig = sum(1 for b in bets if b["signal"] >= 50 and b["buy_date"] >= OPT_FLOOR)
    pre22 = sum(1 for b in bets if b["signal"] >= 50 and b["buy_date"] < OPT_FLOOR)

    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        "status: canonical",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1+2 Options Amplification (REAL premiums)",
        "",
        "> Same as the BS-modeled options backtest but with **real historical option prices** "
        "(Massive/Polygon Developer tier). $100 equity bet unchanged; a real call sleeve is added "
        "on high-signal (≥50) trades, sized by signal. Sleeve return = real exit/entry − 1 on the "
        "~1-year ATM call (split-safe). Options history starts 2022, so the "
        f"**{pre22} pre-2022 high-signal trades get equity only**; {elig} are option-eligible. "
        f"As of {TODAY}.",
        "",
        f"**Equity-only baseline:** {base['n']} trades, ROI {_pct(base['roi'])} vs SPY "
        f"{_pct(base['spy_roi'])} = **{_pct(base_edge)} edge**.",
        "",
        "## Variants (real option premiums)",
        "| variant | trades w/ options | invested | total ROI | SPY-alt ROI | **edge** | Δ edge |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for name, st, _tiers in VARIANTS:
        r = results[name]
        edge = r["roi"] - r["spy_roi"]
        d = "" if st is None else f"{_pct(edge - base_edge)}"
        L.append(
            f"| {name} | {r['n_opt']} | ${r['invested']:,.0f} | {_pct(r['roi'])} | "
            f"{_pct(r['spy_roi'])} | **{_pct(edge)}** | {d} |"
        )
    L += [
        "",
        "## Read",
        "- These are REAL premiums — entry near the disclosure date and exit near expiry (or the "
        "latest real close for not-yet-expired contracts), pulled per contract from Polygon.",
        "- Compare Δ edge to the BS-modeled run: if the real edge is similar or better, options "
        "amplification is validated; if much lower, the modeled premiums were too cheap.",
        "- Caveats: ~1-year ATM calls only (one structure); exit uses the last available real "
        "close, so a worthless-but-untraded expiry can be slightly over-valued; pre-2022 trades "
        "excluded from the sleeve; House+Senate; survivorship-pruned.",
    ]
    md = "\n".join(L)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"\nwrote {_OUT}\n")
    print(md)


if __name__ == "__main__":
    run()
