"""Do the Phase-2 LLM scores separate realized winners? Joins `phase2_analyses` to the
realized `trade_returns` and measures how buy / confidence / signal_strength / trader_alpha
relate to 1-year market-EXCESS outcomes. Output informs two ways to fold Phase 2 into the buy
strategy: (a) FILTER on the scores, (b) SIZE the bet by the scores. Writes a vault report.

  DATABASE_URL=... uv run python -m insider_trader.phase2_eval
"""

from __future__ import annotations

from datetime import datetime
from statistics import fmean, median

from . import store
from .signals import _auc

WIN = 0.5  # "winner" = beat SPY by >= +50% over 360d (exc_360)
_OUT = "/home/carter/vault/Projects/Black Box/Insider Trader/Backtests/2026-06-25 Phase 2 Signal Evaluation.md"


def _load(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.buy, a.confidence, a.signal_strength, a.trader_alpha, a.runup, "
            "tr.exc_360, tr.ret_360, tr.exc_90 "
            "FROM phase2_analyses a JOIN trade_returns tr USING(transaction_id) "
            "WHERE a.prompt_version='p2-v1'"
        )
        cols = ["buy", "confidence", "signal", "alpha", "runup", "exc_360", "ret_360", "exc_90"]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    for r in rows:
        r["win"] = (r["exc_360"] or 0) >= WIN if r["exc_360"] is not None else None
    return rows


def _stat(rows: list[dict]) -> dict:
    real = [r for r in rows if r["exc_360"] is not None]
    n = len(real)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "win_rate": sum(r["win"] for r in real) / n,
        "mean_exc": fmean(r["exc_360"] for r in real),
        "med_exc": median(r["exc_360"] for r in real),
        "mean_ret": fmean(r["ret_360"] for r in real),
    }


def _line(label, s, base=None):
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — | — |"
    lift = f"{s['mean_exc'] - base['mean_exc']:+.1%}" if base else ""
    return (
        f"| {label} | {s['n']} | {s['win_rate'] * 100:.0f}% | {s['mean_exc']:+.1%} | "
        f"{s['med_exc']:+.1%} | {s['mean_ret']:+.1%} |"
    )


