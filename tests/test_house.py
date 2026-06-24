"""Parser tests against real PTR-text formats (offline; no network)."""

from __future__ import annotations

from datetime import date

from insider_trader.house import parse_transactions

# Mirrors real House PTR extracted text, incl. the wrapping quirks:
#  - inline ticker + complete amount (ALB)
#  - inline ticker, [type] + high-amount wrapped to next line (SCHW)
#  - ticker + [type] wrapped to next line, low amount trailing "-" (JNJ)
#  - no-ticker government security, amount wrapped (Treasury)
#  - ticker wrapped, amount complete inline (DE), no owner prefix => SELF
SAMPLE = """ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
SP Albemarle Corporation (ALB) [ST] S 12/21/2023 01/08/2024 $1,001 - $15,000
F      S     : New
SP Charles Schwab Corporation (SCHW) P 12/14/2023 01/08/2024 $50,001 -
[ST] $100,000
SP Johnson & Johnson Common Stock S 06/13/2024 07/03/2024 $15,001 -
(JNJ) [ST] $50,000
US Treasury Bill 912797HP5 [GS] P 06/25/2024 07/03/2024 $250,001 -
$500,000
Deere & Company Common Stock S 08/14/2024 09/18/2024 $1,001 - $15,000
(DE) [ST]
"""


def test_parses_all_five_transactions():
    ts = parse_transactions(SAMPLE, "DOC1")
    assert len(ts) == 5


def test_inline_ticker_complete_amount():
    t = parse_transactions(SAMPLE, "D")[0]
    assert (t.owner, t.ticker, t.txn_type) == ("SP", "ALB", "sale")
    assert t.txn_date == date(2023, 12, 21) and t.disclosure_date == date(2024, 1, 8)
    assert (t.amount_low, t.amount_high) == (1001, 15000)
    assert t.delay_days == 18


def test_high_amount_wraps_to_next_line():
    schw = parse_transactions(SAMPLE, "D")[1]
    assert schw.ticker == "SCHW" and schw.txn_type == "purchase"
    assert (schw.amount_low, schw.amount_high) == (50001, 100000)
    assert schw.asset_type == "ST"


def test_ticker_wraps_to_next_line():
    jnj = parse_transactions(SAMPLE, "D")[2]
    assert jnj.ticker == "JNJ" and jnj.amount_high == 50000


def test_non_equity_kept_with_null_ticker():
    tb = parse_transactions(SAMPLE, "D")[3]
    assert tb.ticker is None and tb.owner == "SELF"
    assert (tb.amount_low, tb.amount_high) == (250001, 500000)


def test_self_owner_and_wrapped_ticker():
    de = parse_transactions(SAMPLE, "D")[4]
    assert de.owner == "SELF" and de.ticker == "DE" and de.txn_type == "sale"


def test_amount_open_ended():
    txt = "SP Big Co (BIG) [ST] P 01/02/2024 01/10/2024 $50,000,000\n"
    t = parse_transactions(txt, "D")[0]
    assert t.amount_low == 50_000_000 and t.amount_high is None


def test_implausible_year_dates_dropped_but_trade_kept():
    # A typo'd disclosure year ("2204") is dropped to None; the trade itself stays.
    txt = "SP Typo Co (TYP) [ST] P 06/22/2024 06/22/2204 $1,001 - $15,000\n"
    t = parse_transactions(txt, "D")[0]
    assert t.ticker == "TYP" and t.txn_date == date(2024, 6, 22)
    assert t.disclosure_date is None and t.delay_days is None


def test_plausible_late_filing_kept():
    # A 2015 trade disclosed in 2025 is a real late filing — keep it (both years valid).
    txt = "SP Late Co (LATE) [ST] S 05/08/2015 05/15/2025 $1,001 - $15,000\n"
    t = parse_transactions(txt, "D")[0]
    assert t.txn_date == date(2015, 5, 8) and t.disclosure_date == date(2025, 5, 15)
    assert t.delay_days == (date(2025, 5, 15) - date(2015, 5, 8)).days
