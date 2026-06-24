"""Follow-the-signal backtest: do high-suspicion congressional BUYS beat the market?

The honest, look-ahead-safe test. We act ONLY on public info: "buy" each disclosed
purchase on its **disclosure date** (when it became public), hold N days, measure the
return vs SPY. The scorer is outcome-blind; returns are measured here, separately —
then we ask whether the suspicion score predicts EXCESS return, and how a deployable
"follow only the suspicious ones" portfolio would have done across several regimes.

Scores with opus (model-aware cache, so re-runs reuse only same-model scores).
Caveats printed: sample size, regime, survivorship, residual pre-cutoff hindsight —
forward/live signals are the true out-of-sample test.

  ANTHROPIC_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.backtest
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from . import analyze, store


@dataclass(frozen=True)
class BacktestConfig:
    start: str = "2024-07-01"
    end: str = "2025-12-31"
    sample: int = 200
    hold_days: int = 90
    benchmark: str = "SPY"
    model: str = "claude-opus-4-8"  # sharper than Haiku; the live-quality scorer
    use_thinking: bool = False  # off for bulk speed; live single-trade scoring uses it
    follow_quantile: float = 0.25  # "suspicious portfolio" = top 25% by score
    label: str = "all"


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
        except Exception:  # noqa: BLE001 - symbol missing/delisted
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


# --- scoring cache (model-aware) --------------------------------------------


def _cached_score(conn, txn_id: int, model: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT suspicion_score FROM analyses WHERE transaction_id=%s AND model=%s "
            "ORDER BY created DESC LIMIT 1",
            (txn_id, model),
        )
        r = cur.fetchone()
    return r[0] if r else None


# --- one window -------------------------------------------------------------


def run(cfg: BacktestConfig, conn, verbose: bool = True) -> dict:
    with conn.cursor() as cur:
        cur.execute(analyze._SCHEMA)  # ensure `analyses` exists
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, m.full_name, t.ticker, t.disclosure_date "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date BETWEEN %s AND %s "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id LIMIT %s",
            (cfg.start, cfg.end, cfg.sample),
        )
        rows = cur.fetchall()
    conn.commit()

    prices = _load_prices([r[2] for r in rows], cfg.start, "2026-06-24", cfg.benchmark)
    bench = prices.get(cfg.benchmark, {})

    trades: list[Trade] = []
    skipped = scored = 0
    for i, (txn_id, member, ticker, ddate) in enumerate(rows, 1):
        ser = prices.get(ticker)
        ret = _return(ser, ddate, cfg.hold_days) if ser else None
        bret = _return(bench, ddate, cfg.hold_days)
        if ret is None or bret is None:
            skipped += 1
            continue
        sc = _cached_score(conn, txn_id, cfg.model)
        if sc is None:
            res = analyze.analyze_transaction(
                conn, txn_id, model=cfg.model, use_thinking=cfg.use_thinking
            )
            if not res:
                continue
            sc = res[0].suspicion_score
            scored += 1
        trades.append(Trade(txn_id, member, ticker, ddate, sc, ret, ret - bret))
        if verbose and i % 25 == 0:
            print(
                f"  [{cfg.label}] {i}/{len(rows)} ({len(trades)} usable, {scored} scored)",
                flush=True,
            )

    return _metrics(trades, cfg, skipped)


def _bucket(s: int) -> str:
    return (
        "d. 70-100 (high)"
        if s >= 70
        else "c. 50-69"
        if s >= 50
        else "b. 30-49"
        if s >= 30
        else "a. 0-29 (low)"
    )


def _portfolio(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0, "mean_excess": 0.0, "mean_ret": 0.0, "beat": 0.0}
    return {
        "n": len(trades),
        "mean_excess": mean(t.excess for t in trades),
        "mean_ret": mean(t.ret for t in trades),
        "beat": sum(t.excess > 0 for t in trades) / len(trades),
    }


def _metrics(trades: list[Trade], cfg: BacktestConfig, skipped: int) -> dict:
    n = len(trades)
    ranked = sorted(trades, key=lambda t: t.score)
    q = max(1, round(n * cfg.follow_quantile))
    topq, bottomq = ranked[-q:], ranked[:q]
    cutoff = ranked[-q].score if ranked else 0
    by: dict[str, list[Trade]] = {}
    for t in trades:
        by.setdefault(_bucket(t.score), []).append(t)
    return {
        "label": cfg.label,
        "window": f"{cfg.start}..{cfg.end}",
        "n": n,
        "skipped": skipped,
        "hold_days": cfg.hold_days,
        "follow_all": _portfolio(trades),
        "follow_suspicious": {**_portfolio(topq), "min_score": cutoff},
        "buckets": {b: _portfolio(ts) for b, ts in sorted(by.items())},
        "edge": (mean(t.excess for t in topq) - mean(t.excess for t in bottomq)) if n else 0.0,
        "top_examples": sorted(trades, key=lambda t: t.score, reverse=True)[:6],
    }


# --- multi-window driver ----------------------------------------------------

WINDOWS = [
    ("2024 H2 (bull)", "2024-07-01", "2024-12-31"),
    ("2025 H1 (selloff)", "2025-01-01", "2025-06-30"),
    ("2025 H2", "2025-07-01", "2025-12-31"),
]


def run_windows(windows=WINDOWS, sample: int = 200, model: str = "claude-opus-4-8") -> list[dict]:
    conn = store.connect()
    if not conn:
        raise RuntimeError("DATABASE_URL required")
    results = []
    for label, start, end in windows:
        print(f"\n=== window: {label} ({start}..{end}) ===", flush=True)
        cfg = BacktestConfig(start=start, end=end, sample=sample, model=model, label=label)
        results.append(run(cfg, conn))
    conn.close()
    return results


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def report(results: list[dict]) -> str:
    lines = [
        "=" * 78,
        "  INSIDER TRADER — follow-the-signal backtest, opus-scored, by regime (90d vs SPY)",
        "=" * 78,
        f"  {'window':<20} {'n':>4} | {'FOLLOW-ALL excess':>18} | "
        f"{'FOLLOW-SUSPICIOUS excess':>26} | {'edge':>7}",
        "  " + "-" * 76,
    ]
    for m in results:
        fa, fs = m["follow_all"], m["follow_suspicious"]
        lines.append(
            f"  {m['label']:<20} {m['n']:>4} | "
            f"{_pct(fa['mean_excess']):>9} (beat {fa['beat'] * 100:>3.0f}%) | "
            f"{_pct(fs['mean_excess']):>9} (n={fs['n']:>2}, beat {fs['beat'] * 100:>3.0f}%) | "
            f"{_pct(m['edge']):>7}"
        )
    # pooled
    lines += ["  " + "-" * 76, "  Per-window score buckets (mean excess vs SPY):"]
    for m in results:
        bs = " ".join(
            f"{b.split('.')[0]}:{_pct(s['mean_excess'])}(n{s['n']})"
            for b, s in m["buckets"].items()
        )
        lines.append(f"    {m['label']:<20} {bs}")
    lines += [
        "=" * 78,
        "  FOLLOW-ALL = copy every congressional buy.  FOLLOW-SUSPICIOUS = only the",
        "  top-quartile by suspicion score (the deployable signal).  edge = top-q minus",
        "  bottom-q mean excess.  Positive 'suspicious' excess / edge = the score adds value.",
        "  Caveats: still a ~2yr window, survivorship (unpriced dropped), and residual",
        "  pre-cutoff scorer hindsight — forward signals remain the true test (spec 05 §5.5).",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(run_windows()))
