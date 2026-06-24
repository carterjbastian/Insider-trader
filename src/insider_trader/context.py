"""Point-in-time context assembly for the analysis skill (look-ahead safe).

Builds the inputs the 'funny business' skill will judge, using ONLY information
available at the disclosure date: the member's profile + committees, the traded
asset's sector, and the member's PRIOR trading history (disclosures strictly
before this one). No future prices/returns ever enter here — outcomes are measured
separately by the backtest (vault spec 05 §5.5).

The SQL fetches; ``summarize_history`` is a pure function (unit-tested offline).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta


def summarize_history(prior: list[dict], this_ticker: str | None, as_of: date) -> dict:
    """Pure summary of a member's prior trades (what was knowable at ``as_of``).

    ``prior`` rows: ticker, sector, txn_type, disclosure_date, delay_days,
    amount_low, amount_high.
    """
    n = len(prior)
    buys = sum(1 for r in prior if r["txn_type"] == "purchase")
    sells = sum(1 for r in prior if r["txn_type"] == "sale")
    sectors = Counter(r["sector"] for r in prior if r.get("sector"))
    delays = sorted(r["delay_days"] for r in prior if r.get("delay_days") is not None)
    cutoff = as_of - timedelta(days=90)
    recent = sum(1 for r in prior if r.get("disclosure_date") and r["disclosure_date"] >= cutoff)
    return {
        "n_prior_trades": n,
        "n_buys": buys,
        "n_sells": sells,
        "distinct_tickers": len({r["ticker"] for r in prior if r.get("ticker")}),
        "times_traded_this_ticker": sum(1 for r in prior if r.get("ticker") == this_ticker),
        "top_sectors": sectors.most_common(3),
        "median_delay_days": (delays[len(delays) // 2] if delays else None),
        "trades_last_90d": recent,
    }


def _member(conn, bioguide: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT full_name, party, chamber, state, committees FROM members WHERE bioguide=%s",
            (bioguide,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {"full_name": r[0], "party": r[1], "chamber": r[2], "state": r[3], "committees": r[4]}


def _sector(conn, ticker: str | None) -> dict | None:
    if not ticker:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, sector, industry FROM securities WHERE ticker=%s AND ok", (ticker,)
        )
        r = cur.fetchone()
    return {"name": r[0], "sector": r[1], "industry": r[2]} if r else None


def _prior_trades(conn, bioguide: str, as_of: date) -> list[dict]:
    """Member's transactions disclosed strictly before ``as_of`` (point-in-time)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.ticker, s.sector, t.txn_type, t.disclosure_date, t.delay_days, "
            "t.amount_low, t.amount_high "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "LEFT JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "WHERE f.bioguide=%s AND t.disclosure_date < %s",
            (bioguide, as_of),
        )
        cols = ["ticker", "sector", "txn_type", "disclosure_date", "delay_days",
                "amount_low", "amount_high"]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def event_context(conn, transaction_id: int) -> dict | None:
    """Assemble the full point-in-time context for analyzing one transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.ticker, t.asset_name, t.txn_type, t.txn_date, t.disclosure_date, "
            "t.delay_days, t.amount_low, t.amount_high, f.bioguide "
            "FROM transactions t JOIN filings f USING(doc_id) WHERE t.id=%s",
            (transaction_id,),
        )
        r = cur.fetchone()
    if not r or not r[8]:
        return None
    ticker, asset, ttype, tdate, ddate, delay, lo, hi, bioguide = r
    as_of = ddate or tdate
    prior = _prior_trades(conn, bioguide, as_of)
    return {
        "event": {
            "ticker": ticker, "asset": asset, "txn_type": ttype,
            "txn_date": tdate, "disclosure_date": ddate, "delay_days": delay,
            "amount_low": lo, "amount_high": hi,
        },
        "member": _member(conn, bioguide),
        "asset_sector": _sector(conn, ticker),
        "history": summarize_history(prior, ticker, as_of) if as_of else None,
        "as_of": as_of,
    }
