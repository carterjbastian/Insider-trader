"""Market-normalized trade returns + progressive (point-in-time) trader metrics.

Two DB layers that feed the flagging model:

1. **trade_returns** — every priceable congressional PURCHASE's RAW and
   market-EXCESS (vs SPY) forward return at 30/90/360 days. Excess normalizes away
   "bought the bottom" / macro so we can see real stock-selection.

2. **trader_metrics** — for each trade, a snapshot of the member's track record using
   ONLY prior trades whose outcome was realized *before* this trade (look-ahead-safe):
   mean/median excess, win rate, big-winner rate, loser rate, consistency (stdev), and
   whether THIS trade is abnormal for them (new ticker/sector, outsized). Stored
   per-trade = "progressive" (each row is the running metric as of that trade).

  DATABASE_URL=... uv run python -m insider_trader.metrics
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import date, timedelta

from . import store

HORIZONS = (30, 90, 360)
BENCHMARK = "SPY"


# --- prices + returns -------------------------------------------------------


def _load_purchases(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, f.bioguide, m.full_name, t.ticker, s.sector, t.txn_date, "
            "t.disclosure_date, t.amount_low, t.amount_high "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "LEFT JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL AND t.txn_date IS NOT NULL "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id"
        )
        cols = [
            "txn_id",
            "bioguide",
            "member",
            "ticker",
            "sector",
            "txn_date",
            "disclosure_date",
            "amount_low",
            "amount_high",
        ]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _prices(tickers: list[str], batch: int = 60) -> dict[str, dict[date, float]]:
    import yfinance as yf

    syms = sorted(set(tickers) | {BENCHMARK})
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
                out[s] = {ts.date(): float(v) for ts, v in close.items()}
        time.sleep(0.5)
        print(f"  prices {min(i + batch, len(syms))}/{len(syms)} ({len(out)} ok)", flush=True)
    return out


def _on_or_after(series: dict[date, float], d: date) -> date | None:
    cand = [dd for dd in series if dd >= d]
    return min(cand) if cand else None


def _ret(series: dict[date, float], entry: date, h: int) -> float | None:
    e = _on_or_after(series, entry)
    x = _on_or_after(series, entry + timedelta(days=h))
    if not e or not x or x <= e:
        return None
    return series[x] / series[e] - 1.0


def compute_returns(purchases: list[dict], prices: dict) -> list[dict]:
    bench = prices.get(BENCHMARK, {})
    rows = []
    for p in purchases:
        ser = prices.get(p["ticker"])
        if not ser:
            continue
        r = dict(p)
        any_ret = False
        for h in HORIZONS:
            raw = _ret(ser, p["txn_date"], h)
            mkt = _ret(bench, p["txn_date"], h)
            r[f"ret_{h}"] = raw
            r[f"exc_{h}"] = (raw - mkt) if (raw is not None and mkt is not None) else None
            any_ret = any_ret or raw is not None
        if any_ret:
            rows.append(r)
    return rows


# --- progressive trader metrics (point-in-time) -----------------------------


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _median(xs):
    return statistics.median(xs) if xs else None


def _stdev(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else None


def _rate(flags):
    return (sum(flags) / len(flags)) if flags else None


def _amount_mid(r: dict) -> float:
    lo, hi = r.get("amount_low"), r.get("amount_high")
    if lo is None:
        return 0.0
    return (lo + hi) / 2 if hi else float(lo)


def compute_trader_metrics(returns_rows: list[dict]) -> list[dict]:
    """Per-trade, expanding-window track record over the member's PRIOR realized trades."""
    by_member: dict[str, list[dict]] = defaultdict(list)
    for r in returns_rows:
        by_member[r["bioguide"]].append(r)

    out = []
    for bio, trades in by_member.items():
        trades.sort(key=lambda r: r["txn_date"])
        for i, t in enumerate(trades):
            d = t["txn_date"]
            prior_all = trades[:i]
            # realized-by-now prior trades for each horizon (look-ahead safe)
            p90 = [
                p["exc_90"]
                for p in prior_all
                if p["exc_90"] is not None and p["txn_date"] + timedelta(days=90) <= d
            ]
            p360 = [
                p["exc_360"]
                for p in prior_all
                if p["exc_360"] is not None and p["txn_date"] + timedelta(days=360) <= d
            ]
            prior_amounts = [_amount_mid(p) for p in prior_all]
            out.append(
                {
                    "transaction_id": t["txn_id"],
                    "bioguide": bio,
                    "member": t["member"],
                    "txn_date": d,
                    "ticker": t["ticker"],
                    "n_prior_all": len(prior_all),
                    "n_prior_90": len(p90),
                    "prior_mean_exc90": _mean(p90),
                    "prior_median_exc90": _median(p90),
                    "prior_winrate90": _rate([x > 0 for x in p90]),
                    "prior_bigwin90": _rate([x >= 0.5 for x in p90]),
                    "prior_loser90": _rate([x < 0 for x in p90]),
                    "prior_stdev_exc90": _stdev(p90),
                    "n_prior_360": len(p360),
                    "prior_mean_exc360": _mean(p360),
                    "prior_bigwin360": _rate([x >= 1.0 for x in p360]),
                    "traded_ticker_before": any(p["ticker"] == t["ticker"] for p in prior_all),
                    "traded_sector_before": any(
                        p.get("sector") and p["sector"] == t.get("sector") for p in prior_all
                    ),
                    "amount_vs_prior_median": (
                        _amount_mid(t) / _median(prior_amounts)
                        if prior_amounts and _median(prior_amounts)
                        else None
                    ),
                }
            )
    return out


