"""Which heuristics separate the winner-pick trades from the rest?

Univariate (and a few combined) evaluation of every quantitative feature we have —
trader track-record, the trade itself, and the asset — against a market-EXCESS
winner target. For each feature we report:
  - AUC: how well it ranks winners above non-winners (0.5 = none, >0.6 = useful).
  - lift: winner-rate in the feature's top group vs the base rate (precision signal).
  - recall: share of all winners the top group captures.
  - rule-out: the bottom group's winner-rate (low = safe to exclude) and the share of
    winners that bottom group would cost us (low = we don't lose the good ones).
Plus a per-year consistency check on the strongest separators, and a couple of
combined filters. All features are point-in-time / look-ahead-safe (trader_metrics).

  DATABASE_URL=... uv run python -m insider_trader.signals
"""

from __future__ import annotations

from statistics import fmean

from . import store

WINNER_EXC = 0.5  # "winner-pick" = beat the market by >= +50% over 360 days


def _load(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT tm.transaction_id, tm.txn_date, tr.ticker, tr.sector, tr.exc_360,
                   tm.n_prior_90, tm.prior_mean_exc90, tm.prior_winrate90, tm.prior_bigwin90,
                   tm.prior_loser90, tm.prior_stdev_exc90, tm.prior_mean_exc360,
                   tm.prior_bigwin360, tm.traded_ticker_before, tm.traded_sector_before,
                   tm.amount_vs_prior_median, t.amount_low, t.amount_high, t.delay_days,
                   fr.cnt AS ticker_freq
            FROM trader_metrics tm
            JOIN trade_returns tr USING(transaction_id)
            JOIN transactions t ON t.id = tm.transaction_id
            JOIN (SELECT ticker, count(*) cnt FROM transactions
                  WHERE ticker IS NOT NULL GROUP BY ticker) fr ON fr.ticker = tr.ticker
            WHERE tr.exc_360 IS NOT NULL
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    for r in rows:
        lo, hi = r["amount_low"], r["amount_high"]
        r["amount_mid"] = (lo + hi) / 2 if (lo and hi) else (lo or 0)
        r["new_ticker"] = not r["traded_ticker_before"]
        r["win"] = r["exc_360"] >= WINNER_EXC
    return rows


def _auc(pairs: list[tuple[float, bool]]) -> float | None:
    """P(value[winner] > value[loser]); ties counted as 0.5 via average ranks."""
    pairs = sorted(pairs)
    n = len(pairs)
    pos = sum(1 for _, lab in pairs if lab)
    neg = n - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < n:
        j = i
        while j < n and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        rank_sum += avg_rank * sum(1 for k in range(i, j) if pairs[k][1])
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _continuous(rows, feat) -> dict | None:
    data = [(r[feat], r["win"]) for r in rows if r.get(feat) is not None]
    if len(data) < 100:
        return None
    auc = _auc(data)
    if auc is None:
        return None
    data.sort()
    n = len(data)
    k = n // 5
    low, high = data[:k], data[-k:]
    base = sum(w for _, w in data) / n
    lo_wr = sum(w for _, w in low) / len(low)
    hi_wr = sum(w for _, w in high) / len(high)
    total_win = sum(w for _, w in data)
    # if higher = better, top group recall + bottom rule-out; else flip
    direction = "high" if auc >= 0.5 else "low"
    top, bot = (high, low) if direction == "high" else (low, high)
    top_wr = hi_wr if direction == "high" else lo_wr
    return {
        "feature": feat,
        "auc": auc,
        "base": base,
        "n": n,
        "top_group": f"top-quintile ({direction})",
        "top_wr": top_wr,
        "top_lift": top_wr / base if base else 0,
        "top_recall": sum(w for _, w in top) / total_win if total_win else 0,
        "ruleout_wr": (lo_wr if direction == "high" else hi_wr),
        "ruleout_winner_cost": sum(w for _, w in bot) / total_win if total_win else 0,
    }


def _boolean(rows, feat) -> dict | None:
    data = [(bool(r[feat]), r["win"]) for r in rows if r.get(feat) is not None]
    t = [w for v, w in data if v]
    f = [w for v, w in data if not v]
    if len(t) < 50 or len(f) < 50:
        return None
    base = sum(w for _, w in data) / len(data)
    tw, fw = fmean(t), fmean(f)
    hi_is_true = tw >= fw
    total = sum(t) + sum(f)
    return {
        "feature": feat,
        "base": base,
        "n": len(data),
        "true_wr": tw,
        "false_wr": fw,
        "lift": (max(tw, fw) / base) if base else 0,
        "best_group": "True" if hi_is_true else "False",
        "best_recall": (sum(t) if hi_is_true else sum(f)) / total if total else 0,
    }


CONT = [
    "prior_mean_exc90",
    "prior_winrate90",
    "prior_bigwin90",
    "prior_bigwin360",
    "prior_mean_exc360",
    "prior_loser90",
    "prior_stdev_exc90",
    "n_prior_90",
    "amount_mid",
    "amount_vs_prior_median",
    "delay_days",
    "ticker_freq",
]
BOOL = ["new_ticker", "traded_sector_before"]


def _year_consistency(rows, feat, top_frac=0.2) -> dict:
    """Top-group winner-rate vs base, per year — is the separator consistent?"""
    by_year: dict[int, list] = {}
    for r in rows:
        if r.get(feat) is not None:
            by_year.setdefault(r["txn_date"].year, []).append((r[feat], r["win"]))
    out = {}
    for y, data in sorted(by_year.items()):
        if len(data) < 40:
            continue
        data.sort(reverse=True)  # high feature first
        k = max(1, int(len(data) * top_frac))
        base = sum(w for _, w in data) / len(data)
        top_wr = sum(w for _, w in data[:k]) / k
        out[y] = (top_wr, base, top_wr - base)
    return out


def run() -> None:
    conn = store.connect()
    rows = _load(conn)
    conn.close()
    base = sum(r["win"] for r in rows) / len(rows)
    print("=" * 84)
    print(f"  SIGNAL SEPARATION — target: exc_360 >= +{WINNER_EXC * 100:.0f}% (beat market)")
    print(
        f"  {len(rows)} trades with realized 360d excess | winners: "
        f"{sum(r['win'] for r in rows)} ({base * 100:.1f}% base rate)"
    )
    print("=" * 84)

    cont = sorted(
        (c for c in (_continuous(rows, f) for f in CONT) if c),
        key=lambda d: abs(d["auc"] - 0.5),
        reverse=True,
    )
    print(
        f"  {'feature':<22}{'AUC':>6}{'top group':>18}{'top wr':>8}{'lift':>6}"
        f"{'recall':>8}{'ruleout wr':>11}"
    )
    print("  " + "-" * 80)
    for d in cont:
        print(
            f"  {d['feature']:<22}{d['auc']:>6.3f}{d['top_group']:>18}"
            f"{d['top_wr'] * 100:>7.1f}%{d['top_lift']:>6.1f}{d['top_recall'] * 100:>7.0f}%"
            f"{d['ruleout_wr'] * 100:>10.1f}%"
        )
    print("\n  Booleans:")
    for f in BOOL:
        b = _boolean(rows, f)
        if b:
            print(
                f"  {b['feature']:<22} base {b['base'] * 100:.1f}% | "
                f"True {b['true_wr'] * 100:.1f}% vs False {b['false_wr'] * 100:.1f}% | "
                f"lift {b['lift']:.1f} (best={b['best_group']}, "
                f"recall {b['best_recall'] * 100:.0f}%)"
            )

    # sector winner-rates
    print("\n  Sector winner-rate (min 80 trades):")
    by_sec: dict[str, list] = {}
    for r in rows:
        if r.get("sector"):
            by_sec.setdefault(r["sector"], []).append(r["win"])
    secs = sorted(
        ((s, fmean(w), len(w)) for s, w in by_sec.items() if len(w) >= 80),
        key=lambda x: x[1],
        reverse=True,
    )
    for s, wr, n in secs:
        print(f"    {s:<26} {wr * 100:>5.1f}%  (n={n})")

    print(
        "\n  Per-year consistency of the strongest separator "
        f"({cont[0]['feature']}, top-20% winner-rate vs base):"
    )
    for y, (tw, b, diff) in _year_consistency(rows, cont[0]["feature"]).items():
        flag = "OK" if diff > 0 else "--"
        print(
            f"    {y}: top {tw * 100:>5.1f}% vs base {b * 100:>5.1f}%  ({diff * 100:+.1f}pp) {flag}"
        )

    # Combined rule-out filters: shrink the candidate set while keeping the winners.
    amts = sorted(r["amount_mid"] for r in rows if r.get("amount_mid"))
    amed = amts[len(amts) // 2]
    growth = {"Technology", "Basic Materials", "Energy", "Communication Services", "Industrials"}
    defensive = {"Utilities", "Real Estate", "Consumer Defensive", "Financial Services"}
    filters = [
        ("growth sector", lambda r: r.get("sector") in growth),
        ("small size (<= median $)", lambda r: r["amount_mid"] <= amed),
        ("trader had a prior big-win", lambda r: (r.get("prior_bigwin90") or 0) > 0),
        ("EXCLUDE defensive sectors", lambda r: r.get("sector") not in defensive),
        ("growth + small size", lambda r: r.get("sector") in growth and r["amount_mid"] <= amed),
        (
            "growth + small + prior big-win",
            lambda r: (
                r.get("sector") in growth
                and r["amount_mid"] <= amed
                and (r.get("prior_bigwin90") or 0) > 0
            ),
        ),
    ]
    total_win = sum(r["win"] for r in rows)
    base = total_win / len(rows)
    print("\n  COMBINED RULE-OUT FILTERS (shrink candidate set for the LLM, keep the winners):")
    print(f"  {'filter':<34}{'kept':>7}{'win rate':>10}{'recall':>8}{'lift':>6}")
    print("  " + "-" * 64)
    for name, pred in filters:
        kept = [r for r in rows if pred(r)]
        if not kept:
            continue
        kw = sum(r["win"] for r in kept)
        print(
            f"  {name:<34}{len(kept) / len(rows) * 100:>6.0f}%{kw / len(kept) * 100:>9.1f}%"
            f"{kw / total_win * 100:>7.0f}%{(kw / len(kept)) / base:>6.1f}"
        )
    print("=" * 84)


if __name__ == "__main__":
    run()
