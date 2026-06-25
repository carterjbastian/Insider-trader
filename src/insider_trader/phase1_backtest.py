"""Phase-1 isolation backtest: $100/signal on the quantitative buy gate; 7 sell sims.

CANONICAL PHASE-1 GATE = "high-precision" (set by Carter 2026-06-25): a House
PURCHASE in a GROWTH sector, SMALL position ($1k-$15k bracket), by a PROVEN trader
(prior 90-day market-beating big-win). The looser "exclude-defensive" gate is retired
(kept only for reproducing the 06-24 report). We BUY $100 at the close on/after the
**disclosure date** (public info — never the member's trade date), then simulate seven
sell rules and compare every result to the same $100, same dates, in SPY.

Look-ahead-safe: signals act on the disclosure date; positions not yet sold at sim end
are marked-to-market. See the vault "Backtesting Playbook". Writes a markdown report.

  DATABASE_URL=... uv run python -m insider_trader.phase1_backtest                # high-precision
  DATABASE_URL=... uv run python -m insider_trader.phase1_backtest missed-action  # run-up sweep
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from datetime import date, datetime, timedelta

from . import store

DEFENSIVE = {"Utilities", "Real Estate", "Consumer Defensive", "Financial Services"}
BET = 100.0
TODAY = date(2026, 6, 24)
STRATS = [
    ("30-day", 30),
    ("60-day", 60),
    ("90-day", 90),
    ("6-month", 180),
    ("1-year", 360),
    ("sell-when-trader-sells", "trader"),
    ("buy & hold", "hold"),
]
GROWTH = {"Technology", "Basic Materials", "Energy", "Communication Services", "Industrials"}
_BTDIR = "/home/carter/vault/Projects/Black Box/Insider Trader/Backtests/"
GATES = {
    "exclude-defensive": {
        "desc": "House PURCHASE, tradeable ticker, sector NOT in {Utilities, Real Estate, "
        "Consumer Defensive, Financial Services} — the cheap rule-out gate.",
        "out": _BTDIR + "2026-06-24 Phase 1 Isolation.md",
    },
    "high-precision": {
        "desc": "House PURCHASE in a GROWTH sector (Technology / Basic Materials / Energy / "
        "Communication Services / Industrials), SMALL position ($1,001-$15,000 bracket), by a "
        "PROVEN trader (had a prior 90-day market-beating big-win) — the high-conviction gate.",
        "out": _BTDIR + "2026-06-25 Phase 1 High-Precision.md",
    },
}
# "missed-action" sweep: on top of high-precision, exclude buys whose ticker already ran up
# more than X% between the member's purchase date and the public disclosure date (the gain we
# couldn't capture). Thresholds chosen as round levels spanning the run-up distribution.
MISSED_THRESHOLDS = [0.05, 0.10, 0.20, 0.30, 0.50]
_MISSED_OUT = _BTDIR + "2026-06-25 Phase 1 Missed-Action Sweep.md"
# Directional cohorts: high-precision AND the pre-disclosure move falls in a band. Tests the
# inverse of the missed-action idea — does momentum (buy run-ups) or dip-buying (buy declines)
# beat buying everything? Each is a stand-alone INCLUSION filter, not a cumulative exclusion.
COHORTS = [
    ("ran up >= +20%", lambda g: g >= 0.20),
    ("ran up >= +10%", lambda g: g >= 0.10),
    ("ran up >= +5%", lambda g: g >= 0.05),
    ("any run-up (> 0%)", lambda g: g > 0),
    ("flat (-5%..+5%)", lambda g: -0.05 <= g <= 0.05),
    ("any decline (< 0%)", lambda g: g < 0),
    ("fell <= -5%", lambda g: g <= -0.05),
    ("fell <= -10%", lambda g: g <= -0.10),
    ("fell <= -20%", lambda g: g <= -0.20),
]
_DIR_OUT = _BTDIR + "2026-06-25 Phase 1 Run-up Direction Cohorts.md"


# --- data -------------------------------------------------------------------


def _load(conn, gate="exclude-defensive"):
    base = (
        "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
        "f.bioguide, m.full_name, t.ticker, s.sector, t.disclosure_date, t.txn_date "
        "FROM transactions t JOIN filings f USING(doc_id) "
        "JOIN members m ON m.bioguide=f.bioguide "
        "JOIN securities s ON s.ticker=t.ticker AND s.ok "
    )
    common = (
        "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
        "AND t.disclosure_date IS NOT NULL AND s.sector IS NOT NULL "
    )
    order = "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id"
    with conn.cursor() as cur:
        if gate == "high-precision":
            cur.execute(
                base
                + "JOIN trader_metrics tm ON tm.transaction_id=t.id "
                + common
                + "AND s.sector = ANY(%s) AND t.amount_high <= 15000 "
                "AND tm.prior_bigwin90 > 0 " + order,
                (list(GROWTH),),
            )
        else:
            cur.execute(base + common + "AND s.sector <> ALL(%s) " + order, (list(DEFENSIVE),))
        sigs = [
            dict(
                zip(["bioguide", "member", "ticker", "sector", "disc", "txn_date"], r, strict=True)
            )
            for r in cur.fetchall()
        ]
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


def _prices(tickers, batch=60):
    import yfinance as yf

    syms = sorted(set(tickers) | {"SPY"})
    out: dict[str, dict[date, float]] = {}
    for i in range(0, len(syms), batch):
        chunk = syms[i : i + batch]
        df = yf.download(
            chunk,
            start="2013-01-01",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        for s in chunk:
            try:
                close = df[s]["Close"].dropna()
            except Exception:  # noqa: BLE001
                continue
            if len(close):
                ser = {ts.date(): float(v) for ts, v in close.items()}
                vals = list(ser.values())
                # drop OTC/illiquid/split-artifact series (a >1000x full-range ratio is a
                # data error, not a real equity — these blow up long-hold aggregates).
                if min(vals) <= 0 or max(vals) / min(vals) > 1000:
                    continue
                out[s] = ser
        time.sleep(0.5)
        print(f"  prices {min(i + batch, len(syms))}/{len(syms)} ({len(out)} ok)", flush=True)
    return out


def _on_after(series, d):
    cand = [dd for dd in series if dd >= d]
    if not cand:
        return None
    k = min(cand)
    return k, series[k]


def _last(series):
    k = max(series)
    return k, series[k]


# --- simulation -------------------------------------------------------------


def _resolve_sell(key, buy_date, bioguide, ticker, ser, sales):
    """Return (sell_date, sell_close) if sold by TODAY, else None (open position)."""
    if key == "hold":
        return None
    if key == "trader":
        sd = next((s for s in sales.get((bioguide, ticker), []) if s > buy_date), None)
        if not sd:
            return None
        hit = _on_after(ser, sd)
        return hit if (hit and hit[0] <= TODAY) else None
    target = buy_date + timedelta(days=key)  # fixed horizon in days
    hit = _on_after(ser, target)
    return hit if (hit and hit[0] <= TODAY) else None


def _simulate(strat_key, bets, prices):
    spy = prices["SPY"]
    rows = []
    for b in bets:
        ser = prices[b["ticker"]]
        sell = _resolve_sell(
            strat_key, b["buy_date"], b["bioguide"], b["ticker"], ser, sales=b["sales"]
        )
        if sell:
            sell_date, sell_close = sell
            sold = True
        else:
            sell_date, sell_close = _last(ser)
            sold = False
        ret = sell_close / b["buy_close"] - 1.0
        end_val = b["shares"] * sell_close
        spy_sell = _on_after(spy, sell_date) or _last(spy)
        spy_ret = spy_sell[1] / b["spy_buy"] - 1.0
        spy_end = BET * (spy_sell[1] / b["spy_buy"])
        rows.append(
            {"ret": ret, "exc": ret - spy_ret, "end_val": end_val, "spy_end": spy_end, "sold": sold}
        )
    return rows


def _agg(rows):
    n = len(rows)
    sold = [r for r in rows if r["sold"]]
    open_ = [r for r in rows if not r["sold"]]
    rets = [r["ret"] for r in rows]
    excs = [r["exc"] for r in rows]
    invested = BET * n
    recouped = sum(r["end_val"] for r in sold)
    unsold = sum(r["end_val"] for r in open_)
    end_value = recouped + unsold
    spy_end_total = sum(r["spy_end"] for r in rows)
    return {
        "n": n,
        "pct_open": len(open_) / n,
        "invested": invested,
        "recouped": recouped,
        "unsold": unsold,
        "end_value": end_value,
        "roi": end_value / invested - 1,
        "ret_avg": statistics.fmean(rets),
        "ret_med": statistics.median(rets),
        "ret_var": statistics.pvariance(rets),
        "exc_avg": statistics.fmean(excs),
        "exc_med": statistics.median(excs),
        "exc_var": statistics.pvariance(excs),
        "win_rate": sum(r["ret"] > 0 for r in rows) / n,
        "beat_rate": sum(r["exc"] > 0 for r in rows) / n,
        "spy_end_total": spy_end_total,
        "spy_roi": spy_end_total / invested - 1,
    }


def _activity(bets):
    months = Counter((b["disc"].year, b["disc"].month) for b in bets)
    counts = list(months.values())
    ym = sorted(months)
    # longest gap (consecutive calendar months with 0 bets) across the span
    gap = maxgap = 0
    if ym:
        cur = date(ym[0][0], ym[0][1], 1)
        end = date(ym[-1][0], ym[-1][1], 1)
        while cur <= end:
            if (cur.year, cur.month) in months:
                gap = 0
            else:
                gap += 1
                maxgap = max(maxgap, gap)
            cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return {
        "n": len(bets),
        "avg_per_mo": statistics.fmean(counts),
        "med_per_mo": statistics.median(counts),
        "max_in_mo": max(counts),
        "max_gap": maxgap,
        "span": f"{ym[0]}..{ym[-1]}" if ym else "n/a",
    }


def _gap(ser, txn_date, buy):
    """Purchase->disclosure price move: the run-up we couldn't capture because we can
    only legally act on the (public) disclosure date. None if no txn-date price."""
    hit = _on_after(ser, txn_date)
    if not hit or hit[1] <= 0:
        return None
    return buy[1] / hit[1] - 1.0


def _build_bets(sigs, sales, prices):
    bets = []
    for s in sigs:
        ser = prices.get(s["ticker"])
        buy = _on_after(ser, s["disc"]) if ser else None
        spy_buy0 = _on_after(prices["SPY"], buy[0]) if buy else None
        if not buy or not spy_buy0:
            continue
        bets.append(
            {
                "member": s["member"],
                "bioguide": s["bioguide"],
                "ticker": s["ticker"],
                "disc": s["disc"],
                "buy_date": buy[0],
                "buy_close": buy[1],
                "shares": BET / buy[1],
                "spy_buy": spy_buy0[1],
                "sales": sales,
                "gap": _gap(ser, s["txn_date"], buy),
            }
        )
    return bets


def _sims(bets, prices):
    return {name: _agg(_simulate(key, bets, prices)) for name, key in STRATS}


def run(gate: str = "high-precision", out_path: str | None = None) -> dict:
    out_path = out_path or GATES[gate]["out"]
    conn = store.connect()
    sigs, sales = _load(conn, gate)
    conn.close()
    print(f"[{gate}] {len(sigs)} Phase-1 buy signals; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])

    bets = _build_bets(sigs, sales, prices)
    activity = _activity(bets)
    results = _sims(bets, prices)
    md = _render(activity, results, GATES[gate]["desc"])
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nwrote {out_path}\n")
    print(md)
    return {"activity": activity, "results": results}


def _pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]


def run_missed_action(thresholds=None, out_path=None) -> dict:
    """High-precision gate + a sweep of pre-disclosure run-up exclusion thresholds."""
    thresholds = thresholds or MISSED_THRESHOLDS
    out_path = out_path or _MISSED_OUT
    conn = store.connect()
    sigs, sales = _load(conn, "high-precision")
    conn.close()
    print(f"[missed-action] {len(sigs)} high-precision signals; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])

    bets = _build_bets(sigs, sales, prices)
    gaps = sorted(b["gap"] for b in bets if b["gap"] is not None)
    n_unknown = sum(1 for b in bets if b["gap"] is None)

    baseline = _sims(bets, prices)
    variants = []  # (threshold, kept_count, ruled_out, results)
    for t in thresholds:
        kept = [b for b in bets if b["gap"] is None or b["gap"] <= t]
        variants.append((t, len(kept), len(bets) - len(kept), _sims(kept, prices)))

    md = _render_missed(len(bets), gaps, n_unknown, baseline, variants)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nwrote {out_path}\n")
    print(md)
    return {"n": len(bets), "gaps": gaps, "baseline": baseline, "variants": variants}


def run_directional(out_path=None) -> dict:
    """Inversion of the missed-action test: does buying ONLY run-ups (momentum) or ONLY
    declines (dip-buying) beat buying every high-precision signal?"""
    out_path = out_path or _DIR_OUT
    conn = store.connect()
    sigs, sales = _load(conn, "high-precision")
    conn.close()
    print(f"[directional] {len(sigs)} high-precision signals; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])

    bets = _build_bets(sigs, sales, prices)
    classifiable = [b for b in bets if b["gap"] is not None]
    baseline = _sims(bets, prices)
    cohorts = []  # (label, n, results-or-None)
    for label, pred in COHORTS:
        c = [b for b in classifiable if pred(b["gap"])]
        cohorts.append((label, len(c), _sims(c, prices) if c else None))

    md = _render_directional(len(bets), len(classifiable), baseline, cohorts)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nwrote {out_path}\n")
    print(md)
    return {"baseline": baseline, "cohorts": cohorts}


# --- markdown report --------------------------------------------------------


def _pct(x):
    return f"{x * 100:+.1f}%"


def _render(act, results, gate_desc) -> str:
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1 Isolation Backtest",
        "",
        f"> **Buy gate:** {gate_desc} **$100** bought at the close on/after the **disclosure "
        "date**. Phase-1 quantitative gate only — no LLM. Compared to the same $100 in **SPY** "
        f"on the same buy/sell dates. Look-ahead-safe; open positions marked-to-market as of "
        f"{TODAY}. See [[../Backtesting Playbook]].",
        "",
        "## Activity (same for all sell strategies — the buy side is identical)",
        f"- **Bets placed:** {act['n']}  (signal months {act['span']})",
        f"- **Bets/month:** avg {act['avg_per_mo']:.1f}, median {act['med_per_mo']:.0f}, "
        f"max in one month {act['max_in_mo']}, longest no-bet stretch {act['max_gap']} months",
        "",
        "## Results by sell strategy",
        "",
        "| sell strategy | invested | recouped | unsold value | **total ROI** | SPY-alt ROI | "
        "edge | mean ret (raw) | mean ret (mkt-adj) | median | win% | beat-mkt% | % open |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name, _ in STRATS:
        r = results[name]
        L.append(
            f"| {name} | ${r['invested']:,.0f} | ${r['recouped']:,.0f} | ${r['unsold']:,.0f} | "
            f"**{_pct(r['roi'])}** | {_pct(r['spy_roi'])} | "
            f"{_pct(r['roi'] - r['spy_roi'])} | "
            f"{_pct(r['ret_avg'])} | {_pct(r['exc_avg'])} | {_pct(r['ret_med'])} | "
            f"{r['win_rate'] * 100:.0f}% | {r['beat_rate'] * 100:.0f}% | "
            f"{r['pct_open'] * 100:.0f}% |"
        )
    L += [
        "",
        "## Per-strategy detail (variance of returns)",
        "| sell strategy | end value | raw ret var | mkt-adj ret var |",
        "|---|--:|--:|--:|",
    ]
    for name, _ in STRATS:
        r = results[name]
        L.append(f"| {name} | ${r['end_value']:,.0f} | {r['ret_var']:.3f} | {r['exc_var']:.3f} |")
    L += [
        "",
        "## Notes",
        "- **invested** = $100 × bets. **recouped** = realized sale proceeds. **unsold value** "
        "= current mark-to-market of positions not yet sold (matters most for buy & hold and "
        "for fixed-horizon sells whose window hasn't elapsed).",
        "- **total ROI** = (recouped + unsold − invested) / invested. **SPY-alt ROI** = same "
        "$100 bets/sells in SPY. **edge** = strategy ROI − SPY-alt ROI.",
        "- **mkt-adj** return = per-bet return minus SPY over the identical window.",
        "- Caveats: House-only; survivorship (unpriced/delisted dropped); OTC/illiquid/split-"
        "artifact tickers excluded (>1000x full-range ratio = bad data); Phase-1 gate is "
        "sector-only (the cleanest rule-out). To be re-validated with Senate data. Phase 2 "
        "(LLM insider-proximity) is the layer meant to improve on this.",
    ]
    return "\n".join(L)


def _render_missed(n_bets, gaps, n_unknown, baseline, variants) -> str:
    mean_gap = statistics.fmean(gaps) if gaps else 0.0
    med_gap = statistics.median(gaps) if gaps else 0.0
    pos = sum(1 for g in gaps if g > 0)
    hi = baseline["1-year"]
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1 + Missed-Action Filter Sweep",
        "",
        "> **Base gate:** high-precision (House PURCHASE in a GROWTH sector, SMALL position "
        "$1k–$15k, by a PROVEN trader with a prior 90-day big-win). **Added filter under test:** "
        "exclude any buy whose ticker had already risen more than **X%** between the member's "
        "**purchase date** and the **disclosure date** — the run-up we couldn't capture because "
        "we can only act on the public disclosure. $100/bet, all 7 sell rules, vs SPY on the "
        f"same dates. Look-ahead-safe; open positions marked-to-market as of {TODAY}.",
        "",
        "## Pre-disclosure run-up distribution (high-precision set)",
        f"- **{len(gaps)}** of {n_bets} bets have a measurable purchase→disclosure move "
        f"({n_unknown} lacked a purchase-date price and are never auto-excluded).",
        f"- mean **{_pct(mean_gap)}**, median **{_pct(med_gap)}**; "
        f"**{pos / len(gaps) * 100:.0f}%** rose before disclosure (rest flat/down).",
        "- percentiles (move by disclosure): "
        + ", ".join(
            f"p{int(q * 100)} {_pct(_pctile(gaps, q))}" for q in (0.5, 0.6, 0.75, 0.9, 0.95)
        ),
        "",
        "## Trades ruled out by threshold",
        "| exclude run-up > | bets kept | ruled out | % ruled out |",
        "|---|--:|--:|--:|",
        f"| (baseline — none) | {n_bets} | 0 | 0% |",
    ]
    for t, kept, ruled, _ in variants:
        L.append(f"| {t * 100:.0f}% | {kept} | {ruled} | {ruled / n_bets * 100:.0f}% |")

    L += [
        "",
        "## Market-adjusted EDGE vs SPY, by sell strategy",
        "_Each cell = strategy ROI − SPY-alt ROI over the identical windows. "
        "Δ = change vs the no-filter baseline._",
        "",
        "| sell strategy | baseline | "
        + " | ".join(f">{int(t * 100)}%" for t, _, _, _ in variants)
        + " |",
        "|---|--:|" + "--:|" * len(variants),
    ]
    for name, _ in STRATS:
        b_edge = baseline[name]["roi"] - baseline[name]["spy_roi"]
        cells = [f"**{_pct(b_edge)}**"]
        for _, _, _, res in variants:
            e = res[name]["roi"] - res[name]["spy_roi"]
            cells.append(f"{_pct(e)} ({_pct(e - b_edge)})")
        L.append(f"| {name} | " + " | ".join(cells) + " |")

    L += [
        "",
        "## Total ROI, by sell strategy",
        "| sell strategy | baseline | "
        + " | ".join(f">{int(t * 100)}%" for t, _, _, _ in variants)
        + " |",
        "|---|--:|" + "--:|" * len(variants),
    ]
    for name, _ in STRATS:
        cells = [f"**{_pct(baseline[name]['roi'])}**"]
        cells += [_pct(res[name]["roi"]) for _, _, _, res in variants]
        L.append(f"| {name} | " + " | ".join(cells) + " |")

    L += [
        "",
        "## Read",
        f"- Baseline high-precision 1-yr edge is **{_pct(hi['roi'] - hi['spy_roi'])}** "
        f"({hi['n']} bets). The question: does cutting already-ran-up buys *raise* the edge "
        "by enough to justify the lost volume?",
        "- A threshold helps only if the kept set's edge rises **and** enough bets survive. "
        "Watch the Δ columns: positive Δ with a small % ruled out = a free improvement; "
        "positive Δ that needs cutting half the book = a volume/edge trade-off to weigh.",
        "- Caveats: House-only; survivorship-pruned; OTC/split-artifact tickers excluded. "
        "Pre-disclosure move uses adjusted closes at/after the purchase and disclosure dates. "
        "To be re-validated with Senate data.",
    ]
    return "\n".join(L)


def _edge(r):
    return r["roi"] - r["spy_roi"]


def _render_directional(n_bets, n_class, baseline, cohorts) -> str:
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1: Pre-Disclosure Move Direction Cohorts",
        "",
        "> **Base gate:** high-precision. **Question:** instead of *excluding* run-ups, what if "
        "we INCLUDE-ONLY trades whose ticker moved a certain way between the member's purchase "
        "and the disclosure? Each row below is a stand-alone inclusion filter (momentum = buy "
        "run-ups; dip = buy declines). $100/bet, all 7 sell rules, vs SPY on the same dates. "
        f"Look-ahead-safe; marked-to-market as of {TODAY}.",
        "",
        f"- {n_class} of {n_bets} high-precision bets have a measurable purchase->disclosure move.",
        f"- Baseline (buy everything) 1-yr edge: **{_pct(_edge(baseline['1-year']))}**, "
        f"buy&hold edge: **{_pct(_edge(baseline['buy & hold']))}**.",
        "",
        "## Market-adjusted EDGE vs SPY, by cohort x sell strategy",
        "_Cell = strategy ROI - SPY-alt ROI. **N** = bets in the cohort (small N = noisy)._",
        "",
        "| cohort | N | " + " | ".join(name for name, _ in STRATS) + " |",
        "|---|--:|" + "--:|" * len(STRATS),
    ]
    b_cells = " | ".join(f"**{_pct(_edge(baseline[name]))}**" for name, _ in STRATS)
    L.append(f"| **baseline (all)** | {n_bets} | {b_cells} |")
    for label, n, res in cohorts:
        if not res:
            L.append(f"| {label} | {n} | " + " | ".join("-" for _ in STRATS) + " |")
            continue
        cells = " | ".join(_pct(_edge(res[name])) for name, _ in STRATS)
        L.append(f"| {label} | {n} | {cells} |")

    L += [
        "",
        "## Total ROI, by cohort x sell strategy",
        "| cohort | N | " + " | ".join(name for name, _ in STRATS) + " |",
        "|---|--:|" + "--:|" * len(STRATS),
    ]
    b_cells = " | ".join(f"**{_pct(baseline[name]['roi'])}**" for name, _ in STRATS)
    L.append(f"| **baseline (all)** | {n_bets} | {b_cells} |")
    for label, n, res in cohorts:
        if not res:
            L.append(f"| {label} | {n} | " + " | ".join("-" for _ in STRATS) + " |")
            continue
        cells = " | ".join(_pct(res[name]["roi"]) for name, _ in STRATS)
        L.append(f"| {label} | {n} | {cells} |")

    L += [
        "",
        "## Read",
        "- Compare each cohort's edge to the baseline row. If run-up cohorts beat baseline and "
        "decline cohorts trail it, momentum is real (and the missed-action filter was exactly "
        "backwards). If declines win, dip-buying insider names is the better entry.",
        "- Mind **N**: the deep-run-up and deep-decline tails are small and their edges are "
        "noisy — treat them as directional hints, not precise estimates.",
        "- Same caveats as the other Phase-1 backtests (House-only, survivorship-pruned, "
        "OTC/split-artifact excluded). To be re-validated with Senate data.",
    ]
    return "\n".join(L)


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "high-precision"
    if arg == "missed-action":
        run_missed_action()
    elif arg == "directional":
        run_directional()
    else:
        run(arg)