def run() -> None:
    conn = store.connect()
    rows = _load(conn)
    conn.close()
    real = [r for r in rows if r["exc_360"] is not None]
    base = _stat(rows)
    L = [
        "---",
        "type: analysis",
        "epic: Black Box",
        "track: Insider Trader",
        f"created: {datetime.now().strftime('%m-%d-%Y')}",
        "---",
        "# Insider Trader — Phase 2 Signal Evaluation",
        "",
        f"> Do the Phase-2 Opus scores separate realized winners? {len(rows)} locked candidates "
        f"scored; **{len(real)} have a realized 360-day outcome** (the rest are too recent). "
        f"Winner = `exc_360 ≥ +{WIN * 100:.0f}%` (beat SPY by 50%+ over a year). Outcome columns: "
        "**win%**, **mean excess** (per-trade 1-yr market-adjusted return = the edge), median "
        "excess, **mean raw** return. All on the realized subset.",
        "",
        f"**Baseline (all scored, realized):** n={base['n']}, win-rate {base['win_rate'] * 100:.0f}%, "
        f"mean excess **{base['mean_exc']:+.1%}**, median {base['med_exc']:+.1%}, "
        f"mean raw {base['mean_ret']:+.1%}.",
        "",
        "## Discrimination (AUC vs winner; >0.6 = useful, 0.5 = none)",
        f"- signal_strength: **AUC {_auc([(r['signal'], r['win']) for r in real]):.3f}**",
        f"- trader_alpha: **AUC {_auc([(r['alpha'], r['win']) for r in real]):.3f}**",
        f"- runup (Phase-1 momentum, for reference): "
        f"AUC {_auc([(r['runup'], r['win']) for r in real if r['runup'] is not None]):.3f}",
        "",
        "## By BUY recommendation",
        "| group | n | win% | mean excess | median | mean raw |",
        "|---|--:|--:|--:|--:|--:|",
        _line("buy = YES", _stat([r for r in rows if r["buy"]]), base),
        _line("buy = no", _stat([r for r in rows if not r["buy"]]), base),
        "",
        "## By CONFIDENCE",
        "| group | n | win% | mean excess | median | mean raw |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for c in ("High", "Medium", "Low"):
        L.append(_line(f"confidence = {c}", _stat([r for r in rows if r["confidence"] == c]), base))

    L += [
        "",
        "## By SIGNAL_STRENGTH bucket",
        "| bucket | n | win% | mean excess | median | mean raw |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for lo, hi in [(0, 40), (40, 50), (50, 60), (60, 101)]:
        L.append(
            _line(f"signal {lo}–{hi - 1}", _stat([r for r in rows if lo <= r["signal"] < hi]), base)
        )
    L += [
        "",
        "## By TRADER_ALPHA bucket",
        "| bucket | n | win% | mean excess | median | mean raw |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for lo, hi in [(0, 30), (30, 50), (50, 101)]:
        L.append(
            _line(f"alpha {lo}–{hi - 1}", _stat([r for r in rows if lo <= r["alpha"] < hi]), base)
        )

    # candidate filter strategies
    strategies = [
        ("buy = YES", lambda r: r["buy"]),
        ("confidence High or Medium", lambda r: r["confidence"] in ("High", "Medium")),
        ("buy = YES & conf High/Med", lambda r: r["buy"] and r["confidence"] in ("High", "Medium")),
        ("signal_strength >= 50", lambda r: r["signal"] >= 50),
        ("signal_strength >= 55", lambda r: r["signal"] >= 55),
        ("trader_alpha >= 40", lambda r: r["alpha"] >= 40),
        ("trader_alpha >= 50", lambda r: r["alpha"] >= 50),
        ("signal >= 50 OR alpha >= 50", lambda r: r["signal"] >= 50 or r["alpha"] >= 50),
        ("signal >= 50 AND alpha >= 40", lambda r: r["signal"] >= 50 and r["alpha"] >= 40),
        ("buy & signal >= 50", lambda r: r["buy"] and r["signal"] >= 50),
    ]
    L += [
        "",
        "## Candidate FILTER strategies (subset of the locked set)",
        "| filter | kept | % of set | win% | mean excess | mean raw |",
        "|---|--:|--:|--:|--:|--:|",
        f"| (none — Phase-1 locked) | {len(rows)} | 100% | {base['win_rate'] * 100:.0f}% | "
        f"**{base['mean_exc']:+.1%}** | {base['mean_ret']:+.1%} |",
    ]
    for name, pred in strategies:
        kept = [r for r in rows if pred(r)]
        s = _stat(kept)
        if not s.get("n"):
            continue
        L.append(
            f"| {name} | {len(kept)} | {len(kept) / len(rows) * 100:.0f}% | "
            f"{s['win_rate'] * 100:.0f}% | **{s['mean_exc']:+.1%}** | {s['mean_ret']:+.1%} |"
        )

    # bet-sizing gradient
    L += [
        "",
        "## Bet-SIZING gradient (does a higher score → higher return, for tiering bets?)",
        "_If mean raw return rises monotonically with the score, tiering bet size by it adds "
        "return. Compare each tier's mean raw return._",
        "| tier (signal_strength) | n | mean raw | mean excess |",
        "|---|--:|--:|--:|",
    ]
    for lo, hi, lab in [(0, 45, "low <45"), (45, 55, "mid 45–54"), (55, 101, "high 55+")]:
        s = _stat([r for r in rows if lo <= r["signal"] < hi])
        if s.get("n"):
            L.append(f"| {lab} | {s['n']} | {s['mean_ret']:+.1%} | {s['mean_exc']:+.1%} |")

    L += [
        "",
        "## Notes",
        "- Only the realized-360 subset is scored on outcomes; recent trades (2025–26) are "
        "excluded here but still get a Phase-2 score for live use.",
        "- mean excess is the per-trade 1-yr edge; the locked Phase-1 portfolio edge was ~+18–21%. "
        "A filter 'helps' if its mean excess clears the baseline with enough volume left.",
        "- These are correlations on stored scores — the actual buy/sell improvement is measured "
        "by the combined backtest (next).",
    ]
    md = "\n".join(L)
    with open(_OUT, "w") as f:
        f.write(md)
    print(f"wrote {_OUT}\n")
    print(md)


if __name__ == "__main__":
    run()
