"""Phase 1 → Phase 2 combined backtest. Takes the LOCKED Phase-1 candidate set (House+Senate)
and folds in the Phase-2 LLM scores two ways: FILTER (buy only the high-conviction subset) and
SIZE (bet more on high-signal trades). Compares every variant to the locked baseline across the
3 locked sell rules, vs SPY. Writes a vault report.

  DATABASE_URL=... uv run python -m insider_trader.phase12_backtest
"""

from __future__ import annotations

import statistics
from datetime import datetime

from . import store
from .phase1_backtest import GROWTH, TODAY, _last, _on_after, _prices, _resolve_sell

_OUT = "/home/carter/vault/Projects/Black Box/Insider Trader/Backtests/2026-06-25 Phase 1+2 Combined Backtest.md"
SELL = [("1-year", 360), ("sell-when-trader-sells", "trader"), ("buy & hold", "hold")]
LOCKED_RUNUP = 0.05


def _load(conn):
    """Locked candidates (growth+proven+run-up>=5%) with tid + Phase-2 scores, plus members' sales."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, f.bioguide, t.ticker, t.txn_date, t.disclosure_date, a.buy, a.signal_strength "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "JOIN trader_metrics tm ON tm.transaction_id=t.id "
            "LEFT JOIN phase2_analyses a ON a.transaction_id=t.id AND a.prompt_version='p2-v1' "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date IS NOT NULL AND s.sector = ANY(%s) AND tm.prior_bigwin90 > 0 "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id",
            (list(GROWTH),),
        )
        cols = ["tid", "bioguide", "ticker", "txn_date", "disc", "buy", "signal"]
        sigs = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        cur.execute(
            "SELECT f.bioguide, t.ticker, t.disclosure_date FROM transactions t "
            "JOIN filings f USING(doc_id) WHERE t.txn_type='sale' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date IS NOT NULL"
        )
        sales: dict[tuple, list] = {}
        for bio, tk, d in cur.fetchall():
            sales.setdefault((bio, tk), []).append(d)
    for v in sales.values():
        v.sort()
    return sigs, sales


def _build_bets(sigs, sales, prices):
    bets = []
    for s in sigs:
        ser = prices.get(s["ticker"])
        buy = _on_after(ser, s["disc"]) if ser else None
        spy0 = _on_after(prices["SPY"], buy[0]) if buy else None
        txn = _on_after(ser, s["txn_date"]) if ser else None
        if not buy or not spy0 or not txn or txn[1] <= 0:
            continue
        runup = buy[1] / txn[1] - 1.0
        if runup < LOCKED_RUNUP:  # the locked Phase-1 run-up leg
            continue
        bets.append(
            {
                "bioguide": s["bioguide"],
                "ticker": s["ticker"],
                "buy_date": buy[0],
                "buy_close": buy[1],
                "spy_buy": spy0[1],
                "sales": sales,
                "buy": bool(s["buy"]),
                "signal": s["signal"] or 0,
            }
        )
    return bets


def _sim(strat_key, bets, prices, size_fn):
    spy = prices["SPY"]
    inv = recouped = unsold = spy_end_total = 0.0
    excs = []
    wins = beats = sold_n = 0
    for b in bets:
        ser = prices[b["ticker"]]
        bet = size_fn(b)
        shares = bet / b["buy_close"]
        sell = _resolve_sell(strat_key, b["buy_date"], b["bioguide"], b["ticker"], ser, b["sales"])
        if sell:
            sd, sc, sold = sell[0], sell[1], True
        else:
            sd, sc, sold = _last(ser)[0], _last(ser)[1], False
        ret = sc / b["buy_close"] - 1.0
        spy_sell = _on_after(spy, sd) or _last(spy)
        spy_ret = spy_sell[1] / b["spy_buy"] - 1.0
        inv += bet
        if sold:
            recouped += shares * sc
        else:
            unsold += shares * sc
        spy_end_total += bet * (spy_sell[1] / b["spy_buy"])
        excs.append(ret - spy_ret)
        wins += ret > 0
        beats += (ret - spy_ret) > 0
        sold_n += sold
    n = len(bets)
    end_value = recouped + unsold
    return {
        "n": n,
        "invested": inv,
        "end_value": end_value,
        "roi": end_value / inv - 1 if inv else 0,
        "spy_roi": spy_end_total / inv - 1 if inv else 0,
        "exc_avg": statistics.fmean(excs) if excs else 0,
        "win_rate": wins / n if n else 0,
        "beat_rate": beats / n if n else 0,
        "pct_open": (n - sold_n) / n if n else 0,
    }


# --- variants ---------------------------------------------------------------

_FLAT = lambda b: 100.0  # noqa: E731


def _tier(b):
    s = b["signal"]
    return 50.0 if s < 45 else (100.0 if s < 55 else 200.0)


def _buysize(b):
    return 200.0 if b["buy"] else 100.0


VARIANTS = [
    ("Phase-1 locked (baseline)", lambda b: True, _FLAT),
    ("FILTER buy = YES", lambda b: b["buy"], _FLAT),
    ("FILTER signal >= 50", lambda b: b["signal"] >= 50, _FLAT),
    ("FILTER signal >= 55", lambda b: b["signal"] >= 55, _FLAT),
    ("FILTER buy OR signal >= 50", lambda b: b["buy"] or b["signal"] >= 50, _FLAT),
    ("SIZE tiered $50/$100/$200 by signal", lambda b: True, _tier),
    ("SIZE buy->$200 else $100", lambda b: True, _buysize),
]


def _pct(x):
    return f"{x * 100:+.1f}%"


def run() -> None:
    conn = store.connect()
    sigs, sales = _load(conn)
    conn.close()
    print(f"[phase1+2] {len(sigs)} growth+proven; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])
    base_bets = _build_bets(sigs, sales, prices)
    print(f"[phase1+2] {len(base_bets)} locked bets", flush=True)

    # variant -> {sell_name: result}
    results = {}
    for name, filt, size in VARIANTS:
        sel = [b for b in base_bets if filt(b)]
        results[name] = {sn: _sim(key, sel, prices, size) for sn, key in SELL}

    base = results["Phase-1 locked (baseline)"]
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        "status: canonical",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1 → Phase 2 Combined Backtest",
        "",
        "> Locked Phase-1 set (House+Senate), folding in the Phase-2 LLM scores two ways — "
        "**FILTER** (buy only the high-conviction subset) and **SIZE** (bet more on high-signal "
        "trades). $100 base bet; sizing variants vary it. Each compared to the locked baseline "
        f"and to SPY on the same dates. Marked-to-market as of {TODAY}.",
        "",
        "## Market-adjusted EDGE vs SPY (ROI − SPY-alt ROI), by variant × sell rule",
        "_Δ vs the locked baseline in par) on the 1-year column._",
        "",
        "| variant | bets | invested | 1-year | sell-w-trader | buy & hold |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for name, _, _ in VARIANTS:
        r = results[name]
        e1 = r["1-year"]["roi"] - r["1-year"]["spy_roi"]
        d1 = e1 - (base["1-year"]["roi"] - base["1-year"]["spy_roi"])
        et = r["sell-when-trader-sells"]["roi"] - r["sell-when-trader-sells"]["spy_roi"]
        eh = r["buy & hold"]["roi"] - r["buy & hold"]["spy_roi"]
        d1s = "" if name.startswith("Phase-1") else f" ({_pct(d1)})"
        L.append(
            f"| {name} | {r['1-year']['n']} | ${r['1-year']['invested']:,.0f} | "
            f"**{_pct(e1)}**{d1s} | {_pct(et)} | {_pct(eh)} |"
        )

    L += [
        "",
        "## Total ROI, by variant × sell rule",
        "| variant | bets | 1-year | sell-w-trader | buy & hold | 1-yr win% | 1-yr beat% |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for name, _, _ in VARIANTS:
        r = results[name]
        L.append(
            f"| {name} | {r['1-year']['n']} | {_pct(r['1-year']['roi'])} | "
            f"{_pct(r['sell-when-trader-sells']['roi'])} | {_pct(r['buy & hold']['roi'])} | "
            f"{r['1-year']['win_rate'] * 100:.0f}% | {r['1-year']['beat_rate'] * 100:.0f}% |"
        )

    L += [
        "",
        "## Read",
        "- **FILTER** variants concentrate capital into fewer, higher-conviction trades — higher "
        "edge if the LLM signal is real, but fewer bets (more concentration / variance).",
        "- **SIZE** variants keep every locked trade but tilt dollars toward high-signal names — "
        "keeps breadth while leaning into the gradient from the signal evaluation.",
        "- Compare each variant's 1-year edge + Δ to the locked baseline. A variant wins if it "
        "lifts the edge enough to justify lost diversification (filters) or is ~free (sizing).",
        "- Caveats: small filtered samples are noisy; open positions marked-to-market; "
        "House+Senate; survivorship-pruned. The Phase-2 scores are look-ahead-best-effort.",
    ]
    md = "\n".join(L)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"\nwrote {_OUT}\n")
    print(md)


if __name__ == "__main__":
    run()
