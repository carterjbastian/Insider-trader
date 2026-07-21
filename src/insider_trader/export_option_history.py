"""One-off archive of the REAL option history the locked backtests depend on.

Why this exists: `polygon_options` hits Massive/Polygon live on every call and caches nothing.
The Developer tier ($79/mo) reaches back to 2022; the Starter tier (~$30/mo) only keeps a
ROLLING ~2 years. Downgrading therefore makes the 2022→2024 option bars unreachable forever.
This script pulls them once, while the entitlement still exists, into a durable archive.

What it archives, for every Phase-1 trade with disclosure >= 2022 (all tiers, not just Tier 3,
so future re-tiering experiments stay possible), for both structures the backtests use:
  • picks — the contract each (ticker, buy_date, structure) resolves to, plus the unadjusted spot
    used to choose the strike. Archiving the pick means a replay never needs the reference
    endpoint either, only the bars below.
  • bars  — that contract's full daily OHLCV from buy_date to min(expiration, today).

Outputs (both, independently):
  • Neon  — `option_picks_archive` / `option_bars_archive` (durable, off-box; the point of this)
  • local — data/options-history/{picks,bars}.jsonl.gz (gitignored mirror, convenience)

  POLYGON_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.export_option_history
  ...add --dry-run to resolve picks and report coverage without fetching bars or writing.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import polygon_options as po
from . import store
from .final_backtest import _build, _load
from .phase1_backtest import _prices

# (label, otm_offset, target_dte) — atm1yr is the locked Tier-3 sleeve; otm180 was the
# alternative structure tested in real_options_backtest. Archive both.
STRUCTURES = [("atm1yr", 0.0, 365), ("otm180", 0.10, 180)]
FLOOR = date(2022, 1, 1)  # Developer-tier history floor — nothing older exists to archive
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "options-history"

SCHEMA = """
CREATE TABLE IF NOT EXISTS option_picks_archive (
    transaction_id  bigint,
    structure       text,
    ticker          text,
    buy_date        date,
    spot            double precision,
    contract        text,
    strike          double precision,
    expiration      date,
    dte             int,
    archived_at     timestamptz DEFAULT now(),
    PRIMARY KEY (transaction_id, structure)
);
CREATE TABLE IF NOT EXISTS option_bars_archive (
    contract    text,
    bar_date    date,
    open        double precision,
    high        double precision,
    low         double precision,
    close       double precision,
    volume      double precision,
    vwap        double precision,
    n           int,
    PRIMARY KEY (contract, bar_date)
);
"""


def _bar_date(ms: int) -> date:
    return datetime.utcfromtimestamp(ms / 1000).date()


def _resolve_picks(bets, dry_run: bool):
    """Resolve every (bet, structure) to a concrete contract. Returns (picks, bars_by_contract)."""
    import yfinance as yf

    splits: dict[str, object] = {}
    picks, bars_by_contract = [], {}
    todo = [b for b in bets if b["buy_date"] >= FLOOR]
    print(f"[export] {len(todo)} Phase-1 trades >= {FLOOR} x {len(STRUCTURES)} structures")
    for i, b in enumerate(todo, 1):
        tk = b["ticker"]
        if tk not in splits:
            try:
                splits[tk] = yf.Ticker(tk).splits
            except Exception:  # noqa: BLE001
                splits[tk] = None
        spot = po.unadjusted_spot(tk, b["buy_date"], b["buy_close"], splits[tk])
        if not spot:
            print(f"[export]   no spot for {tk} {b['buy_date']} — skipped", flush=True)
            continue
        for label, otm, dte in STRUCTURES:
            pick = po.pick_call(tk, b["buy_date"], spot * (1 + otm), target_dte=dte)
            if not pick:
                continue
            picks.append(
                {
                    "transaction_id": b["tid"],
                    "structure": label,
                    "ticker": tk,
                    "buy_date": b["buy_date"].isoformat(),
                    "spot": spot * (1 + otm),
                    "contract": pick["contract"],
                    "strike": pick["strike"],
                    "expiration": pick["expiration"],
                    "dte": pick["dte"],
                }
            )
            # one contract can serve several trades; fetch its widest needed range once
            exp = min(po._d(pick["expiration"]), date.today())
            prev = bars_by_contract.get(pick["contract"])
            lo = min(prev[0], b["buy_date"]) if prev else b["buy_date"]
            hi = max(prev[1], exp) if prev else exp
            bars_by_contract[pick["contract"]] = (lo, hi)
            time.sleep(0.1)
        if i % 20 == 0:
            print(f"[export] picks {i}/{len(todo)} ({len(bars_by_contract)} contracts)", flush=True)
    print(f"[export] resolved {len(picks)} picks over {len(bars_by_contract)} unique contracts")
    if dry_run:
        print("[export] --dry-run: stopping before bar fetch")
    return picks, bars_by_contract


def _fetch_bars(bars_by_contract):
    rows, empty = [], 0
    total = len(bars_by_contract)
    for i, (contract, (lo, hi)) in enumerate(sorted(bars_by_contract.items()), 1):
        bars = po.daily_bars(contract, lo, hi)
        if not bars:
            empty += 1
        for bar in bars:
            rows.append(
                {
                    "contract": contract,
                    "bar_date": _bar_date(bar["t"]).isoformat(),
                    "open": bar.get("o"),
                    "high": bar.get("h"),
                    "low": bar.get("l"),
                    "close": bar.get("c"),
                    "volume": bar.get("v"),
                    "vwap": bar.get("vw"),
                    "n": bar.get("n"),
                }
            )
        time.sleep(0.1)
        if i % 25 == 0:
            print(f"[export] bars {i}/{total} ({len(rows)} rows)", flush=True)
    print(f"[export] {len(rows)} bar rows; {empty}/{total} contracts returned nothing")
    return rows


def _write_local(picks, bars):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, rows in (("picks", picks), ("bars", bars)):
        path = OUT_DIR / f"{name}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"[export] wrote {len(rows)} rows -> {path} ({path.stat().st_size / 1e6:.1f} MB)")


def _write_neon(picks, bars):
    conn = store.connect()
    if conn is None:
        print("[export] WARNING: no DATABASE_URL / unreachable — local files only, NOT durable")
        return
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.executemany(
            "INSERT INTO option_picks_archive (transaction_id, structure, ticker, buy_date, spot, "
            "contract, strike, expiration, dte) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (transaction_id, structure) DO NOTHING",
            [
                (
                    p["transaction_id"],
                    p["structure"],
                    p["ticker"],
                    p["buy_date"],
                    p["spot"],
                    p["contract"],
                    p["strike"],
                    p["expiration"],
                    p["dte"],
                )
                for p in picks
            ],
        )
        cur.executemany(
            "INSERT INTO option_bars_archive (contract, bar_date, open, high, low, close, volume, "
            "vwap, n) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (contract, bar_date) DO NOTHING",
            [
                (
                    b["contract"],
                    b["bar_date"],
                    b["open"],
                    b["high"],
                    b["low"],
                    b["close"],
                    b["volume"],
                    b["vwap"],
                    b["n"],
                )
                for b in bars
            ],
        )
    print(f"[export] Neon: upserted {len(picks)} picks + {len(bars)} bars (idempotent)")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    conn = store.connect()
    if conn is None:
        raise SystemExit("DATABASE_URL required to load the Phase-1 trade universe")
    with conn:
        sigs, sales = _load(conn)
    print(f"[export] {len(sigs)} candidate transactions from Neon")
    bets = _build(sigs, sales, _prices([s["ticker"] for s in sigs]))
    print(f"[export] {len(bets)} pass the locked Phase-1 filter")
    picks, bars_by_contract = _resolve_picks(bets, dry_run)
    if dry_run:
        return
    bars = _fetch_bars(bars_by_contract)
    _write_local(picks, bars)
    _write_neon(picks, bars)
    print("[export] done — safe to downgrade the Massive plan")


if __name__ == "__main__":
    main()
