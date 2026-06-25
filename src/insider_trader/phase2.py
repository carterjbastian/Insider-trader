"""Phase 2 — holistic LLM analysis of a Phase-1 candidate trade (Opus 4.8).

Assembles a 5-section DOSSIER (trader track-record · the trade · the company · our R&D
meta-heuristics · the member's biographical profile) and asks Opus for a buy call, a
calibrated confidence, two 0-100 scores (signal strength + trader alpha), and written
reasoning. Outputs are persisted to `phase2_analyses` so buy/sell strategies can be
re-tested on the stored analyses without re-paying for the API calls.

Look-ahead handling (best-effort for backtesting; the live system has none):
  1. Inputs are point-in-time — trader metrics use only prior realized trades, committee
     roles are dated, the run-up is the public purchase→disclosure move, company info is
     kept neutral. Bio narrative is the documented weak spot.
  2. The system prompt forbids using any post-disclosure or training-recalled knowledge of
     the specific stock/company/person.

  ANTHROPIC_API_KEY=... DATABASE_URL=... uv run python -m insider_trader.phase2 <transaction_id>
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from . import context, profiles, store

MODEL = "claude-opus-4-8"
PROMPT_VERSION = "p2-v1"

# Distilled from the R&D in Signal Heuristics.md — what actually separates winner-picks, so
# the model weights proven factors and discounts the duds. Static text (no leakage).
META_HEURISTICS = """WHAT OUR QUANTITATIVE R&D FOUND PREDICTS WINNERS (use as priors, not rules):
- SECTOR matters a lot: Technology, Basic Materials, Energy, Industrials, Communication
  Services produce nearly all the big winners; Utilities, Real Estate, Consumer Defensive,
  Financial Services almost never do.
- TRADER TRACK-RECORD matters: members with prior market-beating big wins, a positive mean
  market-excess return, and higher return variance ("swing for the fences") hit more winners.
- PRE-DISCLOSURE MOMENTUM is confirmation, not a missed boat: a stock already UP between the
  purchase and the public disclosure tends to keep outperforming; a stock that FELL into the
  disclosure underperforms (the deeper the drop, the worse).
- These DID NOT predict anything once market-normalized — do NOT weight them: reporting delay,
  ticker obscurity / "lesser-known company", whether it's a new ticker for the member, and
  POSITION SIZE (large positions if anything do slightly worse)."""

SYSTEM = f"""You are a forensic investment analyst evaluating one US congressional stock \
PURCHASE that has already passed a quantitative pre-filter (growth sector, a proven trader, \
and positive pre-disclosure momentum). Your job is to judge, holistically, whether this is a \
"winner-pick" worth following, and whether the member is plausibly trading on an informational \
edge unavailable to an ordinary investor.

You will be given a dossier in five parts: (1) the trader's track record, (2) the trade, \
(3) the company, (4) our research findings, (5) the member's biography. Weigh them together — \
the strongest signal is usually ACCESS (does their committee work, profession, business ties, \
or relationships plausibly give them a non-public edge on THIS company/sector?) combined with \
ABNORMALITY (is this an unusual or conviction trade for them?) and the momentum confirmation.

{META_HEURISTICS}

CRITICAL LOOK-AHEAD RULES:
- Judge ONLY from the dossier's as-of facts. You must NOT use any knowledge of what happened \
to this stock, company, or person AFTER the disclosure date, and you must NOT rely on anything \
you recall from training about this specific ticker, company, or member. If you recognize them, \
deliberately set that aside. This is a point-in-time decision.
- Be calibrated and skeptical: most of these trades, even post-filter, are ordinary. Reserve \
high signal_strength (>70) and trader_alpha (>70) for clear access + abnormality + momentum \
alignment. A generic growth-stock buy by a member with no relevant access is a low score even \
if the filter passed it.

