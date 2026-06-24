"""Ticker -> sector/industry enrichment (the other half of the 'access' layer).

Free via yfinance. Lets the analysis generalize "Financial Services member trades a
bank" to every sector. Resilient: delisted/invalid tickers are recorded as ok=false
so re-runs skip them. Run as a background job over the ~3k distinct tickers:

  uv run python -m insider_trader.securities
"""

from __future__ import annotations

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


def fetch_sector(ticker: str) -> dict | None:
    """Sector/industry/name for a ticker via yfinance, or None if unavailable."""
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info
    except Exception:  # noqa: BLE001 - yfinance throws many things on bad/delisted symbols
        return None
    if not info or not info.get("sector"):
        return None
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }


def enrich(conn, limit: int | None = None, retry_failed: bool = False) -> dict:
    """Fetch sectors for distinct transaction tickers not already resolved."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        conn.commit()
        cur.execute("SELECT DISTINCT ticker FROM transactions WHERE ticker IS NOT NULL")
        tickers = {r[0] for r in cur.fetchall()}
        done_filter = "" if retry_failed else "WHERE ok"
        cur.execute(f"SELECT ticker FROM securities {done_filter}")
        done = {r[0] for r in cur.fetchall()}

    todo = sorted(tickers - done)
    if limit:
        todo = todo[:limit]
    ok = fail = 0
    with conn.cursor() as cur:
        for i, t in enumerate(todo, 1):
            s = fetch_sector(t)
            cur.execute(
                "INSERT INTO securities (ticker, name, sector, industry, ok, fetched_at) "
                "VALUES (%s,%s,%s,%s,%s, now()) ON CONFLICT (ticker) DO UPDATE SET "
                "name=EXCLUDED.name, sector=EXCLUDED.sector, industry=EXCLUDED.industry, "
                "ok=EXCLUDED.ok, fetched_at=now()",
                (t, s and s["name"], s and s["sector"], s and s["industry"], bool(s)),
            )
            ok += bool(s)
            fail += (not s)
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
