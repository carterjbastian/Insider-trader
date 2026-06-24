"""Offline test for the prompt formatter (the LLM call itself needs network)."""

from __future__ import annotations

from insider_trader.analyze import format_context


def test_format_context_includes_key_facts():
    ctx = {
        "event": {
            "ticker": "GS",
            "asset": "Goldman Sachs",
            "txn_type": "purchase",
            "txn_date": "2026-04-16",
            "disclosure_date": "2026-05-05",
            "delay_days": 19,
            "amount_low": 1001,
            "amount_high": 15000,
        },
        "member": {
            "full_name": "Josh Gottheimer",
            "party": "Democrat",
            "state": "NJ",
            "chamber": "house",
            "committees": [{"name": "House Financial Services", "title": None}],
        },
        "asset_sector": {
            "name": "Goldman",
            "sector": "Financial Services",
            "industry": "Capital Markets",
        },
        "history": {
            "n_prior_trades": 3536,
            "n_buys": 1476,
            "n_sells": 2057,
            "distinct_tickers": 444,
            "times_traded_this_ticker": 6,
            "top_sectors": [("Technology", 200)],
            "median_delay_days": 22,
            "trades_last_90d": 50,
        },
        "as_of": "2026-05-05",
    }
    s = format_context(ctx)
    assert "GS" in s and "Goldman Sachs" in s
    assert "Financial Services" in s and "House Financial Services" in s
    assert "19 days" in s and "3536" in s  # delay + prior-trade count
    assert "$1,001" in s


def test_format_context_handles_missing_sector_and_history():
    ctx = {
        "event": {
            "ticker": None,
            "asset": "US Treasury Bill",
            "txn_type": "sale",
            "txn_date": "2025-01-01",
            "disclosure_date": "2025-01-20",
            "delay_days": 19,
            "amount_low": 50001,
            "amount_high": None,
        },
        "member": None,
        "asset_sector": None,
        "history": None,
        "as_of": "2025-01-20",
    }
    s = format_context(ctx)
    assert "sector unknown" in s and "no prior history" in s and "$50,001+" in s
