"""Offline tests for filer->legislator matching (pure logic, no network)."""

from __future__ import annotations

from insider_trader.members import match


def _idx():
    return {
        "smith": [
            {"bioguide": "A", "first_norms": {"john"}, "states": {"CA"}},
            {"bioguide": "B", "first_norms": {"jane"}, "states": {"NY"}},
        ],
        "doe": [{"bioguide": "C", "first_norms": {"richard", "rick"}, "states": {"GA"}}],
    }


def test_unique_last_name():
    assert match("Doe", "Richard W.", "GA", _idx()) == "C"


def test_disambiguate_by_state():
    assert match("Smith", "John", "CA", _idx()) == "A"
    assert match("Smith", "Jane", "NY", _idx()) == "B"


def test_disambiguate_by_first_when_same_state():
    idx = {
        "lee": [
            {"bioguide": "X", "first_norms": {"susie"}, "states": {"NV"}},
            {"bioguide": "Y", "first_norms": {"barbara"}, "states": {"NV"}},
        ]
    }
    assert match("Lee", "Susie", "NV", idx) == "X"


def test_nickname_match():
    assert match("Doe", "Rick", "GA", _idx()) == "C"


def test_unmatched_returns_none():
    assert match("Nobody", "X", "TX", _idx()) is None
    # ambiguous (same last+state, unknown first) -> None
    idx = {
        "a": [
            {"bioguide": "1", "first_norms": {"p"}, "states": {"TX"}},
            {"bioguide": "2", "first_norms": {"q"}, "states": {"TX"}},
        ]
    }
    assert match("A", "Zzz", "TX", idx) is None