OUTPUT:
- buy: true only if, on balance, you'd follow this trade.
- confidence: your confidence in the recommendation (High/Medium/Low).
- signal_strength (0-100): how strongly this looks like a market-beating "winner-pick".
- trader_alpha (0-100): probability the member is acting on professional/personal information \
advantage (vs. luck or generic market exposure).
- reasoning: exactly two paragraphs — first the access/abnormality/momentum case, then the \
recommendation + what drove the scores."""


class Phase2Assessment(BaseModel):
    buy: bool
    confidence: Literal["High", "Medium", "Low"]
    signal_strength: int  # 0-100
    trader_alpha: int  # 0-100
    reasoning: str


# --- dossier assembly -------------------------------------------------------


def _trader_metrics(conn, transaction_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n_prior_90, prior_winrate90, prior_bigwin90, prior_loser90, "
            "prior_mean_exc90, prior_mean_exc360, prior_bigwin360, prior_stdev_exc90 "
            "FROM trader_metrics WHERE transaction_id=%s",
            (transaction_id,),
        )
        r = cur.fetchone()
    if not r:
        return None
    cols = [
        "n_prior_90",
        "prior_winrate90",
        "prior_bigwin90",
        "prior_loser90",
        "prior_mean_exc90",
        "prior_mean_exc360",
        "prior_bigwin360",
        "prior_stdev_exc90",
    ]
    return dict(zip(cols, r, strict=True))


def _profile_md(conn, bioguide: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT profile_md FROM member_profiles WHERE bioguide=%s", (bioguide,))
        r = cur.fetchone()
    return r[0] if r else None


def _amt(lo, hi):
    return f"${lo:,}" + (f"–${hi:,}" if hi else "+") if lo else "undisclosed"


def build_dossier(conn, transaction_id: int, runup: float | None = None) -> dict | None:
    """Assemble the 5-section dossier text + the as-of date. Returns None if not analyzable."""
    ctx = context.event_context(conn, transaction_id)
    if not ctx:
        return None
    e, m, sec = ctx["event"], ctx.get("member"), ctx.get("asset_sector")
    bioguide = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT f.bioguide FROM transactions t JOIN filings f USING(doc_id) WHERE t.id=%s",
            (transaction_id,),
        )
        row = cur.fetchone()
        bioguide = row[0] if row else None
    as_of = ctx["as_of"]
    prior = context._prior_trades(conn, bioguide, as_of) if (bioguide and as_of) else []
    prior_sectors = {p["sector"] for p in prior if p.get("sector")}
    new_sector = bool(sec and sec.get("sector") and sec["sector"] not in prior_sectors)
    traded_ticker = sum(1 for p in prior if p.get("ticker") == e["ticker"])
    tm = _trader_metrics(conn, transaction_id)
    prof = _profile_md(conn, bioguide) if bioguide else None

    who = (
        f"{m['full_name']} ({m['party'] or '?'}-{m['state'] or '?'}, {m['chamber']})" if m else "?"
    )
    # 1. TRADER
    if tm:
        tsec = [
            "## 1. TRADER TRACK RECORD (point-in-time, from prior realized trades only)",
            f"- Prior trades in the last 90d: {tm['n_prior_90']}",
            f"- Prior 90d win rate (beat market): {_p(tm['prior_winrate90'])}; "
            f"prior 90d big-wins: {tm['prior_bigwin90']}; prior 90d losers: {tm['prior_loser90']}",
            f"- Prior mean market-excess return (90d/360d): "
            f"{_p(tm['prior_mean_exc90'])} / {_p(tm['prior_mean_exc360'])}",
            f"- Prior 360d big-wins: {tm['prior_bigwin360']}; return variability (stdev exc 90d): "
            f"{_p(tm['prior_stdev_exc90'])}",
        ]
    else:
        tsec = ["## 1. TRADER TRACK RECORD", "- (no prior metrics)"]
    # 2. TRADE
    runup_str = f"{runup * 100:+.1f}%" if runup is not None else "n/a"
    nov = (
        "FIRST-EVER trade in this sector for them"
        if new_sector
        else "has traded this sector before"
    )
    tradesec = [
        "## 2. THE TRADE",
        f"- {who} BOUGHT {e['ticker']} ({e['asset']})",
        f"- Position size: {_amt(e['amount_low'], e['amount_high'])}",
        f"- Purchase date: {e['txn_date']}  |  Public disclosure date: {e['disclosure_date']}  "
        f"(reporting delay {e['delay_days']} days)",
        f"- Pre-disclosure run-up (purchase→disclosure price move): {runup_str}",
        f"- Novelty: {nov}; traded THIS ticker {traded_ticker}x before.",
    ]
    # 3. COMPANY
    comp_line = (
        f"- {sec['name'] or e['ticker']} — sector: {sec['sector']}, "
        f"industry: {sec['industry'] or '?'}"
        if sec
        else "- (sector unknown)"
    )
    compsec = ["## 3. THE COMPANY", comp_line]
    # 4. META
    metasec = ["## 4. OUR RESEARCH FINDINGS (priors)", META_HEURISTICS]
    # 5. BIO
    biosec = ["## 5. MEMBER BIOGRAPHICAL PROFILE", prof or "(no profile on record)"]

    body = "\n".join(
        ["# CANDIDATE TRADE DOSSIER", f"(as-of analysis date = disclosure date {as_of})", ""]
        + tsec
        + [""]
        + tradesec
        + [""]
        + compsec
        + [""]
        + metasec
        + [""]
        + biosec
        + ["", "Return your structured verdict per the instructions."]
    )
    return {"text": body, "as_of": as_of, "bioguide": bioguide}


def _p(x):
    return "n/a" if x is None else (f"{x * 100:+.1f}%" if abs(x) < 5 else f"{x:.2f}")


# --- scoring + persistence --------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS phase2_analyses (
    id              bigserial PRIMARY KEY,
    transaction_id  bigint REFERENCES transactions(id),
    prompt_version  text,
    model           text,
    buy             boolean,
    confidence      text,
    signal_strength int,
    trader_alpha    int,
    reasoning       text,
    runup           double precision,
    input_tokens    int,
    output_tokens   int,
    created         timestamptz DEFAULT now(),
    UNIQUE (transaction_id, prompt_version, model)
);
"""


