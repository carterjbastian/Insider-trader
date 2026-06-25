"""FINAL strategy backtest v2 — finalized sell rule + quarterly compounding (2022→present).

SELL RULE: exit equity at the SOONER of 18 months post-buy OR when the trader discloses a
sale. Options run ~1 year to expiry.

COMPOUNDING (quarterly matching): at each quarter start, set the largest bet multiplier M such
that the cash pool covers `factor` × the projected matching need, i.e. pool >= factor*(M-1)*P
where P = projected quarterly base spend (trailing-4-qtr average) and factor=1.5 (re-run at 2.0
if 1.5 still dilutes). Matching capital is drawn from the pool (recouped sells + option payoffs);
you always contribute 1x base personally.

Report: strategy details · topline · quarterly longitudinal · start-date dependence · analysis ·
all trade details · 15-year projection (extend the book, and a fresh $0 start in 2026-Q3).

  POLYGON_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.final_backtest_v2
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from . import store
from .final_backtest import _build, _fetch_options, _load
from .phase1_backtest import _last, _on_after, _prices

_OUT = (
    "/home/carter/vault/Projects/Black Box/Insider Trader/Backtests/"
    "2026-06-25 FINAL Strategy Backtest v2 (sell + compounding).md"
)
TODAY = date(2026, 6, 24)
HOLD_CAP = timedelta(days=548)  # ~18 months


def _qidx(d):
    return (d.year - 2022) * 4 + (d.month - 1) // 3


def _qlabel(i):
    return f"{2022 + i // 4}-Q{i % 4 + 1}"


def _price_on(ser, d):
    h = _on_after(ser, d)
    return h[1] if h else _last(ser)[1]


def _prep(bets, prices):
    """Per-bet base (1x) cash flows under the 18mo-or-trader sell rule + option payoff."""
    for b in bets:
        ser = prices[b["ticker"]]
        cap = b["buy_date"] + HOLD_CAP
        tsale = next(
            (s for s in b["sales"].get((b["bioguide"], b["ticker"]), []) if s > b["buy_date"]), None
        )
        target = min(cap, tsale) if tsale else cap
        hit = _on_after(ser, target)
        if hit and hit[0] <= TODAY:
            b["eq_recoup"] = (hit[0], b["equity_bet"] * (hit[1] / b["buy_close"]))
            b["eq_open"] = 0.0
        else:
            b["eq_recoup"] = None
            b["eq_open"] = b["equity_bet"] * (_price_on(ser, TODAY) / b["buy_close"])
        o = b.get("opt")
        if o and o["expired"]:
            b["opt_recoup"] = (o["exit_date"], o["end"])
            b["opt_open"] = 0.0
        elif o:
            b["opt_recoup"] = None
            b["opt_open"] = o["end"]
        else:
            b["opt_recoup"] = None
            b["opt_open"] = 0.0
        b["base_spend"] = b["equity_bet"] + (o["spend"] if o else 0.0)


def _held_at(bets, prices, d):
    """Marked value of positions still open as of date d (×each bet's mult)."""
    tot = 0.0
    for b in bets:
        if "mult" not in b or b["buy_date"] > d:
            continue
        er = b["eq_recoup"]
        if not er or er[0] > d:  # equity still held at d
            tot += (
                b["mult"]
                * b["equity_bet"]
                * (_price_on(prices[b["ticker"]], min(d, TODAY)) / b["buy_close"])
            )
        o = b.get("opt")
        if o:
            orc = b["opt_recoup"]
            if not orc or orc[0] > d:  # option still open at d
                tot += b["mult"] * o["end"]
    return tot


def _spy_at(bets, prices, d):
    """What the same dollars would be worth in SPY, marked at d (open) or at exit (realized)."""
    spy = prices["SPY"]
    tot = 0.0
    for b in bets:
        if "mult" not in b or b["buy_date"] > d:
            continue
        # equity sleeve
        er = b["eq_recoup"]
        ex = er[0] if (er and er[0] <= d) else min(d, TODAY)
        sx = _on_after(spy, ex) or _last(spy)
        tot += b["mult"] * b["equity_bet"] * (sx[1] / b["spy_buy"])
        o = b.get("opt")
        if o:
            orc = b["opt_recoup"]
            ox = orc[0] if (orc and orc[0] <= d) else min(d, TODAY)
            sox = _on_after(spy, ox) or _last(spy)
            tot += b["mult"] * o["spend"] * (sox[1] / b["spy_buy"])
    return tot


def run_engine(bets, prices, factor):
    """Quarterly matching-compounding engine. Returns (records, summary)."""
    for b in bets:
        b.pop("mult", None)
        b.pop("_eqd", None)
        b.pop("_optd", None)
    by_q = defaultdict(list)
    for b in bets:
        by_q[_qidx(b["buy_date"])].append(b)
    last_q = _qidx(TODAY)
    base_by_q = {q: sum(b["base_spend"] for b in by_q[q]) for q in range(last_q + 1)}

    pool = personal = 0.0
    dilution = False
    records = []
    for q in range(last_q + 1):
        prior = [base_by_q[p] for p in range(max(0, q - 4), q)]
        proj = (sum(prior) / len(prior)) if prior else (base_by_q[q] or 1.0)
        m_next = 1 + int(pool // (factor * proj)) if proj > 0 else 1
        m_next = min(m_next, 6)
        qs = date(2022 + q // 4, (q % 4) * 3 + 1, 1)
        port_start = _held_at(bets, prices, qs) + pool

        base = base_by_q[q]
        need = (m_next - 1) * base
        matching = min(pool, need)
        if matching < need - 1e-6:
            dilution = True
        pool -= matching
        am = 1 + (matching / base if base else 0.0)
        for b in by_q[q]:
            b["mult"] = am
        personal += base

        recoup = 0.0
        for b in bets:
            if "mult" not in b:
                continue
            er = b["eq_recoup"]
            if er and _qidx(er[0]) == q and not b.get("_eqd"):
                pool += b["mult"] * er[1]
                recoup += b["mult"] * er[1]
                b["_eqd"] = True
            orc = b["opt_recoup"]
            if orc and _qidx(orc[0]) == q and not b.get("_optd"):
                pool += b["mult"] * orc[1]
                recoup += b["mult"] * orc[1]
                b["_optd"] = True
        records.append(
            {
                "q": _qlabel(q),
                "cap_in": personal,
                "port_start": port_start,
                "realized_pool": pool,
                "next_mult": 1 + int(pool // (factor * proj)) if proj > 0 else 1,
                "recoup": recoup,
            }
        )
    held = _held_at(bets, prices, TODAY)
    total = held + pool
    spy_val = _spy_at(bets, prices, TODAY)
    summary = {
        "personal": personal,
        "pool": pool,
        "held": held,
        "total": total,
        "roi": total / personal - 1 if personal else 0,
        "spy_val": spy_val,
        "spy_roi": spy_val / personal - 1 if personal else 0,
        "dilution": dilution,
        "n": len(bets),
        "last_q": last_q,
        "base_by_q": base_by_q,
    }
    # fill cumulative ROI per record
    for r in records:
        r["roi"] = None  # set in render against running total (approx: port/ cap_in)
    return records, summary


def _irr(outflows, terminal, last_q):
    """Quarterly money-weighted IRR -> annualized. outflows: list per quarter (>=0)."""
    cfs = [-outflows.get(q, 0.0) for q in range(last_q + 1)]
    cfs[-1] += terminal

    def npv(r):
        return sum(c / (1 + r) ** i for i, c in enumerate(cfs))

    lo, hi = -0.9, 2.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (1 + (lo + hi) / 2) ** 4 - 1  # annualize quarterly rate


def _pct(x):
    return f"{x * 100:+.1f}%"


def _project(start_value, annual_contrib, r_strat, r_spy, years, start_year, fresh):
    """Year-by-year topline projection. Returns list of dict rows."""
    rows = []
    v = start_value
    sv = start_value  # spy-equivalent baseline
    cap_in_extra = 0.0  # personal added in the projection window
    for k in range(1, years + 1):
        v = v * (1 + r_strat) + annual_contrib
        sv = sv * (1 + r_spy) + annual_contrib
        cap_in_extra += annual_contrib
        rows.append(
            {
                "year": start_year + k,
                "cap_in_extra": cap_in_extra,
                "value": v,
                "spy_value": sv,
            }
        )
    return rows


def run(factor=1.5) -> None:
    conn = store.connect()
    sigs, sales = _load(conn)
    conn.close()
    print(f"[v2] {len(sigs)} candidates; fetching prices...", flush=True)
    prices = _prices([s["ticker"] for s in sigs])
    bets = _build(sigs, sales, prices)
    print(f"[v2] {len(bets)} bets; fetching tier-3 options...", flush=True)
    _fetch_options(bets)
    _prep(bets, prices)

    rec15, sum15 = run_engine(bets, prices, 1.5)
    sum20 = None
    if sum15["dilution"]:
        _, sum20 = run_engine(bets, prices, 2.0)
    records, summ = (rec15, sum15)  # report uses 1.5; 2.0 shown alongside if it diluted

    md = _render(bets, prices, records, summ, sum20)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"\nwrote {_OUT}\n")
    print(md[:1800])


def _render(bets, prices, records, summ, sum20):
    by_tier = {t: sum(1 for b in bets if b["tier"] == t) for t in (1, 2, 3)}
    n_opt = sum(1 for b in bets if b.get("opt"))
    # IRRs for projection
    outflows = defaultdict(float)
    for b in bets:
        outflows[_qidx(b["buy_date"])] += b["base_spend"]  # personal base
    r_strat = _irr(outflows, summ["total"], summ["last_q"])
    r_spy = _irr(outflows, summ["spy_val"], summ["last_q"])
    annual_contrib = summ["personal"] / ((summ["last_q"] + 1) / 4)

    L = [
        "---",
        "type: backtest",
        "epic: Black Box",
        "track: Insider Trader",
        "status: canonical",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — FINAL Strategy Backtest v2 (sell rule + quarterly compounding)",
        "",
        "## Strategy details",
        "**Phase 1 (locked):** growth-sector PURCHASE by a proven trader (prior 90-day big win) "
        "with a ≥+5% run-up from the member's buy to the public disclosure. Act on disclosure.",
        "**Phase 2 (LLM):** Opus 4.8 `signal_strength` 0–100 from a dossier (trader record, the "
        "trade, the company, our R&D heuristics, a member bio).",
        "**Buy tiers:** Tier 1 (all locked) → $50; Tier 2 (signal ≥ 50) → $100; Tier 3 "
        f"(signal ≥ 60) → $100 + 1-yr ATM calls (contracts = max(1, ⌈$100/entry⌉)). "
        f"Counts: {by_tier[1]} / {by_tier[2]} / {by_tier[3]} ({n_opt} with real contracts).",
        "**SELL RULE (finalized):** equity exits at the SOONER of **18 months** after buy OR "
        "when the trader discloses a sale; options run ~1 year to expiry.",
        "**COMPOUNDING (quarterly matching):** each quarter, set the largest bet multiplier M "
        "such that pool ≥ factor×(M−1)×(projected quarterly base spend, trailing-4-qtr avg). "
        "Matching is drawn from the recouped-cash pool; you always contribute 1× base. "
        "Primary run uses **factor 1.5**; "
        + ("a 2.0 run is shown too (1.5 diluted)." if sum20 else "(no dilution at 1.5)."),
        "",
        "## Topline (factor 1.5, leveraged)",
        f"- **Personal capital contributed:** ${summ['personal']:,.0f} over "
        f"{(summ['last_q'] + 1) / 4:.2f} years.",
        f"- **Total portfolio value now:** ${summ['total']:,.0f} "
        f"(holdings ${summ['held']:,.0f} + cash pool ${summ['pool']:,.0f}).",
        f"- **ROI {_pct(summ['roi'])}** vs same dollars in SPY {_pct(summ['spy_roi'])} "
        f"= **{_pct(summ['roi'] - summ['spy_roi'])} edge**. Money-weighted IRR "
        f"≈ {_pct(r_strat)}/yr (SPY ≈ {_pct(r_spy)}/yr).",
        "- **Dilution at factor 1.5:** "
        + ("YES — see the 2.0 run below." if summ["dilution"] else "no."),
    ]
    if sum20:
        L.append(
            f"- **Factor 2.0 run:** personal ${sum20['personal']:,.0f} → total "
            f"${sum20['total']:,.0f}, ROI {_pct(sum20['roi'])} (edge "
            f"{_pct(sum20['roi'] - sum20['spy_roi'])}); dilution {sum20['dilution']}."
        )

    # quarterly longitudinal
    L += [
        "",
        "## Longitudinal (quarterly)",
        "| quarter | capital in (cum) | portfolio @ start | realized pool | next mult | "
        "ROI (cum) | SPY ROI | edge |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for r in records:
        qs = date(2022 + int(r["q"][:4]) - 2022, (int(r["q"][-1]) - 1) * 3 + 1, 1)
        val = _held_at(bets, prices, min(qs + timedelta(days=92), TODAY)) + r["realized_pool"]
        roi = val / r["cap_in"] - 1 if r["cap_in"] else 0
        spy = _spy_at(bets, prices, min(qs + timedelta(days=92), TODAY))
        sroi = spy / r["cap_in"] - 1 if r["cap_in"] else 0
        L.append(
            f"| {r['q']} | ${r['cap_in']:,.0f} | ${r['port_start']:,.0f} | "
            f"${r['realized_pool']:,.0f} | {r['next_mult']}x | {_pct(roi)} | {_pct(sroi)} | "
            f"{_pct(roi - sroi)} |"
        )

    # start-date dependence
    L += [
        "",
        "## Start-date dependence (fresh compounding from each start)",
        "| started | bets | personal in | total value | ROI | SPY ROI | edge |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    s = date(2022, 1, 1)
    while s <= TODAY - timedelta(days=180):
        sub = [b for b in bets if b["buy_date"] >= s]
        if sub:
            _, su = run_engine(sub, prices, 1.5)
            L.append(
                f"| {s.isoformat()} | {su['n']} | ${su['personal']:,.0f} | ${su['total']:,.0f} | "
                f"{_pct(su['roi'])} | {_pct(su['spy_roi'])} | {_pct(su['roi'] - su['spy_roi'])} |"
            )
        s = date(s.year + (s.month + 5) // 12, (s.month + 5) % 12 + 1, 1)
    run_engine(bets, prices, 1.5)  # restore mults for trade details

    # projection
    L += [
        "",
        "## Projection (15 years, topline year-by-year)",
        f"_Strategy money-weighted IRR ≈ **{_pct(r_strat)}/yr**, SPY ≈ {_pct(r_spy)}/yr, "
        f"annual personal contribution ≈ ${annual_contrib:,.0f}. **Heavy caveat:** the IRR is the "
        "realized rate over a 2022–26 growth-stock window and is almost certainly optimistic "
        "forward; read these as upper-ceiling trajectories, not forecasts._",
        "",
        "### A) Extending the current book (start from today's value)",
        "| year | personal added (cum) | portfolio value | SPY-equiv | edge |",
        "|---|--:|--:|--:|--:|",
    ]
    for row in _project(summ["total"], annual_contrib, r_strat, r_spy, 15, 2026, False):
        edge = row["value"] - row["spy_value"]
        L.append(
            f"| {row['year']} | ${row['cap_in_extra']:,.0f} | ${row['value']:,.0f} | "
            f"${row['spy_value']:,.0f} | ${edge:,.0f} |"
        )
    L += [
        "",
        "### B) Fresh $0 start in 2026-Q3",
        "| year | personal in (cum) | portfolio value | SPY-equiv | edge |",
        "|---|--:|--:|--:|--:|",
    ]
    for row in _project(0.0, annual_contrib, r_strat, r_spy, 15, 2026, True):
        edge = row["value"] - row["spy_value"]
        L.append(
            f"| {row['year']} | ${row['cap_in_extra']:,.0f} | ${row['value']:,.0f} | "
            f"${row['spy_value']:,.0f} | ${edge:,.0f} |"
        )

    # analysis
    L += [
        "",
        "## Analysis & red flags",
        f"- The leveraged strategy turned ${summ['personal']:,.0f} of personal capital into "
        f"${summ['total']:,.0f} ({_pct(summ['roi'])}), a {_pct(summ['roi'] - summ['spy_roi'])} "
        "edge over SPY, with the quarterly matching adding leverage as the pool grew.",
        "- **Matching dilution:** when the signal volume spiked (2025), base spend outran the "
        "pool, so the realized multiplier fell short of intended "
        + ("(happened at 1.5x). " if summ["dilution"] else "(avoided at 1.5x). ")
        + "The 1.5×/2.0× buffers trade growth for safety here.",
        "- **Option tail risk** (Tier 3): a few huge winners carry the option sleeve; most expire "
        "down. Capped downside (premium), convex upside, lumpy.",
        "- **Projection caveat:** the IRR is regime-flattered; sustained 15-yr compounding at "
        "that rate is unrealistic — treat as a ceiling. SPY-equiv uses the realized SPY IRR.",
        "- **Other:** 2022→present only (~4.5y); option data starts 2022; survivorship-pruned; "
        "look-ahead is best-effort on the Phase-2 signal.",
        "",
        "## Trade details (all buys, sales, options)",
        "| buy date | member | ticker | sig | tier | $base eq | sell date (18mo/trader) | "
        "eq ret | option (contracts@entry→exit = ret) |",
        "|---|---|---|--:|--:|--:|---|--:|---|",
    ]
    for b in sorted(bets, key=lambda x: x["buy_date"]):
        er = b["eq_recoup"]
        sell_d = er[0].isoformat() if er else "open"
        eq_ret = (er[1] / b["equity_bet"] - 1) if er else (b["eq_open"] / b["equity_bet"] - 1)
        o = b.get("opt")
        ostr = (
            f"{o['contracts']}@${o['entry']:.2f}→${o['exit']:.2f} = {_pct(o['ret'])}"
            if o
            else ("—" if b["tier"] < 3 else "(no contract)")
        )
        L.append(
            f"| {b['buy_date']} | {b['member']} | {b['ticker']} | {b['signal']} | {b['tier']} | "
            f"${b['equity_bet']:.0f} | {sell_d} | {_pct(eq_ret)} | {ostr} |"
        )
    return "\n".join(L)


if __name__ == "__main__":
    run()
