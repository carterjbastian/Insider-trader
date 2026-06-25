"""Offline tests for Senate eFD parsing helpers (pure logic, no network)."""

from __future__ import annotations

from insider_trader.senate import _amount, _clean_ticker, _ttype


def test_clean_ticker_plain():
    assert _clean_ticker("WFC") == "WFC"


def test_clean_ticker_placeholder_only():
    assert _clean_ticker("--") is None
    assert _clean_ticker("") is None
    assert _clean_ticker(None) is None


def test_clean_ticker_placeholder_then_symbol():
    # the eFD ticker cell renders '--' then the real symbol on a new line
    assert _clean_ticker("--\n      \n   AM") == "AM"
    assert _clean_ticker("--   DIS") == "DIS"


def test_ttype():
    assert _ttype("Purchase") == "purchase"
    assert _ttype("Sale (Full)") == "sale"
    assert _ttype("Exchange") == "exchange"


def test_amount_range():
    assert _amount("$1,001 - $15,000") == (1001, 15000)
    assert _amount("$50,000") == (50000, None)
