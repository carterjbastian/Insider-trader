"""Offline tests for the biographical-profile structured spine (pure logic, no network)."""

from __future__ import annotations

from insider_trader.profiles import build_spine

_LEG = {
    "id": {"bioguide": "X000001", "wikipedia": "Jane Doe", "wikidata": "Q1"},
    "name": {"first": "Jane", "last": "Doe", "official_full": "Jane Q. Doe"},
    "bio": {"birthday": "1960-05-01", "gender": "F"},
    "terms": [
        {"type": "rep", "start": "2007-01-04", "end": "2013-01-03", "state": "OH",
         "district": 3, "party": "Democrat"},
        {"type": "sen", "start": "2013-01-03", "end": "2025-01-03", "state": "OH",
         "party": "Democrat"},
    ],
    "leadership_roles": [{"title": "Majority Whip", "start": "2019-01-03", "end": "2021-01-03"}],
}


def test_spine_tenure_and_timeline():
    sp = build_spine(_LEG, committees=[{"name": "Banking", "rank": 2, "title": None}])
    assert sp["full_name"] == "Jane Q. Doe"
    assert sp["entered_office"] == "2007-01-04"
    assert sp["left_office"] == "2025-01-03"
    assert sp["n_terms"] == 2
    assert sp["timeline"][0]["chamber"] == "House"
    assert sp["timeline"][1]["chamber"] == "Senate"
    assert sp["leadership_roles"][0]["role"] == "Majority Whip"
    assert sp["committees_current"][0]["name"] == "Banking"
    assert sp["wikipedia"] == "Jane Doe"


def test_spine_handles_empty():
    sp = build_spine({"name": {"first": "A", "last": "B"}, "terms": []})
    assert sp["entered_office"] is None
    assert sp["n_terms"] == 0
    assert sp["committees_current"] == []
