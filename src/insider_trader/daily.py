"""Daily orchestration job — keeps the Insider Trader system live.

Pipeline (idempotent; safe to re-run, backfills any gap since the last run):
  1. refresh the roster        (members.enrich — re-match filers, catch new members/committees)
  2. pull new House + Senate trades (incremental: only fetch filings we don't already have)
  3. enrich sectors            (securities.enrich — new tickers)
  4. recompute trader_metrics + market-excess returns (matured priors flip members to "proven")
  5. Phase-1 filter on new disclosures -> Phase-2 score the passers (profile built just-in-time)
  6. emit BUY signals (persisted); sell-signals + broadcast + paper-trade are stubs (item 3)

Member profiles are refreshed on a ~6-month cadence (`refresh_profiles`), separate from this job.

  POLYGON/ANTHROPIC/DATABASE env... uv run python -m insider_trader.daily [--dry-run]
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from . import house, members, metrics, phase2, securities, senate, store
from .phase1_backtest import GROWTH, _on_after, _prices

LOOKBACK_DAYS = 21  # how far back to scan for new locked candidates (covers any seeding gap)

_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id              bigserial PRIMARY KEY,
    transaction_id  bigint REFERENCES transactions(id),
    kind            text,            -- 'buy' | 'sell'
    member          text,
    ticker          text,
    disclosure_date date,
    signal_strength int,
    tier            int,
    runup           double precision,
    detail          jsonb,
    broadcast       boolean DEFAULT false,
    created         timestamptz DEFAULT now(),
    UNIQUE (transaction_id, kind)
);
"""


# --- step 2: incremental trade ingest ---------------------------------------


def pull_house(conn, years) -> int:
    """Fetch + store only House PTRs we don't already have (current/prior year for late filings)."""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM filings")
        have = {r[0] for r in cur.fetchall()}
    n = 0
    for year in years:
        try:
            filings = house.ptrs(year)
        except Exception as e:  # noqa: BLE001
            print(f"  ! house {year}: {type(e).__name__}: {e}", flush=True)
            continue
        for f in filings:
            if f.doc_id in have:
                continue
            try:
                txns = house.fetch_ptr(f)
            except Exception as e:  # noqa: BLE001
                print(f"  ! house ptr {f.doc_id}: {type(e).__name__}: {e}", flush=True)
                txns = []
            store.save_filing(conn, f, bool(txns), len(txns))
            store.replace_transactions(conn, f.doc_id, txns)
            n += 1
    return n


def pull_senate(conn, years) -> int:
    """Incremental Senate pull — senate.ingest is idempotent; it skips already-parsed docs."""
    total = 0
    for year in years:
        try:
            total += senate.ingest(year, year).get("transactions", 0)
        except Exception as e:  # noqa: BLE001
            print(f"  ! senate {year}: {type(e).__name__}: {e}", flush=True)
    return total


# --- step 5: new BUY signals ------------------------------------------------


def _new_locked_candidates(conn) -> list[dict]:
    """Recently-disclosed growth+proven purchases not yet signalled; run-up applied via prices."""
    with conn.cursor() as cur:
        cur.execute(_SIGNALS_DDL)
        conn.commit()
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, f.bioguide, m.full_name, t.ticker, t.txn_date, t.disclosure_date "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "JOIN trader_metrics tm ON tm.transaction_id=t.id "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date >= %s AND s.sector = ANY(%s) AND tm.prior_bigwin90 > 0 "
            "AND NOT EXISTS (SELECT 1 FROM signals g WHERE g.transaction_id=t.id AND g.kind='buy') "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id",
            (date.today() - timedelta(days=LOOKBACK_DAYS), list(GROWTH)),
        )
        cols = ["tid", "bioguide", "member", "ticker", "txn_date", "disc"]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _tier(sig):
    return 3 if sig >= 60 else (2 if sig >= 50 else 1)