def score(dossier_text: str, model: str = MODEL):
    """One Opus call → (assessment, usage). Returns (None, usage) on refusal."""
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=model,
        max_tokens=6000,
        system=SYSTEM,
        messages=[{"role": "user", "content": dossier_text}],
        output_format=Phase2Assessment,
        thinking={"type": "adaptive"},
    )
    usage = getattr(resp, "usage", None)
    if resp.stop_reason == "refusal":
        return None, usage
    return resp.parsed_output, usage


def analyze(
    conn,
    transaction_id: int,
    runup: float | None = None,
    model: str = MODEL,
    prompt_version: str = PROMPT_VERSION,
    skip_existing: bool = True,
) -> Phase2Assessment | None:
    """Build dossier → score → persist (idempotent on transaction+prompt_version+model)."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        conn.commit()
        if skip_existing:
            cur.execute(
                "SELECT buy, confidence, signal_strength, trader_alpha, reasoning "
                "FROM phase2_analyses WHERE transaction_id=%s AND prompt_version=%s AND model=%s",
                (transaction_id, prompt_version, model),
            )
            r = cur.fetchone()
            if r:
                return Phase2Assessment(
                    buy=r[0],
                    confidence=r[1],
                    signal_strength=r[2],
                    trader_alpha=r[3],
                    reasoning=r[4],
                )
    dossier = build_dossier(conn, transaction_id, runup)
    if not dossier:
        return None
    a, usage = score(dossier["text"], model)
    if not a:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO phase2_analyses (transaction_id, prompt_version, model, buy, confidence, "
            "signal_strength, trader_alpha, reasoning, runup, input_tokens, output_tokens) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (transaction_id, prompt_version, model) DO UPDATE SET "
            "buy=EXCLUDED.buy, confidence=EXCLUDED.confidence, "
            "signal_strength=EXCLUDED.signal_strength, trader_alpha=EXCLUDED.trader_alpha, "
            "reasoning=EXCLUDED.reasoning, runup=EXCLUDED.runup, created=now()",
            (
                transaction_id,
                prompt_version,
                model,
                a.buy,
                a.confidence,
                a.signal_strength,
                a.trader_alpha,
                a.reasoning,
                runup,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
            ),
        )
    conn.commit()
    return a


def ensure_profile(conn, bioguide: str, idx=None, chist=None) -> None:
    """Build a member profile if not already present."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM member_profiles WHERE bioguide=%s AND profile_md IS NOT NULL",
            (bioguide,),
        )
        if cur.fetchone():
            return
    profiles.build_profile(conn, bioguide, idx=idx, chist=chist)


# --- batch scoring of the locked candidate set ------------------------------


def locked_candidates(conn) -> list[dict]:
    """The LOCKED Phase-1 set with transaction ids + run-up: growth + proven trader +
    pre-disclosure run-up >= +5%. Mirrors phase1_backtest's growth-proven dedup."""
    from .phase1_backtest import GROWTH, _on_after, _prices

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (f.bioguide, t.ticker, t.txn_date) "
            "t.id, f.bioguide, t.ticker, t.txn_date, t.disclosure_date "
            "FROM transactions t JOIN filings f USING(doc_id) "
            "JOIN members m ON m.bioguide=f.bioguide "
            "JOIN securities s ON s.ticker=t.ticker AND s.ok "
            "JOIN trader_metrics tm ON tm.transaction_id=t.id "
            "WHERE t.txn_type='purchase' AND t.ticker IS NOT NULL "
            "AND t.disclosure_date IS NOT NULL "
            "AND s.sector = ANY(%s) AND tm.prior_bigwin90 > 0 "
            "ORDER BY f.bioguide, t.ticker, t.txn_date, t.id",
            (list(GROWTH),),
        )
        rows = cur.fetchall()
    prices = _prices([r[2] for r in rows])
    out = []
    for tid, bio, tk, td, dd in rows:
        ser = prices.get(tk)
        if not ser:
            continue
        b, d = _on_after(ser, td), _on_after(ser, dd)
        if not b or not d or b[1] <= 0:
            continue
        runup = d[1] / b[1] - 1.0
        if runup >= 0.05:
            out.append({"tid": tid, "bioguide": bio, "ticker": tk, "runup": runup})
    return out


