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
# No-size experiment: drop the small-position cap (growth + proven trader only), crossed with
# baseline + run-up action filters, scored on the 3 sell strategies that matter most.
NOSIZE_ACTIONS = [
    ("baseline (all)", lambda g: True),
    ("any run-up (> 0%)", lambda g: g is not None and g > 0),
    ("ran up >= +5%", lambda g: g is not None and g >= 0.05),
    ("ran up >= +10%", lambda g: g is not None and g >= 0.10),
    ("ran up >= +20%", lambda g: g is not None and g >= 0.20),
]
NOSIZE_SELL = ["1-year", "sell-when-trader-sells", "buy & hold"]
_NOSIZE_OUT = _BTDIR + "2026-06-25 Phase 1 No-Size-Cap x Run-up.md"
# LOCKED Phase-1 strategy (2026-06-25): growth + proven trader (no size cap) + run-up >= +5%.
# These 3 sell rules are the locked candidates. This produces the canonical, full report.
LOCKED_RUNUP = 0.05
LOCKED_SELL = ["1-year", "sell-when-trader-sells", "buy & hold"]
_LOCKED_DESC = (
    "**Phase-1 (LOCKED):** growth sector + proven trader (prior 90-day big-win) + the stock "
    "ran up **>= +5%** between the member's purchase and the disclosure date. No size cap."
)
_LOCKED_OUT = _BTDIR + "2026-06-25 Phase 1 LOCKED — Full Backtest.md"


# --- data -------------------------------------------------------------------


def _load(conn, gate="exclude-defensive", chamber=None):
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
    # House filings carry chamber=NULL (legacy), Senate carry 'senate'.
    ch = {"senate": "AND f.chamber = 'senate' ", "house": "AND f.chamber IS NULL "}.get(chamber, "")
    order = "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id"
    with conn.cursor() as cur:
        if gate in ("high-precision", "growth-proven"):
            # growth-proven = high-precision WITHOUT the small-position cap
            size = "AND t.amount_high <= 15000 " if gate == "high-precision" else ""
            cur.execute(
                base
                + "JOIN trader_metrics tm ON tm.transaction_id=t.id "
                + common
                + ch
                + "AND s.sector = ANY(%s) "
                + size
                + "AND tm.prior_bigwin90 > 0 "
                + order,
                (list(GROWTH),),
            )
        else:
            cur.execute(base + common + ch + "AND s.sector <> ALL(%s) " + order, (list(DEFENSIVE),))
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


def _on_before(series, d):
    cand = [dd for dd in series if dd <= d]
    if not cand:
        return None
    k = max(cand)
    return k, series[k]


# --- simulation -------------------------------------------------------------


def _resolve_sell(key, buy_date, bioguide, ticker, ser, sales, cap=TODAY):
    """Return (sell_date, sell_close) if sold by `cap`, else None (open position)."""
    if key == "hold":
        return None
    if key == "trader":
        sd = next((s for s in sales.get((bioguide, ticker), []) if s > buy_date), None)
        if not sd:
            return None
        hit = _on_after(ser, sd)
        return hit if (hit and hit[0] <= cap) else None
    target = buy_date + timedelta(days=key)  # fixed horizon in days
    hit = _on_after(ser, target)
    return hit if (hit and hit[0] <= cap) else None


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


def _sims(bets, prices, strats=None):
    return {name: _agg(_simulate(key, bets, prices)) for name, key in (strats or STRATS)}


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


def run_nosize(out_path=None) -> dict:
    """Drop the small-position cap (growth + proven trader), cross with run-up action filters,
    scored on the 3 headline sell strategies. Also reports the with-size baseline for contrast."""
    out_path = out_path or _NOSIZE_OUT
    conn = store.connect()
    sigs_ns, sales = _load(conn, "growth-proven")
    sigs_hp, _ = _load(conn, "high-precision")
    conn.close()
    print(
        f"[no-size] {len(sigs_ns)} growth-proven signals (vs {len(sigs_hp)} with size cap); "
        "fetching prices...",
        flush=True,
    )
    prices = _prices([s["ticker"] for s in sigs_ns])
    strats = [(n, k) for n, k in STRATS if n in NOSIZE_SELL]

    bets = _build_bets(sigs_ns, sales, prices)
    hp_bets = _build_bets(sigs_hp, sales, prices)
    hp_base = _sims(hp_bets, prices, strats)

    rows = []  # (label, n, results-or-None)
    for label, pred in NOSIZE_ACTIONS:
        sub = [b for b in bets if pred(b["gap"])]
        rows.append((label, len(sub), _sims(sub, prices, strats) if sub else None))

    md = _render_nosize(len(bets), len(hp_bets), hp_base, rows, strats)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nwrote {out_path}\n")
    print(md)
    return {"n": len(bets), "hp_base": hp_base, "rows": rows}


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


