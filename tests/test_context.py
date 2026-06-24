"""Offline test for the pure point-in-time history summary."""

from __future__ import annotations

from datetime import date

from insider_trader.context import summarize_history


def _row(ticker, sector, ttype, ddate, delay):
    return {
        "ticker": ticker, "sector": sector, "txn_type": ttype,
        "disclosure_date": ddate, "delay_days": delay, "amount_low": 1001, "amount_high": 15000,
    }


def test_summarize_history():
    prior = [
        _row("GS", "Financial Services", "purchase", date(2026, 1, 1), 10),
        _row("GS", "Financial Services", "sale", date(2025, 1, 1), 20),
        _row("AAPL", "Technology", "purchase", date(2026, 3, 1), 5),
    ]
    s = summarize_history(prior, "GS", as_of=date(2026, 4, 1))
    assert s["n_prior_trades"] == 3
    assert (s["n_buys"], s["n_sells"]) == (2, 1)
    assert s["distinct_tickers"] == 2
    assert s["times_traded_this_ticker"] == 2
    assert s["top_sectors"][0] == ("Financial Services", 2)
    assert s["median_delay_days"] == 10
    assert s["trades_last_90d"] == 2  # the two 2026 disclosures (cutoff = 2026-01-01)


def test_summarize_history_empty():
    s = summarize_history([], "GS", as_of=date(2026, 4, 1))
    assert s["n_prior_trades"] == 0 and s["median_delay_days"] is None and s["top_sectors"] == []
