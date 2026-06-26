"""Ticker -> sector/industry enrichment (the other half of the 'access' layer).

Free via yfinance. Lets the analysis generalize "Financial Services member trades a
bank" to every sector. Yahoo rate-limits rapid bursts, so we **pace** requests and
**retry with backoff**; only not-yet-resolved tickers are (re)attempted, so re-runs
just fill the gaps. Run as a background job:

  uv run python -m insider_trader.securities
"""

from __future__ import annotations

import time

from . import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS securities (
    ticker     text PRIMARY KEY,
    name       text,
    sector     text,
    industry   text,
    ok         boolean,
    fetched_at timestamptz DEFAULT now()
);
"""


def fetch_sector(ticker: str, retries: int = 2) -> dict | None:
    """Sector/industry/name via yfinance, or None. Retries with backoff to ride out
    Yahoo rate-limiting (a 3k-request burst gets throttled, producing false misses)."""
    import yfinance as yf

    for attempt in range(retries + 1):
        try:
            info = yf.Ticker(ticker).info
        except Exception:  # noqa: BLE001 - yfinance throws many things on bad/throttled symbols
            info = None
        if info and info.get("sector"):
            return {
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
            }
        # A clean response that simply lacks a sector (delisted / not-found / fund) is a
        # DEFINITIVE miss — don't burn backoff retrying it. Only retry when the request threw
        # (info is None), which is the signature of throttling worth riding out.
        if info is not None:
            return None
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))  # back off, then retry
    return None


def enrich(conn, limit: int | None = None, pause: float = 0.4) -> dict:
    """Resolve sectors for transaction tickers not yet resolved (ok). Paces requests."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        conn.commit()
        cur.execute("SELECT DISTINCT ticker FROM transactions WHERE ticker IS NOT NULL")
        tickers = {r[0] for r in cur.fetchall()}
        # skip resolved tickers AND ones attempted recently (so a daily job doesn't re-hit the
        # ~1k junk/non-equity tickers every run; failures get retried only monthly).
        cur.execute(
            "SELECT ticker FROM securities WHERE ok OR fetched_at > now() - interval '30 days'"
        )
        done = {r[0] for r in cur.fetchall()}

    todo = sorted(tickers - done)
    if limit:
        todo = todo[:limit]
    ok = fail = 0
    with conn.cursor() as cur:
        for i, t in enumerate(todo, 1):
            if pause:
                time.sleep(pause)
            s = fetch_sector(t)
            cur.execute(
                "INSERT INTO securities (ticker, name, sector, industry, ok, fetched_at) "
                "VALUES (%s,%s,%s,%s,%s, now()) ON CONFLICT (ticker) DO UPDATE SET "
                "name=EXCLUDED.name, sector=EXCLUDED.sector, industry=EXCLUDED.industry, "
                "ok=EXCLUDED.ok, fetched_at=now()",
                (t, s and s["name"], s and s["sector"], s and s["industry"], bool(s)),
            )
            ok += bool(s)
            fail += not s
            if i % 100 == 0:
                conn.commit()
                print(f"  {i}/{len(todo)}  ({ok} ok, {fail} fail)", flush=True)
        conn.commit()
    return {"attempted": len(todo), "ok": ok, "fail": fail}


if __name__ == "__main__":
    conn = store.connect()
    if conn:
        print(enrich(conn))
        conn.close()
