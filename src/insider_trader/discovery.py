"""Find the trades we WANT to catch: timely, lesser-known purchases that popped.

For every disclosed congressional PURCHASE we compute the stock's forward return at
30 / 90 / 360 days from the trade date, then surface the big winners — focusing on
LESSER-KNOWN names (the startup-like bets, not mega-caps). "Lesser-known" is proxied
by **low congressional trade-frequency** (mega-caps like AAPL/MSFT are traded by
hundreds; obscure small-caps by a handful). We deliberately do NOT filter by current
market cap — a name that 50x'd after the buy is large-cap *today* but was small then,
so a current-cap filter would hide the very winners we're hunting (cap is annotated
for color only). These are the ground-truth positives; step 2 mines their signals.

  DATABASE_URL=... uv run python -m insider_trader.discovery
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, timedelta

from . import store

HORIZONS = (30, 90, 360)


def _on_or_after(series: dict[date, float], d: date) -> date | None:
    cand = [dd for dd in series if dd >= d]
    return min(cand) if cand else None


def _fwd(series: dict[date, float], entry: date, h: int) -> float | None:
    e = _on_or_after(series, entry)
    x = _on_or_after(series, entry + timedelta(days=h))
    if not e or not x or x <= e:
        return None
    return series[x] / series[e] - 1.0


def _load_purchases(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, m.full_name, t.ticker, t.txn_date, t.disclosure_date, "
            "t.amount_low, t.amount_high "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL AND t.txn_date IS NOT NULL "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id"
        )
        return cur.fetchall()


def _ticker_frequency(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, count(*) FROM transactions WHERE ticker IS NOT NULL GROUP BY ticker"
        )
        return dict(cur.fetchall())


def _prices(tickers: list[str], batch: int = 60) -> dict[str, dict[date, float]]:
    import yfinance as yf

    out: dict[str, dict[date, float]] = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
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
                out[s] = {ts.date(): float(v) for ts, v in close.items()}
        time.sleep(0.5)
        print(f"  prices {min(i + batch, len(tickers))}/{len(tickers)} ({len(out)} ok)", flush=True)
    return out


def _market_caps(tickers: list[str]) -> dict[str, int | None]:
    import yfinance as yf

    caps: dict[str, int | None] = {}
    for t in tickers:
        try:
            caps[t] = yf.Ticker(t).fast_info.get("market_cap")
        except Exception:  # noqa: BLE001
            caps[t] = None
        time.sleep(0.15)
    return caps


def _cap_class(cap: int | None) -> str:
    if cap is None:
        return "?"
    if cap < 300e6:
        return "micro"
    if cap < 2e9:
        return "small"
    if cap < 10e9:
        return "mid"
    return "large"


def run(min_bump: float = 1.0, lesser_known_max_freq: int = 25) -> dict:
    # Read, then CLOSE the connection — the price fetch takes minutes and Neon kills
    # a connection left idle-in-transaction. Reconnect only to persist at the end.
    conn = store.connect()
    purchases = _load_purchases(conn)
    freq = _ticker_frequency(conn)
    conn.close()
    by_ticker: dict[str, list[tuple]] = defaultdict(list)
    for r in purchases:
        by_ticker[r[2]].append(r)
    tickers = sorted(by_ticker)
    print(
        f"{len(purchases)} purchases across {len(tickers)} tickers; fetching prices...", flush=True
    )
    prices = _prices(tickers)

    results = []
    for tk, rows in by_ticker.items():
        ser = prices.get(tk)
        if not ser:
            continue
        for tid, member, ticker, tdate, ddate, lo, hi in rows:
            rets = {h: _fwd(ser, tdate, h) for h in HORIZONS}
            valid = {h: v for h, v in rets.items() if v is not None}
            if not valid:
                continue
            best_h = max(valid, key=lambda h: valid[h])
            results.append(
                {
                    "tid": tid,
                    "member": member,
                    "ticker": ticker,
                    "tdate": tdate,
                    "ddate": ddate,
                    "lo": lo,
                    "hi": hi,
                    "r30": rets.get(30),
                    "r90": rets.get(90),
                    "r360": rets.get(360),
                    "best": valid[best_h],
                    "best_h": best_h,
                    "freq": freq.get(ticker, 0),
                }
            )

    winners = sorted(
        (r for r in results if r["best"] >= min_bump), key=lambda r: r["best"], reverse=True
    )
    lesser = [r for r in winners if r["freq"] <= lesser_known_max_freq]
    caps = _market_caps(sorted({r["ticker"] for r in lesser[:120]}))
    for r in lesser:
        r["cap"] = caps.get(r["ticker"])

    conn = store.connect()
    _persist(conn, lesser)
    conn.close()
    return {"analyzed": len(results), "winners": winners, "lesser": lesser}


def _persist(conn, lesser: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS timely_winners (
                transaction_id bigint, member text, ticker text, txn_date date,
                disclosure_date date, amount_low bigint, amount_high bigint,
                ret_30 double precision, ret_90 double precision, ret_360 double precision,
                best_return double precision, best_horizon int, cong_freq int, market_cap bigint
            )""")
        cur.execute("TRUNCATE timely_winners")
        cur.executemany(
            "INSERT INTO timely_winners VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    r["tid"],
                    r["member"],
                    r["ticker"],
                    r["tdate"],
                    r["ddate"],
                    r["lo"],
                    r["hi"],
                    r["r30"],
                    r["r90"],
                    r["r360"],
                    r["best"],
                    r["best_h"],
                    r["freq"],
                    r.get("cap"),
                )
                for r in lesser
            ],
        )
    conn.commit()


def _pct(x):
    return f"{x * 100:+.0f}%" if x is not None else "  n/a"


def report(m: dict) -> str:
    res_n = m["analyzed"]
    w = m["winners"]
    over = lambda th: sum(r["best"] >= th for r in w)  # noqa: E731
    lesser = m["lesser"]
    lines = [
        "=" * 88,
        "  TIMELY WINNERS — congressional purchases that popped (forward return from trade date)",
        "=" * 88,
        f"  Purchases analyzed (priced): {res_n}",
        f"  Bumps >= +50%: {over(0.5)}   >= +100%: {over(1.0)}   >= +200%: {over(2.0)}   "
        f">= +400%: {over(4.0)}",
        f"  Of the +100% winners, LESSER-KNOWN (<=25 trades on the ticker): {len(lesser)}",
        "-" * 88,
        "  TOP LESSER-KNOWN TIMELY WINNERS  (the trades we want to be flagging)",
        f"  {'member':<22}{'tkr':<6}{'trade':<12}{'+30d':>6}{'+90d':>7}{'+360d':>7}"
        f"{'best':>7}{'cap':>7}{'freq':>5}",
        "  " + "-" * 86,
    ]
    for r in lesser[:35]:
        lines.append(
            f"  {r['member'][:21]:<22}{r['ticker']:<6}{str(r['tdate']):<12}"
            f"{_pct(r['r30']):>6}{_pct(r['r90']):>7}{_pct(r['r360']):>7}"
            f"{_pct(r['best']):>7}{_cap_class(r.get('cap')):>7}{r['freq']:>5}"
        )
    lines += [
        "=" * 88,
        "  'best' = max forward return across 30/90/360d.  'cap' = CURRENT market-cap class",
        "  (winners often grew, so a small-then name reads large now — annotation only).",
        "  'freq' = total congressional trades on that ticker (low = lesser-known).",
        "  These are ground-truth positives; step 2 = mine the signals that flagged them early.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(run()))
