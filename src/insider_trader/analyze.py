"""The 'funny business' scorer — an outcome-blind LLM-judge (Claude).

Given the point-in-time context for one disclosure (member + committees, the
asset's sector, and the member's PRIOR trading history — all from `context.py`),
Claude assesses how much the trade *looks like* it could ride on non-public
information: access × abnormality × timing. It is **outcome-blind by construction**
— it never sees future prices or returns, and is instructed to judge only from the
provided as-of facts (vault spec 05 §5.5). Whether suspicious trades actually made
money is measured separately by the backtest.

Model is configurable (default `claude-opus-4-8`); for bulk historical backtesting a
cheaper model may be preferable — Carter's call.

  ANTHROPIC_API_KEY=... uv run python -m insider_trader.analyze <transaction_id>
"""

from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel

from . import context

MODEL = "claude-opus-4-8"

SYSTEM = """You are a forensic analyst flagging congressional stock trades that may \
ride on non-public information. You assess one disclosed trade and score how much it \
*looks like* informed trading, on three axes:

1. ACCESS — does the member's committee/subcommittee work plausibly give them a \
non-public edge on THIS company or its sector? (e.g. Armed Services↔defense, \
Financial Services↔banks, Energy↔oil/utilities, Health/HELP↔pharma, \
Intelligence↔broad.) Generic membership with no sector tie = weak.
2. ABNORMALITY — is this trade unusual for THEM, given their prior history? A first-ever \
trade in a sector tied to their committee, an outsized position, or a break from their \
usual pattern is more suspicious than routine activity for a frequent trader.
3. TIMING — a long gap between the trade and its disclosure (delay_days), or a trade \
clustered around plausible legislative/oversight events, raises suspicion.

CRITICAL RULES:
- Judge ONLY from the facts provided below. You must NOT use any outside or after-the-fact \
knowledge of what happened to the stock, the member, or related events after the trade \
date. If you recall such things, ignore them. This is a point-in-time assessment.
- Do NOT reward or penalize a trade merely for being in a popular mega-cap; weight the \
ACCESS tie and ABNORMALITY, not the ticker's fame.
- Be calibrated and skeptical. Most congressional trades are unremarkable. Reserve high \
scores (>70) for clear access + abnormality + timing alignment. Default to low scores."""


class SuspicionAssessment(BaseModel):
    suspicion_score: int  # 0-100, higher = more likely informed
    access_relevance: Literal["none", "weak", "moderate", "strong"]
    trade_normality: Literal["routine", "somewhat_unusual", "unusual", "highly_unusual"]
    recommended_action: Literal["ignore", "watch", "follow"]
    signals: list[str]  # short specific flags
    rationale: str  # 2-4 sentences, point-in-time only


def format_context(ctx: dict) -> str:
    e, m, sec, h = ctx["event"], ctx.get("member"), ctx.get("asset_sector"), ctx.get("history")
    committees = (
        "; ".join(sorted({c["name"] for c in m["committees"]}))
        if m and m["committees"]
        else "none on record"
    )
    amt = f"${e['amount_low']:,}" + (f"–${e['amount_high']:,}" if e["amount_high"] else "+")
    sector_str = f"{sec['sector']} / {sec['industry'] or ''}" if sec else "sector unknown"
    if m:
        who = f"{m['full_name']}  ({m['party'] or '?'}-{m['state'] or '?'}, {m['chamber']})"
    else:
        who = "?"
    lines = [
        "TRADE UNDER REVIEW",
        f"  Ticker: {e['ticker']}  ({sector_str})",
        f"  Asset: {e['asset']}",
        f"  Action: {e['txn_type']}   Amount: {amt}",
        f"  Trade date: {e['txn_date']}   Disclosed: {e['disclosure_date']}   "
        f"Reporting delay: {e['delay_days']} days",
        "",
        "MEMBER (as of the trade)",
        f"  {who}",
        f"  Committees: {committees}",
        "",
        "THEIR PRIOR TRADING HISTORY (disclosed strictly before this trade)",
    ]
    if h:
        lines += [
            f"  Total prior trades: {h['n_prior_trades']}  "
            f"({h['n_buys']} buys / {h['n_sells']} sells)",
            f"  Distinct tickers traded: {h['distinct_tickers']}",
            f"  Times they traded THIS ticker before: {h['times_traded_this_ticker']}",
            f"  Their most-traded sectors: {h['top_sectors']}",
            f"  Typical reporting delay (median): {h['median_delay_days']} days",
            f"  Trades in the 90 days before this one: {h['trades_last_90d']}",
        ]
    else:
        lines.append("  (no prior history available)")
    lines.append("\nAssess this trade per your instructions and return the structured verdict.")
    return "\n".join(lines)


def score(ctx: dict, model: str = MODEL) -> SuspicionAssessment | None:
    """Score one disclosure's context. Returns None on a refusal."""
    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{"role": "user", "content": format_context(ctx)}],
        output_format=SuspicionAssessment,
    )
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id                 bigserial PRIMARY KEY,
    transaction_id     bigint REFERENCES transactions(id),
    suspicion_score    int,
    access_relevance   text,
    trade_normality    text,
    recommended_action text,
    signals            jsonb,
    rationale          text,
    model              text,
    created            timestamptz DEFAULT now()
);
"""


def analyze_transaction(conn, transaction_id: int, model: str = MODEL) -> tuple | None:
    """Assemble point-in-time context, score it, persist to `analyses`."""
    import json

    ctx = context.event_context(conn, transaction_id)
    if not ctx:
        return None
    a = score(ctx, model)
    if not a:
        return None
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
        cur.execute(
            "INSERT INTO analyses (transaction_id, suspicion_score, access_relevance, "
            "trade_normality, recommended_action, signals, rationale, model) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                transaction_id,
                a.suspicion_score,
                a.access_relevance,
                a.trade_normality,
                a.recommended_action,
                json.dumps(a.signals),
                a.rationale,
                model,
            ),
        )
    conn.commit()
    return a, ctx


if __name__ == "__main__":
    import sys

    from . import store

    conn = store.connect()
    if conn and len(sys.argv) > 1:
        res = analyze_transaction(conn, int(sys.argv[1]))
        if res:
            a, _ = res
            print(a.model_dump_json(indent=2))
        conn.close()
