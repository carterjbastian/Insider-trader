"""Offline test for progressive (point-in-time) trader metrics."""

from __future__ import annotations

from datetime import date

from insider_trader.metrics import compute_trader_metrics


def _r(txn_id, ticker, sector, d, exc90, exc360):
    return {
        "txn_id": txn_id,
        "bioguide": "A",
        "member": "X",
        "ticker": ticker,
        "sector": sector,
        "txn_date": d,
        "amount_low": 1000,
        "amount_high": 15000,
        "exc_90": exc90,
        "exc_360": exc360,
    }


def test_progressive_point_in_time():
    rows = [
        _r(1, "AAA", "Tech", date(2020, 1, 1), 0.5, 1.0),  # winner
        _r(2, "BBB", "Tech", date(2020, 6, 1), -0.2, -0.2),  # ~5 months later
        _r(3, "AAA", "Tech", date(2021, 1, 1), 0.1, 0.1),  # ~1 year after #1
    ]
    m = {x["transaction_id"]: x for x in compute_trader_metrics(rows)}

    # #1: no prior history
    assert m[1]["n_prior_90"] == 0 and m[1]["prior_mean_exc90"] is None

    # #2 (2020-06-01): #1's 90d return realized 2020-03-31 -> counts; ticker new, sector seen
    assert m[2]["n_prior_90"] == 1 and m[2]["prior_mean_exc90"] == 0.5
    assert m[2]["traded_ticker_before"] is False
    assert m[2]["traded_sector_before"] is True

    # #3 (2021-01-01): both priors' 90d realized; only #1's 360d realized by now
    assert m[3]["n_prior_90"] == 2
    assert m[3]["traded_ticker_before"] is True  # AAA again
    assert m[3]["n_prior_360"] == 1
    assert m[3]["prior_winrate90"] == 0.5  # one +, one -


def test_unrealized_prior_excluded():
    # A prior trade whose 90d window hasn't elapsed by the next trade must NOT count.
    rows = [
        _r(1, "AAA", "Tech", date(2020, 1, 1), 2.0, 2.0),
        _r(2, "BBB", "Tech", date(2020, 2, 1), 0.0, 0.0),  # only 31 days later
    ]
    m = {x["transaction_id"]: x for x in compute_trader_metrics(rows)}
    assert m[2]["n_prior_90"] == 0  # #1's 90d not realized by 2020-02-01
    assert m[2]["n_prior_all"] == 1  # but it's still a prior trade for abnormality