def _render_nosize(n_bets, n_hp, hp_base, rows, strats) -> str:
    names = [n for n, _ in strats]
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 1: No Size Cap x Run-up Filters",
        "",
        "> **Gate:** growth sector + PROVEN trader (prior 90-day big-win), **no position-size "
        "cap** (drops the $1k-$15k bracket that high-precision used). Crossed with a baseline + "
        "run-up action filters, scored on the 3 headline sell rules. $100/bet, vs SPY on the "
        f"same dates. Look-ahead-safe; marked-to-market as of {TODAY}.",
        "",
        f"- **No-size gate keeps {n_bets} bets** vs **{n_hp}** with the size cap "
        f"(+{n_bets - n_hp}, {(n_bets / n_hp - 1) * 100:+.0f}%).",
        "- For contrast, the WITH-size high-precision baseline (no run-up filter): "
        + ", ".join(f"{n} {_pct(_edge(hp_base[n]))} edge" for n in names)
        + ".",
        "",
        "## Market-adjusted EDGE vs SPY",
        "_Cell = strategy ROI - SPY-alt ROI. **N** = bets past the filter._",
        "",
        "| action filter | N | " + " | ".join(names) + " |",
        "|---|--:|" + "--:|" * len(names),
    ]
    for label, n, res in rows:
        if not res:
            L.append(f"| {label} | {n} | " + " | ".join("-" for _ in names) + " |")
            continue
        cells = " | ".join(_pct(_edge(res[nm])) for nm in names)
        bold = "**" if label.startswith("baseline") else ""
        L.append(f"| {bold}{label}{bold} | {n} | {cells} |")

    L += [
        "",
        "## Total ROI",
        "| action filter | N | " + " | ".join(names) + " |",
        "|---|--:|" + "--:|" * len(names),
    ]
    for label, n, res in rows:
        if not res:
            L.append(f"| {label} | {n} | " + " | ".join("-" for _ in names) + " |")
            continue
        cells = " | ".join(_pct(res[nm]["roi"]) for nm in names)
        bold = "**" if label.startswith("baseline") else ""
        L.append(f"| {bold}{label}{bold} | {n} | {cells} |")

    L += [
        "",
        "## Read",
        "- Compare the no-size **baseline** row's edge to the with-size high-precision numbers "
        "above: that's the pure cost/benefit of dropping the position-size cap.",
        "- Then read down the run-up rows: does momentum still lift the edge once the size cap "
        "is gone, and how many more bets do we keep at each threshold?",
        "- Same caveats as the other Phase-1 backtests. To be re-validated with Senate data.",
    ]
    return "\n".join(L)


# --- longitudinal (per-period) analysis -------------------------------------

_QEND = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _period_key(d, period):
    return (d.year,) if period == "year" else (d.year, (d.month - 1) // 3 + 1)


def _period_start(key, period):
    return date(key[0], 1, 1) if period == "year" else date(key[0], (key[1] - 1) * 3 + 1, 1)


def _period_end(key, period):
    if period == "year":
        return date(key[0], 12, 31)
    m, dd = _QEND[key[1]]
    return date(key[0], m, dd)


def _period_label(key, period):
    return str(key[0]) if period == "year" else f"{key[0]} Q{key[1]}"


def _window_row(strat_key, b, prices, pe):
    """One bet, held within its period and closed/marked at the period end `pe`."""
    ser = prices[b["ticker"]]
    sell = _resolve_sell(
        strat_key, b["buy_date"], b["bioguide"], b["ticker"], ser, b["sales"], cap=pe
    )
    if sell:
        exit_date, exit_close, sold = sell[0], sell[1], True
    else:
        mark = _on_before(ser, pe) or _on_after(ser, pe)
        exit_date, exit_close, sold = mark[0], mark[1], False
    proceeds = b["shares"] * exit_close
    spy = prices["SPY"]
    spy_exit = _on_before(spy, exit_date) or _on_after(spy, exit_date)
    spy_end = BET * (spy_exit[1] / b["spy_buy"])
    return sold, proceeds, spy_end


def _longitudinal(strat_key, bets, prices, period, first_buy):
    groups: dict[tuple, list] = {}
    for b in bets:
        groups.setdefault(_period_key(b["buy_date"], period), []).append(b)
    out = []
    for key in sorted(groups):
        pe = _period_end(key, period)
        rows = [_window_row(strat_key, b, prices, pe) for b in groups[key]]
        n = len(rows)
        cap_in = BET * n
        cap_out = sum(p for sold, p, _ in rows if sold)
        remaining = sum(p for sold, p, _ in rows if not sold)
        end = cap_out + remaining
        spy_end = sum(s for _, _, s in rows)
        complete = pe <= TODAY and _period_start(key, period) >= _period_start(
            _period_key(first_buy, period), period
        )
        out.append(
            {
                "label": _period_label(key, period),
                "n": n,
                "cap_in": cap_in,
                "cap_out": cap_out,
                "remaining": remaining,
                "roi": end / cap_in - 1,
                "spy_roi": spy_end / cap_in - 1,
                "complete": complete,
            }
        )
    return out


def _maxdd(returns):
    """Max peak-to-trough drawdown of an equity curve that compounds each period's ROI."""
    equity, peak, dd = 1.0, 1.0, 0.0
    for r in returns:
        equity *= 1 + r
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak)
    return dd