def _persist(conn, tid, runup, a, usage, prompt_version, model):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO phase2_analyses (transaction_id, prompt_version, model, buy, confidence, "
            "signal_strength, trader_alpha, reasoning, runup, input_tokens, output_tokens) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (transaction_id, prompt_version, model) DO UPDATE SET "
            "buy=EXCLUDED.buy, confidence=EXCLUDED.confidence, "
            "signal_strength=EXCLUDED.signal_strength, trader_alpha=EXCLUDED.trader_alpha, "
            "reasoning=EXCLUDED.reasoning, runup=EXCLUDED.runup, created=now()",
            (
                tid,
                prompt_version,
                model,
                a.buy,
                a.confidence,
                a.signal_strength,
                a.trader_alpha,
                a.reasoning,
                runup,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
            ),
        )
    conn.commit()


def score_all(conn, workers: int = 8, model: str = MODEL, prompt_version: str = PROMPT_VERSION):
    """Full run: build every needed member profile (concurrent), then score every locked
    candidate (concurrent). Idempotent — skips profiles/analyses already present, so it resumes.
    DB access stays on the main thread; only network calls are fanned out."""
    import concurrent.futures as cf

    cands = locked_candidates(conn)
    print(f"[score_all] {len(cands)} locked candidates", flush=True)

    # 1) member profiles (build the missing ones; network concurrent, save serial)
    idx, chist = profiles.legislator_index(), profiles._committee_history()
    with conn.cursor() as cur:
        cur.execute("SELECT bioguide FROM member_profiles WHERE profile_md IS NOT NULL")
        have = {r[0] for r in cur.fetchall()}
    need = sorted({c["bioguide"] for c in cands} - have)
    print(f"[score_all] building {len(need)} member profiles...", flush=True)

    def _profile(b):  # never raise — a failed member is skipped, not fatal to the batch
        try:
            return profiles.compute_profile(b, idx, chist)
        except Exception as e:  # noqa: BLE001
            print(f"  ! profile {b} failed: {type(e).__name__}: {e}", flush=True)
            return None

    with cf.ThreadPoolExecutor(max_workers=min(5, workers)) as ex:  # gentle on Wikipedia
        for p in ex.map(_profile, need):
            if p:
                profiles.save_profile(
                    conn,
                    p["bioguide"],
                    p["spine"],
                    p["wikipedia"],
                    p["profile_md"],
                    p["sources"],
                    p["model"],
                )
    print("[score_all] profiles done", flush=True)

    # 2) which candidates still need scoring at this prompt_version + model
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        conn.commit()
        cur.execute(
            "SELECT transaction_id FROM phase2_analyses WHERE prompt_version=%s AND model=%s",
            (prompt_version, model),
        )
        scored = {r[0] for r in cur.fetchall()}
    todo = [c for c in cands if c["tid"] not in scored]
    print(
        f"[score_all] scoring {len(todo)} (skipping {len(cands) - len(todo)} already done)...",
        flush=True,
    )

    # 3) build dossiers serially (DB), score concurrently (network), persist serially (DB)
    dossiers = [(c, build_dossier(conn, c["tid"], c["runup"])) for c in todo]

    def _do(item):
        c, dos = item
        if not dos:
            return c, None, None
        try:
            a, usage = score(dos["text"], model)
        except Exception as e:  # noqa: BLE001 — isolate a bad call; idempotent resume catches it
            print(f"  ! score tid={c['tid']} failed: {type(e).__name__}: {e}", flush=True)
            return c, None, None
        return c, a, usage

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for c, a, usage in ex.map(_do, dossiers):
            if a:
                _persist(conn, c["tid"], c["runup"], a, usage, prompt_version, model)
            done += 1
            if done % 25 == 0:
                print(f"[score_all] {done}/{len(todo)} scored", flush=True)
    print(f"[score_all] complete: {done} scored", flush=True)
    return {"candidates": len(cands), "profiles_built": len(need), "scored": done}


if __name__ == "__main__":
    import sys

    conn = store.connect()
    if conn and len(sys.argv) > 1 and sys.argv[1] == "all":
        print(score_all(conn))
        conn.close()
    elif conn and len(sys.argv) > 1:
        tid = int(sys.argv[1])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT f.bioguide FROM transactions t JOIN filings f USING(doc_id) WHERE t.id=%s",
                (tid,),
            )
            bio = cur.fetchone()
        if bio and bio[0]:
            ensure_profile(conn, bio[0])
        a = analyze(conn, tid, skip_existing=False)
        print(a.model_dump_json(indent=2) if a else "no result")
        conn.close()
