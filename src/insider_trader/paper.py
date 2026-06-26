"""Live paper-trading engine — the source of truth for the ongoing simulation.

Follows the LOCKED strategy exactly: tier sizing ($50 / $100 / $100+1yr-ATM-calls), the
quarterly 2x matching multiplier from the realized-gains pool, and the sell rule (sooner of
18 months or when the trigger-trader discloses a sale). Equity bought at the disclosure-date
close; options at the real Polygon premium. State lives in Postgres (paper_*) and is rendered
to a vault markdown doc each daily run.

Inception = the first daily run (2026-06-27); day-of signals only, no backfill. Used by `daily.py`.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from . import polygon_options as po
from . import store
from .phase1_backtest import _last, _on_after, _prices

PORTFOLIO_MD = "/home/carter/vault/Projects/Black Box/Insider Trader/Live Paper Portfolio.md"
HOLD_CAP = timedelta(days=548)  # 18 months
MATCH_FACTOR = 2.0  # locked: pool must cover 2x the projected matching need

_DDL = """
CREATE TABLE IF NOT EXISTS paper_state (
    id int PRIMARY KEY DEFAULT 1,
    inception date,
    multiplier int DEFAULT 1,
    mult_quarter text,
    pool double precision DEFAULT 0,
    personal_in double precision DEFAULT 0,
    CHECK (id = 1)
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id              bigserial PRIMARY KEY,
    transaction_id  bigint,
    kind            text,            -- 'equity' | 'option'
    member          text, ticker text,
    open_date       date,
    open_price      double precision,
    qty             double precision,   -- shares, or option contracts
    cost            double precision,   -- $ deployed (incl. matching)
    personal        double precision,   -- $ of that from personal capital
    multiplier      int,
    signal_strength int, tier int,
    strike          double precision, expiry date,   -- options only
    status          text DEFAULT 'open',
    close_date      date, close_price double precision, proceeds double precision,
    UNIQUE (transaction_id, kind)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id bigserial PRIMARY KEY, ts date, action text, kind text,
    member text, ticker text, qty double precision, price double precision,
    amount double precision, note text, created timestamptz DEFAULT now()
);
"""


def _state(conn):
    with conn.cursor() as cur:
        cur.execute(_DDL)
        cur.execute(
            "SELECT inception, multiplier, mult_quarter, pool, personal_in FROM paper_state"
        )
        r = cur.fetchone()
        if not r:
            cur.execute("INSERT INTO paper_state (id) VALUES (1)")
            conn.commit()
            return {
                "inception": None,
                "multiplier": 1,
                "mult_quarter": None,
                "pool": 0.0,
                "personal_in": 0.0,
            }
    return {
        "inception": r[0],
        "multiplier": r[1],
        "mult_quarter": r[2],
        "pool": float(r[3]),
        "personal_in": float(r[4]),
    }


def _save_state(conn, st):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE paper_state SET inception=%s, multiplier=%s, mult_quarter=%s, pool=%s, "
            "personal_in=%s WHERE id=1",
            (st["inception"], st["multiplier"], st["mult_quarter"], st["pool"], st["personal_in"]),
        )
    conn.commit()


def _log(conn, ts, action, kind, member, ticker, qty, price, amount, note=""):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO paper_trades (ts, action, kind, member, ticker, qty, price, amount, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ts, action, kind, member, ticker, qty, price, amount, note),
        )
    conn.commit()


def _quarter(d):
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _set_multiplier(conn, st, today):
    """At each new quarter, set the matching multiplier from the realized-gains pool (2x rule)."""
    q = _quarter(today)
    if st["mult_quarter"] == q:
        return
    with conn.cursor() as cur:  # projected base spend = trailing avg over prior quarters
        cur.execute(
            "SELECT count(DISTINCT to_char(open_date,'YYYY-Q')) , "
            "sum(personal) FROM paper_positions WHERE open_date < %s",
            (date(today.year, ((today.month - 1) // 3) * 3 + 1, 1),),
        )
        nq, spent = cur.fetchone()
    projected = (float(spent) / nq) if (nq and spent) else 1.0
    m = 1 + int(st["pool"] // (MATCH_FACTOR * projected)) if projected > 0 else 1
    st["multiplier"], st["mult_quarter"] = min(m, 6), q
    _save_state(conn, st)


# --- buys -------------------------------------------------------------------


def _tier(sig):
    return 3 if sig >= 60 else (2 if sig >= 50 else 1)


def apply_buys(conn, today, prices):
    """Open positions for buy signals not yet acted on. Equity at disclosure close; Tier-3 also
    buys real 1-yr ATM calls."""
    import yfinance as yf

    st = _state(conn)
    _set_multiplier(conn, st, today)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.transaction_id, g.member, g.ticker, g.disclosure_date, g.signal_strength, "
            "f.bioguide FROM signals g JOIN transactions t ON t.id=g.transaction_id "
            "JOIN filings f USING(doc_id) WHERE g.kind='buy' AND NOT EXISTS "
            "(SELECT 1 FROM paper_positions p WHERE p.transaction_id=g.transaction_id)",
        )
        todo = cur.fetchall()
    mult = st["multiplier"]
    for tid, member, ticker, disc, sig, _bio in todo:
        ser = prices.get(ticker)
        buy = _on_after(ser, disc) if ser else None
        if not buy:
            continue
        tier = _tier(sig)
        base_eq = 100.0 if tier >= 2 else 50.0
        eq_cost = base_eq * mult
        shares = eq_cost / buy[1]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO paper_positions (transaction_id, kind, member, ticker, open_date, "
                "open_price, qty, cost, personal, multiplier, signal_strength, tier) "
                "VALUES (%s,'equity',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (transaction_id, kind) DO NOTHING",
                (tid, member, ticker, buy[0], buy[1], shares, eq_cost, base_eq, mult, sig, tier),
            )
        conn.commit()
        st["personal_in"] += base_eq
        st["pool"] -= (mult - 1) * base_eq  # matching drawn from realized-gains pool
        _log(
            conn,
            today,
            "BUY",
            "equity",
            member,
            ticker,
            shares,
            buy[1],
            eq_cost,
            f"tier {tier}, x{mult}",
        )
        if tier == 3:  # 1-yr ATM call sleeve
            spot = po.unadjusted_spot(
                ticker, disc, adj_close=buy[1], splits=yf.Ticker(ticker).splits
            )
            res = po.option_premium_path(ticker, disc, spot, target_dte=365) if spot else None
            if res and res.get("entry_premium"):
                entry = res["entry_premium"]
                ncon = max(1, math.ceil(100 / entry)) * mult
                ocost = ncon * entry
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO paper_positions (transaction_id, kind, member, ticker, "
                        "open_date, open_price, qty, cost, personal, multiplier, signal_strength, "
                        "tier, strike, expiry) "
                        "VALUES (%s,'option',%s,%s,%s,%s,%s,%s,%s,%s,%s,3,%s,%s) "
                        "ON CONFLICT (transaction_id, kind) DO NOTHING",
                        (
                            tid,
                            member,
                            ticker,
                            buy[0],
                            entry,
                            ncon,
                            ocost,
                            ncon * entry / mult,
                            mult,
                            sig,
                            res["strike"],
                            res["expiration"],
                        ),
                    )
                conn.commit()
                st["personal_in"] += ncon * entry / mult
                st["pool"] -= (mult - 1) * ncon * entry / mult
                _log(
                    conn,
                    today,
                    "BUY",
                    "option",
                    member,
                    ticker,
                    ncon,
                    entry,
                    ocost,
                    f"1yr ATM call strike {res['strike']}",
                )
    _save_state(conn, st)


# --- sells ------------------------------------------------------------------


def apply_sells(conn, today, prices):
    """Close equity at the sooner of 18mo or a trigger-trader sale; options at expiry."""
    st = _state(conn)
    sold = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.transaction_id, p.kind, p.member, p.ticker, p.open_date, p.qty, "
            "p.expiry, p.strike, f.bioguide FROM paper_positions p "
            "JOIN transactions t ON t.id=p.transaction_id JOIN filings f USING(doc_id) "
            "WHERE p.status='open'",
        )
        opens = cur.fetchall()
    for pid, _tid, kind, member, ticker, odate, qty, expiry, strike, bio in opens:
        ser = prices.get(ticker)
        if not ser:
            continue
        sell_date = sell_px = None
        if kind == "equity":
            with conn.cursor() as cur:  # trigger-trader's first disclosed sale after our buy
                cur.execute(
                    "SELECT min(t.disclosure_date) FROM transactions t "
                    "JOIN filings f USING(doc_id) "
                    "WHERE f.bioguide=%s AND t.ticker=%s AND t.txn_type='sale' "
                    "AND t.disclosure_date > %s",
                    (bio, ticker, odate),
                )
                tsale = cur.fetchone()[0]
            target = min(odate + HOLD_CAP, tsale) if tsale else odate + HOLD_CAP
            if target <= today:
                hit = _on_after(ser, target) or _last(ser)
                sell_date, sell_px = hit[0], hit[1]
        else:  # option: at expiry
            exp = expiry if isinstance(expiry, date) else None
            if exp and exp <= today:
                hit = _on_after(ser, exp) or _last(ser)
                sell_date, sell_px = exp, max(0.0, hit[1] - strike)  # intrinsic at expiry
        if sell_date:
            proceeds = qty * sell_px
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE paper_positions SET status='closed', close_date=%s, close_price=%s, "
                    "proceeds=%s WHERE id=%s",
                    (sell_date, sell_px, proceeds, pid),
                )
            conn.commit()
            st["pool"] += proceeds  # realized -> available for future matching
            _log(conn, today, "SELL", kind, member, ticker, qty, sell_px, proceeds, "")
            sold.append({"member": member, "ticker": ticker, "kind": kind})
    _save_state(conn, st)
    return sold


# --- valuation + render -----------------------------------------------------


def _occ_symbol(ticker, expiry, strike):
    exp = expiry.strftime("%y%m%d")
    return f"O:{ticker}{exp}C{int(round(strike * 1000)):08d}"


def _option_market_price(ticker, expiry, strike, fallback):
    """Current market price of an open call from Polygon (latest daily close); fallback = entry."""
    try:
        bars = po.daily_bars(
            _occ_symbol(ticker, expiry, strike), date.today() - timedelta(days=7), date.today()
        )
        return bars[-1]["c"] if bars else fallback
    except Exception:  # noqa: BLE001
        return fallback


def _mark_equity(ser, qty):
    px = _last(ser)[1] if ser else 0.0
    return px, qty * px


def snapshot(conn, prices):
    st = _state(conn)
    rows = []
    held = 0.0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, member, ticker, open_date, open_price, qty, cost, personal, "
            "multiplier, signal_strength, tier, strike, expiry, status, close_date, close_price, "
            "proceeds FROM paper_positions ORDER BY open_date, id"
        )
        cols = [
            "id",
            "kind",
            "member",
            "ticker",
            "open_date",
            "open_price",
            "qty",
            "cost",
            "personal",
            "multiplier",
            "signal_strength",
            "tier",
            "strike",
            "expiry",
            "status",
            "close_date",
            "close_price",
            "proceeds",
        ]
        for r in cur.fetchall():
            d = dict(zip(cols, r, strict=True))
            if d["status"] == "open":
                if d["kind"] == "option":
                    px = _option_market_price(
                        d["ticker"], d["expiry"], float(d["strike"] or 0), float(d["open_price"])
                    )
                    val = float(d["qty"]) * px
                else:
                    px, val = _mark_equity(prices.get(d["ticker"]), float(d["qty"]))
                d["mark_price"], d["value"] = px, val
                held += val
            else:
                d["value"] = float(d["proceeds"] or 0)
            rows.append(d)
    return st, rows, held


def render(conn, today, prices):
    st, rows, held = snapshot(conn, prices)
    total = held + st["pool"]
    personal = st["personal_in"] or 0.0
    ret_str = f"{(total / personal - 1) * 100:+.1f}%" if personal else "— (no positions yet)"
    opens = [r for r in rows if r["status"] == "open"]
    closed = [r for r in rows if r["status"] == "closed"]
    L = [
        "---",
        "type: paper-portfolio",
        "epic: Black Box",
        "track: Insider Trader",
        f"updated: {today.isoformat()}",
        "---",
        "# Insider Trader — Live Paper Portfolio",
        "",
        f"> **Source of truth for the live paper-trading simulation** of the locked strategy. "
        f"Inception {st['inception']}. Updated every daily run. Not investment advice — "
        "educational only.",
        "",
        "## Summary",
        f"- **Personal capital deployed:** ${personal:,.2f}",
        f"- **Current portfolio value:** ${total:,.2f}  (open positions ${held:,.2f} + "
        f"realized-gains pool ${st['pool']:,.2f})",
        f"- **Total return on personal capital:** {ret_str}",
        f"- **Open positions:** {len(opens)}  |  **Closed:** {len(closed)}  |  "
        f"**Current matching multiplier:** {st['multiplier']}x ({st['mult_quarter']})",
        "",
        "## Open positions",
        "| opened | member | ticker | kind | tier | qty | cost | mark | value | gain |",
        "|---|---|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    for r in opens:
        g = r["value"] / r["cost"] - 1 if r["cost"] else 0
        L.append(
            f"| {r['open_date']} | {r['member']} | {r['ticker']} | {r['kind']} | {r['tier']} | "
            f"{r['qty']:.2f} | ${r['cost']:,.0f} | ${r.get('mark_price', 0):,.2f} | "
            f"${r['value']:,.0f} | {g * 100:+.0f}% |"
        )
    L += [
        "",
        "## Closed positions",
        "| opened | closed | member | ticker | kind | cost | proceeds | gain |",
        "|---|---|---|---|---|--:|--:|--:|",
    ]
    for r in closed:
        g = (r["proceeds"] or 0) / r["cost"] - 1 if r["cost"] else 0
        L.append(
            f"| {r['open_date']} | {r['close_date']} | {r['member']} | {r['ticker']} | {r['kind']} "
            f"| ${r['cost']:,.0f} | ${r['proceeds'] or 0:,.0f} | {g * 100:+.0f}% |"
        )
    L += [
        "",
        "## Trade log",
        "| date | action | kind | member | ticker | qty | price | amount |",
        "|---|---|---|---|---|--:|--:|--:|",
    ]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, action, kind, member, ticker, qty, price, amount FROM paper_trades "
            "ORDER BY created DESC LIMIT 200"
        )
        for ts, action, kind, member, ticker, qty, price, amount in cur.fetchall():
            L.append(
                f"| {ts} | {action} | {kind} | {member} | {ticker} | {qty:.2f} | "
                f"${price:,.2f} | ${amount:,.0f} |"
            )
    with open(PORTFOLIO_MD, "w") as f:
        f.write("\n".join(L))
    return {"total": total, "personal": personal, "open": len(opens), "closed": len(closed)}


def run(conn, today=None):
    """Daily paper step: set inception, apply new buys, process sells, mark + render."""
    today = today or date.today()
    st = _state(conn)
    if not st["inception"]:
        st["inception"] = today
        _save_state(conn, st)
    # gather every ticker we hold or have a signal for
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ticker FROM paper_positions WHERE status='open' "
            "UNION SELECT DISTINCT ticker FROM signals WHERE kind='buy'"
        )
        tickers = [r[0] for r in cur.fetchall() if r[0]]
    prices = _prices(tickers) if tickers else {"SPY": {}}
    apply_buys(conn, today, prices)
    sells = apply_sells(conn, today, prices)
    port = render(conn, today, prices)
    return {"port": port, "sells": sells}


if __name__ == "__main__":
    c = store.connect()
    print(json.dumps(run(c), default=str, indent=2))
    c.close()
