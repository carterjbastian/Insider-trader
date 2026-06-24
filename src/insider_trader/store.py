"""Neon (Postgres) store for House disclosures.

Optional/resilient — if ``DATABASE_URL`` is unset or unreachable, callers can still
parse (no persistence). Shares the Black Box Neon project with other strategies;
tables here are namespaced (``filings``, ``transactions``).
"""

from __future__ import annotations

import os

from .models import Filing, Transaction

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    doc_id      text PRIMARY KEY,
    last        text,
    first       text,
    suffix      text,
    state_dst   text,
    filing_type text,
    filing_date date,
    year        int,
    parsed      boolean,
    txn_count   int,
    ingested_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS transactions (
    id              bigserial PRIMARY KEY,
    doc_id          text REFERENCES filings(doc_id),
    owner           text,
    asset_name      text,
    ticker          text,
    asset_type      text,
    txn_type        text,
    txn_date        date,
    disclosure_date date,
    delay_days      int,
    amount_low      bigint,
    amount_high     bigint
);
CREATE INDEX IF NOT EXISTS ix_txn_ticker ON transactions(ticker);
CREATE INDEX IF NOT EXISTS ix_txn_doc ON transactions(doc_id);
"""


def connect():
    """Live Neon connection (schema ensured) or ``None`` if unavailable."""
    url = os.environ.get("DATABASE_URL")
    if not url or psycopg is None:
        print("[store] DATABASE_URL unset (or psycopg missing) — no persistence.")
        return None
    try:
        conn = psycopg.connect(url, connect_timeout=15)
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        return conn
    except Exception as e:  # noqa: BLE001
        print(f"[store] Neon unavailable ({type(e).__name__}: {e}); not persisting.")
        return None


def save_filing(conn, f: Filing, parsed: bool, txn_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filings (doc_id, last, first, suffix, state_dst, filing_type, "
            "filing_date, year, parsed, txn_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (doc_id) DO UPDATE SET parsed=EXCLUDED.parsed, "
            "txn_count=EXCLUDED.txn_count, ingested_at=now()",
            (
                f.doc_id,
                f.last,
                f.first,
                f.suffix,
                f.state_dst,
                f.filing_type,
                f.filing_date,
                f.year,
                parsed,
                txn_count,
            ),
        )
    conn.commit()


def replace_transactions(conn, doc_id: str, txns: list[Transaction]) -> None:
    """Idempotent: clear any prior rows for this filing, then insert."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transactions WHERE doc_id = %s", (doc_id,))
        if txns:
            cur.executemany(
                "INSERT INTO transactions (doc_id, owner, asset_name, ticker, asset_type, "
                "txn_type, txn_date, disclosure_date, delay_days, amount_low, amount_high) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        t.doc_id,
                        t.owner,
                        t.asset_name,
                        t.ticker,
                        t.asset_type,
                        t.txn_type,
                        t.txn_date,
                        t.disclosure_date,
                        t.delay_days,
                        t.amount_low,
                        t.amount_high,
                    )
                    for t in txns
                ],
            )
    conn.commit()
