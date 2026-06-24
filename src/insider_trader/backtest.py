"""Follow-the-signal backtest: do high-suspicion congressional BUYS beat the market?

The honest, look-ahead-safe test. We act ONLY on public info: "buy" each disclosed
purchase on its **disclosure date** (when it became public), hold N days, and measure
the return vs SPY. The scorer is outcome-blind; the returns are measured here,
separately — then we ask whether higher suspicion scores predict higher EXCESS return.

Caveats (printed in the report): small sample, ~2-yr single regime, survivorship
(delisted/unpriced tickers dropped), and the scorer's residual parametric-hindsight
risk on pre-cutoff data — live/forward signals are the true out-of-sample test.

Bulk-scores with a cheap model (Haiku, no thinking) and caches scores in `analyses`.

  ANTHROPIC_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.backtest
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from . import analyze, store

SCORING_MODEL = "claude-haiku-4-5"  # cheap/fast for bulk; opus is the live default


@dataclass(frozen=True)
class BacktestConfig:
    start: str = "2024-07-01"  # disclosure-date window (need realized N-day returns)
    end: str = "2026-03-01"
    sample: int = 150
    hold_days: int = 90
    benchmark: str = "SPY"
    model: str = SCORING_MODEL


@dataclass
class Trade:
    txn_id: int
    member: str
    ticker: str
    disclosure: date
    score: int
    ret: float
    excess: float


# --- prices -----------------------------------------------------------------


def _load_prices(
    tickers: list[str], start: str, end: str, benchmark: str
) -> dict[str, dict[date, float]]:
    import yfinance as yf

    syms = sorted(set(tickers) | {benchmark})
    df = yf.download(
        syms,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    out: dict[str, dict[date, float]] = {}
    for s in syms:
        try:
            close = df[s]["Close"].dropna()
        except Exception:  # noqa: BLE001 - symbol missing/delisted in the frame
            continue
        if len(close):
            out[s] = {ts.date(): float(v) for ts, v in close.items()}
    return out


def _on_or_after(series: dict[date, float], d: date) -> date | None:
    cand = [dd for dd in series if dd >= d]
    return min(cand) if cand else None


def _return(series: dict[date, float], entry: date, hold: int) -> float | None:
    e = _on_or_after(series, entry)
    x = _on_or_after(series, entry + timedelta(days=hold))
    if not e or not x or x <= e:
        return None
    return series[x] / series[e] - 1.0


# --- scoring cache ----------------------------------------------------------


def _cached_score(conn, txn_id: int) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT suspicion_score FROM analyses WHERE transaction_id=%s "
            "ORDER BY created DESC LIMIT 1",
            (txn_id,),
        )
        r = cur.fetchone()
    return r[0] if r else None


# --- the backtest -----------------------------------------------------------


def run(cfg: BacktestConfig | None = None, verbose: bool = True) -> dict:
    cfg = cfg or BacktestConfig()
    conn = store.connect()
    if not conn:
        raise RuntimeError("DATABASE_URL required")

    with conn.cursor() as cur:
        cur.execute(analyze._SCHEMA)  # ensure `analyses` exists before the cache lookup
        cur.execute(
            "SELECT t.id, m.full_name, t.ticker, t.disclosure_date "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date BETWEEN %s AND %s "
            "ORDER BY t.id LIMIT %s",
            (cfg.start, cfg.end, cfg.sample),
        )
        rows = cur.fetchall()
    conn.commit()

    prices = _load_prices([r[2] for r in rows], cfg.start, "2026-06-24", cfg.benchmark)
    bench = prices.get(cfg.benchmark, {})

    trades: list[Trade] = []
    skipped_price = scored = 0
    for i, (txn_id, member, ticker, ddate) in enumerate(rows, 1):
        ser = prices.get(ticker)
        if not ser:
            skipped_price += 1
            continue
        ret = _return(ser, ddate, cfg.hold_days)
        bret = _return(bench, ddate, cfg.hold_days)
        if ret is None or bret is None:
            skipped_price += 1
            continue
        sc = _cached_score(conn, txn_id)
        if sc is None:
            res = analyze.analyze_transaction(conn, txn_id, model=cfg.model, use_thinking=False)
            if not res:
                continue
            sc = res[0].suspicion_score
            scored += 1
        trades.append(Trade(txn_id, member, ticker, ddate, sc, ret, ret - bret))
        if verbose and i % 25 == 0:
            print(
                f"  {i}/{len(rows)} processed ({len(trades)} usable, {scored} newly scored)",
                flush=True,
            )

    conn.close()
    return _metrics(trades, cfg, skipped_price)


def _bucket(score: int) -> str:
    if score >= 70:
        return "d. 70-100 (high)"
    if score >= 50:
        return "c. 50-69"
    if score >= 30:
        return "b. 30-49"
    return "a. 0-29 (low)"


def _metrics(trades: list[Trade], cfg: BacktestConfig, skipped: int) -> dict:
    n = len(trades)
    by: dict[str, list[Trade]] = {}
    for t in trades:
        by.setdefault(_bucket(t.score), []).append(t)
    buckets = {
        b: {
            "n": len(ts),
            "mean_excess": mean(t.excess for t in ts),
            "mean_ret": mean(t.ret for t in ts),
            "win_rate": sum(t.excess > 0 for t in ts) / len(ts),
        }
        for b, ts in sorted(by.items())
    }
    ranked = sorted(trades, key=lambda t: t.score)
    q = max(1, n // 4)
    bottomq = ranked[:q]
    topq = ranked[-q:]
    return {
        "n": n,
        "skipped_price": skipped,
        "hold_days": cfg.hold_days,
        "overall_mean_excess": mean(t.excess for t in trades) if n else 0.0,
        "overall_mean_ret": mean(t.ret for t in trades) if n else 0.0,
        "overall_win_rate": (sum(t.excess > 0 for t in trades) / n) if n else 0.0,
        "buckets": buckets,
        "topq_mean_excess": mean(t.excess for t in topq) if topq else 0.0,
        "bottomq_mean_excess": mean(t.excess for t in bottomq) if bottomq else 0.0,
        "top_examples": sorted(trades, key=lambda t: t.score, reverse=True)[:5],
    }


def report(m: dict) -> str:
    pct = lambda x: f"{x * 100:+.1f}%"  # noqa: E731
    lines = [
        "=" * 70,
        f"  INSIDER TRADER — follow-the-signal backtest ({m['hold_days']}-day hold vs SPY)",
        "=" * 70,
        f"  Usable trades: {m['n']}  (skipped {m['skipped_price']} for missing/short price data)",
        f"  Following ALL congressional buys: mean return {pct(m['overall_mean_ret'])}, "
        f"mean EXCESS vs SPY {pct(m['overall_mean_excess'])}, "
        f"beat-SPY rate {m['overall_win_rate'] * 100:.0f}%",
        "-" * 70,
        "  Does the suspicion score predict EXCESS return?",
        f"  {'bucket':<18} {'n':>4} {'mean excess':>12} {'mean ret':>10} {'beat-SPY':>9}",
    ]
    for b, s in m["buckets"].items():
        lines.append(
            f"  {b:<18} {s['n']:>4} {pct(s['mean_excess']):>12} {pct(s['mean_ret']):>10} "
            f"{s['win_rate'] * 100:>7.0f}%"
        )
    lines += [
        "-" * 70,
        f"  Top-quartile (most suspicious) mean excess : {pct(m['topq_mean_excess'])}",
        f"  Bottom-quartile (least)        mean excess : {pct(m['bottomq_mean_excess'])}",
        f"  >>> SCORER EDGE (top minus bottom): "
        f"{pct(m['topq_mean_excess'] - m['bottomq_mean_excess'])}",
        "-" * 70,
        "  Highest-scored trades in sample:",
    ]
    for t in m["top_examples"]:
        lines.append(
            f"    [{t.score:3d}] {t.member[:22]:22} {t.ticker:5} {t.disclosure}  "
            f"ret {pct(t.ret)}  excess {pct(t.excess)}"
        )
    lines += [
        "=" * 70,
        "  Caveats: small sample, ~2yr single regime, survivorship (unpriced dropped),",
        "  and residual scorer hindsight on pre-cutoff data. Forward signals are the",
        "  true out-of-sample test. See vault spec 05 §5.5.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(run()))