def _downside(strat_key, bets, prices, quarters):
    rows = _simulate(strat_key, bets, prices)
    rets = sorted(r["ret"] for r in rows)
    n = len(rets)
    qroi = [q["roi"] for q in quarters if q["complete"]]
    return {
        "worst_bet": rets[0],
        "best_bet": rets[-1],
        "pct_neg": sum(1 for r in rets if r < 0) / n,
        "pct_halved": sum(1 for r in rets if r <= -0.5) / n,
        "q_maxdd": _maxdd(qroi),
        "worst_q": min(qroi) if qroi else None,
    }


# --- LOCKED full report -----------------------------------------------------


_SCOPES = {None: "House + Senate", "house": "House only", "senate": "Senate only"}
_SCOPE_OUT = {
    None: _BTDIR + "2026-06-25 Phase 1 LOCKED — House+Senate.md",
    "house": _LOCKED_OUT,
    "senate": _BTDIR + "2026-06-25 Phase 1 LOCKED — Senate Only.md",
}


def run_locked(chamber=None, out_path=None) -> dict:
    """Canonical, full backtest of the LOCKED Phase-1 filter: summary + topline + detail +
    per-year edge + downside/drawdown + longitudinal (annual & quarterly). `chamber` scopes
    the universe: None = House+Senate, 'house' = House only, 'senate' = Senate only."""
    scope = _SCOPES[chamber]
    out_path = out_path or _SCOPE_OUT[chamber]
    conn = store.connect()
    sigs, sales = _load(conn, "growth-proven", chamber)
    conn.close()
    print(f"[locked:{scope}] {len(sigs)} growth-proven signals; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])

    allbets = _build_bets(sigs, sales, prices)
    bets = [b for b in allbets if b["gap"] is not None and b["gap"] >= LOCKED_RUNUP]
    first_buy = min(b["buy_date"] for b in bets)
    strats = [(n, k) for n, k in STRATS if n in LOCKED_SELL]

    activity = _activity(bets)
    results = _sims(bets, prices, strats)
    annual = {n: _longitudinal(k, bets, prices, "year", first_buy) for n, k in strats}
    quarterly = {n: _longitudinal(k, bets, prices, "quarter", first_buy) for n, k in strats}
    downside = {n: _downside(k, bets, prices, quarterly[n]) for n, k in strats}

    md = _render_locked(activity, results, annual, quarterly, downside, strats, len(allbets), scope)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nwrote {out_path}\n")
    print(md)
    return {"scope": scope, "activity": activity, "results": results}


def _long_table(rows):
    L = [
        "| period | trades | capital in | capital out | portfolio left | ROI | mkt-adj ROI |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for r in rows:
        star = "" if r["complete"] else "\\*"
        L.append(
            f"| {r['label']}{star} | {r['n']} | ${r['cap_in']:,.0f} | ${r['cap_out']:,.0f} | "
            f"${r['remaining']:,.0f} | {_pct(r['roi'])} | {_pct(r['roi'] - r['spy_roi'])} |"
        )
    return L


def _render_locked(
    act, results, annual, quarterly, downside, strats, n_core, scope="House + Senate"
) -> str:
    names = [n for n, _ in strats]
    hp = results["1-year"]
    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        "status: canonical",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        f"scope: {scope}",
        "---",
        f"# Insider Trader — Phase 1 LOCKED: Full Backtest ({scope})",
        "",
        f"> **Universe: {scope}.** {_LOCKED_DESC} $100 bought at the close on/after the "
        "**disclosure date**; compared "
        "to the same $100 in **SPY** on the same dates. Look-ahead-safe; open positions marked "
        f"to market as of {TODAY}. The 3 sell rules below are the **locked candidates** (we pick "
        "one before paper trading). See [[../Phase 1 Strategy (Locked)]] and [[../Backtesting "
        "Playbook]].",
        "",
        "## Summary",
        f"- **{act['n']} trades** pass the locked filter ({n_core} clear the growth+proven core; "
        f"the run-up >= +5% leg keeps {act['n']}). Signal months {act['span']}; "
        f"avg {act['avg_per_mo']:.1f}/mo, longest no-signal gap {act['max_gap']} months.",
        f"- **Best risk-adjusted candidate (1-year hold): +{hp['roi'] * 100:.1f}% total ROI vs "
        f"SPY's {hp['spy_roi'] * 100:+.1f}% = a +{(hp['roi'] - hp['spy_roi']) * 100:.1f}% edge** "
        f"over the same windows; {hp['win_rate'] * 100:.0f}% of bets finished green.",
        "- Longitudinal read on consistency is in the per-year / per-quarter tables at the end.",
        "",
        "## Topline — cumulative results by sell strategy",
        "| sell strategy | trades | total ROI | SPY-alt ROI | **edge** | mean ret | "
        "mean mkt-adj | median | win% | beat-mkt% | % still open |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name in names:
        r = results[name]
        L.append(
            f"| {name} | {r['n']} | {_pct(r['roi'])} | {_pct(r['spy_roi'])} | "
            f"**{_pct(r['roi'] - r['spy_roi'])}** | {_pct(r['ret_avg'])} | {_pct(r['exc_avg'])} | "
            f"{_pct(r['ret_med'])} | {r['win_rate'] * 100:.0f}% | {r['beat_rate'] * 100:.0f}% | "
            f"{r['pct_open'] * 100:.0f}% |"
        )

    L += [
        "",
        "## Detailed breakdown — dispersion & downside",
        "| sell strategy | end value | raw ret var | mkt-adj var | worst bet | best bet | "
        "% bets red | % bets halved | quarterly max drawdown | worst quarter |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name in names:
        r = results[name]
        d = downside[name]
        wq = _pct(d["worst_q"]) if d["worst_q"] is not None else "n/a"
        L.append(
            f"| {name} | ${r['end_value']:,.0f} | {r['ret_var']:.3f} | {r['exc_var']:.3f} | "
            f"{_pct(d['worst_bet'])} | {_pct(d['best_bet'])} | {d['pct_neg'] * 100:.0f}% | "
            f"{d['pct_halved'] * 100:.0f}% | {d['q_maxdd'] * 100:.0f}% | {wq} |"
        )
    L += [
        "",
        "_Worst/best bet = single-position return. % halved = bets that lost >=50%. Quarterly "
        "max drawdown = deepest peak-to-trough of an equity curve that reinvests fresh into each "
        "quarter's cohort (see longitudinal). These quantify the bumpiness behind the averages.",
        "",
        "## Longitudinal analysis (start fresh each period, hold for just that period)",
        "_Each row: start from $0 at the period's open, place $100 on every signal that fires "
        "in the period, and close/mark the book at the period end. **capital out** = proceeds "
        "from positions the sell rule closed within the period; **portfolio left** = "
        "mark-to-market of positions still open at period end. \\* = partial/incomplete period "
        "(first cohort starts mid-2014; the latest is still in progress).",
    ]
    for name in names:
        L += ["", f"### {name} — by year"]
        L += _long_table(annual[name])
    for name in names:
        L += ["", f"### {name} — by quarter"]
        L += _long_table(quarterly[name])

    # consistency read on the 1-year candidate
    ya = annual["1-year"]
    done_yrs = [r for r in ya if r["complete"]]
    neg_yrs = [r["label"] for r in done_yrs if r["roi"] - r["spy_roi"] < 0]
    qq = quarterly["1-year"]
    done_q = [r for r in qq if r["complete"]]
    neg_q = sum(1 for r in done_q if r["roi"] - r["spy_roi"] < 0)
    L += [
        "",
        "## Consistency & variance (read)",
        f"- **Annual (1-year hold):** of {len(done_yrs)} complete years, "
        f"{len(done_yrs) - len(neg_yrs)} beat SPY and {len(neg_yrs)} trailed it"
        + (f" ({', '.join(neg_yrs)})" if neg_yrs else "")
        + ". Watch whether the down years cluster recently (edge decay) or are scattered.",
        f"- **Quarterly (1-year hold):** {len(done_q) - neg_q} of {len(done_q)} complete "
        f"quarters beat SPY ({neg_q} trailed). Quarterly is noisier — a run of consecutive "
        "negative quarters is the real risk signal, more than any single down quarter.",
        "- Compare the early years to the most recent complete ones: if the edge is shrinking "
        "as more people copy congressional trades, the recent cohorts will show it first.",
        "- Caveats: House-only; survivorship-pruned; OTC/split-artifact excluded; late-period "
        "cohorts hold for less than a full window. Re-validate with Senate data.",
    ]
    return "\n".join(L)


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "high-precision"
    if arg == "missed-action":
        run_missed_action()
    elif arg == "directional":
        run_directional()
    elif arg == "nosize":
        run_nosize()
    elif arg == "locked":
        run_locked()  # House + Senate combined (DB now holds both)
    elif arg == "locked-house":
        run_locked("house")
    elif arg == "locked-senate":
        run_locked("senate")
    else:
        run(arg)
