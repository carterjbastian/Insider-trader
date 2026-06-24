"""Backfill House PTR disclosures into Neon.

  uv run python -m insider_trader.backfill <start_year> <end_year> [max_per_year]

Idempotent: re-running re-ingests (filings upsert, transactions replaced per doc).
``max_per_year`` (optional) caps filings per year — handy for a quick smoke test.
"""

from __future__ import annotations

import sys

from . import house, store


def run(start: int, end: int, max_per_year: int | None = None) -> None:
    conn = store.connect()
    grand_filings = grand_txns = 0
    for year in range(start, end + 1):
        try:
            filings = house.ptrs(year)
        except Exception as e:  # noqa: BLE001 - a missing/failed year shouldn't kill the run
            print(f"{year}: index unavailable ({type(e).__name__}: {e}) — skipping")
            continue
        if max_per_year:
            filings = filings[:max_per_year]
        parsed = empty = txns = 0
        for f in filings:
            try:
                ts = house.fetch_ptr(f)
            except Exception as e:  # noqa: BLE001 - skip a bad doc, keep going
                print(f"  ! {f.doc_id} {type(e).__name__}: {e}")
                ts = []
            if conn:
                store.save_filing(conn, f, parsed=bool(ts), txn_count=len(ts))
                store.replace_transactions(conn, f.doc_id, ts)
            parsed += bool(ts)
            empty += not ts
            txns += len(ts)
        grand_filings += len(filings)
        grand_txns += txns
        print(
            f"{year}: {len(filings)} PTRs ({parsed} parsed, {empty} empty) -> {txns} transactions"
        )
    if conn:
        conn.close()
    print(
        f"TOTAL: {grand_filings} filings -> {grand_txns} transactions"
        f"{' (DB persistence off)' if not store else ''}"
    )


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print("usage: python -m insider_trader.backfill <start_year> <end_year> [max_per_year]")
        raise SystemExit(1)
    start = int(a[0])
    end = int(a[1]) if len(a) > 1 else start
    cap = int(a[2]) if len(a) > 2 else None
    run(start, end, cap)