# --- persistence ------------------------------------------------------------

_RETURNS_DDL = """
CREATE TABLE IF NOT EXISTS trade_returns (
    transaction_id bigint PRIMARY KEY, bioguide text, ticker text, sector text,
    txn_date date, disclosure_date date,
    ret_30 double precision, ret_90 double precision, ret_360 double precision,
    exc_30 double precision, exc_90 double precision, exc_360 double precision
);"""

_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS trader_metrics (
    transaction_id bigint PRIMARY KEY, bioguide text, member text, txn_date date, ticker text,
    n_prior_all int, n_prior_90 int,
    prior_mean_exc90 double precision, prior_median_exc90 double precision,
    prior_winrate90 double precision, prior_bigwin90 double precision,
    prior_loser90 double precision, prior_stdev_exc90 double precision,
    n_prior_360 int, prior_mean_exc360 double precision, prior_bigwin360 double precision,
    traded_ticker_before boolean, traded_sector_before boolean,
    amount_vs_prior_median double precision
);"""


def _store_returns(conn, rows: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute(_RETURNS_DDL)
        cur.execute("TRUNCATE trade_returns")
        cur.executemany(
            "INSERT INTO trade_returns VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    r["txn_id"],
                    r["bioguide"],
                    r["ticker"],
                    r.get("sector"),
                    r["txn_date"],
                    r.get("disclosure_date"),
                    r["ret_30"],
                    r["ret_90"],
                    r["ret_360"],
                    r["exc_30"],
                    r["exc_90"],
                    r["exc_360"],
                )
                for r in rows
            ],
        )
    conn.commit()


def _store_metrics(conn, rows: list[dict]) -> None:
    cols = [
        "transaction_id",
        "bioguide",
        "member",
        "txn_date",
        "ticker",
        "n_prior_all",
        "n_prior_90",
        "prior_mean_exc90",
        "prior_median_exc90",
        "prior_winrate90",
        "prior_bigwin90",
        "prior_loser90",
        "prior_stdev_exc90",
        "n_prior_360",
        "prior_mean_exc360",
        "prior_bigwin360",
        "traded_ticker_before",
        "traded_sector_before",
        "amount_vs_prior_median",
    ]
    with conn.cursor() as cur:
        cur.execute(_METRICS_DDL)
        cur.execute("TRUNCATE trader_metrics")
        placeholders = ",".join(["%s"] * len(cols))
        cur.executemany(
            f"INSERT INTO trader_metrics ({','.join(cols)}) VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()


def run() -> dict:
    conn = store.connect()
    purchases = _load_purchases(conn)
    conn.close()  # release before the long price fetch (Neon idle-txn timeout)
    print(f"{len(purchases)} purchases; fetching prices...", flush=True)
    prices = _prices([p["ticker"] for p in purchases])
    returns_rows = compute_returns(purchases, prices)
    metric_rows = compute_trader_metrics(returns_rows)

    conn = store.connect()
    _store_returns(conn, returns_rows)
    _store_metrics(conn, metric_rows)
    conn.close()
    return {"purchases": len(purchases), "priced": len(returns_rows), "metrics": len(metric_rows)}


if __name__ == "__main__":
    print(run())