def new_buy_signals(conn) -> list[dict]:
    cands = _new_locked_candidates(conn)
    if not cands:
        return []
    prices = _prices([c["ticker"] for c in cands])
    out = []
    for c in cands:
        ser = prices.get(c["ticker"])
        buy = _on_after(ser, c["disc"]) if ser else None
        txn = _on_after(ser, c["txn_date"]) if ser else None
        if not buy or not txn or txn[1] <= 0 or buy[1] / txn[1] - 1.0 < 0.05:
            continue  # fails the run-up leg of Phase 1
        runup = buy[1] / txn[1] - 1.0
        phase2.ensure_profile(conn, c["bioguide"])
        a = phase2.analyze(conn, c["tid"], runup=runup)
        sig = a.signal_strength if a else 0
        tier = _tier(sig)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signals (transaction_id, kind, member, ticker, disclosure_date, "
                "signal_strength, tier, runup, detail) "
                "VALUES (%s,'buy',%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (transaction_id, kind) DO NOTHING",
                (
                    c["tid"],
                    c["member"],
                    c["ticker"],
                    c["disc"],
                    sig,
                    tier,
                    runup,
                    json.dumps({"buy_rec": a.buy if a else None}),
                ),
            )
        conn.commit()
        out.append({**c, "signal": sig, "tier": tier, "runup": runup})
    return out


# --- step 6: stubs for item 3 -----------------------------------------------


def sell_signals(conn):  # noqa: ARG001
    """TODO (item 3): emit sells when the trigger-trader discloses a sale or 18mo elapses."""
    return []


def broadcast(signals):  # noqa: ARG001
    """TODO (item 3): push to Telegram + email. For now, log only."""
    for s in signals:
        print(
            f"  SIGNAL buy {s['ticker']} by {s['member']} | tier {s['tier']} "
            f"(signal {s['signal']}, run-up {s['runup'] * 100:+.0f}%)",
            flush=True,
        )


def update_paper_portfolio(conn, signals):  # noqa: ARG001
    """TODO (item 3): apply the locked sizing/compounding to a simulated portfolio."""


def refresh_profiles(conn, max_age_days=183):
    """Rebuild member profiles older than ~6 months (committees/roles drift). Run periodically."""
    idx = chist = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bioguide FROM member_profiles "
            "WHERE created < now() - (%s || ' days')::interval",
            (max_age_days,),
        )
        stale = [r[0] for r in cur.fetchall()]
    from . import profiles

    if stale:
        idx, chist = profiles.legislator_index(), profiles._committee_history()
    for bio in stale:
        profiles.build_profile(conn, bio, idx=idx, chist=chist)
    return len(stale)


def run(dry_run: bool = False) -> dict:
    y = date.today().year
    years = [y - 1, y]  # current + prior year (late/amended filings)
    conn = store.connect()
    print("[daily] 1. refreshing roster...", flush=True)
    roster = members.enrich(conn)
    print(f"   matched {roster['matched']}/{roster['filers']} filers", flush=True)

    print("[daily] 2. pulling new trades...", flush=True)
    nh = pull_house(conn, years)
    ns = pull_senate(conn, years)
    print(f"   +{nh} House filings, +{ns} Senate txns", flush=True)

    print("[daily] 3. enriching sectors...", flush=True)
    securities.enrich(conn)
    conn.close()

    print("[daily] 4. recomputing trader_metrics + returns...", flush=True)
    metrics.run()

    print("[daily] 5. Phase-1 -> Phase-2 on new disclosures...", flush=True)
    conn = store.connect()
    buys = new_buy_signals(conn)
    sells = sell_signals(conn)
    print(f"   {len(buys)} new buy signals, {len(sells)} sell signals", flush=True)

    if not dry_run:
        broadcast(buys)
        update_paper_portfolio(conn, buys + sells)
    conn.close()
    return {"house": nh, "senate": ns, "buys": len(buys), "sells": len(sells)}


if __name__ == "__main__":
    import sys

    print(run(dry_run="--dry-run" in sys.argv))
