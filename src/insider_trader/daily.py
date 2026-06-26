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
from datetime import date

from . import house, members, metrics, notify, paper, phase2, securities, senate, store
from .phase1_backtest import GROWTH, _on_after, _prices

# GO-LIVE: we only act on disclosures dated on/after this — a true day-of strategy, no backfill.
# Portfolio (paper + any real) starts EMPTY and fills only as genuinely-new signals arrive.
GO_LIVE = date(2026, 6, 27)

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


def pull_house(conn, years):
    """Fetch + store only House PTRs we don't already have. Returns (n_new, [filing dicts])."""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM filings")
        have = {r[0] for r in cur.fetchall()}
    new = []
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
            new.append(
                {
                    "member": f"{f.first} {f.last}".strip(),
                    "url": f"{house.BASE}/ptr-pdfs/{f.year}/{f.doc_id}.pdf",
                    "n": len(txns),
                }
            )
    return len(new), new


def pull_senate(years):
    """Incremental Senate pull (manages own connection). Returns (n_new_txns, [filing dicts])."""
    total, new = 0, []
    for year in years:
        try:
            r = senate.ingest(year, year)
            total += r.get("transactions", 0)
            new += r.get("new_docs", [])
        except Exception as e:  # noqa: BLE001
            print(f"  ! senate {year}: {type(e).__name__}: {e}", flush=True)
    return total, new


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
            (GO_LIVE, list(GROWTH)),
        )
        cols = ["tid", "bioguide", "member", "ticker", "txn_date", "disc"]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _tier(sig):
    return 3 if sig >= 60 else (2 if sig >= 50 else 1)


def new_buy_signals(conn) -> list[dict]:
    cands = _new_locked_candidates(conn)
    if not cands:
        return []
    conn.commit()  # close the read transaction before the (slow) price fetch
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


# --- profiles refresh (periodic, not every run) -----------------------------


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


RUNLOG = "/home/carter/vault/Scratchpad/Ingests/Insider Trader Run Log.md"
_TIER_LABEL = {1: "$50", 2: "$100", 3: "$100 + 1yr ATM calls"}


def _pt_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %I:%M %p PT")


def _append_runlog(text: str) -> None:
    """Append-only — never rewrites or deletes prior entries (orchestrator trims separately)."""
    with open(RUNLOG, "a") as f:
        f.write(text + "\n")


def _log_success(stamp, new_filings, buys, sells, tg_ok, em_ok):
    L = [f"\n## {stamp} — ✅ completed"]
    if new_filings:
        L.append(f"- **New trades scraped:** {len(new_filings)} filing(s):")
        for d in new_filings[:40]:
            L.append(f"  - {d['member']} ({d['n']} txns) — [source]({d['url']})")
    else:
        L.append("- **New trades scraped:** 0")
    if buys:
        L.append("- **BUY signals:**")
        for b in buys:
            L.append(
                f"  - {b['ticker']} (copy {b['member']}) — Phase-1: run-up "
                f"{b['runup'] * 100:+.0f}%; Phase-2: signal {b['signal']} → "
                f"**tier {b['tier']} ({_TIER_LABEL[b['tier']]})**"
            )
    if sells:
        L.append("- **SELL signals:**")
        L += [f"  - {s['ticker']} (copy {s['member']})" for s in sells]
    if not buys and not sells:
        L.append("- **Signals:** none")
    acts = [f"BUY {b['ticker']} {_TIER_LABEL[b['tier']]}" for b in buys] + [
        f"SELL {s['ticker']}" for s in sells
    ]
    L.append(f"- **Action items:** {'; '.join(acts) if acts else 'none'}")
    em = "Y" if em_ok else ("N/A (no signals)" if not (buys or sells) else "N")
    L.append(f"- **Telegram sent:** {'Y' if tg_ok else 'N'}  |  **Email sent:** {em}")
    _append_runlog("\n".join(L))


def run(dry_run: bool = False) -> dict:
    stamp = _pt_now()
    new_filings = []
    try:
        y = date.today().year
        years = [y - 1, y]  # current + prior year (late/amended filings)
        # Short-lived connection per step, closed before each slow network step, so nothing sits
        # idle-in-transaction across the ~5-min pulls (Neon kills idle-in-tx conns after ~5 min).
        print("[daily] 1. refreshing roster...", flush=True)
        conn = store.connect()
        roster = members.enrich(conn)
        conn.close()
        print(f"   matched {roster['matched']}/{roster['filers']} filers", flush=True)

        print("[daily] 2. pulling new trades...", flush=True)
        conn = store.connect()
        nh, house_new = pull_house(conn, years)
        conn.close()  # close before the slow Senate pull so this conn can't go stale
        ns, senate_new = pull_senate(years)  # manages its own connection
        new_filings = house_new + senate_new
        print(f"   +{nh} House filings, +{ns} Senate txns", flush=True)

        print("[daily] 3. enriching sectors...", flush=True)
        conn = store.connect()
        securities.enrich(conn)
        conn.close()

        print("[daily] 4. recomputing trader_metrics + returns...", flush=True)
        metrics.run()

        print("[daily] 5. Phase-1 -> Phase-2 on new disclosures...", flush=True)
        conn = store.connect()
        buys = new_buy_signals(conn)

        print("[daily] 6. updating paper portfolio...", flush=True)
        pr = paper.run(conn)
        sells, port = pr["sells"], pr["port"]
        conn.close()
        print(
            f"   {len(buys)} buys, {len(sells)} sells | portfolio ${port['total']:,.0f}", flush=True
        )

        print("[daily] 7. broadcasting...", flush=True)
        stats = {
            "date": date.today().isoformat(),
            "house": nh,
            "senate": ns,
            "candidates": len(buys),
        }
        summary = notify.run_summary(stats, buys, sells, port)
        print(summary, flush=True)
        tg_ok = em_ok = False
        if not dry_run:
            tg_ok = notify.telegram(summary)  # Channel 1: personal, every run
            if buys or sells:  # Channel 2: friends email, only on a signal
                kinds = "/".join(filter(None, ["Buy" if buys else "", "Sell" if sells else ""]))
                em_ok = notify.email(
                    f"Insider Trader: New {kinds} Signal/s ({date.today().isoformat()})",
                    notify.signal_email_body(buys, sells, port),
                )
        _log_success(stamp, new_filings, buys, sells, tg_ok, em_ok)
        return {"house": nh, "senate": ns, "buys": len(buys), "sells": len(sells)}
    except Exception as e:  # noqa: BLE001 — never fail quietly: log + alert, then re-raise
        err = f"{type(e).__name__}: {e}"
        print(f"[daily] FAILED: {err}", flush=True)
        _append_runlog(
            f"\n## {stamp} — ❌ FAILED\n- **Error:** {err}\n"
            f"- **New trades scraped before failure:** {len(new_filings)}\n"
            "- **Action item:** DEBUG + RERUN the daily job.\n"
            "- **Telegram sent:** Y (failure alert)"
        )
        if not dry_run:
            notify.telegram(
                f"❌ Insider Trader daily run FAILED ({stamp})\n{err}\n→ Action: debug + rerun."
            )
        raise


if __name__ == "__main__":
    import sys

    print(run(dry_run="--dry-run" in sys.argv))
