"""FINAL buy-strategy backtest (2022→present) — the 3-tier signal-scaled strategy.

Tiers (by Phase-2 signal_strength), equity bet always held under 3 sell rules:
  • Tier 1 — every Phase-1 locked trade            -> $50 equity
  • Tier 2 — signal_strength >= 50                 -> $100 equity
  • Tier 3 — signal_strength >= 60                 -> $100 equity + 1-yr ATM CALLS:
        buy whole contracts at the entry premium until cumulative spend first reaches $100
        (always >= 1 contract), i.e. contracts = max(1, ceil(100 / entry_premium)).

Window 2022→now (real option history floor). Writes a detailed markdown report:
strategy details · topline · monthly longitudinal · start-date dependence · analysis · trades.

  POLYGON_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.final_backtest
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import polygon_options as po
from . import store
from .phase1_backtest import GROWTH, TODAY, _last, _on_after, _prices, _resolve_sell

_OUT = (
    "/home/carter/vault/Projects/Active/Black Box/Insider Trader/Backtests/"
    "2026-06-25 FINAL Buy Strategy Backtest.md"
)
START = date(2022, 1, 1)
LOCKED_RUNUP = 0.05
T2, T3 = 50, 60  # signal thresholds for the $100 and options tiers
SELL = [("1-year", 360), ("sell-when-trader-sells", "trader"), ("buy & hold", "hold")]


def _tier(sig):
    return 3 if sig >= T3 else (2 if sig >= T2 else 1)


def _equity_bet(tier):
    return 100.0 if tier >= 2 else 50.0


def _load(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, f.bioguide, m.full_name, t.ticker, t.txn_date, t.disclosure_date, "
            "a.signal_strength, a.buy "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "JOIN trader_metrics tm ON tm.transaction_id=t.id "
            "LEFT JOIN phase2_analyses a ON a.transaction_id=t.id AND a.prompt_version='p2-v1' "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date >= %s AND s.sector = ANY(%s) AND tm.prior_bigwin90 > 0 "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id",
            (START, list(GROWTH)),
        )
        cols = ["tid", "bioguide", "member", "ticker", "txn_date", "disc", "signal", "buy"]
        sigs = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        cur.execute(
            "SELECT f.bioguide, t.ticker, t.disclosure_date FROM transactions t "
            "JOIN filings f USING(doc_id) WHERE t.txn_type='sale' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date IS NOT NULL"
        )
        sales = defaultdict(list)
        for bio, tk, d in cur.fetchall():
            sales[(bio, tk)].append(d)
    for v in sales.values():
        v.sort()
    return sigs, sales


def _build(sigs, sales, prices):
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
        sig = s["signal"] or 0
        tier = _tier(sig)
        bets.append(
            {
                "tid": s["tid"],
                "bioguide": s["bioguide"],
                "member": s["member"],
                "ticker": s["ticker"],
                "signal": sig,
                "tier": tier,
                "equity_bet": _equity_bet(tier),
                "buy_date": buy[0],
                "buy_close": buy[1],
                "spy_buy": spy0[1],
                "sales": sales,
            }
        )
    return bets


def _fetch_options(bets):
    """Real 1-yr ATM call path for every Tier-3 bet."""
    import yfinance as yf

    splits: dict[str, object] = {}
    t3 = [b for b in bets if b["tier"] == 3]
    print(f"[final] fetching real options for {len(t3)} tier-3 trades...", flush=True)
    for i, b in enumerate(t3, 1):
        if b["ticker"] not in splits:
            try:
                splits[b["ticker"]] = yf.Ticker(b["ticker"]).splits
            except Exception:  # noqa: BLE001
                splits[b["ticker"]] = None
        spot = po.unadjusted_spot(b["ticker"], b["buy_date"], b["buy_close"], splits[b["ticker"]])
        b["opt"] = None
        if spot:
            res = po.option_premium_path(b["ticker"], b["buy_date"], spot, target_dte=365)
            if res and res.get("entry_premium") and res.get("last_close"):
                entry = res["entry_premium"]
                ncon = max(1, math.ceil(100 / entry))
                exp = datetime.strptime(res["expiration"], "%Y-%m-%d").date()
                b["opt"] = {
                    "entry": entry,
                    "exit": res["last_close"],
                    "contracts": ncon,
                    "spend": ncon * entry,
                    "end": ncon * res["last_close"],
                    "ret": res["last_close"] / entry - 1.0,
                    "exit_date": min(exp, TODAY),
                    "expired": exp <= TODAY,
                    "strike": res["strike"],
                }
        if i % 8 == 0:
            print(f"[final] {i}/{len(t3)}", flush=True)
        time.sleep(0.12)


# --- per-bet outcomes -------------------------------------------------------


def _equity_outcome(b, prices, key):
    ser = prices[b["ticker"]]
    spy = prices["SPY"]
    sell = _resolve_sell(key, b["buy_date"], b["bioguide"], b["ticker"], ser, b["sales"])
    if sell:
        sd, sc, sold = sell[0], sell[1], True
    else:
        sd, sc, sold = _last(ser)[0], _last(ser)[1], False
    shares = b["equity_bet"] / b["buy_close"]
    spy_sell = _on_after(spy, sd) or _last(spy)
    return {
        "sold": sold,
        "ret": sc / b["buy_close"] - 1.0,
        "end": shares * sc,
        "spy_alt": b["equity_bet"] * (spy_sell[1] / b["spy_buy"]),
    }


def _agg(bets, prices, key):
    """Aggregate the book under one equity sell rule + the (rule-independent) option sleeve."""
    cash_in = opt_spend = opt_end = 0.0
    eq_real = eq_open = spy_end = 0.0
    eq_rets, opt_rets = [], []
    eq_win = eq_loss = opt_win = opt_loss = 0
    spy_opt = 0.0
    for b in bets:
        o = _equity_outcome(b, prices, key)
        cash_in += b["equity_bet"]
        if o["sold"]:
            eq_real += o["end"]
        else:
            eq_open += o["end"]
        spy_end += o["spy_alt"]
        eq_rets.append(o["ret"])
        eq_win += o["ret"] > 0
        eq_loss += o["ret"] <= 0
        opt = b.get("opt")
        if opt:
            cash_in += opt["spend"]
            opt_spend += opt["spend"]
            opt_end += opt["end"]
            spy = prices["SPY"]
            spy_x = _on_after(spy, opt["exit_date"]) or _last(spy)
            spy_opt += opt["spend"] * (spy_x[1] / b["spy_buy"])
            opt_rets.append(opt["ret"])
            opt_win += opt["ret"] > 0
            opt_loss += opt["ret"] <= 0
    end = eq_real + eq_open + opt_end
    spy_total = spy_end + spy_opt
    return {
        "n": len(bets),
        "cash_in": cash_in,
        "eq_realized": eq_real,
        "remaining": eq_open,
        "opt_spend": opt_spend,
        "opt_end": opt_end,
        "end": end,
        "roi": end / cash_in - 1 if cash_in else 0,
        "spy_roi": spy_total / cash_in - 1 if cash_in else 0,
        "eq_win": eq_win,
        "eq_loss": eq_loss,
        "opt_win": opt_win,
        "opt_loss": opt_loss,
        "eq_max_gain": max(eq_rets) if eq_rets else 0,
        "eq_max_loss": min(eq_rets) if eq_rets else 0,
        "opt_max_gain": max(opt_rets) if opt_rets else 0,
        "opt_max_loss": min(opt_rets) if opt_rets else 0,
    }


def _pct(x):
    return f"{x * 100:+.1f}%"


def _render(bets, prices):
    by_tier = {t: sum(1 for b in bets if b["tier"] == t) for t in (1, 2, 3)}
    n_opt = sum(1 for b in bets if b.get("opt"))
    aggs = {name: _agg(bets, prices, key) for name, key in SELL}
    a1 = aggs["1-year"]

    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        "status: canonical",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — FINAL Buy Strategy Backtest (2022→present)",
        "",
        "## Strategy details",
        "**Phase 1 (locked quantitative filter)** — a US congressional PURCHASE that is: in a "
        "GROWTH sector (Technology / Basic Materials / Energy / Communication Services / "
        "Industrials); by a PROVEN trader (had a prior 90-day market-beating big win, "
        "point-in-time); and where the stock ROSE ≥ +5% between the member's purchase and the "
        "public disclosure date (momentum confirmation). We act on the disclosure date (public).",
        "",
        "**Phase 2 (LLM signal)** — Opus 4.8 reads a dossier (trader track-record, the trade, the "
        "company, our R&D meta-heuristics, and a biographical profile of the member) and returns a "
        "`signal_strength` 0–100. Our evaluation showed signal_strength separates winners (the "
        "buy flag and signal_strength carry the signal; trader_alpha and confidence do not).",
        "",
        "**Buy tiers (by signal_strength):**",
        f"- **Tier 1 — every locked trade → $50 equity.** ({by_tier[1]} bets)",
        f"- **Tier 2 — signal ≥ {T2} → $100 equity.** ({by_tier[2]} bets)",
        f"- **Tier 3 — signal ≥ {T3} → $100 equity + 1-yr ATM calls.** ({by_tier[3]} bets, "
        f"{n_opt} with a tradeable contract). Contracts = max(1, ⌈$100 / entry premium⌉) — buy "
        "whole contracts until spend first reaches $100, always ≥1. Real Polygon premiums; held "
        "to expiry (or marked at the latest real close if not yet expired).",
        "",
        "**Sell strategies (equity sleeve, all three reported):** 1-year hold · "
        "sell-when-the-trader-discloses-a-sale · buy & hold. Options always run ~1 year to expiry. "
        f"Window: {START} → {TODAY}. Compared to the same dollars in SPY over each position's "
        "window. Look-ahead-safe; open positions marked-to-market.",
        "",
        "## Topline (cumulative)",
        f"- **Bets:** {len(bets)} total — Tier 1 (${50}): {by_tier[1]}, Tier 2 ($100): "
        f"{by_tier[2]}, Tier 3 ($100+opts): {by_tier[3]}.",
        f"- **Options:** spent **${a1['opt_spend']:,.0f}** on {n_opt} option positions, "
        f"returned **${a1['opt_end']:,.0f}** (×{a1['opt_end'] / a1['opt_spend']:.1f} "
        f"on premium)."
        if a1["opt_spend"]
        else "- Options: none.",
        f"- **Stock trades (by 1-yr return):** {a1['eq_win']} winners / {a1['eq_loss']} losers; "
        f"max gain {_pct(a1['eq_max_gain'])}, max loss {_pct(a1['eq_max_loss'])}.",
        f"- **Option trades:** {a1['opt_win']} winners / {a1['opt_loss']} losers; "
        f"max gain {_pct(a1['opt_max_gain'])}, max loss {_pct(a1['opt_max_loss'])}.",
        "",
        "| sell strategy | cash in | realized (cash out) | portfolio remaining | options end | "
        "**total ROI** | SPY ROI | edge |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name, _ in SELL:
        a = aggs[name]
        L.append(
            f"| {name} | ${a['cash_in']:,.0f} | ${a['eq_realized']:,.0f} | "
            f"${a['remaining']:,.0f} | ${a['opt_end']:,.0f} | **{_pct(a['roi'])}** | "
            f"{_pct(a['spy_roi'])} | {_pct(a['roi'] - a['spy_roi'])} |"
        )

    # monthly longitudinal (cohort by buy month, 1-year equity + options)
    L += [
        "",
        "## Longitudinal (by buy-month cohort; 1-year equity + options sleeve)",
        "| month | bets | cash in | options spend | options end | total ROI | SPY ROI | edge |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    months = sorted({(b["buy_date"].year, b["buy_date"].month) for b in bets})
    for y, m in months:
        cohort = [b for b in bets if (b["buy_date"].year, b["buy_date"].month) == (y, m)]
        a = _agg(cohort, prices, 360)
        L.append(
            f"| {y}-{m:02d} | {a['n']} | ${a['cash_in']:,.0f} | ${a['opt_spend']:,.0f} | "
            f"${a['opt_end']:,.0f} | {_pct(a['roi'])} | {_pct(a['spy_roi'])} | "
            f"{_pct(a['roi'] - a['spy_roi'])} |"
        )

    # start-date dependence (1-year equity + options)
    L += [
        "",
        "## Start-date dependence (what if I'd started later; 1-year + options)",
        "| started | bets | total ROI | SPY ROI | edge |",
        "|---|--:|--:|--:|--:|",
    ]
    starts = []
    s = START
    while s <= TODAY - timedelta(days=180):
        starts.append(s)
        # +6 months
        s = date(s.year + (s.month + 5) // 12, (s.month + 5) % 12 + 1, 1)
    for st in starts:
        sub = [b for b in bets if b["buy_date"] >= st]
        if not sub:
            continue
        a = _agg(sub, prices, 360)
        L.append(
            f"| {st.isoformat()} | {a['n']} | {_pct(a['roi'])} | {_pct(a['spy_roi'])} | "
            f"{_pct(a['roi'] - a['spy_roi'])} |"
        )

    # analysis
    edge1 = a1["roi"] - a1["spy_roi"]
    L += [
        "",
        "## Analysis & red flags",
        f"- The strategy's 1-year ROI is **{_pct(a1['roi'])}** vs SPY {_pct(a1['spy_roi'])} "
        f"(**{_pct(edge1)} edge**) over {START}→now, on ${a1['cash_in']:,.0f} deployed.",
        f"- The option sleeve is **tail-driven**: {a1['opt_win']}/{a1['opt_win'] + a1['opt_loss']} "
        "option positions won, but the returns are carried by a few huge winners (max option gain "
        f"{_pct(a1['opt_max_gain'])}); losers go toward −100% (premium is the capped downside). "
        "Expect lumpy results — a great year can hinge on one or two option winners.",
        "- **Concentration:** Tier-3 (options) is a small set; check the trade list for "
        "single-trader / single-name concentration before sizing up.",
        "- **Look-ahead:** equity is fully point-in-time; the Phase-2 signal is best-effort "
        "point-in-time (bio narrative is the soft spot); options use real contemporaneous prices.",
        "- **Caveats:** 2022→present is only ~4.5 years / limited regimes; option exit uses the "
        "last real close (a worthless-but-untraded expiry can be slightly over-valued); "
        "survivorship-pruned; House+Senate.",
        "",
        "## Trade details (all bets, sorted by date)",
        "| date | member | ticker | signal | tier | $eq | 1-yr ret | option (contracts @ entry → "
        "exit = ret) |",
        "|---|---|---|--:|--:|--:|--:|---|",
    ]
    for b in sorted(bets, key=lambda x: x["buy_date"]):
        o = _equity_outcome(b, prices, 360)
        opt = b.get("opt")
        ostr = (
            f"{opt['contracts']}@${opt['entry']:.2f}→${opt['exit']:.2f} = {_pct(opt['ret'])}"
            if opt
            else ("—" if b["tier"] < 3 else "(no contract)")
        )
        L.append(
            f"| {b['buy_date']} | {b['member']} | {b['ticker']} | {b['signal']} | {b['tier']} | "
            f"${b['equity_bet']:.0f} | {_pct(o['ret'])} | {ostr} |"
        )
    return "\n".join(L)


def run() -> None:
    conn = store.connect()
    sigs, sales = _load(conn)
    conn.close()
    print(f"[final] {len(sigs)} locked candidates 2022+; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])
    bets = _build(sigs, sales, prices)
    print(f"[final] {len(bets)} bets", flush=True)
    _fetch_options(bets)
    md = _render(bets, prices)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"\nwrote {_OUT}\n")
    print(md[:2500])


if __name__ == "__main__":
    run()
